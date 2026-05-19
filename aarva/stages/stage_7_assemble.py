"""Stage 7 — Edition assembly.

Slot-fill the daily edition from articles with status='scored'.

v0.1 slot structure (no briefing slot yet — wire branch deferred to v0.2):

    deep_feature              the anchor long-form piece (highest-ranking overall)
    lens_card_future          highest-ranking piece tagged future_gazing
    lens_card_humans          highest-ranking piece tagged humans_and_humanity
    lens_card_behind          highest-ranking piece tagged behind_the_news
    curiosity                 highest-ranking piece with JTBD=curiosity
    smart_escape              highest-ranking piece with JTBD=smart_escape

Constraint priorities (v0.1):
  Hard   no duplicate article across slots within a single edition
  Medium each lens-card slot must match its lens (otherwise leave empty + log)
  Soft   prefer articles not used in past editions (graceful fall-back if no
         fresh options for a slot, so the slot still gets filled)

Slots are filled in priority order: the deep feature first (consuming the
best overall article), then lens cards, then JTBD slots. This means
lens-restricted slots get the best remaining article in their lens after the
deep feature is chosen.

Deferred to v0.2: briefing slot (wire branch), personalisation (per-user
edition), topic-concentration cap, stochastic sampling / serendipity,
pairings, trending-cap awareness.

Rebuild semantics: if an edition for today already exists, this stage
deletes it (cascading edition_pieces) and rebuilds. Article statuses for
its old pieces are reset to 'scored' first. This is dev-friendly for v0.1
where we re-run frequently; in v0.2 we'll add a --confirm-rebuild gate.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from aarva.config import PipelineConfig
from aarva.db import Database

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Slot specifications
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SlotSpec:
    """Describes one slot in the daily edition."""
    name: str
    lens: Optional[str] = None        # required lens tag, or None to allow any
    jtbd: Optional[str] = None        # required JTBD (primary), or None to allow any
    description: str = ""


V01_SLOTS: list[SlotSpec] = [
    SlotSpec("deep_feature",      description="anchor long-form piece, any lens"),
    SlotSpec("lens_card_future",  lens="future_gazing"),
    SlotSpec("lens_card_humans",  lens="humans_and_humanity"),
    SlotSpec("lens_card_behind",  lens="behind_the_news"),
    SlotSpec("curiosity",         jtbd="curiosity"),
    SlotSpec("smart_escape",      jtbd="smart_escape"),
]


# ─────────────────────────────────────────────────────────────────────────────
# Data types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Candidate:
    """A scored article eligible for slot assignment."""
    article_id: int
    title: str
    lens: Optional[str]
    pillar: Optional[str]
    jtbd_primary: Optional[str]
    jtbd_secondary: Optional[str]
    ranking_score: float
    word_count: int


@dataclass
class AssemblyStats:
    candidate_pool: int = 0
    slots_filled: int = 0
    slots_skipped: list[str] = field(default_factory=list)
    edition_id: Optional[int] = None
    rebuilt_existing: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# Loading
# ─────────────────────────────────────────────────────────────────────────────

def _load_candidates(db: Database) -> list[Candidate]:
    """Pull every article currently in status='scored' with its score row."""
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT a.id, a.title, a.word_count,
                   s.lens, s.pillar, s.jtbd_primary, s.jtbd_secondary,
                   COALESCE(s.ranking_score, 0.0) AS ranking_score
              FROM articles a
              JOIN article_scores s ON s.article_id = a.id
             WHERE a.status = 'scored'
        """).fetchall()
    return [
        Candidate(
            article_id=int(r["id"]),
            title=r["title"],
            lens=r["lens"],
            pillar=r["pillar"],
            jtbd_primary=r["jtbd_primary"],
            jtbd_secondary=r["jtbd_secondary"],
            ranking_score=float(r["ranking_score"]),
            word_count=int(r["word_count"] or 0),
        )
        for r in rows
    ]


def _articles_used_in_past_editions(db: Database) -> set[int]:
    """Article IDs that have appeared in any past edition."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT article_id FROM edition_pieces"
        ).fetchall()
    return {int(r["article_id"]) for r in rows}


# ─────────────────────────────────────────────────────────────────────────────
# Slot selection
# ─────────────────────────────────────────────────────────────────────────────

def _matches_slot(candidate: Candidate, slot: SlotSpec) -> bool:
    """Strict match: lens + JTBD (primary OR secondary) must match if specified."""
    if slot.lens is not None and candidate.lens != slot.lens:
        return False
    if slot.jtbd is not None:
        if candidate.jtbd_primary != slot.jtbd and candidate.jtbd_secondary != slot.jtbd:
            return False
    return True


def _select_for_slot(
    candidates: list[Candidate],
    slot: SlotSpec,
    already_chosen: set[int],
    past_edition_ids: set[int],
) -> Optional[Candidate]:
    """Pick the best candidate for a slot.

    Selection rules (in order):
      1. Strict match on slot constraints.
      2. Exclude already-chosen-in-this-edition.
      3. Prefer not-in-past-editions. Fall back to past-edition pieces only
         if no fresh option exists.
      4. Within a tier, sort by ranking_score descending.
    """
    pool = [c for c in candidates
            if c.article_id not in already_chosen
            and _matches_slot(c, slot)]
    if not pool:
        return None

    fresh = [c for c in pool if c.article_id not in past_edition_ids]
    used_before = [c for c in pool if c.article_id in past_edition_ids]

    chosen_tier = fresh if fresh else used_before
    return max(chosen_tier, key=lambda c: c.ranking_score)


# ─────────────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────────────

def _delete_existing_edition_for_today(db: Database, today: date) -> bool:
    """If an edition for today exists, reset its articles and delete it.

    Returns True if a delete happened (caller logs it).
    """
    with db.connect() as conn:
        existing = conn.execute(
            "SELECT id FROM editions WHERE edition_date = ?",
            (today.isoformat(),),
        ).fetchone()
        if not existing:
            return False

        edition_id = int(existing["id"])
        # Reset articles that were in this edition back to 'scored'
        conn.execute("""
            UPDATE articles
               SET status = 'scored'
             WHERE id IN (SELECT article_id FROM edition_pieces WHERE edition_id = ?)
               AND status = 'in_edition'
        """, (edition_id,))
        # Cascade will delete edition_pieces.
        conn.execute("DELETE FROM editions WHERE id = ?", (edition_id,))
    return True


def _persist_edition(
    db: Database,
    today: date,
    assignments: list[tuple[SlotSpec, Candidate]],
) -> int:
    """Create the edition row + edition_pieces; update article statuses."""
    with db.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO editions (edition_date) VALUES (?)",
            (today.isoformat(),),
        )
        edition_id = int(cursor.lastrowid)

        for position, (slot, candidate) in enumerate(assignments):
            conn.execute(
                """
                INSERT INTO edition_pieces
                    (edition_id, article_id, slot, position)
                VALUES (?, ?, ?, ?)
                """,
                (edition_id, candidate.article_id, slot.name, position),
            )
            conn.execute(
                "UPDATE articles SET status = 'in_edition' WHERE id = ?",
                (candidate.article_id,),
            )

    return edition_id


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def assemble_edition(
    config: PipelineConfig,
    db: Database,
    *,
    edition_date: Optional[date] = None,
) -> AssemblyStats:
    """Assemble today's edition. Returns stats and the edition id."""
    today = edition_date or date.today()
    stats = AssemblyStats()

    # If today's edition exists, rebuild it (reset article statuses too).
    # The delete happens BEFORE we capture past-edition history, so a rebuild
    # treats today's picks as freshly-available rather than as already-used.
    stats.rebuilt_existing = _delete_existing_edition_for_today(db, today)
    if stats.rebuilt_existing:
        logger.info("Stage 7: existing edition for %s deleted; rebuilding.", today)

    candidates = _load_candidates(db)
    stats.candidate_pool = len(candidates)
    if not candidates:
        logger.warning("Stage 7: no scored articles available; edition not built.")
        return stats

    past_edition_ids = _articles_used_in_past_editions(db)

    # Pretty-print candidate pool to the log for transparency.
    logger.info("Stage 7: candidate pool — %d scored articles", stats.candidate_pool)
    by_lens = {}
    for c in candidates:
        by_lens.setdefault(c.lens or "(no-lens)", []).append(c)
    for lens, lens_candidates in sorted(by_lens.items()):
        logger.info("  %s: %d candidates", lens, len(lens_candidates))

    chosen: set[int] = set()
    assignments: list[tuple[SlotSpec, Candidate]] = []

    for slot in V01_SLOTS:
        pick = _select_for_slot(candidates, slot, chosen, past_edition_ids)
        if pick is None:
            stats.slots_skipped.append(slot.name)
            constraint = []
            if slot.lens:
                constraint.append(f"lens={slot.lens}")
            if slot.jtbd:
                constraint.append(f"jtbd={slot.jtbd}")
            logger.warning(
                "  [%s] EMPTY — no candidate matching %s",
                slot.name, ", ".join(constraint) or "any",
            )
            continue
        chosen.add(pick.article_id)
        assignments.append((slot, pick))
        flags = []
        if pick.article_id in past_edition_ids:
            flags.append("RE-USED FROM PAST EDITION")
        logger.info(
            "  [%-20s] article %d (%.2f) — %s%s",
            slot.name, pick.article_id, pick.ranking_score,
            pick.title[:55],
            " (" + ", ".join(flags) + ")" if flags else "",
        )

    if not assignments:
        logger.warning("Stage 7: no slots filled — edition will not be persisted.")
        return stats

    edition_id = _persist_edition(db, today, assignments)
    stats.edition_id = edition_id
    stats.slots_filled = len(assignments)

    logger.info(
        "Stage 7: edition #%d for %s — %d slots filled, %d skipped (%s)",
        edition_id, today, stats.slots_filled, len(stats.slots_skipped),
        ", ".join(stats.slots_skipped) if stats.slots_skipped else "none",
    )
    return stats

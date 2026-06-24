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
from typing import Any, Optional

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
    # Ad-hoc filters added via the review CLI:
    publication_filter: Optional[str] = None   # case-insens. substr match on pub name
    topic_keyword: Optional[str] = None        # case-insens. substr match on title
    description: str = ""
    # Per-slot freshness window. None = no date filter (the default,
    # appropriate for evergreen slots like deep_feature, curiosity,
    # smart_escape, delight). For news-y / forward-looking slots
    # (lens_card_future, lens_card_behind), the slot is meaningless
    # if the candidate is months old — so we cap to last N days.
    # Configurable per slot via assembly.slot_max_age_days in
    # pipeline.yaml (overrides the V01_SLOTS default).
    max_age_days: Optional[int] = None


V01_SLOTS: list[SlotSpec] = [
    SlotSpec("deep_feature",      description="anchor long-form piece, any lens"),
    # Future-gazing pieces lose relevance fast — a 'where the world is
    # heading' essay from 3 months ago is in a different world than
    # today. Capped to the last 6 days by default; override via
    # assembly.slot_max_age_days.lens_card_future in pipeline.yaml.
    SlotSpec("lens_card_future",  lens="future_gazing",       max_age_days=6),
    SlotSpec("lens_card_humans",  lens="humans_and_humanity"),
    # Behind-the-news is news-adjacent context — only meaningful if the
    # underlying news is still recent. Same 6-day cap as future-gazing.
    SlotSpec("lens_card_behind",  lens="behind_the_news",     max_age_days=6),
    SlotSpec("curiosity",         jtbd="curiosity"),
    # Two smart-escape slots: lineups skew cerebral and the reviewer
    # benefits from a light-hearted alternative they can keep or drop
    # in review. Second pick is the next-best smart_escape after the
    # first (dedupe on article_id is automatic in the slot loop).
    SlotSpec("smart_escape",      jtbd="smart_escape"),
    SlotSpec("smart_escape",      jtbd="smart_escape"),
    # Delight: genuinely light/fun/playful pieces (humour, oddities,
    # wit). Distinct from smart_escape's restorative tone. Sits at
    # the end of the edition for a "send the listener off smiling"
    # finish.
    SlotSpec("delight",           jtbd="delight"),
]

# Look-up table for the review CLI's "+behind" etc. shortcuts. Each
# alias maps to the SlotSpec used when the user asks for an extra slot
# of that type. The alias side is the *user-facing* command syntax;
# the SlotSpec side replays the same lens/JTBD constraints as V01_SLOTS.
SLOT_ALIASES: dict[str, SlotSpec] = {
    "feature":   SlotSpec("deep_feature",     description="extra deep feature"),
    "future":    SlotSpec("lens_card_future", lens="future_gazing",
                          max_age_days=6),
    "humans":    SlotSpec("lens_card_humans", lens="humans_and_humanity"),
    "behind":    SlotSpec("lens_card_behind", lens="behind_the_news",
                          max_age_days=6),
    "curiosity": SlotSpec("curiosity",        jtbd="curiosity"),
    "escape":    SlotSpec("smart_escape",     jtbd="smart_escape"),
    "delight":   SlotSpec("delight",          jtbd="delight"),
}


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
    publication_id: int
    publication_name: str
    cluster_id: Optional[int]   # event cluster from Stage 1.5, for topic-cap
    published_date: Optional[date]   # used by slots with max_age_days


# Length buckets (kickoff §2: short ≤ 8 min, medium 8-15 min, long > 15 min).
# Assuming ~150 wpm narration rate the word-count thresholds are:
#   short:  word_count < 1200       (under 8 min)
#   medium: 1200 ≤ word_count < 2250 (8–15 min)
#   long:   word_count >= 2250      (over 15 min)
LENGTH_THRESHOLDS_WORDS = (1200, 2250)


def _length_bucket(word_count: int) -> str:
    if word_count < LENGTH_THRESHOLDS_WORDS[0]:
        return "short"
    if word_count < LENGTH_THRESHOLDS_WORDS[1]:
        return "medium"
    return "long"


def _length_targets_for_edition(
    distribution: dict[str, float], n_slots: int,
) -> dict[str, int]:
    """Convert proportional config (e.g., short:0.30, medium:0.50, long:0.20)
    into integer slot counts. Rounds and adjusts to ensure the sum equals
    n_slots (extra slot, if any, goes to the largest bucket)."""
    if not distribution:
        return {"short": 0, "medium": 0, "long": 0}
    raw = {k: float(v) * n_slots for k, v in distribution.items()}
    floored = {k: int(v) for k, v in raw.items()}
    deficit = n_slots - sum(floored.values())
    if deficit > 0:
        # Distribute the deficit to buckets with the largest fractional part
        fractions = sorted(
            ((raw[k] - floored[k], k) for k in raw),
            key=lambda pair: pair[0], reverse=True,
        )
        for _, key in fractions[:deficit]:
            floored[key] += 1
    # Ensure all three buckets are present (default 0)
    for key in ("short", "medium", "long"):
        floored.setdefault(key, 0)
    return floored


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
    """Pull every article currently in status='scored' with its score row +
    its Stage-1.5 event cluster (if any).

    The LEFT JOIN on article_clusters means singletons (Stage 1.5 sees most
    articles as their own cluster) and articles ingested before Stage 1.5
    ran end up with cluster_id=NULL. That's fine — the topic-cap logic
    treats NULL as "ungrouped" and lets them all through, which is the
    right default since unclustered articles have no known topic affinity.

    Hard-exclude articles that the reviewer rejected in any past edition.
    Without this, `aarva.review` rejection only resets the article's
    status back to 'scored' (so Stage 7 can re-fill the same slot in the
    same edition), which means tomorrow's edition sees the same article
    again in the candidate pool — defeating the point of the rejection.
    The NOT EXISTS subquery against edition_rejections enforces the
    cross-edition block. To 'un-reject' an article later, run:
        DELETE FROM edition_rejections WHERE article_id = ?
    """
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT a.id, a.title, a.word_count, a.publication_id,
                   a.published_date,
                   p.name AS publication_name,
                   s.lens, s.pillar, s.jtbd_primary, s.jtbd_secondary,
                   COALESCE(s.ranking_score, 0.0) AS ranking_score,
                   ac.cluster_id
              FROM articles a
              JOIN article_scores s ON s.article_id = a.id
              JOIN publications p ON p.id = a.publication_id
              LEFT JOIN article_clusters ac ON ac.article_id = a.id
             WHERE a.status = 'scored'
               AND NOT EXISTS (
                   SELECT 1 FROM edition_rejections er
                    WHERE er.article_id = a.id
               )
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
            publication_id=int(r["publication_id"]),
            publication_name=r["publication_name"],
            cluster_id=(int(r["cluster_id"]) if r["cluster_id"] is not None else None),
            published_date=_parse_iso_date(r["published_date"]),
        )
        for r in rows
    ]


def _parse_iso_date(value) -> Optional[date]:
    """SQLite's date columns come back as strings ('2026-06-20') or
    None. Parse to a date object, swallow malformed values as None."""
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _articles_used_in_past_editions(db: Database) -> set[int]:
    """Article IDs that have appeared in any past edition."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT article_id FROM edition_pieces"
        ).fetchall()
    return {int(r["article_id"]) for r in rows}


def _recent_publication_weights(
    db: Database, lookback_editions: int,
) -> dict[int, float]:
    """Penalty weight per publication_id based on appearance in the
    last N daily editions.

    Idea: pubs that appeared in the most recent edition get the biggest
    penalty; older appearances decay. This is the cross-edition
    rotation force that fixes "same 4-5 pubs every day" without
    needing Stage 1.5's per-pub cap to be working.

    Returns: { publication_id: penalty } where penalty is a value
    subtracted from ranking_score at selection time.

    Decay: 0.12 for most recent edition, 0.08, 0.05, 0.03, 0.02 — so a
    pub that appeared in *every* one of the last 5 editions gets a
    cumulative ~0.30 penalty. A pub that didn't appear at all is 0.
    Multiple appearances within one edition (rare; max-per-pub cap is
    usually 1) accumulate.
    """
    if lookback_editions <= 0:
        return {}
    # Decay schedule: most-recent first.
    decay = [0.12, 0.08, 0.05, 0.03, 0.02, 0.01, 0.01, 0.01]
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT e.id AS edition_id, a.publication_id
              FROM editions e
              JOIN edition_pieces ep ON ep.edition_id = e.id
              JOIN articles a ON a.id = ep.article_id
             WHERE e.edition_type = 'daily'
               AND ep.review_status = 'approved'
             ORDER BY e.edition_date DESC, e.id DESC
        """).fetchall()

    # Walk editions newest-first; assign decay[i] to each pub in
    # edition i (0-indexed). Stop after lookback_editions.
    edition_ids_seen: list[int] = []
    weights: dict[int, float] = {}
    for r in rows:
        eid = int(r["edition_id"])
        if eid not in edition_ids_seen:
            if len(edition_ids_seen) >= lookback_editions:
                break
            edition_ids_seen.append(eid)
        idx = edition_ids_seen.index(eid)
        if idx >= len(decay):
            continue
        pub_id = int(r["publication_id"])
        weights[pub_id] = weights.get(pub_id, 0.0) + decay[idx]
    return weights


def _load_candidate_embeddings(
    db: Database, article_ids: list[int],
) -> dict[int, "np.ndarray"]:
    """Load embeddings for the candidate pool. Returns
    {article_id: vector}. Articles without an embedding are simply
    absent from the dict (caller handles missing gracefully — for
    those we just don't compute a taste score)."""
    import numpy as np
    if not article_ids:
        return {}
    placeholders = ",".join("?" for _ in article_ids)
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT id, embedding FROM articles "
            f"WHERE id IN ({placeholders}) AND embedding IS NOT NULL",
            article_ids,
        ).fetchall()
    out: dict[int, np.ndarray] = {}
    for r in rows:
        out[int(r["id"])] = np.frombuffer(r["embedding"], dtype=np.float32)
    return out


# In-memory cache for the centroids. Process-local — fine for the
# CLI pipeline (one process per run) and for FastAPI single-worker
# deployments. For multi-worker servers, treat this as a per-worker
# cache (the centroids would diverge briefly after a new approval,
# converging at the TTL boundary — acceptable for a ranking signal
# that's already a tiebreaker, not a hard filter).
_TASTE_CACHE: dict[str, Any] = {
    "expires_at": 0.0,
    "approval": None, "rejection": None, "n_approved": 0, "n_rejected": 0,
}
_TASTE_CACHE_TTL_SECONDS = 600   # 10 minutes


def _invalidate_taste_cache() -> None:
    """Drop the centroid cache — call after a batch of new approvals
    if you want them reflected immediately, otherwise the natural TTL
    will pick them up."""
    _TASTE_CACHE["expires_at"] = 0.0


def _taste_centroids(
    db: Database,
) -> tuple[Optional["np.ndarray"], Optional["np.ndarray"], int, int]:
    """Build (approval_centroid, rejection_centroid, n_approved, n_rejected)
    from past review decisions in the embedding space.

    Sources:
      - Approvals: edition_pieces rows with review_status='approved'
        joined to articles.embedding.
      - Rejections: same with review_status='rejected', plus any rows
        in edition_rejections (where the reviewer explicitly removed
        a piece during refill).

    Centroids are means of L2-normalised embeddings (which is what the
    BGE model emits, so they're already unit vectors). Returns Nones
    for whichever side has no data — caller skips that direction.

    Cached in-process for 10 minutes — the centroids drift slowly
    relative to the cost of recomputing them on every Stage 7 / web
    request.
    """
    import time
    import numpy as np

    if time.time() < _TASTE_CACHE["expires_at"]:
        return (
            _TASTE_CACHE["approval"],
            _TASTE_CACHE["rejection"],
            _TASTE_CACHE["n_approved"],
            _TASTE_CACHE["n_rejected"],
        )

    def _fetch_vecs(query: str, params: tuple = ()) -> list[np.ndarray]:
        with db.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            np.frombuffer(r["embedding"], dtype=np.float32) for r in rows
            if r["embedding"]
        ]

    approved = _fetch_vecs("""
        SELECT DISTINCT a.embedding
          FROM articles a
          JOIN edition_pieces ep ON ep.article_id = a.id
         WHERE ep.review_status = 'approved'
           AND a.embedding IS NOT NULL
    """)
    rejected = _fetch_vecs("""
        SELECT DISTINCT a.embedding
          FROM articles a
          JOIN edition_pieces ep ON ep.article_id = a.id
         WHERE ep.review_status = 'rejected'
           AND a.embedding IS NOT NULL
        UNION
        SELECT DISTINCT a.embedding
          FROM articles a
          JOIN edition_rejections er ON er.article_id = a.id
         WHERE a.embedding IS NOT NULL
    """)
    approval_centroid = (
        np.mean(np.stack(approved), axis=0) if approved else None
    )
    rejection_centroid = (
        np.mean(np.stack(rejected), axis=0) if rejected else None
    )

    # Cache for next call within the TTL window.
    _TASTE_CACHE.update({
        "expires_at": time.time() + _TASTE_CACHE_TTL_SECONDS,
        "approval": approval_centroid,
        "rejection": rejection_centroid,
        "n_approved": len(approved),
        "n_rejected": len(rejected),
    })
    return approval_centroid, rejection_centroid, len(approved), len(rejected)


def _cosine(a: "np.ndarray", b: "np.ndarray") -> float:
    """Cosine similarity. Vectors are typically already L2-normalised
    by the BGE embedding client, but we re-normalise defensively."""
    import numpy as np
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _compute_taste_scores(
    db: Database,
    article_ids: list[int],
    min_approvals: int,
    approval_weight: float = 2.0,
) -> dict[int, float]:
    """Per-candidate taste score = approval_weight * cos(approval) -
    cos(rejection). Higher = more like things the reviewer has
    historically approved.

    Returns {} if there aren't enough approvals yet (cold-start guard).
    Articles without embeddings simply get no entry in the dict — the
    caller treats absence as score=0.
    """
    approval, rejection, n_app, n_rej = _taste_centroids(db)
    if n_app < min_approvals:
        # Cold-start: not enough signal yet to bias on.
        logger.info(
            "Stage 7: taste-centroid bias inactive — only %d approvals "
            "(need ≥%d to engage).",
            n_app, min_approvals,
        )
        return {}
    if approval is None:
        return {}

    embeddings = _load_candidate_embeddings(db, article_ids)
    scores: dict[int, float] = {}
    for aid, vec in embeddings.items():
        s = approval_weight * _cosine(vec, approval)
        if rejection is not None:
            s -= _cosine(vec, rejection)
        scores[aid] = s
    logger.info(
        "Stage 7: taste-centroid bias active — %d approvals / %d rejections "
        "in history. Scored %d / %d candidates with embeddings.",
        n_app, n_rej, len(scores), len(article_ids),
    )
    return scores


def _articles_rejected_for_edition(db: Database, edition_id: int) -> set[int]:
    """Article IDs the user explicitly rejected for this edition."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT article_id FROM edition_rejections WHERE edition_id = ?",
            (edition_id,),
        ).fetchall()
    return {int(r["article_id"]) for r in rows}


def _load_edition_overrides(
    db: Database, edition_id: int,
) -> tuple[list[str], list[str], dict[str, str]]:
    """Read the review-CLI overrides (extra_slots / dropped_slots /
    slot_biases) from the editions row. Returns ([], [], {}) if the
    row doesn't exist or the columns are NULL.
    """
    with db.connect() as conn:
        row = conn.execute(
            "SELECT extra_slots, dropped_slots, slot_biases "
            "FROM editions WHERE id = ?",
            (edition_id,),
        ).fetchone()
    if not row:
        return [], [], {}
    extra = json.loads(row["extra_slots"] or "[]")
    dropped = json.loads(row["dropped_slots"] or "[]")
    biases = json.loads(row["slot_biases"] or "{}")
    return list(extra), list(dropped), dict(biases)


def _expand_slot_list(
    extra_slots: list[str], dropped_slots: list[str],
) -> list[SlotSpec]:
    """Build the per-edition slot list from V01_SLOTS plus overrides.

    Order: V01_SLOTS in original order with dropped removed, then
    extra slots appended at the end (so the user sees their additions
    grouped at the bottom of the edition, which keeps the editorial
    rhythm of deep-feature → lens-cards → curiosity → escape intact
    even when the user added extras).
    """
    dropped_set = set(dropped_slots)
    base = [s for s in V01_SLOTS if s.name not in dropped_set]

    # Map each extra slot name back to its SlotSpec. We accept:
    #   - alias form ("behind")
    #   - full slot name ("lens_card_behind")
    #   - "pub:<name>" — filtered by publication (substring match)
    #   - "topic:<keyword>" — filtered by title keyword (substring match)
    extras: list[SlotSpec] = []
    alias_by_slot_name = {v.name: v for v in SLOT_ALIASES.values()}
    for token in extra_slots:
        if token.startswith("pub:"):
            pub_name = token[len("pub:"):].strip()
            if not pub_name:
                logger.warning("Stage 7: empty pub filter — skipping")
                continue
            extras.append(SlotSpec(
                name=f"pub_{pub_name.replace(' ', '_').lower()}",
                publication_filter=pub_name,
                description=f"extra slot constrained to publication '{pub_name}'",
            ))
        elif token.startswith("topic:"):
            keyword = token[len("topic:"):].strip()
            if not keyword:
                logger.warning("Stage 7: empty topic keyword — skipping")
                continue
            extras.append(SlotSpec(
                name=f"topic_{keyword.lower().replace(' ', '_')}",
                topic_keyword=keyword,
                description=f"extra slot whose title contains '{keyword}'",
            ))
        elif token in SLOT_ALIASES:
            extras.append(SLOT_ALIASES[token])
        elif token in alias_by_slot_name:
            extras.append(alias_by_slot_name[token])
        else:
            logger.warning(
                "Stage 7: extra_slots entry '%s' is not a recognised slot "
                "alias or name; skipping.", token,
            )
    return base + extras


def _approved_pieces_for_edition(db: Database, edition_id: int) -> dict[str, list[dict]]:
    """Map slot → [list of {article_id, position}] for pieces already approved.

    Returns a *list* per slot because the review CLI's "+behind" / "+humans"
    / etc. commands can produce duplicate slot names (e.g., two
    lens_card_behind slots in one edition). The assembly loop consumes
    these in order — if slot S appears twice in edition_slots and two
    pieces are already approved for S, both freeze.
    """
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT slot, article_id, position
              FROM edition_pieces
             WHERE edition_id = ?
               AND review_status = 'approved'
             ORDER BY position
        """, (edition_id,)).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["slot"], []).append({
            "article_id": int(r["article_id"]),
            "position": int(r["position"] or 0),
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Slot selection
# ─────────────────────────────────────────────────────────────────────────────

def _matches_slot(
    candidate: Candidate,
    slot: SlotSpec,
    today: Optional[date] = None,
) -> bool:
    """Strict match: lens + JTBD (primary OR secondary) + publication +
    topic keyword + freshness window must all match if specified.
    Unspecified = wildcard.

    today: edition date the slot is being filled for. Required when
    slot.max_age_days is set (otherwise the freshness check no-ops).
    """
    if slot.lens is not None and candidate.lens != slot.lens:
        return False
    if slot.jtbd is not None:
        if candidate.jtbd_primary != slot.jtbd and candidate.jtbd_secondary != slot.jtbd:
            return False
    if slot.publication_filter is not None:
        # Case-insensitive substring match — lets the user type
        # "+pub:smithsonian" to match "Smithsonian Magazine".
        if (slot.publication_filter.lower()
                not in (candidate.publication_name or "").lower()):
            return False
    if slot.topic_keyword is not None:
        # Case-insensitive substring match against title. Excerpt search
        # is intentionally out of scope (Candidate doesn't carry it).
        if slot.topic_keyword.lower() not in (candidate.title or "").lower():
            return False
    if slot.max_age_days is not None and today is not None:
        # News-y slots (lens_card_future / lens_card_behind) require the
        # candidate to be within the freshness window. Articles with no
        # published_date are excluded — we can't verify their age, and
        # for time-sensitive slots, the safer default is to skip.
        if candidate.published_date is None:
            return False
        age = (today - candidate.published_date).days
        if age > slot.max_age_days:
            return False
    return True


def _select_for_slot(
    candidates: list[Candidate],
    slot: SlotSpec,
    already_chosen: set[int],
    past_edition_ids: set[int],
    pub_picks: dict[int, int],
    max_per_publication: int,
    cluster_picks: dict[int, int],
    max_per_cluster: int,
    length_picks: dict[str, int],
    length_targets: dict[str, int],
    slot_length_bias: Optional[str] = None,   # "shorter" | "longer" | None
    publication_penalties: Optional[dict[int, float]] = None,
    taste_scores: Optional[dict[int, float]] = None,
    taste_bias_weight: float = 0.0,
    today: Optional[date] = None,
) -> Optional[Candidate]:
    """Pick the best candidate for a slot.

    Selection rules (in order):
      1. Strict match on slot constraints (lens / JTBD).
      2. Exclude already-chosen-in-this-edition.
      3. Per-publication cap: hard floor with soft-relax. If applying the
         cap empties the pool, fill the slot anyway and log a warning.
      4. Topic-concentration cap (per event cluster): same hard-with-relax
         pattern. cluster_id=NULL candidates always pass (they're
         unclustered).
      5. Length-distribution preference (soft): prefer candidates whose
         length bucket is still below target. If no candidate is below
         target, relax silently — the targets are a steering signal, not
         a hard constraint.
      6. Prefer not-in-past-editions. Past-edition pieces only used if no
         fresh option exists.
      7. Within a tier, sort by ranking_score descending.
    """
    pool = [c for c in candidates
            if c.article_id not in already_chosen
            and _matches_slot(c, slot, today=today)]
    if not pool:
        return None

    # Apply per-publication cap.
    capped = [c for c in pool
              if pub_picks.get(c.publication_id, 0) < max_per_publication]
    if capped:
        pool = capped
    else:
        logger.warning(
            "  [%s] per-publication cap (%d) yields no candidates — "
            "relaxing constraint for this slot",
            slot.name, max_per_publication,
        )

    # Apply per-cluster (topic-concentration) cap. cluster_id=None means
    # the article isn't in any cluster, so it always passes.
    capped_by_cluster = [
        c for c in pool
        if (c.cluster_id is None
            or cluster_picks.get(c.cluster_id, 0) < max_per_cluster)
    ]
    if capped_by_cluster:
        pool = capped_by_cluster
    else:
        logger.warning(
            "  [%s] per-cluster cap (%d) yields no candidates — "
            "relaxing constraint for this slot",
            slot.name, max_per_cluster,
        )

    # Length-distribution preference (soft). Filter to candidates whose
    # length bucket is still below target. If nothing fits, fall back to
    # the full pool silently — the targets are aspirational, not hard.
    if length_targets:
        under_target = [
            c for c in pool
            if length_picks.get(_length_bucket(c.word_count), 0)
                < length_targets.get(_length_bucket(c.word_count), 0)
        ]
        if under_target:
            pool = under_target
        # If under_target is empty, leave pool unchanged. No warning —
        # this is a steering signal, not a constraint.

    fresh = [c for c in pool if c.article_id not in past_edition_ids]
    used_before = [c for c in pool if c.article_id in past_edition_ids]

    chosen_tier = fresh if fresh else used_before

    # Per-slot length bias from the review CLI's "Nl" / "Ns" commands.
    # When the user rejects with a length preference, the slot remembers
    # that preference and the next refill respects it. We do this as a
    # restricted-pool filter (top half of candidates by word_count in the
    # bias direction), then pick the best by ranking_score within that
    # subset — so we don't completely abandon ranking quality in service
    # of length.
    if slot_length_bias in ("shorter", "longer") and len(chosen_tier) > 1:
        reverse = (slot_length_bias == "longer")
        sorted_by_length = sorted(
            chosen_tier, key=lambda c: c.word_count, reverse=reverse,
        )
        # Take the top half (rounded up) of candidates in the preferred
        # length direction, then return the highest-ranking among them.
        cutoff = max(1, (len(sorted_by_length) + 1) // 2)
        chosen_tier = sorted_by_length[:cutoff]

    # Final effective score = ranking_score
    #                         − publication-rotation penalty
    #                         + taste_bias_weight * taste_score
    #
    # Publication rotation pushes recently-used pubs down; taste bias
    # lifts candidates whose embedding sits near the reviewer's
    # historical approvals (and away from rejections).
    penalties = publication_penalties or {}
    tastes = taste_scores or {}
    def _effective_score(c: Candidate) -> float:
        return (
            c.ranking_score
            - penalties.get(c.publication_id, 0.0)
            + taste_bias_weight * tastes.get(c.article_id, 0.0)
        )
    return max(chosen_tier, key=_effective_score)


# ─────────────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────────────

def _delete_existing_edition_for_today(db: Database, today: date) -> bool:
    """If a DAILY edition for today exists, reset its articles and delete it.

    Filtered to edition_type='daily' so we never accidentally clobber a
    crosscut episode that happens to share today's date.
    """
    with db.connect() as conn:
        existing = conn.execute(
            "SELECT id FROM editions "
            " WHERE edition_date = ? AND edition_type = 'daily'",
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
        # Cascade will delete edition_pieces. Also clears any rejections
        # rows for this edition via the same cascade.
        conn.execute("DELETE FROM editions WHERE id = ?", (edition_id,))
    return True


def _refill_for_review(db: Database, today: date) -> Optional[int]:
    """In review mode, keep the existing DAILY edition + any approved
    pieces, delete only the non-approved ones, reset their articles to
    'scored' so they're re-pickable.

    Filtered to edition_type='daily' so we never accidentally refill a
    crosscut episode's pieces with daily content.
    """
    with db.connect() as conn:
        existing = conn.execute(
            "SELECT id FROM editions "
            " WHERE edition_date = ? AND edition_type = 'daily'",
            (today.isoformat(),),
        ).fetchone()
        if not existing:
            return None

        edition_id = int(existing["id"])
        # Reset the *non-approved* pieces' articles to 'scored' so they can
        # be re-picked. Approved articles stay at 'in_edition'.
        conn.execute("""
            UPDATE articles
               SET status = 'scored'
             WHERE id IN (
                SELECT article_id FROM edition_pieces
                 WHERE edition_id = ?
                   AND review_status != 'approved'
             )
               AND status = 'in_edition'
        """, (edition_id,))
        # Delete only the non-approved pieces from edition_pieces.
        conn.execute("""
            DELETE FROM edition_pieces
             WHERE edition_id = ?
               AND review_status != 'approved'
        """, (edition_id,))
    return edition_id


def _persist_edition(
    db: Database,
    today: date,
    assignments: list[tuple[SlotSpec, Candidate]],
    *,
    review_status: str,
    existing_edition_id: Optional[int] = None,
) -> int:
    """Create or update the edition row and insert edition_pieces.

    If `existing_edition_id` is provided (review-mode refill), reuse it
    and append new pieces beside the already-approved ones, preserving
    each new piece's slot's natural position in V01_SLOTS.

    Otherwise (fresh build) insert a new editions row and lay out all
    assignments at positions 0..N.
    """
    with db.connect() as conn:
        if existing_edition_id is None:
            cursor = conn.execute(
                "INSERT INTO editions (edition_date, edition_type) "
                "VALUES (?, 'daily')",
                (today.isoformat(),),
            )
            edition_id = int(cursor.lastrowid)
            position_lookup = {slot.name: i for i, slot in enumerate(V01_SLOTS)}
        else:
            edition_id = existing_edition_id
            position_lookup = {slot.name: i for i, slot in enumerate(V01_SLOTS)}

        for slot, candidate in assignments:
            position = position_lookup.get(slot.name, 0)
            conn.execute(
                """
                INSERT INTO edition_pieces
                    (edition_id, article_id, slot, position, review_status)
                VALUES (?, ?, ?, ?, ?)
                """,
                (edition_id, candidate.article_id, slot.name, position, review_status),
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
    """Assemble today's edition. Returns stats and the edition id.

    Three modes:

    1. Normal full build (review.enabled=false). Today's edition (if it
       exists) is deleted and rebuilt from scratch. All new pieces get
       review_status='approved'. Daily run continues straight into
       Stages 8-10.

    2. First-build under review (review.enabled=true). Today's edition
       gets created with new pieces all marked review_status='proposed'.
       Daily run halts here; user runs `python -m aarva.review` to
       approve / reject.

    3. Re-fill under review (review.enabled=true, edition exists, some
       pieces already approved). Approved pieces stay frozen. Rejected
       pieces' article_ids are in edition_rejections (excluded from the
       candidate pool). Stage 7 fills only the unfilled slots with new
       proposed candidates. Loops until user approves all.
    """
    today = edition_date or date.today()
    stats = AssemblyStats()

    # Decide which mode we're in.
    review_cfg = (config.raw.get("review") or {})
    review_enabled = bool(review_cfg.get("enabled", False))
    new_piece_status = "proposed" if review_enabled else "approved"

    existing_edition_id: Optional[int] = None
    rejected_for_this_edition: set[int] = set()
    approved_pieces: dict[str, list[dict]] = {}

    if review_enabled:
        # Refill mode: keep approved pieces, only re-pick the rejected slots.
        existing_edition_id = _refill_for_review(db, today)
        if existing_edition_id is not None:
            rejected_for_this_edition = _articles_rejected_for_edition(
                db, existing_edition_id,
            )
            approved_pieces = _approved_pieces_for_edition(db, existing_edition_id)
            stats.rebuilt_existing = bool(approved_pieces) or bool(rejected_for_this_edition)
            if stats.rebuilt_existing:
                logger.info(
                    "Stage 7: review refill — %d approved frozen, %d previously "
                    "rejected; filling remaining slots.",
                    len(approved_pieces), len(rejected_for_this_edition),
                )
            else:
                logger.info("Stage 7: review enabled — first build for %s.", today)
    else:
        # Full rebuild — drop the whole existing edition if any.
        stats.rebuilt_existing = _delete_existing_edition_for_today(db, today)
        if stats.rebuilt_existing:
            logger.info("Stage 7: existing edition for %s deleted; rebuilding.", today)

    candidates = _load_candidates(db)
    # In review-refill mode, exclude the articles the user already rejected.
    if rejected_for_this_edition:
        candidates = [c for c in candidates
                      if c.article_id not in rejected_for_this_edition]
    stats.candidate_pool = len(candidates)
    if not candidates and not approved_pieces:
        logger.warning("Stage 7: no scored articles available; edition not built.")
        return stats

    past_edition_ids = _articles_used_in_past_editions(db)

    # Read review-CLI overrides for this edition (extra_slots, dropped_slots,
    # slot_biases). On the first build these are all empty.
    extra_slot_names: list[str] = []
    dropped_slot_names: list[str] = []
    slot_biases: dict[str, str] = {}
    if existing_edition_id is not None:
        extra_slot_names, dropped_slot_names, slot_biases = (
            _load_edition_overrides(db, existing_edition_id)
        )

    # Build the per-edition slot list: V01_SLOTS minus dropped, plus extras.
    edition_slots = _expand_slot_list(extra_slot_names, dropped_slot_names)

    # Read assembly knobs from config.
    assembly_cfg = config.assembly or {}
    max_per_publication = int(assembly_cfg.get("max_per_publication_per_edition", 1))
    max_per_cluster = int(assembly_cfg.get("max_per_cluster_per_edition", 1))

    # Apply per-slot max_age_days overrides from pipeline.yaml. The
    # V01_SLOTS defaults (6 days for lens_card_future and
    # lens_card_behind) already match the intent. This block lets the
    # user adjust them or add caps to other slots via
    # assembly.slot_max_age_days without code changes.
    slot_age_overrides = assembly_cfg.get("slot_max_age_days", {}) or {}
    if slot_age_overrides:
        from dataclasses import replace
        edition_slots = [
            replace(s, max_age_days=int(slot_age_overrides[s.name]))
            if s.name in slot_age_overrides else s
            for s in edition_slots
        ]

    # Length-distribution targets scale to the number of slots in this
    # particular edition (after any add/drop overrides).
    length_dist_cfg = assembly_cfg.get("length_distribution", {}) or {}
    length_targets = _length_targets_for_edition(length_dist_cfg, len(edition_slots))

    logger.info(
        "Stage 7: caps — max_per_publication=%d, max_per_cluster=%d",
        max_per_publication, max_per_cluster,
    )
    logger.info(
        "Stage 7: edition slots (%d total): %s%s%s",
        len(edition_slots),
        ", ".join(s.name for s in edition_slots),
        f"   [+{len(extra_slot_names)} extras]" if extra_slot_names else "",
        f"   [-{len(dropped_slot_names)} dropped]" if dropped_slot_names else "",
    )
    if slot_biases:
        logger.info("Stage 7: slot length biases: %s", slot_biases)
    logger.info(
        "Stage 7: length targets — short=%d, medium=%d, long=%d (of %d slots)",
        length_targets.get("short", 0),
        length_targets.get("medium", 0),
        length_targets.get("long", 0),
        len(edition_slots),
    )

    # Pretty-print candidate pool to the log for transparency.
    logger.info("Stage 7: candidate pool — %d scored articles", stats.candidate_pool)
    by_lens = {}
    for c in candidates:
        by_lens.setdefault(c.lens or "(no-lens)", []).append(c)
    for lens, lens_candidates in sorted(by_lens.items()):
        logger.info("  %s: %d candidates", lens, len(lens_candidates))

    # Seed `chosen`, `pub_picks`, `cluster_picks`, and `length_picks` from
    # already-approved pieces so they count against caps and targets.
    chosen: set[int] = set()
    pub_picks: dict[int, int] = {}
    cluster_picks: dict[int, int] = {}
    length_picks: dict[str, int] = {}
    # Flatten the approved_pieces (slot → list) into a flat id list to
    # seed the per-edition tracking dicts.
    approved_ids = [p["article_id"]
                    for plist in approved_pieces.values() for p in plist]
    if approved_ids:
        with db.connect() as conn:
            placeholders = ",".join("?" for _ in approved_ids)
            for row in conn.execute(f"""
                SELECT a.id, a.publication_id, a.word_count, ac.cluster_id
                  FROM articles a
                  LEFT JOIN article_clusters ac ON ac.article_id = a.id
                 WHERE a.id IN ({placeholders})
            """, approved_ids).fetchall():
                aid = int(row["id"])
                chosen.add(aid)
                pub_picks[int(row["publication_id"])] = (
                    pub_picks.get(int(row["publication_id"]), 0) + 1
                )
                if row["cluster_id"] is not None:
                    cid = int(row["cluster_id"])
                    cluster_picks[cid] = cluster_picks.get(cid, 0) + 1
                bucket = _length_bucket(int(row["word_count"] or 0))
                length_picks[bucket] = length_picks.get(bucket, 0) + 1

    # Cross-edition publication-rotation weights. Read from config.
    pub_cooldown_n = int(assembly_cfg.get("publication_cooldown_editions", 5))
    publication_penalties = _recent_publication_weights(db, pub_cooldown_n)
    if publication_penalties:
        # Log the top penalties so the operator can see what's being
        # pushed down this run.
        with db.connect() as conn:
            id_to_name = {
                int(r["id"]): r["name"]
                for r in conn.execute(
                    "SELECT id, name FROM publications "
                    "WHERE id IN (" + ",".join(
                        "?" for _ in publication_penalties
                    ) + ")", list(publication_penalties.keys())
                )
            }
        top = sorted(publication_penalties.items(),
                     key=lambda kv: kv[1], reverse=True)[:6]
        logger.info(
            "Stage 7: publication-cooldown penalties (last %d editions): %s",
            pub_cooldown_n,
            ", ".join(f"{id_to_name.get(pid, pid)}=-{p:.2f}"
                      for pid, p in top),
        )

    # Taste-centroid bias from reviewer history. Compute approval/
    # rejection centroids once and score the whole candidate pool.
    # Disabled until at least `min_approvals_for_taste` approvals
    # have accumulated (cold-start guard).
    taste_bias_weight = float(
        assembly_cfg.get("taste_bias_weight", 0.07)
    )
    min_approvals_for_taste = int(
        assembly_cfg.get("min_approvals_for_taste", 10)
    )
    taste_scores: dict[int, float] = {}
    if taste_bias_weight > 0:
        taste_scores = _compute_taste_scores(
            db,
            article_ids=[c.article_id for c in candidates],
            min_approvals=min_approvals_for_taste,
        )

    assignments: list[tuple[SlotSpec, Candidate]] = []

    # Track how many already-approved pieces of each slot type we've
    # consumed during the loop — supports duplicate slot names (e.g.,
    # the user added two lens_card_behind slots and two are already
    # approved; we want to freeze both).
    consumed_approved: dict[str, int] = {k: 0 for k in approved_pieces}

    for slot in edition_slots:
        # If there's an unconsumed approved piece for this slot, freeze it
        # and continue. With duplicate slots in edition_slots, this lets
        # each occurrence consume one approved piece in turn.
        existing_for_slot = approved_pieces.get(slot.name, [])
        already_consumed = consumed_approved.get(slot.name, 0)
        if already_consumed < len(existing_for_slot):
            piece = existing_for_slot[already_consumed]
            logger.info(
                "  [%-20s] FROZEN — already approved (article %d)",
                slot.name, piece["article_id"],
            )
            consumed_approved[slot.name] = already_consumed + 1
            continue

        bias = slot_biases.get(slot.name)
        pick = _select_for_slot(
            candidates, slot, chosen, past_edition_ids,
            pub_picks, max_per_publication,
            cluster_picks, max_per_cluster,
            length_picks, length_targets,
            slot_length_bias=bias,
            publication_penalties=publication_penalties,
            taste_scores=taste_scores,
            taste_bias_weight=taste_bias_weight,
            today=today,
        )
        if pick is None:
            stats.slots_skipped.append(slot.name)
            constraint = []
            if slot.lens:
                constraint.append(f"lens={slot.lens}")
            if slot.jtbd:
                constraint.append(f"jtbd={slot.jtbd}")
            if slot.publication_filter:
                constraint.append(f"pub~='{slot.publication_filter}'")
            if slot.topic_keyword:
                constraint.append(f"title~='{slot.topic_keyword}'")
            logger.warning(
                "  [%s] EMPTY — no candidate matching %s",
                slot.name, ", ".join(constraint) or "any",
            )
            continue
        chosen.add(pick.article_id)
        pub_picks[pick.publication_id] = pub_picks.get(pick.publication_id, 0) + 1
        if pick.cluster_id is not None:
            cluster_picks[pick.cluster_id] = cluster_picks.get(pick.cluster_id, 0) + 1
        bucket = _length_bucket(pick.word_count)
        length_picks[bucket] = length_picks.get(bucket, 0) + 1
        assignments.append((slot, pick))
        flags = []
        if pick.article_id in past_edition_ids:
            flags.append("RE-USED FROM PAST EDITION")
        logger.info(
            "  [%-20s] article %d (%.2f) [%s/%s] — %s%s",
            slot.name, pick.article_id, pick.ranking_score,
            pick.publication_name[:18], bucket,
            pick.title[:48],
            " (" + ", ".join(flags) + ")" if flags else "",
        )

    if not assignments and not approved_pieces:
        logger.warning("Stage 7: no slots filled — edition will not be persisted.")
        return stats

    if assignments:
        edition_id = _persist_edition(
            db, today, assignments,
            review_status=new_piece_status,
            existing_edition_id=existing_edition_id,
        )
    else:
        # Nothing new to write, but the edition (with approved pieces)
        # already exists. Just return its id.
        edition_id = existing_edition_id    # safe: approved_pieces implies existing
    stats.edition_id = edition_id
    total_approved = sum(len(lst) for lst in approved_pieces.values())
    stats.slots_filled = len(assignments) + total_approved

    if review_enabled:
        logger.info(
            "Stage 7: edition #%d for %s — %d total slots filled (%d approved, "
            "%d newly proposed), %d skipped (%s). Review enabled — pipeline "
            "halting here. Run `python -m aarva.review` to approve.",
            edition_id, today, stats.slots_filled,
            total_approved, len(assignments),
            len(stats.slots_skipped),
            ", ".join(stats.slots_skipped) if stats.slots_skipped else "none",
        )
    else:
        logger.info(
            "Stage 7: edition #%d for %s — %d slots filled, %d skipped (%s)",
            edition_id, today, stats.slots_filled, len(stats.slots_skipped),
            ", ".join(stats.slots_skipped) if stats.slots_skipped else "none",
        )
    return stats

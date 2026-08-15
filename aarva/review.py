"""Interactive cold-start review CLI.

Workflow during the cold-start phase (per kickoff §2: "Cold-start phase uses
lightweight human review"):

  1. The morning launchd job runs Stages 1–7 of the pipeline. Stage 7
     creates today's edition with all pieces marked review_status='proposed'
     and halts the pipeline.

  2. The user runs `python -m aarva.review`. This CLI shows the proposed
     pieces with scores, lens/JTBD tags, byline, snippet, and source URL.
     The user marks each piece as approve or reject.

  3. Approved pieces stay frozen with review_status='approved'.
     Rejected pieces are deleted from edition_pieces and their article_id
     is added to edition_rejections so Stage 7's re-run won't propose
     them again for this edition.

  4. If any pieces were rejected, the user re-runs:
         python -m aarva.daily --stage 7
     Stage 7 will keep the approved pieces frozen and pick replacements
     for the rejected slots from the remaining candidate pool.

  5. The user re-runs `python -m aarva.review` to assess the new picks.
     Loop until all pieces are approved.

  6. Once all pieces are approved, the user runs:
         bash scripts/finalize_edition.sh
     This runs Stages 8 → 9 → 10 and publishes.

Usage (venv active):
    python -m aarva.review                    # review the most recent edition
    python -m aarva.review --edition-id 7     # review a specific edition
    python -m aarva.review --auto-approve     # approve everything without prompts
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Make the package importable when run via `python -m aarva.review` or as a
# script; both work.
if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aarva.config import load_pipeline_config
from aarva.db import Database
from aarva.services.edition_ops import add_article_to_todays_edition
from aarva.services.review_reasons import REJECTION_REASONS


from aarva.cli_utils import BOLD, DIM, RED, GREEN, YELLOW, BLUE  # noqa: F401


@dataclass
class ReviewPiece:
    index: int               # 1-based for display
    edition_id: int
    article_id: int
    slot: str
    position: int
    review_status: str       # 'proposed' or 'approved' — see _load_review_pieces
    title: str
    byline: Optional[str]
    publication_name: str
    canonical_url: str
    rigour: Optional[float]
    posture: Optional[float]
    self_implication: Optional[float]
    ranking_score: Optional[float]
    lens: Optional[str]
    pillar: Optional[str]
    jtbd_primary: Optional[str]
    excerpt: Optional[str]
    word_count: Optional[int]    # used to show audio length + steer Nl/Ns bias

    @property
    def estimated_minutes(self) -> float:
        """Rough audio length estimate at ~150 wpm narration."""
        return (self.word_count or 0) / 150.0


@dataclass
class TrendingItem:
    """One unresolved trend_hits row — see docs/session_plan_trend_
    signal_for_delight.md. Either matched (matched_article_id set) or
    fallback (fallback_urls_json set, populated by the GDELT search) —
    never both; the matcher only runs the fallback when no vector
    match cleared the threshold."""
    index: int               # 1-based for display, distinct from ReviewPiece's
    trend_id: int
    trend_phrase_en: str
    source_name: str
    matched_article_id: Optional[int]
    matched_title: Optional[str]
    matched_url: Optional[str]
    match_score: Optional[float]
    matched_jtbd: Optional[str]
    fallback_urls: list[dict]   # [{"url", "title", "domain"}, ...] — [] if none


def _load_trending(db: Database) -> list[TrendingItem]:
    """Unresolved trends (operator_action IS NULL), newest first. Shown
    regardless of age — unlike edition pieces, a trend nobody has
    looked at yet shouldn't silently disappear from view.

    One display row per trend_hits row, NOT grouped by phrase text —
    each source's crawl already inserts its own distinct row per
    (source_name, trend_phrase, date) via the DB's idempotency index,
    so there's no real "same row from multiple sources" case to merge.
    An earlier version GROUPed BY trend_phrase_en to collapse the rare
    case of two different sources producing the same translated
    phrase, but SQLite's bare-column selection under GROUP BY is
    non-deterministic — dismissing the merged display row could
    silently leave one underlying trend_hits row stuck unresolved
    forever. Showing them as separate, independently resolvable rows
    is both simpler and correct."""
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT th.id, th.trend_phrase_en, th.trend_phrase, th.source_name,
                   th.matched_article_id, th.match_score, th.fallback_urls_json,
                   a.title AS matched_title, a.canonical_url AS matched_url,
                   s.jtbd_primary AS matched_jtbd
              FROM trend_hits th
              LEFT JOIN articles a ON a.id = th.matched_article_id
              LEFT JOIN article_scores s ON s.article_id = th.matched_article_id
             WHERE th.operator_action IS NULL
             ORDER BY th.id DESC
        """).fetchall()

    items = []
    for i, r in enumerate(rows, start=1):
        fallback_urls = json.loads(r["fallback_urls_json"] or "[]")
        items.append(TrendingItem(
            index=i,
            trend_id=r["id"],
            trend_phrase_en=r["trend_phrase_en"] or r["trend_phrase"],
            source_name=r["source_name"],
            matched_article_id=r["matched_article_id"],
            matched_title=r["matched_title"],
            matched_url=r["matched_url"],
            match_score=r["match_score"],
            matched_jtbd=r["matched_jtbd"],
            fallback_urls=fallback_urls,
        ))
    return items


def _print_trending(items: list[TrendingItem]) -> None:
    if not items:
        return
    print(BOLD("═" * 70))
    print(BOLD("  Trending topics"))
    print(BOLD("═" * 70))
    for t in items:
        print()
        print(f"  {BOLD(f'[t{t.index}]')}  {BOLD(t.trend_phrase_en)}  "
              f"{DIM('(' + t.source_name + ')')}")
        if t.matched_article_id:
            tags = f"  {DIM('JTBD=' + t.matched_jtbd)}" if t.matched_jtbd else ""
            print(f"       {GREEN('-> Aarva match:')} #{t.matched_article_id} "
                  f"{BOLD(t.matched_title)} {DIM(f'(score {t.match_score:.1f})')}{tags}")
            print(f"       {BLUE(t.matched_url)}")
            print(DIM(f"       [t{t.index}a=add / t{t.index}d=dismiss]"))
        elif t.fallback_urls:
            print(f"       {YELLOW('-> No Aarva match.')} "
                  f"GDELT fallback: {len(t.fallback_urls)} candidate URL(s)")
            for u in t.fallback_urls[:5]:
                print(f"          {DIM('-')} {u.get('title') or u.get('url')}")
                print(f"            {BLUE(u.get('url'))}")
            print(DIM(f"       [t{t.index}i=ingest first URL / t{t.index}d=dismiss]"))
        else:
            print(f"       {DIM('-> No Aarva match; GDELT fallback found nothing.')}")
            print(DIM(f"       [t{t.index}d=dismiss]"))
    print()
    print(DIM("─" * 70))


def _find_edition_to_review(db: Database, edition_id: Optional[int]) -> Optional[int]:
    """If edition_id was given, return it. Otherwise find the most recent
    edition that has at least one 'proposed' piece."""
    with db.connect() as conn:
        if edition_id is not None:
            row = conn.execute(
                "SELECT id FROM editions WHERE id = ?", (edition_id,),
            ).fetchone()
            return int(row["id"]) if row else None

        row = conn.execute("""
            SELECT e.id
              FROM editions e
              JOIN edition_pieces ep ON ep.edition_id = e.id
             WHERE ep.review_status = 'proposed'
               AND e.edition_type = 'daily'
             ORDER BY e.edition_date DESC, e.id DESC
             LIMIT 1
        """).fetchone()
        return int(row["id"]) if row else None


def _load_review_pieces(db: Database, edition_id: int) -> list[ReviewPiece]:
    """Load both 'proposed' and 'approved' pieces for this edition, in
    slot order, with continuous 1-based indices spanning both statuses.

    Review CLI polish, Fix 2 (2026-07-18 — docs/session_plan_review_
    cli_polish.md): approved pieces need to be visible (and indexable)
    so the reviewer can un-approve one with 'Nu' — previously only
    proposed pieces were loaded, so an approved piece had no index to
    reference."""
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT ep.edition_id, ep.article_id, ep.slot, ep.position,
                   ep.review_status,
                   a.title, a.byline, a.canonical_url, a.excerpt, a.word_count,
                   p.name AS publication_name,
                   s.rigour, s.posture, s.self_implication, s.ranking_score,
                   s.lens, s.pillar, s.jtbd_primary
              FROM edition_pieces ep
              JOIN articles a ON a.id = ep.article_id
              JOIN publications p ON p.id = a.publication_id
              LEFT JOIN article_scores s ON s.article_id = a.id
             WHERE ep.edition_id = ?
               AND ep.review_status IN ('proposed', 'approved')
             ORDER BY ep.position
        """, (edition_id,)).fetchall()

    return [
        ReviewPiece(
            index=i + 1,
            edition_id=int(r["edition_id"]),
            article_id=int(r["article_id"]),
            slot=r["slot"],
            position=int(r["position"] or 0),
            review_status=r["review_status"],
            title=r["title"] or "",
            byline=r["byline"],
            publication_name=r["publication_name"] or "",
            canonical_url=r["canonical_url"] or "",
            rigour=r["rigour"],
            posture=r["posture"],
            self_implication=r["self_implication"],
            ranking_score=r["ranking_score"],
            lens=r["lens"],
            pillar=r["pillar"],
            jtbd_primary=r["jtbd_primary"],
            excerpt=r["excerpt"],
            word_count=int(r["word_count"]) if r["word_count"] is not None else None,
        )
        for i, r in enumerate(rows)
    ]


def _approved_count(db: Database, edition_id: int) -> int:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM edition_pieces "
            "WHERE edition_id = ? AND review_status = 'approved'",
            (edition_id,),
        ).fetchone()
    return int(row["n"])


def _print_piece(p: ReviewPiece) -> None:
    """Render one piece, two-paragraphs-or-so worth of context."""
    scores = ""
    if p.rigour is not None and p.posture is not None:
        scores = (
            f"rigour={p.rigour:.2f}  posture={p.posture:.2f}"
            f"  self={p.self_implication or 0:.2f}  rank={p.ranking_score or 0:.2f}"
        )

    tags_parts = []
    if p.lens: tags_parts.append(f"lens={p.lens}")
    if p.pillar: tags_parts.append(f"pillar={p.pillar}")
    if p.jtbd_primary: tags_parts.append(f"JTBD={p.jtbd_primary}")
    tags = "  ".join(tags_parts)

    # Length string: word count + estimated audio minutes. Helps the
    # user decide whether to ask for a longer/shorter alternative.
    length_str = ""
    if p.word_count:
        length_str = f"{p.word_count:,} words  ·  ~{p.estimated_minutes:.0f} min audio"

    approved_marker = f"{GREEN('✓ approved')}  " if p.review_status == "approved" else ""
    print()
    print(f"  {BOLD(f'[{p.index}]')}  {approved_marker}{BOLD(p.slot.replace('_', ' '))}  "
          f"{DIM('—')}  {YELLOW(p.publication_name)}")
    print(f"       {BOLD(p.title)}")
    if p.byline:
        print(f"       {DIM('by ' + p.byline)}")
    if length_str:
        print(f"       {DIM(length_str)}")
    if scores:
        print(f"       {DIM(scores)}")
    if tags:
        print(f"       {DIM(tags)}")
    print(f"       {BLUE(p.canonical_url)}")
    if p.excerpt:
        # Show ~3 lines of the article body.
        snippet = " ".join((p.excerpt or "").split())[:280]
        wrapped = textwrap.fill(snippet, width=80, initial_indent="       ",
                                subsequent_indent="       ")
        print(DIM(wrapped))


def _print_header(edition_id: int, today_iso: str, n_proposed: int, n_approved: int) -> None:
    print()
    print(BOLD("═" * 70))
    print(BOLD(f"  Aarva edition #{edition_id}  ·  {today_iso}"))
    print(BOLD("═" * 70))
    if n_approved:
        print(f"  {GREEN(str(n_approved) + ' approved')} (frozen), "
              f"{YELLOW(str(n_proposed) + ' proposed')} awaiting review")
    else:
        print(f"  {n_proposed} proposed pieces awaiting review")
    print()


# Slot aliases that the user can append with "+behind", "+humans", etc.
# Must match aarva.stages.stage_7_assemble.SLOT_ALIASES — kept in sync.
KNOWN_SLOT_ALIASES = {
    "feature":   "deep_feature",
    "future":    "lens_card_future",
    "humans":    "lens_card_humans",
    "behind":    "lens_card_behind",
    "curiosity": "curiosity",
    "escape":    "smart_escape",
    "delight":   "delight",
}


def _parse_decisions(
    raw: str, n_pieces: int, proposed_indices: Optional[set[int]] = None,
    n_trends: int = 0,
) -> dict:
    """Parse the review-CLI command line into a structured decisions dict.

    Returns a dict with three keys:
      piece_actions: {piece_index: ('a' | 'r' | 'd' | 'u', length_bias_or_None)}
                     'a' = approve, 'r' = reject (with optional bias),
                     'd' = drop without refill, 'u' = un-approve
      add_slots:     [alias, ...] — extras to ADD via "+behind" etc.
      add_bias:      currently unused; reserved

    Command syntax:
      <N>             approve piece N
      <N>a            approve piece N
      <N>r            reject piece N (refill with no bias)
      <N>l            reject piece N, refill prefer LONGER
      <N>s            reject piece N, refill prefer SHORTER
      <N>d            drop piece N entirely; no refill
      <N>u            un-approve piece N (approved → proposed)
      t<N>a           add trending-topic match N to today's edition
      t<N>d           dismiss trending-topic N
      t<N>i           ingest trending-topic N's GDELT-fallback first URL,
                      then add it to today's edition
      +behind         add a lens_card_behind slot
      +humans         add a lens_card_humans slot
      +future, +feature, +curiosity, +escape, +delight  (other aliases)
      +pub:NAME       add a slot constrained to a specific publication.
                      Case-insensitive SUBSTRING match — use one word
                      with no spaces (e.g. +pub:smithsonian matches
                      "Smithsonian Magazine"; +pub:hindu matches
                      "The Hindu"; +pub:rocks matches "War on the Rocks").
      +topic:KEYWORD  add a slot whose title contains the keyword
                      (case-insensitive — e.g. +topic:history).
                      Single-word keyword, no spaces.
      all-a           approve everything (no slot adds)
      all-r           reject everything (no slot adds; no length bias)
      (empty)         approve all + no other changes

    proposed_indices: piece indices whose review_status is currently
    'proposed', vs. already-'approved' (Fix 2, docs/session_plan_
    review_cli_polish.md). The blanket shortcuts (all-a / all-r /
    empty) only sweep THESE indices — approved pieces stay frozen
    unless explicitly referenced by index (e.g. '3u'), matching the
    existing "approved pieces stay frozen" invariant. None (default)
    means treat every index as sweepable, for callers that don't
    distinguish (kept for backward compatibility)."""
    cleaned = raw.strip().lower().replace(",", " ").replace(";", " ")
    sweepable = (
        set(range(1, n_pieces + 1)) if proposed_indices is None
        else proposed_indices
    )

    decisions = {
        "piece_actions": {},   # type: dict[int, tuple[str, Optional[str]]]
        "add_slots": [],       # type: list[str]
        "trend_actions": {},   # type: dict[int, str] -- {trend_index: 'a'|'d'|'i'}
    }

    if cleaned in ("", "all-a", "alla", "a"):
        for i in sweepable:
            decisions["piece_actions"][i] = ("a", None)
        return decisions
    if cleaned in ("all-r", "allr", "r"):
        for i in sweepable:
            decisions["piece_actions"][i] = ("r", None)
        return decisions

    for tok in cleaned.split():
        if not tok:
            continue

        # "+alias" → add a slot.
        # Three forms:
        #   "+behind"                    — standard alias (lens/jtbd)
        #   "+pub:smithsonian"           — extra slot from a specific pub
        #                                  (case-insensitive substring match
        #                                  against publication name)
        #   "+topic:history"             — extra slot whose title contains
        #                                  the keyword (case-insensitive)
        if tok.startswith("+"):
            alias = tok[1:]
            if alias.startswith("pub:") or alias.startswith("topic:"):
                # Validate there's a non-empty value after the colon.
                _, _, value = alias.partition(":")
                if not value.strip():
                    raise ValueError(
                        f"'{tok}': missing value after the colon. "
                        f"Use e.g. +pub:smithsonian or +topic:history"
                    )
                decisions["add_slots"].append(alias)
                continue
            if alias not in KNOWN_SLOT_ALIASES:
                raise ValueError(
                    f"'{tok}': unknown slot alias. Try "
                    f"+{'/+ '.join(KNOWN_SLOT_ALIASES)} "
                    f"or +pub:<name> / +topic:<keyword>"
                )
            decisions["add_slots"].append(alias)
            continue

        # "t<N><a|d|i>" — trend-row action (add / dismiss / ingest
        # GDELT fallback's first URL). Checked before the piece-index
        # branch below since a leading 't' would fail int()-parsing there.
        if tok[0] == "t" and len(tok) >= 3:
            trend_action_char = tok[-1]
            if trend_action_char not in ("a", "d", "i"):
                raise ValueError(
                    f"'{tok}': unknown trend action '{trend_action_char}' "
                    f"— use t<N>a / t<N>d / t<N>i"
                )
            try:
                trend_idx = int(tok[1:-1])
            except ValueError as e:
                raise ValueError(
                    f"can't parse '{tok}' as t<number><a|d|i>"
                ) from e
            if trend_idx < 1 or trend_idx > n_trends:
                raise ValueError(f"trend t{trend_idx} out of range (1-{n_trends})")
            decisions["trend_actions"][trend_idx] = trend_action_char
            continue

        # "<N>" or "<N><action_char>"
        # Default action is approve if no suffix.
        action_char = "a"
        bias = None
        if tok[-1] in ("a", "r", "l", "s", "d", "u"):
            action_char = tok[-1]
            num_part = tok[:-1]
        else:
            num_part = tok
        if not num_part:
            raise ValueError(f"can't parse '{tok}' as <number>[a|r|l|s|d|u]")
        try:
            idx = int(num_part)
        except ValueError as e:
            raise ValueError(f"can't parse '{tok}' as <number>[a|r|l|s|d|u]") from e
        if idx < 1 or idx > n_pieces:
            raise ValueError(f"piece {idx} out of range (1–{n_pieces})")

        # 'l' and 's' are forms of reject with a length bias.
        if action_char == "l":
            action_char, bias = "r", "longer"
        elif action_char == "s":
            action_char, bias = "r", "shorter"

        decisions["piece_actions"][idx] = (action_char, bias)

    return decisions


def _prompt_reject_reasons(
    pieces: list[ReviewPiece], decisions: dict,
) -> dict[int, tuple[str, Optional[str]]]:
    """Ask the reviewer WHY for each piece marked 'r' this round.

    Reviewer feedback learning loop, Phase 1 (docs/session_plan_
    reviewer_learning_loop.md). Returns {piece_index: (reason_code,
    reason_note_or_None)}. Called once, after decisions are parsed and
    before the final confirm — so a reject typed as part of a batch
    line (e.g. '1a 2r 3s') still gets its reason captured immediately,
    matching the existing single-confirm flow rather than fragmenting
    it per piece."""
    reasons: dict[int, tuple[str, Optional[str]]] = {}
    piece_by_index = {p.index: p for p in pieces}
    rejected_indices = [
        idx for idx, (action, _bias) in decisions["piece_actions"].items()
        if action == "r"
    ]
    if not rejected_indices:
        return reasons

    print()
    print(BOLD("Why were these rejected?"))
    menu = "  ".join(
        f"{i}={code}" for i, (code, _label) in enumerate(REJECTION_REASONS, start=1)
    )
    print(DIM(f"  {menu}"))

    for idx in rejected_indices:
        piece = piece_by_index.get(idx)
        title = piece.title if piece else f"piece {idx}"
        while True:
            raw = input(BOLD(f"  [{idx}] {title[:60]} — reason (1-{len(REJECTION_REASONS)}): ")).strip()
            try:
                choice = int(raw)
                if not (1 <= choice <= len(REJECTION_REASONS)):
                    raise ValueError
            except ValueError:
                print(RED(f"    Enter a number 1-{len(REJECTION_REASONS)}."))
                continue
            code, _label = REJECTION_REASONS[choice - 1]
            note = None
            if code == "other":
                note = input(BOLD("    Note: ")).strip() or None
            reasons[idx] = (code, note)
            break

    return reasons


def _apply_decisions(
    db: Database,
    edition_id: int,
    pieces: list[ReviewPiece],
    decisions: dict,
) -> dict:
    """Apply decisions to the DB. Returns a small summary dict.

    Side effects:
      - Pieces marked 'a' → review_status = 'approved'
      - Pieces marked 'r' → deleted from edition_pieces, added to
        edition_rejections (with reason/reason_note from decisions
        ['reject_reasons'] if present — see _prompt_reject_reasons),
        article status reset to 'scored'. If the action carries a
        length bias, the slot's bias is persisted on
        editions.slot_biases so Stage 7's refill respects it.
      - Pieces marked 'd' → deleted from edition_pieces, the slot
        name is added to editions.dropped_slots so Stage 7 won't refill
        it, AND the article_id is added to editions.dropped_article_ids
        so Stage 7 won't pick it into any OTHER slot of THIS edition
        either (review CLI polish Fix 1, docs/session_plan_review_cli_
        polish.md) — still eligible for future editions.
      - Pieces marked 'u' → review_status flipped back from 'approved'
        to 'proposed' (Fix 2 — un-approve; a no-op if the piece wasn't
        actually approved).
      - Slots in decisions['add_slots'] are appended to
        editions.extra_slots so Stage 7's next run adds them.
    """
    summary = {
        "approved": 0, "rejected": 0, "dropped": 0, "added": 0, "unapproved": 0,
    }

    # Load current overrides from the editions row, mutate them in
    # Python, then write back. SQLite has no native JSON_set so this is
    # the cleanest approach.
    with db.connect() as conn:
        row = conn.execute(
            "SELECT extra_slots, dropped_slots, slot_biases, dropped_article_ids "
            "FROM editions WHERE id = ?", (edition_id,),
        ).fetchone()
    extra_slots = json.loads((row["extra_slots"] if row else None) or "[]")
    dropped_slots = json.loads((row["dropped_slots"] if row else None) or "[]")
    slot_biases = json.loads((row["slot_biases"] if row else None) or "{}")
    dropped_article_ids = json.loads(
        (row["dropped_article_ids"] if row else None) or "[]",
    )

    with db.connect() as conn:
        for piece in pieces:
            action, bias = decisions["piece_actions"].get(piece.index, ("a", None))

            if action == "a":
                conn.execute(
                    "UPDATE edition_pieces SET review_status = 'approved' "
                    "WHERE edition_id = ? AND article_id = ?",
                    (edition_id, piece.article_id),
                )
                # When a piece is approved, any prior bias for its slot is
                # consumed — clear it.
                slot_biases.pop(piece.slot, None)
                summary["approved"] += 1

            elif action == "u":
                # Un-approve: flip back to 'proposed' so the reviewer can
                # re-decide next round. Guard the WHERE on the current
                # status so this is a no-op if the piece isn't approved.
                conn.execute(
                    "UPDATE edition_pieces SET review_status = 'proposed' "
                    "WHERE edition_id = ? AND article_id = ? "
                    "AND review_status = 'approved'",
                    (edition_id, piece.article_id),
                )
                summary["unapproved"] += 1

            elif action == "r":
                # Reject + maybe set length bias for the slot.
                reason, reason_note = decisions.get("reject_reasons", {}).get(
                    piece.index, (None, None),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO edition_rejections "
                    "(edition_id, article_id, slot_at_rejection, reason, reason_note) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (edition_id, piece.article_id, piece.slot, reason, reason_note),
                )
                conn.execute(
                    "DELETE FROM edition_pieces "
                    "WHERE edition_id = ? AND article_id = ?",
                    (edition_id, piece.article_id),
                )
                conn.execute(
                    "UPDATE articles SET status = 'scored' WHERE id = ?",
                    (piece.article_id,),
                )
                if bias:
                    slot_biases[piece.slot] = bias
                summary["rejected"] += 1

            elif action == "d":
                # Drop: remove the piece + add the slot to dropped_slots
                # so Stage 7 won't refill it, AND add the article_id to
                # dropped_article_ids so Stage 7 won't pick this article
                # into any OTHER slot of THIS edition either. We don't
                # add to rejections because we don't want to ban the
                # article from FUTURE editions — the user just doesn't
                # want it in this one.
                conn.execute(
                    "DELETE FROM edition_pieces "
                    "WHERE edition_id = ? AND article_id = ?",
                    (edition_id, piece.article_id),
                )
                conn.execute(
                    "UPDATE articles SET status = 'scored' WHERE id = ?",
                    (piece.article_id,),
                )
                if piece.slot not in dropped_slots:
                    dropped_slots.append(piece.slot)
                if piece.article_id not in dropped_article_ids:
                    dropped_article_ids.append(piece.article_id)
                # Any bias for this slot is now meaningless — clear it.
                slot_biases.pop(piece.slot, None)
                summary["dropped"] += 1

        # Append requested extra slots. Stage 7 reads extra_slots and
        # appends one slot per entry, so repeating "+behind" twice in one
        # command line produces two extra behind-the-news slots.
        for alias in decisions.get("add_slots", []):
            extra_slots.append(alias)
            summary["added"] += 1

        # Write back the mutated overrides.
        conn.execute(
            "UPDATE editions SET extra_slots = ?, dropped_slots = ?, "
            "slot_biases = ?, dropped_article_ids = ? WHERE id = ?",
            (json.dumps(extra_slots), json.dumps(dropped_slots),
             json.dumps(slot_biases), json.dumps(dropped_article_ids),
             edition_id),
        )

    return summary


def _apply_trend_decisions(
    db: Database, items: list[TrendingItem], trend_actions: dict[int, str],
) -> dict:
    """Apply trend-row decisions. Returns {'trend_added': n, 'trend_dismissed': n}.

    'a' (add): calls add_article_to_todays_edition for the matched
    article, slot='delight' if its JTBD is delight else 'bonus' (no
    new slot type for v1 — see the spec's Review CLI section).
    'i' (ingest): ingests the fallback list's first URL via the same
    aarva.ingest_url machinery the CLI tool itself uses, then adds it
    the same way as 'a'.
    'd' (dismiss): just marks operator_action, no side effects.
    Any trend not mentioned in trend_actions is left unresolved —
    it'll show up again next time review runs."""
    summary = {"trend_added": 0, "trend_dismissed": 0}
    by_index = {t.index: t for t in items}

    for idx, action in trend_actions.items():
        item = by_index.get(idx)
        if item is None:
            continue

        if action == "d":
            with db.connect() as conn:
                conn.execute(
                    "UPDATE trend_hits SET operator_action = 'dismissed', "
                    "resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (item.trend_id,),
                )
            summary["trend_dismissed"] += 1
            continue

        article_id = item.matched_article_id
        if action == "i":
            if not item.fallback_urls:
                print(YELLOW(f"  t{idx}: no fallback URL to ingest, skipping."))
                continue
            from aarva.ingest_url import _ingest_one
            config = load_pipeline_config()
            url = item.fallback_urls[0]["url"]
            article_id = _ingest_one(config, db, url, dry_run=False)
            if article_id is None:
                print(RED(f"  t{idx}: ingest failed for {url}, skipping."))
                continue

        if article_id is None:
            print(YELLOW(
                f"  t{idx}: no Aarva match to add — use t{idx}i to ingest a "
                f"fallback URL instead, or t{idx}d to dismiss. Skipping."
            ))
            continue

        slot = "delight" if item.matched_jtbd == "delight" else "bonus"
        # review_status='approved' (not the default 'proposed') — the
        # tNa/tNi keystroke IS the operator's approval decision, and a
        # 'proposed' trend-added piece gets silently deleted by Stage
        # 7's rebuild (DELETE ... WHERE review_status != 'approved')
        # before any second review pass runs. Happened for real in
        # production 2026-08-15 — see docs/session_plan_trend_adds_
        # auto_approve.md.
        result = add_article_to_todays_edition(
            db, article_id, slot=slot, review_status="approved",
        )
        if result == "added":
            with db.connect() as conn:
                conn.execute(
                    "UPDATE trend_hits SET operator_action = 'added', "
                    "resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (item.trend_id,),
                )
            summary["trend_added"] += 1
        elif result == "no_edition":
            print(YELLOW(f"  t{idx}: no daily edition exists for today, skipping."))
        elif result == "already_present":
            print(DIM(f"  t{idx}: article already in today's edition."))

    return summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--edition-id", type=int, default=None,
                        help="Specific edition id to review (default: most "
                             "recent with proposed pieces)")
    parser.add_argument("--auto-approve", action="store_true",
                        help="Approve all proposed pieces without prompting "
                             "(useful in non-interactive contexts)")
    args = parser.parse_args(argv)

    config = load_pipeline_config()
    db = Database(config.db_path)

    edition_id = _find_edition_to_review(db, args.edition_id)
    if edition_id is None:
        print(RED("No edition with proposed pieces found."))
        print("Either Stage 7 hasn't run yet, or all pieces are already approved.")
        return 0

    pieces = _load_review_pieces(db, edition_id)
    if not pieces:
        print(GREEN(f"Edition #{edition_id} has no pieces to review."))
        return 0

    with db.connect() as conn:
        row = conn.execute(
            "SELECT edition_date FROM editions WHERE id = ?", (edition_id,),
        ).fetchone()
    today_iso = str(row["edition_date"]) if row else "?"

    # No separate enabled flag (removed 2026-08-13 per user decision) —
    # running `--stage 3` is itself the opt-in gesture; whatever it
    # finds always surfaces here. Still never auto-added to an
    # edition — the operator picks add/dismiss per trend below.
    trending_items = _load_trending(db)
    _print_trending(trending_items)

    _print_header(edition_id, today_iso, len(pieces), _approved_count(db, edition_id))

    for p in pieces:
        _print_piece(p)

    print()
    print(DIM("─" * 70))

    proposed_indices = {p.index for p in pieces if p.review_status == "proposed"}

    if args.auto_approve:
        decisions = {
            "piece_actions": {i: ("a", None) for i in proposed_indices},
            "add_slots": [],
            "trend_actions": {},   # trends are never auto-decided — always operator-picked
        }
        print(GREEN(f"Auto-approving all {len(proposed_indices)} proposed piece(s)."))
    else:
        print(BOLD("Per-piece commands:"))
        print(DIM("  Na  approve piece N        Nd  drop piece N (no refill)"))
        print(DIM("  Nr  reject piece N         Nl  reject + prefer LONGER replacement"))
        print(DIM("                             Ns  reject + prefer SHORTER replacement"))
        print(DIM("  Nu  un-approve piece N (approved -> proposed, re-decide next round)"))
        if trending_items:
            print(BOLD("Trending commands:"))
            print(DIM("  tNa  add trend N's match      tNd  dismiss trend N"))
            print(DIM("  tNi  ingest trend N's GDELT-fallback URL and add it"))
        print(BOLD("Edition-level commands:"))
        print(DIM("  +behind  +humans  +future  +feature  +curiosity  +escape"))
        print(DIM("    add another slot of that type for refill"))
        print(BOLD("Shortcuts:"))
        print(DIM("  all-a    approve everything proposed (approved pieces untouched)"))
        print(DIM("  (empty)  approve everything proposed (approved pieces untouched)"))
        print(DIM("  Example: '1a 2l 3a 4d 5a 6s +behind t1a t2d'"))
        print()
        while True:
            try:
                raw = input(BOLD("> "))
            except (EOFError, KeyboardInterrupt):
                print()
                print(YELLOW("Cancelled. No changes made."))
                return 1
            try:
                decisions = _parse_decisions(
                    raw, len(pieces), proposed_indices, n_trends=len(trending_items),
                )
                break
            except ValueError as e:
                print(RED(f"  Couldn't parse that: {e}"))
                print(DIM("  Try again, or Ctrl-C to cancel."))
        decisions["reject_reasons"] = _prompt_reject_reasons(pieces, decisions)

    # Confirm before writing. Group actions by type for a readable summary.
    actions = decisions["piece_actions"]
    n_approve = sum(1 for v in actions.values() if v[0] == "a")
    n_reject  = sum(1 for v in actions.values() if v[0] == "r")
    n_drop    = sum(1 for v in actions.values() if v[0] == "d")
    n_unapprove = sum(1 for v in actions.values() if v[0] == "u")
    n_longer  = sum(1 for v in actions.values() if v[1] == "longer")
    n_shorter = sum(1 for v in actions.values() if v[1] == "shorter")
    n_added   = len(decisions.get("add_slots", []))

    print()
    parts = [GREEN(f"approve {n_approve}")]
    if n_reject:
        parts.append(RED(f"reject {n_reject}"))
        bias_bits = []
        if n_longer:  bias_bits.append(f"{n_longer} longer")
        if n_shorter: bias_bits.append(f"{n_shorter} shorter")
        if bias_bits:
            parts[-1] += DIM(f" ({', '.join(bias_bits)})")
    if n_drop:
        parts.append(YELLOW(f"drop {n_drop}"))
    if n_unapprove:
        parts.append(BLUE(f"un-approve {n_unapprove}"))
    if n_added:
        parts.append(BLUE(f"+{n_added} slot{'s' if n_added > 1 else ''}"))
    trend_actions = decisions.get("trend_actions", {})
    n_trend_add = sum(1 for a in trend_actions.values() if a in ("a", "i"))
    n_trend_dismiss = sum(1 for a in trend_actions.values() if a == "d")
    if n_trend_add:
        parts.append(GREEN(f"add {n_trend_add} trend{'s' if n_trend_add > 1 else ''}"))
    if n_trend_dismiss:
        parts.append(YELLOW(f"dismiss {n_trend_dismiss} trend{'s' if n_trend_dismiss > 1 else ''}"))
    print(f"  About to: {'  ·  '.join(parts)}")

    if not args.auto_approve:
        try:
            confirm = input(BOLD("Proceed? [Y/n]: ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            print(YELLOW("Cancelled."))
            return 1
        if confirm in ("n", "no"):
            print(YELLOW("Cancelled."))
            return 1

    summary = _apply_decisions(db, edition_id, pieces, decisions)
    trend_summary = _apply_trend_decisions(db, trending_items, trend_actions)
    s_approved = summary["approved"]
    s_rejected = summary["rejected"]
    s_dropped = summary["dropped"]
    s_added = summary["added"]
    s_unapproved = summary["unapproved"]
    print()
    print(f"  {GREEN(f'✓ {s_approved} approved')}, "
          f"{RED(f'✗ {s_rejected} rejected')}, "
          f"{YELLOW(f'⊘ {s_dropped} dropped')}, "
          f"{BLUE(f'↺ {s_unapproved} un-approved')}, "
          f"{BLUE(f'+ {s_added} slots added')}")
    if trend_summary["trend_added"] or trend_summary["trend_dismissed"]:
        added_str = GREEN(f"+ {trend_summary['trend_added']} trend(s) added")
        dismissed_str = YELLOW(f"⊘ {trend_summary['trend_dismissed']} trend(s) dismissed")
        print(f"  {added_str}, {dismissed_str}")

    # Print next-step guidance.
    print()
    print(DIM("─" * 70))
    needs_refill = (summary["rejected"] > 0 or summary["added"] > 0)
    if needs_refill:
        print(BOLD("Next:"))
        print("  Stage 7 will refill rejected slots and add any extras.")
        print(f"  {YELLOW('python -m aarva.daily --stage 7')}")
        print("  Then re-run this review:")
        print(f"  {YELLOW('python -m aarva.review')}")
    else:
        remaining_proposed = sum(
            1 for p in _load_review_pieces(db, edition_id)
            if p.review_status == "proposed"
        )
        if remaining_proposed == 0:
            print(GREEN(BOLD("All pieces approved! ✓")))
            print("  Run the finalize script to generate hooks, contexts, audio, "
                  "and publish:")
            print(f"  {YELLOW('bash scripts/finalize_edition.sh')}")
        else:
            print(f"  {remaining_proposed} proposed piece(s) still remaining.")
            print("  Re-run this review to handle them, or run with --auto-approve.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

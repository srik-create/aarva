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


from aarva.cli_utils import BOLD, DIM, RED, GREEN, YELLOW, BLUE  # noqa: F401


@dataclass
class ProposedPiece:
    index: int               # 1-based for display
    edition_id: int
    article_id: int
    slot: str
    position: int
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


def _load_proposed(db: Database, edition_id: int) -> list[ProposedPiece]:
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT ep.edition_id, ep.article_id, ep.slot, ep.position,
                   a.title, a.byline, a.canonical_url, a.excerpt, a.word_count,
                   p.name AS publication_name,
                   s.rigour, s.posture, s.self_implication, s.ranking_score,
                   s.lens, s.pillar, s.jtbd_primary
              FROM edition_pieces ep
              JOIN articles a ON a.id = ep.article_id
              JOIN publications p ON p.id = a.publication_id
              LEFT JOIN article_scores s ON s.article_id = a.id
             WHERE ep.edition_id = ?
               AND ep.review_status = 'proposed'
             ORDER BY ep.position
        """, (edition_id,)).fetchall()

    return [
        ProposedPiece(
            index=i + 1,
            edition_id=int(r["edition_id"]),
            article_id=int(r["article_id"]),
            slot=r["slot"],
            position=int(r["position"] or 0),
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


def _print_piece(p: ProposedPiece) -> None:
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

    print()
    print(f"  {BOLD(f'[{p.index}]')}  {BOLD(p.slot.replace('_', ' '))}  "
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


def _parse_decisions(raw: str, n_pieces: int) -> dict:
    """Parse the review-CLI command line into a structured decisions dict.

    Returns a dict with three keys:
      piece_actions: {piece_index: ('a' | 'r' | 'd', length_bias_or_None)}
                     'a' = approve, 'r' = reject (with optional bias),
                     'd' = drop without refill
      add_slots:     [alias, ...] — extras to ADD via "+behind" etc.
      add_bias:      currently unused; reserved

    Command syntax:
      <N>             approve piece N
      <N>a            approve piece N
      <N>r            reject piece N (refill with no bias)
      <N>l            reject piece N, refill prefer LONGER
      <N>s            reject piece N, refill prefer SHORTER
      <N>d            drop piece N entirely; no refill
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
    """
    cleaned = raw.strip().lower().replace(",", " ").replace(";", " ")

    decisions = {
        "piece_actions": {},   # type: dict[int, tuple[str, Optional[str]]]
        "add_slots": [],       # type: list[str]
    }

    if cleaned in ("", "all-a", "alla", "a"):
        for i in range(1, n_pieces + 1):
            decisions["piece_actions"][i] = ("a", None)
        return decisions
    if cleaned in ("all-r", "allr", "r"):
        for i in range(1, n_pieces + 1):
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

        # "<N>" or "<N><action_char>"
        # Default action is approve if no suffix.
        action_char = "a"
        bias = None
        if tok[-1] in ("a", "r", "l", "s", "d"):
            action_char = tok[-1]
            num_part = tok[:-1]
        else:
            num_part = tok
        if not num_part:
            raise ValueError(f"can't parse '{tok}' as <number>[a|r|l|s|d]")
        try:
            idx = int(num_part)
        except ValueError as e:
            raise ValueError(f"can't parse '{tok}' as <number>[a|r|l|s|d]") from e
        if idx < 1 or idx > n_pieces:
            raise ValueError(f"piece {idx} out of range (1–{n_pieces})")

        # 'l' and 's' are forms of reject with a length bias.
        if action_char == "l":
            action_char, bias = "r", "longer"
        elif action_char == "s":
            action_char, bias = "r", "shorter"

        decisions["piece_actions"][idx] = (action_char, bias)

    return decisions


def _apply_decisions(
    db: Database,
    edition_id: int,
    pieces: list[ProposedPiece],
    decisions: dict,
) -> dict:
    """Apply decisions to the DB. Returns a small summary dict.

    Side effects:
      - Pieces marked 'a' → review_status = 'approved'
      - Pieces marked 'r' → deleted from edition_pieces, added to
        edition_rejections, article status reset to 'scored'.
        If the action carries a length bias, the slot's bias is
        persisted on editions.slot_biases so Stage 7's refill respects it.
      - Pieces marked 'd' → deleted from edition_pieces AND the slot
        name is added to editions.dropped_slots so Stage 7 won't refill it.
      - Slots in decisions['add_slots'] are appended to
        editions.extra_slots so Stage 7's next run adds them.
    """
    summary = {"approved": 0, "rejected": 0, "dropped": 0, "added": 0}

    # Load current overrides from the editions row, mutate them in
    # Python, then write back. SQLite has no native JSON_set so this is
    # the cleanest approach.
    with db.connect() as conn:
        row = conn.execute(
            "SELECT extra_slots, dropped_slots, slot_biases "
            "FROM editions WHERE id = ?", (edition_id,),
        ).fetchone()
    extra_slots = json.loads((row["extra_slots"] if row else None) or "[]")
    dropped_slots = json.loads((row["dropped_slots"] if row else None) or "[]")
    slot_biases = json.loads((row["slot_biases"] if row else None) or "{}")

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

            elif action == "r":
                # Reject + maybe set length bias for the slot.
                conn.execute(
                    "INSERT OR IGNORE INTO edition_rejections "
                    "(edition_id, article_id, slot_at_rejection) VALUES (?, ?, ?)",
                    (edition_id, piece.article_id, piece.slot),
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
                # so Stage 7 won't refill it. We don't add to rejections
                # because we don't want to ban the article from FUTURE
                # editions — the user just doesn't want it in this one.
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
            "slot_biases = ? WHERE id = ?",
            (json.dumps(extra_slots), json.dumps(dropped_slots),
             json.dumps(slot_biases), edition_id),
        )

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

    pieces = _load_proposed(db, edition_id)
    if not pieces:
        print(GREEN(f"Edition #{edition_id} has no proposed pieces — all approved already."))
        return 0

    with db.connect() as conn:
        row = conn.execute(
            "SELECT edition_date FROM editions WHERE id = ?", (edition_id,),
        ).fetchone()
    today_iso = str(row["edition_date"]) if row else "?"

    _print_header(edition_id, today_iso, len(pieces), _approved_count(db, edition_id))

    for p in pieces:
        _print_piece(p)

    print()
    print(DIM("─" * 70))

    if args.auto_approve:
        decisions = {
            "piece_actions": {i: ("a", None) for i in range(1, len(pieces) + 1)},
            "add_slots": [],
        }
        print(GREEN(f"Auto-approving all {len(pieces)} pieces."))
    else:
        print(BOLD("Per-piece commands:"))
        print(DIM("  Na  approve piece N        Nd  drop piece N (no refill)"))
        print(DIM("  Nr  reject piece N         Nl  reject + prefer LONGER replacement"))
        print(DIM("                             Ns  reject + prefer SHORTER replacement"))
        print(BOLD("Edition-level commands:"))
        print(DIM("  +behind  +humans  +future  +feature  +curiosity  +escape"))
        print(DIM("    add another slot of that type for refill"))
        print(BOLD("Shortcuts:"))
        print(DIM("  all-a    approve everything"))
        print(DIM("  (empty)  approve everything"))
        print(DIM("  Example: '1a 2l 3a 4d 5a 6s +behind'"))
        print()
        while True:
            try:
                raw = input(BOLD("> "))
            except (EOFError, KeyboardInterrupt):
                print()
                print(YELLOW("Cancelled. No changes made."))
                return 1
            try:
                decisions = _parse_decisions(raw, len(pieces))
                break
            except ValueError as e:
                print(RED(f"  Couldn't parse that: {e}"))
                print(DIM("  Try again, or Ctrl-C to cancel."))

    # Confirm before writing. Group actions by type for a readable summary.
    actions = decisions["piece_actions"]
    n_approve = sum(1 for v in actions.values() if v[0] == "a")
    n_reject  = sum(1 for v in actions.values() if v[0] == "r")
    n_drop    = sum(1 for v in actions.values() if v[0] == "d")
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
    if n_added:
        parts.append(BLUE(f"+{n_added} slot{'s' if n_added > 1 else ''}"))
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
    s_approved = summary["approved"]
    s_rejected = summary["rejected"]
    s_dropped = summary["dropped"]
    s_added = summary["added"]
    print()
    print(f"  {GREEN(f'✓ {s_approved} approved')}, "
          f"{RED(f'✗ {s_rejected} rejected')}, "
          f"{YELLOW(f'⊘ {s_dropped} dropped')}, "
          f"{BLUE(f'+ {s_added} slots added')}")

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
        remaining_proposed = len(_load_proposed(db, edition_id))
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

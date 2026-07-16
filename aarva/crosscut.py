"""Crosscut review CLI — pick one pair from the longlist.

Two-stage review workflow (this module handles stage 1):

  Stage 1 (this module):
    Pair-detection has persisted ~10 candidate pairs to
    crosscut_pair_candidates. The user runs `python -m aarva.crosscut`
    and sees the longlist, with each candidate's topic, the two angles,
    and Aarva's one-line connection summary. The user picks ONE.

  Stage 2 (Phase 3 onward, in a follow-up module):
    Once picked, the pipeline generates intro / bridge / outro / key
    passages and builds a crosscut edition. The user gets a second
    review pass over those LLM outputs before TTS/publish.

Usage:
    python -m aarva.crosscut                # list & pick today's candidate
    python -m aarva.crosscut --list-only    # just print the longlist
    python -m aarva.crosscut --pick N       # pick candidate id N without prompt
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aarva.config import load_pipeline_config
from aarva.db import Database


from aarva.cli_utils import BOLD, DIM, RED, GREEN, YELLOW, BLUE, MAGENTA  # noqa: F401


@dataclass
class _Candidate:
    id: int
    candidate_date: str
    article_a_id: int
    article_b_id: int
    title_a: str
    title_b: str
    pub_a: str
    pub_b: str
    word_count_a: int
    word_count_b: int
    topic_label: str
    angle_a_label: str
    angle_b_label: str
    connection_summary: str
    connection_score: float
    divergence_score: float
    selected: bool
    # Divergent-view tier (2026-07-15) — 'OPPOSING_VIEWS' /
    # 'DIFFERENT_ANGLES', or None for rows persisted before this
    # column existed. See docs/session_plan_users_and_crosscut_
    # upgrades.md §2.
    stance: Optional[str]


def _load_today_longlist(db: Database, today: date) -> list[_Candidate]:
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT cpc.id, cpc.candidate_date, cpc.article_a_id, cpc.article_b_id,
                   cpc.topic_label, cpc.angle_a_label, cpc.angle_b_label,
                   cpc.connection_summary, cpc.connection_score,
                   cpc.divergence_score, cpc.selected_at, cpc.stance,
                   a.title AS title_a, a.word_count AS wc_a,
                   pa.name AS pub_a,
                   b.title AS title_b, b.word_count AS wc_b,
                   pb.name AS pub_b
              FROM crosscut_pair_candidates cpc
              JOIN articles a ON a.id = cpc.article_a_id
              JOIN articles b ON b.id = cpc.article_b_id
              JOIN publications pa ON pa.id = a.publication_id
              JOIN publications pb ON pb.id = b.publication_id
             WHERE cpc.candidate_date = ?
               AND cpc.superseded_at IS NULL
             ORDER BY cpc.connection_score DESC, cpc.divergence_score DESC
        """, (today.isoformat(),)).fetchall()
    return [
        _Candidate(
            id=int(r["id"]),
            candidate_date=str(r["candidate_date"]),
            article_a_id=int(r["article_a_id"]),
            article_b_id=int(r["article_b_id"]),
            title_a=r["title_a"] or "",
            title_b=r["title_b"] or "",
            pub_a=r["pub_a"] or "",
            pub_b=r["pub_b"] or "",
            word_count_a=int(r["wc_a"] or 0),
            word_count_b=int(r["wc_b"] or 0),
            topic_label=r["topic_label"] or "",
            angle_a_label=r["angle_a_label"] or "",
            angle_b_label=r["angle_b_label"] or "",
            connection_summary=r["connection_summary"] or "",
            connection_score=float(r["connection_score"] or 0),
            divergence_score=float(r["divergence_score"] or 0),
            selected=bool(r["selected_at"]),
            stance=r["stance"],
        )
        for r in rows
    ]


def _mark_selected(db: Database, candidate_id: int) -> None:
    with db.connect() as conn:
        conn.execute("""
            UPDATE crosscut_pair_candidates
               SET selected_at = CURRENT_TIMESTAMP
             WHERE id = ?
        """, (candidate_id,))


def _est_minutes(words: int) -> float:
    return words / 150.0


def _print_candidate(idx: int, c: _Candidate) -> None:
    score_str = f"{c.connection_score:.0f}/10"
    div_str = f"div={c.divergence_score:.0f}"
    print()
    print(f"  {BOLD(f'[{idx}]')}  {BOLD(c.topic_label or '(no topic)')}  "
          f"{DIM('—')}  {YELLOW(score_str)}  {DIM(div_str)}"
          + (f"  {MAGENTA('[divergent]')}" if c.stance == "OPPOSING_VIEWS" else "")
          + (f"  {GREEN('★ selected')}" if c.selected else ""))
    print(f"       {DIM(c.connection_summary)}")
    print(f"       {DIM('A:')} {BLUE(c.pub_a)}  "
          f"{DIM(f'{c.word_count_a:,}w / ~{_est_minutes(c.word_count_a):.0f}m')}  "
          f"— {c.title_a[:55]}")
    print(f"          {DIM('angle:')} {c.angle_a_label}")
    print(f"       {DIM('B:')} {BLUE(c.pub_b)}  "
          f"{DIM(f'{c.word_count_b:,}w / ~{_est_minutes(c.word_count_b):.0f}m')}  "
          f"— {c.title_b[:55]}")
    print(f"          {DIM('angle:')} {c.angle_b_label}")


def _print_header(today_iso: str, n: int) -> None:
    print()
    print(BOLD("═" * 70))
    print(BOLD(f"  Aarva Crosscut — longlist for {today_iso}"))
    print(BOLD("═" * 70))
    print(f"  {n} pair candidate{'s' if n != 1 else ''}, ranked by connection score")
    print()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--list-only", action="store_true",
                        help="Print longlist and exit (no selection prompt)")
    parser.add_argument("--pick", type=int, default=None,
                        help="Non-interactively pick candidate id N")
    args = parser.parse_args(argv)

    config = load_pipeline_config()
    db = Database(config.db_path)
    today = date.today()

    cands = _load_today_longlist(db, today)
    if not cands:
        print(RED(f"No crosscut candidates for {today.isoformat()}."))
        print("Run the pair-detection stage first:")
        print(f"  {YELLOW('python -m aarva.daily --stage crosscut')}")
        return 0

    _print_header(today.isoformat(), len(cands))
    for i, c in enumerate(cands, 1):
        _print_candidate(i, c)

    print()
    print(DIM("─" * 70))

    if args.list_only:
        return 0

    if args.pick is not None:
        # Look up by candidate id directly (not list index).
        matching = [c for c in cands if c.id == args.pick]
        if not matching:
            print(RED(f"No candidate with id {args.pick} in today's longlist."))
            return 1
        chosen = matching[0]
    else:
        already = next((c for c in cands if c.selected), None)
        if already:
            print(YELLOW(f"A pair has already been selected today: "
                        f"[{cands.index(already) + 1}] {already.topic_label}"))
            try:
                confirm = input(BOLD("Pick a different pair? [y/N]: ")).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if confirm not in ("y", "yes"):
                return 0

        print(BOLD("Enter the number of the pair you want for today's "
                   "crosscut episode, or 'q' to cancel."))
        try:
            raw = input(BOLD("> ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        if raw in ("", "q", "quit"):
            print(DIM("No selection made."))
            return 0
        try:
            idx = int(raw)
        except ValueError:
            print(RED(f"Not a number: '{raw}'"))
            return 1
        if idx < 1 or idx > len(cands):
            print(RED(f"Out of range (1–{len(cands)})"))
            return 1
        chosen = cands[idx - 1]

    _mark_selected(db, chosen.id)
    print()
    print(GREEN(BOLD(f"✓ Selected: {chosen.topic_label}")))
    print(DIM(f"  {chosen.connection_summary}"))
    print()
    print(BOLD("Next:"))
    print(f"  {YELLOW('python -m aarva.daily --crosscut-build')}")
    print(DIM("  generates intro/bridge/outro/key-passages from the selected pair"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

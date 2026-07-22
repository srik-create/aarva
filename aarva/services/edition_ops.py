"""Shared "add article to today's edition" primitive.

See docs/session_plan_operator_search_and_url_ingest.md. Both
`python -m aarva.search --add-to-edition` and `python -m aarva.
ingest_url --add-to-edition` need the same operation: insert an
article as a proposed piece in today's daily edition, bypassing Stage
7's automatic slot-fill selection. The added piece integrates
transparently with the existing `python -m aarva.review` CLI —
Stage 8 (hook/context) processes it like any other NULL-hook piece.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from aarva.db import Database

AddResult = Literal["added", "already_present", "no_edition"]


def add_article_to_todays_edition(
    db: Database,
    article_id: int,
    slot: str = "manual_addition",
    position: int | None = None,
) -> AddResult:
    """Insert `article_id` as a proposed piece in today's daily edition.

    Does NOT auto-create today's edition — that's Stage 7's job, and
    an operator adding an article manually before Stage 7 has run
    today has nothing to add it to yet. Does NOT re-validate candidate
    eligibility (aarva.services.candidate_filter) — the caller decides
    what's worth adding; this primitive only handles the mechanics of
    getting it into edition_pieces.

    Returns:
      "no_edition"       — no daily edition exists for today yet.
      "already_present"  — article_id is already in this edition's
                            edition_pieces (any review_status) — no-op,
                            not an error. Matches the spec's explicit
                            non-goal of dedup beyond exact-article-id.
      "added"            — inserted as review_status='proposed',
                            hook/contextualisation NULL (Stage 8's next
                            run fills them — it already skips only
                            pieces that HAVE a hook, so NULL-hook
                            manual additions get processed normally).
    """
    today = date.today().isoformat()
    with db.connect() as conn:
        edition_row = conn.execute(
            "SELECT id FROM editions "
            "WHERE edition_date = ? AND edition_type = 'daily'",
            (today,),
        ).fetchone()
        if not edition_row:
            return "no_edition"
        edition_id = int(edition_row["id"])

        existing = conn.execute(
            "SELECT 1 FROM edition_pieces "
            "WHERE edition_id = ? AND article_id = ?",
            (edition_id, article_id),
        ).fetchone()
        if existing:
            return "already_present"

        if position is None:
            max_row = conn.execute(
                "SELECT MAX(position) AS max_pos FROM edition_pieces "
                "WHERE edition_id = ?",
                (edition_id,),
            ).fetchone()
            position = int(max_row["max_pos"] or 0) + 1

        conn.execute(
            """
            INSERT INTO edition_pieces
                (edition_id, article_id, slot, position,
                 hook, contextualisation, audio_url, review_status)
            VALUES (?, ?, ?, ?, NULL, NULL, NULL, 'proposed')
            """,
            (edition_id, article_id, slot, position),
        )
        conn.execute(
            "UPDATE articles SET status = 'in_edition' WHERE id = ?",
            (article_id,),
        )

    return "added"

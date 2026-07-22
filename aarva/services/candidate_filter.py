"""Shared "valid candidate for today's edition" eligibility filter.

See docs/session_plan_operator_search_and_url_ingest.md. An article is
a valid candidate if ALL of:

1. articles.status = 'scored' — passed extraction + scoring.
2. Not already used in a genuinely published edition: no edition_pieces
   row with review_status='approved' AND audio_url IS NOT NULL (any
   edition_type). NOTE: editions.published_date is NOT a reliable
   "has this been published" flag — it defaults to CURRENT_TIMESTAMP
   at row creation (Stage 7 time), so it's non-NULL for every edition
   from the moment it exists, published or not. audio_url IS NOT NULL
   is the real signal — it's what aarva/services/queries.py's own
   load_daily_pieces_with_audio ("pieces ... whose audio has been
   generated") already keys published-ness on.
3. Not in edition_rejections (any edition — a reviewer reject is a
   standing "don't propose this again" signal).
4. Not in TODAY's edition's dropped_article_ids (review CLI polish
   Fix 1 — a same-edition-only exclusion, see docs/session_plan_
   review_cli_polish.md).
5. articles.full_text IS NOT NULL AND LENGTH(full_text) > 0.

Used by both `python -m aarva.search --for-edition` and
`python -m aarva.ingest_url` (implicitly — a freshly-ingested article
naturally satisfies 2/3/4 since it can't yet be in any of those
tables) to keep one definition of "eligible pool" instead of each
tool re-deriving it.
"""
from __future__ import annotations

import json
from datetime import date

from aarva.db import Database


def excluded_article_ids(db: Database, *, today: date | None = None) -> set[int]:
    """Article IDs that FAIL criteria 2/3/4 above — already published,
    rejected, or dropped from today's edition. Criteria 1/5 (status
    and full_text) are cheap enough to apply directly in a caller's
    own WHERE clause; this only covers the exclusion sets that need a
    cross-table look-up."""
    today = today or date.today()
    excluded: set[int] = set()

    with db.connect() as conn:
        published_rows = conn.execute("""
            SELECT DISTINCT ep.article_id
              FROM edition_pieces ep
             WHERE ep.review_status = 'approved'
               AND ep.audio_url IS NOT NULL
        """).fetchall()
        excluded.update(int(r["article_id"]) for r in published_rows)

        rejected_rows = conn.execute(
            "SELECT DISTINCT article_id FROM edition_rejections"
        ).fetchall()
        excluded.update(int(r["article_id"]) for r in rejected_rows)

        today_row = conn.execute(
            "SELECT dropped_article_ids FROM editions "
            "WHERE edition_date = ? AND edition_type = 'daily'",
            (today.isoformat(),),
        ).fetchone()
    if today_row and today_row["dropped_article_ids"]:
        dropped = json.loads(today_row["dropped_article_ids"])
        excluded.update(int(a) for a in dropped)

    return excluded


def valid_candidate_where_clause() -> str:
    """SQL fragment for criteria 1 + 5 (status + non-empty full_text),
    intended to be ANDed into a caller's own WHERE clause on an
    `articles a` alias. Criteria 2/3/4 (the cross-table exclusions)
    aren't expressible as a simple fragment — use
    `excluded_article_ids()` and filter `a.id NOT IN (...)` separately,
    or exclude in Python after loading the pool (cheaper for the
    typical pool size here)."""
    return "a.status = 'scored' AND a.full_text IS NOT NULL AND LENGTH(a.full_text) > 0"

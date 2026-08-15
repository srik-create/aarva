"""Tests for aarva/services/edition_ops.py's add_article_to_todays_edition
primitive, focused on the review_status parameter added 2026-08-15 —
see docs/session_plan_trend_adds_auto_approve.md.

Real production bug: a trend-added piece landed as review_status=
'proposed' (the primitive's original hardcoded default). A normal
iterative-review re-run of Stage 7 then wiped it via
stage_7_assemble.py's rebuild-refill, which deletes every piece with
review_status != 'approved' to make room for re-picks — the trend
add never got a second review pass, so it was silently gone from the
published edition. Covers: the new parameter's default stays
backward-compatible for aarva.search / aarva.ingest_url, and a real
regression test driving the actual Stage 7 rebuild-refill function
against a constructed DB matching the 2026-08-15 production timeline.
"""
from __future__ import annotations

from datetime import date

import pytest

from aarva.db import Database
from aarva.services.edition_ops import add_article_to_todays_edition
from aarva.stages.stage_7_assemble import _refill_for_review


@pytest.fixture
def edition_ops_db(tmp_path):
    """A real on-disk DB with today's daily edition already assembled
    and one scored article available to add."""
    db = Database(str(tmp_path / "aarva.db"))
    with db.connect() as conn:
        conn.execute("INSERT INTO publications (name, enabled) VALUES ('Pub', 1)")
        pub_id = conn.execute("SELECT id FROM publications").fetchone()[0]
        conn.execute(
            "INSERT INTO articles (canonical_url, title, publication_id, "
            "full_text, status) VALUES ('https://x/a', 'A', ?, 'body', 'scored')",
            (pub_id,),
        )
        article_id = conn.execute(
            "SELECT id FROM articles WHERE canonical_url = 'https://x/a'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO editions (edition_date, edition_type) "
            "VALUES (date('now'), 'daily')"
        )
    return {"db": db, "article_id": article_id, "pub_id": pub_id}


class TestReviewStatusParameter:
    def test_default_is_still_proposed(self, edition_ops_db):
        """Backward compat: aarva.search / aarva.ingest_url call this
        with no review_status argument and must keep getting 'proposed'."""
        db = edition_ops_db["db"]
        result = add_article_to_todays_edition(db, edition_ops_db["article_id"])
        assert result == "added"
        with db.connect() as conn:
            piece = conn.execute(
                "SELECT review_status FROM edition_pieces WHERE article_id = ?",
                (edition_ops_db["article_id"],),
            ).fetchone()
        assert piece["review_status"] == "proposed"

    def test_explicit_approved_is_honored(self, edition_ops_db):
        db = edition_ops_db["db"]
        result = add_article_to_todays_edition(
            db, edition_ops_db["article_id"], review_status="approved",
        )
        assert result == "added"
        with db.connect() as conn:
            piece = conn.execute(
                "SELECT review_status FROM edition_pieces WHERE article_id = ?",
                (edition_ops_db["article_id"],),
            ).fetchone()
        assert piece["review_status"] == "approved"


class TestStage7RebuildRegression:
    """Real end-to-end simulation of the 2026-08-15 production bug's
    exact mechanism: a trend-added piece + normal proposed pieces in
    the same edition, then _refill_for_review runs (what a real
    `python -m aarva.daily --stage 7` re-run does under review mode).
    """

    def test_approved_trend_add_survives_rebuild_proposed_pieces_do_not(
        self, edition_ops_db,
    ):
        db = edition_ops_db["db"]
        pub_id = edition_ops_db["pub_id"]

        with db.connect() as conn:
            edition_id = conn.execute("SELECT id FROM editions").fetchone()[0]

            # A normal Stage-7-proposed regular piece, still awaiting review.
            conn.execute(
                "INSERT INTO articles (canonical_url, title, publication_id, "
                "full_text, status) VALUES ('https://x/regular', 'Regular', "
                "?, 'body', 'in_edition')",
                (pub_id,),
            )
            regular_id = conn.execute(
                "SELECT id FROM articles WHERE canonical_url = 'https://x/regular'"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO edition_pieces (edition_id, article_id, slot, "
                "position, review_status) VALUES (?, ?, 'curiosity', 1, 'proposed')",
                (edition_id, regular_id),
            )

        # Trend add via the real primitive, exactly as review.py's
        # _apply_trend_decisions now calls it.
        result = add_article_to_todays_edition(
            db, edition_ops_db["article_id"], slot="delight",
            review_status="approved",
        )
        assert result == "added"

        # This is the exact function Stage 7 calls on every re-run
        # under review mode to refill empty slots.
        _refill_for_review(db, date.today())

        with db.connect() as conn:
            remaining = {
                r["article_id"]: r["review_status"]
                for r in conn.execute(
                    "SELECT article_id, review_status FROM edition_pieces"
                ).fetchall()
            }

        assert edition_ops_db["article_id"] in remaining, (
            "the approved trend-added piece must survive Stage 7's rebuild"
        )
        assert remaining[edition_ops_db["article_id"]] == "approved"
        assert regular_id not in remaining, (
            "the still-proposed regular piece should be wiped, as designed, "
            "to make room for Stage 7's re-pick"
        )

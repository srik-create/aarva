"""Tests for the trend-signal auto-dismiss-stale housekeeping.

See docs/session_plan_trend_hits_auto_dismiss_stale.md. Real
production incident, 2026-08-20: review's "Trending" sections have no
time filter on operator_action IS NULL, so every day's crawl only ever
added unresolved rows — 800+ backlogged trend_hits accumulated with
nothing ever sweeping yesterday's undecided ones.
"""
from __future__ import annotations

import pytest

from aarva.config import load_pipeline_config
from aarva.db import Database
from aarva.services.trend_maintenance import dismiss_stale_hits


@pytest.fixture
def maintenance_db(tmp_path):
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
    return {"db": db, "article_id": article_id}


def _insert_trend(db, hours_old, operator_action=None):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO trend_hits (source_name, trend_phrase, trend_phrase_en, "
            "seen_at, operator_action) "
            "VALUES ('src', ?, ?, datetime('now', ?), ?)",
            (f"phrase-{hours_old}-{operator_action}", f"phrase-{hours_old}-{operator_action}",
             f"-{hours_old} hours", operator_action),
        )


def _insert_virality(db, article_id, hours_old, operator_action=None):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO article_virality_hits (article_id, source_name, "
            "external_url, seen_at, operator_action) "
            "VALUES (?, 'hn', ?, datetime('now', ?), ?)",
            (article_id, f"https://x/{hours_old}-{operator_action}",
             f"-{hours_old} hours", operator_action),
        )


class TestDismissStaleHits:
    def test_stale_unresolved_trend_is_dismissed(self, maintenance_db):
        """Verification case 1: mixed seen_at, only the old ones move."""
        db = maintenance_db["db"]
        _insert_trend(db, hours_old=30)  # stale
        _insert_trend(db, hours_old=12)  # fresh
        config = load_pipeline_config()

        stats = dismiss_stale_hits(config, db)
        assert stats.trends_dismissed == 1

        with db.connect() as conn:
            rows = {
                r["trend_phrase"]: (r["operator_action"], r["resolved_at"])
                for r in conn.execute(
                    "SELECT trend_phrase, operator_action, resolved_at FROM trend_hits"
                ).fetchall()
            }
        stale_action, stale_resolved = rows["phrase-30-None"]
        fresh_action, fresh_resolved = rows["phrase-12-None"]
        assert stale_action == "auto_dismissed_stale"
        assert stale_resolved is not None
        assert fresh_action is None
        assert fresh_resolved is None

    def test_stale_unresolved_virality_hit_is_dismissed(self, maintenance_db):
        """Verification case 2: parallel case for article_virality_hits."""
        db = maintenance_db["db"]
        article_id = maintenance_db["article_id"]
        _insert_virality(db, article_id, hours_old=30)
        _insert_virality(db, article_id, hours_old=12)
        config = load_pipeline_config()

        stats = dismiss_stale_hits(config, db)
        assert stats.virality_dismissed == 1

        with db.connect() as conn:
            rows = {
                r["external_url"]: r["operator_action"]
                for r in conn.execute(
                    "SELECT external_url, operator_action FROM article_virality_hits"
                ).fetchall()
            }
        assert rows["https://x/30-None"] == "auto_dismissed_stale"
        assert rows["https://x/12-None"] is None

    def test_second_call_is_idempotent(self, maintenance_db):
        """Verification case 3."""
        db = maintenance_db["db"]
        _insert_trend(db, hours_old=30)
        config = load_pipeline_config()

        stats1 = dismiss_stale_hits(config, db)
        assert stats1.trends_dismissed == 1

        stats2 = dismiss_stale_hits(config, db)
        assert stats2.trends_dismissed == 0
        assert stats2.virality_dismissed == 0

    def test_already_resolved_rows_are_not_touched(self, maintenance_db):
        """Verification case 4: 'dismissed' and 'added' rows, even
        older than the cutoff, must not be overwritten."""
        db = maintenance_db["db"]
        article_id = maintenance_db["article_id"]
        _insert_trend(db, hours_old=100, operator_action="dismissed")
        _insert_trend(db, hours_old=100, operator_action="added")
        _insert_virality(db, article_id, hours_old=100, operator_action="dismissed")
        config = load_pipeline_config()

        stats = dismiss_stale_hits(config, db)
        assert stats.trends_dismissed == 0
        assert stats.virality_dismissed == 0

        with db.connect() as conn:
            actions = [
                r["operator_action"]
                for r in conn.execute("SELECT operator_action FROM trend_hits").fetchall()
            ]
        assert set(actions) == {"dismissed", "added"}

    def test_config_override_changes_cutoff(self, maintenance_db, monkeypatch):
        """Verification case 6: a 48h cutoff must NOT dismiss a 30h-old row."""
        db = maintenance_db["db"]
        _insert_trend(db, hours_old=30)
        config = load_pipeline_config()
        monkeypatch.setattr(
            config.__class__, "trends",
            property(lambda self: {"stale_after_hours": 48}),
        )

        stats = dismiss_stale_hits(config, db)
        assert stats.trends_dismissed == 0

    def test_default_cutoff_is_24_hours(self, maintenance_db):
        """Backward-compat: omitting stale_after_hours from config
        falls back to the locked 24h default."""
        db = maintenance_db["db"]
        _insert_trend(db, hours_old=25)
        config = load_pipeline_config()
        # No monkeypatch — real pipeline.yaml already has stale_after_hours: 24.
        stats = dismiss_stale_hits(config, db)
        assert stats.trends_dismissed == 1


class TestStage3EndToEnd:
    def test_stage3_dismisses_backlog_before_fresh_crawl_inserts(
        self, maintenance_db, monkeypatch,
    ):
        """Verification case 5: simulate the 2026-08-20 backlog, run
        the full --stage 3 sequence (crawler HTTP mocked), confirm old
        rows are auto-dismissed and today's fresh ones remain
        unresolved."""
        db = maintenance_db["db"]

        # Simulate a 100-row backlog, all stale.
        for i in range(100):
            with db.connect() as conn:
                conn.execute(
                    "INSERT INTO trend_hits (source_name, trend_phrase, "
                    "trend_phrase_en, seen_at) "
                    "VALUES ('src', ?, ?, datetime('now', '-30 hours'))",
                    (f"old-phrase-{i}", f"old-phrase-{i}"),
                )

        from aarva.sources import trend_crawler as crawler_module

        def fake_rss(geo, cache=False):
            return [{"trend": "fresh phrase", "traffic": "100+",
                     "published": None, "news_articles": [], "explore_link": None}]

        monkeypatch.setattr(crawler_module.trendspyg, "download_google_trends_rss", fake_rss)

        config = load_pipeline_config()

        class _StubLLM:
            def complete(self, prompt, *, expect_json=True, temperature=None, timeout=120):
                return "translated"
            @property
            def name(self): return "stub"

        from aarva.config import TrendSource
        sources = [TrendSource(name="google_trends_us", region="US", weight=0.7,
                                enabled=True, notes=None)]

        dismiss_stats = dismiss_stale_hits(config, db)
        assert dismiss_stats.trends_dismissed == 100

        crawler_module.crawl_trend_sources(config, db, sources=sources, llm=_StubLLM())

        with db.connect() as conn:
            unresolved = conn.execute(
                "SELECT trend_phrase FROM trend_hits WHERE operator_action IS NULL"
            ).fetchall()
            stale_count = conn.execute(
                "SELECT COUNT(*) AS n FROM trend_hits WHERE operator_action = 'auto_dismissed_stale'"
            ).fetchone()["n"]

        assert stale_count == 100
        assert {r["trend_phrase"] for r in unresolved} == {"fresh phrase"}

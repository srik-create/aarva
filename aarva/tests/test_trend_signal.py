"""Tests for the trend-signal layer (delight/timeliness).

See docs/session_plan_trend_signal_for_delight.md. Covers: the
ASCII-based translation-need heuristic, the crawler's idempotency
(trendspyg mocked — no real network calls in the automated suite; the
crawler was smoke-tested against live Google Trends by hand during
implementation), the matcher's guardrail SQL, blacklist, semantic
retrieval + re-rank + GDELT fallback (all external calls mocked — no
real Gemini/embedding/GDELT spend), and the review CLI's trend-action
parsing + apply logic against a disposable on-disk DB.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from aarva.config import TrendSource
from aarva.db import Database
import aarva.review as review_module
from aarva.review import (
    TrendingItem,
    _apply_trend_decisions,
    _load_trending,
    _parse_decisions,
)
from aarva.services.trend_matcher import (
    TrendMatchStats,
    _allowlist_domains,
    _gdelt_fallback_search,
    _is_blacklisted,
    _load_candidate_articles,
    _recently_surfaced_article_ids,
    _rerank,
    _semantic_candidates,
    match_trends,
)
from aarva.sources.trend_crawler import _needs_translation, crawl_trend_sources


class TestNeedsTranslation:
    def test_ascii_phrase_does_not_need_translation(self):
        assert _needs_translation("nfl games today") is False

    def test_non_ascii_phrase_needs_translation(self):
        assert _needs_translation("गूगल पिक्सल") is True

    def test_mixed_ascii_with_diacritic_needs_translation(self):
        # Known false-positive documented in trend_crawler.py — accepted
        # as harmless (a cheap no-op translation call).
        assert _needs_translation("Jagiellonia Białystok") is True


class TestIsBlacklisted:
    def test_no_blacklist_never_matches(self):
        assert _is_blacklisted("brad pitt", []) is False

    def test_substring_match_is_case_insensitive(self):
        assert _is_blacklisted("Some Politician Scandal", ["politician"]) is True

    def test_non_matching_phrase(self):
        assert _is_blacklisted("brad pitt", ["politician", "celebrity death"]) is False


@pytest.fixture
def trend_db(tmp_path):
    return Database(str(tmp_path / "aarva.db"))


class TestTrendCrawler:
    def _fake_rss(self, geo, cache=False):
        return [
            {"trend": f"trend one {geo}", "traffic": "100+",
             "published": None, "news_articles": [], "explore_link": None},
            {"trend": f"trend two {geo}", "traffic": "200+",
             "published": None, "news_articles": [], "explore_link": None},
        ]

    def test_inserts_new_hits_and_counts_them(self, trend_db, monkeypatch):
        from aarva.sources import trend_crawler as module
        monkeypatch.setattr(module.trendspyg, "download_google_trends_rss", self._fake_rss)

        from aarva.config import load_pipeline_config
        config = load_pipeline_config()

        class _StubLLM:
            def complete(self, prompt, *, expect_json=True, temperature=None, timeout=120):
                return "translated"
            @property
            def name(self): return "stub"

        sources = [TrendSource(name="google_trends_us", region="US", weight=0.7,
                                enabled=True, notes=None)]
        stats = crawl_trend_sources(config, trend_db, sources=sources, llm=_StubLLM())
        assert stats.sources_processed == 1
        assert stats.hits_added == 2
        assert stats.trends_seen == 2

        with trend_db.connect() as conn:
            rows = conn.execute("SELECT trend_phrase FROM trend_hits").fetchall()
        assert {r["trend_phrase"] for r in rows} == {"trend one US", "trend two US"}

    def test_recrawl_same_day_is_idempotent(self, trend_db, monkeypatch):
        from aarva.sources import trend_crawler as module
        monkeypatch.setattr(module.trendspyg, "download_google_trends_rss", self._fake_rss)
        from aarva.config import load_pipeline_config
        config = load_pipeline_config()

        class _StubLLM:
            def complete(self, prompt, *, expect_json=True, temperature=None, timeout=120):
                return "translated"
            @property
            def name(self): return "stub"

        sources = [TrendSource(name="google_trends_us", region="US", weight=0.7,
                                enabled=True, notes=None)]
        crawl_trend_sources(config, trend_db, sources=sources, llm=_StubLLM())
        stats2 = crawl_trend_sources(config, trend_db, sources=sources, llm=_StubLLM())
        assert stats2.hits_added == 0
        assert stats2.hits_already_seen == 2

    def test_disabled_source_is_skipped(self, trend_db, monkeypatch):
        from aarva.sources import trend_crawler as module
        called = []
        monkeypatch.setattr(
            module.trendspyg, "download_google_trends_rss",
            lambda geo, cache=False: called.append(geo) or [],
        )
        from aarva.config import load_pipeline_config
        config = load_pipeline_config()
        sources = [TrendSource(name="google_trends_us", region="US", weight=0.7,
                                enabled=False, notes=None)]
        stats = crawl_trend_sources(config, trend_db, sources=sources)
        assert called == []
        assert stats.sources_processed == 0

    def test_one_source_failing_does_not_break_the_others(self, trend_db, monkeypatch):
        from aarva.sources import trend_crawler as module

        def fake_rss(geo, cache=False):
            if geo == "BAD":
                raise RuntimeError("network exploded")
            return [{"trend": "ok trend", "traffic": "100+", "published": None,
                     "news_articles": [], "explore_link": None}]

        monkeypatch.setattr(module.trendspyg, "download_google_trends_rss", fake_rss)
        from aarva.config import load_pipeline_config
        config = load_pipeline_config()
        sources = [
            TrendSource(name="bad_source", region="BAD", weight=0.5, enabled=True, notes=None),
            TrendSource(name="good_source", region="US", weight=0.5, enabled=True, notes=None),
        ]
        stats = crawl_trend_sources(config, trend_db, sources=sources)
        assert stats.sources_failed == 1
        assert stats.sources_processed == 1
        assert stats.hits_added == 1


class TestGdeltFallbackSearch:
    def test_no_domains_returns_empty_without_network_call(self, monkeypatch):
        called = []
        monkeypatch.setattr("httpx.get", lambda *a, **k: called.append(1))
        result = _gdelt_fallback_search("brad pitt", [], 25, "14d")
        assert result == []
        assert called == []

    def test_domain_clause_and_query_construction(self, monkeypatch):
        captured = {}

        class _FakeResponse:
            def raise_for_status(self): pass
            def json(self):
                return {"articles": [
                    {"url": "https://aeon.co/a", "title": "A", "domain": "aeon.co"},
                ]}

        def fake_get(url, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            return _FakeResponse()

        monkeypatch.setattr("httpx.get", fake_get)
        result = _gdelt_fallback_search("brad pitt", ["aeon.co", "vox.com"], 25, "14d")
        assert captured["params"]["query"] == "brad pitt (domain:aeon.co OR domain:vox.com)"
        assert result == [{"url": "https://aeon.co/a", "title": "A", "domain": "aeon.co"}]

    def test_network_failure_returns_empty_not_raises(self, monkeypatch):
        def fake_get(*a, **k):
            raise RuntimeError("429")
        monkeypatch.setattr("httpx.get", fake_get)
        result = _gdelt_fallback_search("brad pitt", ["aeon.co"], 25, "14d")
        assert result == []


class TestAllowlistDomains:
    def test_strips_scheme_and_www_and_path(self, monkeypatch):
        from aarva.config import Publication
        monkeypatch.setattr(
            "aarva.services.trend_matcher.load_publications",
            lambda: [
                Publication(name="A", rss_url=None, homepage="https://www.aeon.co/",
                            tier="A", enabled=True, licence_status=None, notes=None),
                Publication(name="B", rss_url=None, homepage="http://vox.com/section",
                            tier="A", enabled=True, licence_status=None, notes=None),
                Publication(name="Disabled", rss_url=None, homepage="https://x.com",
                            tier="A", enabled=False, licence_status=None, notes=None),
                Publication(name="NoHomepage", rss_url=None, homepage=None,
                            tier="A", enabled=True, licence_status=None, notes=None),
            ],
        )
        domains = _allowlist_domains()
        assert domains == ["aeon.co", "vox.com"]


@pytest.fixture
def matcher_db(tmp_path):
    """A real on-disk DB with a mix of articles at different ages,
    JTBDs, and statuses, to exercise the guardrail SQL for real."""
    db = Database(str(tmp_path / "aarva.db"))
    with db.connect() as conn:
        conn.execute("INSERT INTO publications (name, enabled) VALUES ('Pub', 1)")
        pub_id = conn.execute("SELECT id FROM publications").fetchone()[0]

        def make_article(url, hours_old, jtbd, status, has_embedding=True):
            vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
            conn.execute(
                "INSERT INTO articles (canonical_url, title, publication_id, "
                "full_text, status, published_date, embedding, embedding_model) "
                "VALUES (?, 'Title', ?, 'body', ?, "
                "datetime('now', ?), ?, 'test-model')",
                (url, pub_id, status, f"-{hours_old} hours",
                 vec.tobytes() if has_embedding else None),
            )
            article_id = conn.execute(
                "SELECT id FROM articles WHERE canonical_url = ?", (url,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO article_scores (article_id, jtbd_primary) VALUES (?, ?)",
                (article_id, jtbd),
            )
            return article_id

        ids = {
            "old_delight": make_article("https://x/1", 72, "delight", "scored"),
            "too_new": make_article("https://x/2", 10, "delight", "scored"),
            "wrong_jtbd": make_article("https://x/3", 72, "keep_up_to_date", "scored"),
            "in_edition": make_article("https://x/4", 72, "delight", "in_edition"),
            "no_embedding": make_article("https://x/5", 72, "curiosity", "scored", has_embedding=False),
        }
    return {"db": db, "ids": ids}


class TestLoadCandidateArticles:
    def test_guardrails_filter_correctly(self, matcher_db):
        candidates = _load_candidate_articles(
            matcher_db["db"], age_min_hours=48,
            allowed_jtbds=["delight", "curiosity", "smart_escape", "keep_ahead"],
            embedding_model="test-model", exclude_ids=set(),
        )
        candidate_ids = {c["id"] for c in candidates}
        assert candidate_ids == {matcher_db["ids"]["old_delight"]}

    def test_exclude_ids_removes_recently_surfaced(self, matcher_db):
        candidates = _load_candidate_articles(
            matcher_db["db"], age_min_hours=48,
            allowed_jtbds=["delight"],
            embedding_model="test-model",
            exclude_ids={matcher_db["ids"]["old_delight"]},
        )
        assert candidates == []

    def test_empty_jtbd_list_returns_empty(self, matcher_db):
        assert _load_candidate_articles(
            matcher_db["db"], 48, [], "test-model", set(),
        ) == []


class TestRecentlySurfacedArticleIds:
    def test_only_recent_matched_trends_count(self, matcher_db):
        db = matcher_db["db"]
        recent_article_id = matcher_db["ids"]["old_delight"]
        old_article_id = matcher_db["ids"]["wrong_jtbd"]
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO trend_hits (source_name, trend_phrase, matched_article_id, seen_at) "
                "VALUES ('src', 'phrase', ?, datetime('now', '-2 days'))",
                (recent_article_id,),
            )
            conn.execute(
                "INSERT INTO trend_hits (source_name, trend_phrase, matched_article_id, seen_at) "
                "VALUES ('src', 'phrase2', ?, datetime('now', '-10 days'))",
                (old_article_id,),
            )
        recent = _recently_surfaced_article_ids(db, window_days=7)
        assert recent == {recent_article_id}


class TestSemanticCandidates:
    def test_unions_top_k_across_phrasings_deduped(self):
        candidates = [
            {"id": 1, "title": "A", "embedding": np.array([1.0, 0.0], dtype=np.float32)},
            {"id": 2, "title": "B", "embedding": np.array([0.0, 1.0], dtype=np.float32)},
        ]

        class _StubEmbedding:
            def embed(self, texts, *, task_type=None):
                text = texts[0]
                if text == "phrasing_a":
                    return np.array([[1.0, 0.0]], dtype=np.float32)
                return np.array([[0.0, 1.0]], dtype=np.float32)

        result = _semantic_candidates(
            ["phrasing_a", "phrasing_b"], candidates, _StubEmbedding(), top_k=1,
        )
        assert {c["id"] for c in result} == {1, 2}

    def test_no_candidates_returns_empty(self):
        assert _semantic_candidates(["x"], [], object(), top_k=10) == []


class TestRerank:
    def test_picks_highest_scoring_candidate(self):
        candidates = [{"id": 5, "title": "A"}, {"id": 6, "title": "B"}]

        class _StubLLM:
            def complete(self, prompt, *, expect_json=True, temperature=None, timeout=120):
                return {"scores": {"5": 2.0, "6": 4.5}}

        best_id, score = _rerank("some trend", candidates, _StubLLM())
        assert best_id == 6
        assert score == 4.5

    def test_malformed_response_returns_none(self):
        candidates = [{"id": 5, "title": "A"}]

        class _StubLLM:
            def complete(self, prompt, *, expect_json=True, temperature=None, timeout=120):
                return {}

        best_id, score = _rerank("some trend", candidates, _StubLLM())
        assert best_id is None
        assert score == 0.0

    def test_empty_candidates_returns_none_without_llm_call(self):
        called = []

        class _StubLLM:
            def complete(self, *a, **k):
                called.append(1)
                return {}

        best_id, score = _rerank("some trend", [], _StubLLM())
        assert best_id is None
        assert called == []


class TestMatchTrendsIntegration:
    def test_blacklisted_trend_is_dismissed_without_llm_calls(self, matcher_db):
        db = matcher_db["db"]
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO trend_hits (source_name, trend_phrase, trend_phrase_en) "
                "VALUES ('google_trends_us', 'some politician scandal', 'some politician scandal')"
            )

        from aarva.config import load_pipeline_config
        config = load_pipeline_config()
        import types
        config = config.__class__(**{**config.__dict__, "raw": {
            **config.raw, "trends": {"blacklist_phrases": ["politician"]},
        }})

        called = {"llm": 0}

        class _StubLLM:
            def complete(self, *a, **k):
                called["llm"] += 1
                return {}
            @property
            def name(self): return "stub"

        class _StubEmbedding:
            name = "test-model"
            def embed(self, texts, *, task_type=None):
                return np.array([[1.0, 0.0, 0.0]], dtype=np.float32)

        stats = match_trends(config, db, llm=_StubLLM(), embedding_client=_StubEmbedding())
        assert stats.blacklisted == 1
        assert called["llm"] == 0
        with db.connect() as conn:
            row = conn.execute("SELECT operator_action FROM trend_hits").fetchone()
        assert row["operator_action"] == "dismissed"

    def test_no_unresolved_trends_is_a_noop(self, matcher_db):
        from aarva.config import load_pipeline_config
        config = load_pipeline_config()
        stats = match_trends(config, matcher_db["db"])
        assert stats.trends_processed == 0


class TestParseDecisionsTrendTokens:
    def test_add_action_parses(self):
        decisions = _parse_decisions("t1a", n_pieces=0, proposed_indices=set(), n_trends=3)
        assert decisions["trend_actions"] == {1: "a"}

    def test_dismiss_and_ingest_actions_parse(self):
        decisions = _parse_decisions("t1d t2i", n_pieces=0, proposed_indices=set(), n_trends=3)
        assert decisions["trend_actions"] == {1: "d", 2: "i"}

    def test_mixed_piece_and_trend_tokens_in_one_line(self):
        decisions = _parse_decisions(
            "1a t1d 2r", n_pieces=2, proposed_indices={1, 2}, n_trends=2,
        )
        assert decisions["piece_actions"] == {1: ("a", None), 2: ("r", None)}
        assert decisions["trend_actions"] == {1: "d"}

    def test_out_of_range_trend_index_raises(self):
        with pytest.raises(ValueError, match="out of range"):
            _parse_decisions("t5a", n_pieces=0, proposed_indices=set(), n_trends=2)

    def test_unknown_trend_action_char_raises(self):
        with pytest.raises(ValueError, match="unknown trend action"):
            _parse_decisions("t1x", n_pieces=0, proposed_indices=set(), n_trends=2)


@pytest.fixture
def review_db_with_edition(tmp_path):
    db = Database(str(tmp_path / "aarva.db"))
    with db.connect() as conn:
        conn.execute("INSERT INTO publications (name, enabled) VALUES ('Pub', 1)")
        pub_id = conn.execute("SELECT id FROM publications").fetchone()[0]
        conn.execute(
            "INSERT INTO articles (canonical_url, title, publication_id, full_text, status) "
            "VALUES ('https://x/matched', 'Matched Title', ?, 'body', 'scored')",
            (pub_id,),
        )
        article_id = conn.execute(
            "SELECT id FROM articles WHERE canonical_url = 'https://x/matched'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO article_scores (article_id, jtbd_primary) VALUES (?, 'delight')",
            (article_id,),
        )
        conn.execute(
            "INSERT INTO editions (edition_date, edition_type) VALUES (date('now'), 'daily')"
        )
    return {"db": db, "article_id": article_id}


class TestApplyTrendDecisions:
    def test_add_action_adds_to_edition_and_resolves(self, review_db_with_edition):
        db = review_db_with_edition["db"]
        article_id = review_db_with_edition["article_id"]
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO trend_hits (source_name, trend_phrase, trend_phrase_en, "
                "matched_article_id, match_score) VALUES ('src', 'p', 'p', ?, 4.0)",
                (article_id,),
            )
        items = _load_trending(db)
        assert len(items) == 1

        summary = _apply_trend_decisions(db, items, {1: "a"})
        assert summary == {"trend_added": 1, "trend_dismissed": 0}

        with db.connect() as conn:
            piece = conn.execute(
                "SELECT slot FROM edition_pieces WHERE article_id = ?", (article_id,),
            ).fetchone()
            resolved = conn.execute(
                "SELECT operator_action FROM trend_hits",
            ).fetchone()
        assert piece["slot"] == "delight"
        assert resolved["operator_action"] == "added"

    def test_dismiss_action_marks_resolved_without_edition_write(self, review_db_with_edition):
        db = review_db_with_edition["db"]
        article_id = review_db_with_edition["article_id"]
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO trend_hits (source_name, trend_phrase, trend_phrase_en, "
                "matched_article_id, match_score) VALUES ('src', 'p', 'p', ?, 4.0)",
                (article_id,),
            )
        items = _load_trending(db)
        summary = _apply_trend_decisions(db, items, {1: "d"})
        assert summary == {"trend_added": 0, "trend_dismissed": 1}
        with db.connect() as conn:
            n_pieces = conn.execute("SELECT COUNT(*) AS n FROM edition_pieces").fetchone()["n"]
        assert n_pieces == 0

    def test_non_delight_jtbd_uses_bonus_slot(self, review_db_with_edition):
        db = review_db_with_edition["db"]
        article_id = review_db_with_edition["article_id"]
        with db.connect() as conn:
            conn.execute(
                "UPDATE article_scores SET jtbd_primary = 'curiosity' WHERE article_id = ?",
                (article_id,),
            )
            conn.execute(
                "INSERT INTO trend_hits (source_name, trend_phrase, trend_phrase_en, "
                "matched_article_id, match_score) VALUES ('src', 'p', 'p', ?, 4.0)",
                (article_id,),
            )
        items = _load_trending(db)
        _apply_trend_decisions(db, items, {1: "a"})
        with db.connect() as conn:
            piece = conn.execute(
                "SELECT slot FROM edition_pieces WHERE article_id = ?", (article_id,),
            ).fetchone()
        assert piece["slot"] == "bonus"

    def test_ingest_action_calls_ingest_one_and_adds(self, review_db_with_edition, monkeypatch):
        db = review_db_with_edition["db"]
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO trend_hits (source_name, trend_phrase, trend_phrase_en, "
                "fallback_urls_json) VALUES ('src', 'p', 'p', ?)",
                (json.dumps([{"url": "https://x/new", "title": "New", "domain": "x"}]),),
            )
            pub_id = conn.execute("SELECT id FROM publications").fetchone()[0]
            conn.execute(
                "INSERT INTO articles (canonical_url, title, publication_id, full_text, status) "
                "VALUES ('https://x/new', 'New Title', ?, 'body', 'ingested')",
                (pub_id,),
            )
            new_article_id = conn.execute(
                "SELECT id FROM articles WHERE canonical_url = 'https://x/new'"
            ).fetchone()[0]

        monkeypatch.setattr(
            "aarva.ingest_url._ingest_one",
            lambda config, db, url, dry_run: new_article_id,
        )
        items = _load_trending(db)
        summary = _apply_trend_decisions(db, items, {1: "i"})
        assert summary["trend_added"] == 1
        with db.connect() as conn:
            piece = conn.execute(
                "SELECT article_id FROM edition_pieces",
            ).fetchone()
        assert piece["article_id"] == new_article_id

    def test_unmentioned_trend_stays_unresolved(self, review_db_with_edition):
        db = review_db_with_edition["db"]
        article_id = review_db_with_edition["article_id"]
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO trend_hits (source_name, trend_phrase, trend_phrase_en, "
                "matched_article_id, match_score) VALUES ('src', 'p', 'p', ?, 4.0)",
                (article_id,),
            )
        items = _load_trending(db)
        summary = _apply_trend_decisions(db, items, {})
        assert summary == {"trend_added": 0, "trend_dismissed": 0}
        with db.connect() as conn:
            row = conn.execute("SELECT operator_action FROM trend_hits").fetchone()
        assert row["operator_action"] is None

    def test_add_on_no_match_trend_warns_and_stays_unresolved(self, review_db_with_edition, capsys):
        """A trend with no vector match has nothing for 'a' to add — this
        must warn the operator (suggest tNi/tNd), not silently no-op,
        and the trend must remain unresolved so it isn't lost."""
        db = review_db_with_edition["db"]
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO trend_hits (source_name, trend_phrase, trend_phrase_en, "
                "fallback_urls_json) VALUES ('src', 'p', 'p', '[]')"
            )
        items = _load_trending(db)
        summary = _apply_trend_decisions(db, items, {1: "a"})
        assert summary == {"trend_added": 0, "trend_dismissed": 0}
        assert "no Aarva match to add" in capsys.readouterr().out


@pytest.fixture
def full_review_scenario(tmp_path):
    """A real on-disk DB with a proposed piece in today's daily edition
    PLUS an unresolved trend_hits row — enough for a real main() drive,
    not just the individual helper functions."""
    db_path = tmp_path / "aarva.db"
    db = Database(str(db_path))
    with db.connect() as conn:
        conn.execute("INSERT INTO publications (name, enabled) VALUES ('Pub', 1)")
        pub_id = conn.execute("SELECT id FROM publications").fetchone()[0]
        conn.execute(
            "INSERT INTO articles (canonical_url, title, publication_id, "
            "full_text, status) VALUES ('https://x/piece', 'Piece Title', "
            "?, 'body text', 'scored')",
            (pub_id,),
        )
        piece_id = conn.execute(
            "SELECT id FROM articles WHERE canonical_url = 'https://x/piece'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO article_scores (article_id, rigour, posture, jtbd_primary) "
            "VALUES (?, 0.8, 0.8, 'curiosity')",
            (piece_id,),
        )
        conn.execute(
            "INSERT INTO editions (edition_date, edition_type) "
            "VALUES (date('now'), 'daily')"
        )
        edition_id = conn.execute("SELECT id FROM editions").fetchone()[0]
        conn.execute(
            "INSERT INTO edition_pieces (edition_id, article_id, slot, "
            "position, review_status) VALUES (?, ?, 'curiosity', 1, 'proposed')",
            (edition_id, piece_id),
        )
        conn.execute(
            "INSERT INTO trend_hits (source_name, trend_phrase, trend_phrase_en, "
            "fallback_urls_json) VALUES ('google_trends_us', 'p', 'p', '[]')"
        )
    return db_path


class TestTrendingAlwaysSurfaces:
    """No separate enabled flag (removed 2026-08-13 per user decision —
    running `--stage 3` is itself the opt-in, not a second toggle).
    Whatever trend_hits has unresolved always surfaces in review."""

    def _drive_main(self, db_path, monkeypatch):
        monkeypatch.setenv("AARVA_DB_PATH", str(db_path))
        inputs = iter(["", "y"])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(inputs))
        review_module.main([])

    def test_unresolved_trend_surfaces_with_no_config_toggle_needed(
        self, full_review_scenario, monkeypatch, capsys,
    ):
        self._drive_main(full_review_scenario, monkeypatch)
        assert "Trending topics" in capsys.readouterr().out

    def test_no_unresolved_trends_hides_section(self, tmp_path, monkeypatch, capsys):
        db_path = tmp_path / "aarva.db"
        db = Database(str(db_path))
        with db.connect() as conn:
            conn.execute("INSERT INTO publications (name, enabled) VALUES ('Pub', 1)")
            pub_id = conn.execute("SELECT id FROM publications").fetchone()[0]
            conn.execute(
                "INSERT INTO articles (canonical_url, title, publication_id, "
                "full_text, status) VALUES ('https://x/piece', 'Piece Title', "
                "?, 'body text', 'scored')",
                (pub_id,),
            )
            piece_id = conn.execute(
                "SELECT id FROM articles WHERE canonical_url = 'https://x/piece'"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO article_scores (article_id, rigour, posture, jtbd_primary) "
                "VALUES (?, 0.8, 0.8, 'curiosity')",
                (piece_id,),
            )
            conn.execute(
                "INSERT INTO editions (edition_date, edition_type) "
                "VALUES (date('now'), 'daily')"
            )
            edition_id = conn.execute("SELECT id FROM editions").fetchone()[0]
            conn.execute(
                "INSERT INTO edition_pieces (edition_id, article_id, slot, "
                "position, review_status) VALUES (?, ?, 'curiosity', 1, 'proposed')",
                (edition_id, piece_id),
            )
        self._drive_main(db_path, monkeypatch)
        assert "Trending topics" not in capsys.readouterr().out

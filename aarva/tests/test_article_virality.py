"""Tests for the reverse-lookup virality signal.

See docs/session_plan_trend_signal_v2.md concept B. Covers: the scan
candidate SQL (JTBD filter, NO age minimum per the locked asymmetric
guardrail decision), the already-scanned skip logic, HN Algolia
URL-search's client-side exact-URL filtering (HN's own query param
does fuzzy text matching, not exact equality — verified live
2026-08-20), and the end-to-end scan against a disposable on-disk DB
with mocked HTTP (no real HN spend in the automated suite — the real
service was verified against live production data during
implementation, finding 2 genuine Aarva articles currently on HN).
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from aarva.db import Database
from aarva.services.article_virality import (
    _already_scanned_urls,
    _hn_url_search,
    _load_scan_candidates,
    scan_for_virality,
)


@pytest.fixture
def virality_db(tmp_path):
    """Real on-disk DB with articles at various ages/JTBDs/statuses —
    exercises the reverse-lookup scan's JTBD-only guardrail (NO age
    minimum, unlike forward matching)."""
    db = Database(str(tmp_path / "aarva.db"))
    with db.connect() as conn:
        conn.execute("INSERT INTO publications (name, enabled) VALUES ('Pub', 1)")
        pub_id = conn.execute("SELECT id FROM publications").fetchone()[0]

        def make(url, days_old, jtbd, status):
            conn.execute(
                "INSERT INTO articles (canonical_url, title, publication_id, "
                "full_text, status, published_date) "
                "VALUES (?, 'Title', ?, 'body', ?, datetime('now', ?))",
                (url, pub_id, status, f"-{days_old} days"),
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
            "very_fresh": make("https://x/fresh", 0, "delight", "scored"),
            "very_old_but_allowed_jtbd": make("https://x/old-allowed", 89, "curiosity", "scored"),
            "beyond_scan_window": make("https://x/too-old", 100, "delight", "scored"),
            "wrong_jtbd": make("https://x/wrong-jtbd", 10, "keep_up_to_date", "scored"),
            "in_edition": make("https://x/in-edition", 10, "delight", "in_edition"),
        }
    return {"db": db, "ids": ids}


class TestLoadScanCandidates:
    ALL_JTBDS = ["delight", "curiosity", "smart_escape", "keep_ahead"]

    def test_very_fresh_article_is_a_candidate(self, virality_db):
        """Confirms the locked asymmetric-guardrail decision: NO age
        minimum for reverse lookup, unlike forward matching's 48h
        floor — a same-day article is eligible."""
        candidates = _load_scan_candidates(virality_db["db"], self.ALL_JTBDS, 90)
        ids = {c["id"] for c in candidates}
        assert virality_db["ids"]["very_fresh"] in ids

    def test_old_article_within_scan_window_is_a_candidate(self, virality_db):
        candidates = _load_scan_candidates(virality_db["db"], self.ALL_JTBDS, 90)
        ids = {c["id"] for c in candidates}
        assert virality_db["ids"]["very_old_but_allowed_jtbd"] in ids

    def test_beyond_scan_window_is_excluded(self, virality_db):
        """The 90-day window is a scan-COST cap, not an editorial
        guardrail — but it still bounds what gets queried."""
        candidates = _load_scan_candidates(virality_db["db"], self.ALL_JTBDS, 90)
        ids = {c["id"] for c in candidates}
        assert virality_db["ids"]["beyond_scan_window"] not in ids

    def test_wrong_jtbd_excluded(self, virality_db):
        candidates = _load_scan_candidates(virality_db["db"], self.ALL_JTBDS, 90)
        ids = {c["id"] for c in candidates}
        assert virality_db["ids"]["wrong_jtbd"] not in ids

    def test_in_edition_status_excluded(self, virality_db):
        candidates = _load_scan_candidates(virality_db["db"], self.ALL_JTBDS, 90)
        ids = {c["id"] for c in candidates}
        assert virality_db["ids"]["in_edition"] not in ids

    def test_empty_jtbd_list_returns_empty(self, virality_db):
        assert _load_scan_candidates(virality_db["db"], [], 90) == []


class TestAlreadyScannedUrls:
    def test_returns_article_ids_with_existing_hn_hit(self, virality_db):
        db = virality_db["db"]
        article_id = virality_db["ids"]["very_fresh"]
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO article_virality_hits (article_id, source_name, "
                "external_url) VALUES (?, 'hn', 'https://news.ycombinator.com/item?id=1')",
                (article_id,),
            )
        assert _already_scanned_urls(db) == {article_id}

    def test_empty_when_no_hits_exist(self, virality_db):
        assert _already_scanned_urls(virality_db["db"]) == set()


class TestHnUrlSearch:
    def _fake_response(self, hits):
        class _Resp:
            def raise_for_status(self): pass
            def json(self): return {"hits": hits}
        return _Resp()

    def test_exact_url_match_required(self, monkeypatch):
        """HN's query param does fuzzy text matching, not exact
        equality (verified live 2026-08-20 — a search for one URL
        also matched a same-domain-different-path variant). Must
        filter client-side or risk misattributing a different
        article's buzz."""
        import aarva.services.article_virality as module
        hits = [
            {"url": "https://example.com/exact", "points": 200,
             "num_comments": 10, "created_at_i": 9999999999, "objectID": "1"},
            {"url": "https://example.com/exact/variant", "points": 500,
             "num_comments": 50, "created_at_i": 9999999999, "objectID": "2"},
        ]
        monkeypatch.setattr(
            module.httpx, "get",
            lambda url, params=None, timeout=None: self._fake_response(hits),
        )
        result = _hn_url_search("https://example.com/exact", 100, 14)
        assert len(result) == 1
        assert result[0]["objectID"] == "1"

    def test_below_points_threshold_excluded(self, monkeypatch):
        import aarva.services.article_virality as module
        hits = [{"url": "https://example.com/x", "points": 50,
                 "num_comments": 1, "created_at_i": 9999999999, "objectID": "1"}]
        monkeypatch.setattr(
            module.httpx, "get",
            lambda url, params=None, timeout=None: self._fake_response(hits),
        )
        result = _hn_url_search("https://example.com/x", 100, 14)
        assert result == []

    def test_outside_lookback_window_excluded(self, monkeypatch):
        import aarva.services.article_virality as module
        hits = [{"url": "https://example.com/x", "points": 500,
                 "num_comments": 1, "created_at_i": 1, "objectID": "1"}]
        monkeypatch.setattr(
            module.httpx, "get",
            lambda url, params=None, timeout=None: self._fake_response(hits),
        )
        result = _hn_url_search("https://example.com/x", 100, 14)
        assert result == []

    def test_network_failure_returns_empty_not_raises(self, monkeypatch):
        import aarva.services.article_virality as module
        def fake_get(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(module.httpx, "get", fake_get)
        result = _hn_url_search("https://example.com/x", 100, 14)
        assert result == []


class TestScanForVirality:
    def test_end_to_end_inserts_hit_and_marks_idempotent(self, virality_db, monkeypatch):
        import aarva.services.article_virality as module
        import time as time_module

        article_id = virality_db["ids"]["very_fresh"]
        db = virality_db["db"]

        def fake_search(canonical_url, points_threshold, lookback_days):
            if canonical_url == "https://x/fresh":
                return [{"url": canonical_url, "points": 321, "num_comments": 45,
                          "created_at_i": int(time_module.time()), "objectID": "999"}]
            return []

        monkeypatch.setattr(module, "_hn_url_search", fake_search)
        from aarva.config import load_pipeline_config
        config = load_pipeline_config()

        stats = scan_for_virality(config, db)
        assert stats.hits_added == 1

        with db.connect() as conn:
            row = conn.execute(
                "SELECT article_id, source_name, score, num_comments, external_url "
                "FROM article_virality_hits",
            ).fetchone()
        assert row["article_id"] == article_id
        assert row["source_name"] == "hn"
        assert row["score"] == 321
        assert row["external_url"] == "https://news.ycombinator.com/item?id=999"

        # Re-running the scan should skip this article entirely — it
        # already has an HN hit, so _already_scanned_urls excludes it.
        stats2 = scan_for_virality(config, db)
        assert stats2.articles_scanned == len(
            _load_scan_candidates(
                db, ["delight", "curiosity", "smart_escape", "keep_ahead"], 90,
            ),
        ) - 1
        assert stats2.hits_added == 0

    def test_no_candidates_is_a_noop(self, tmp_path, monkeypatch):
        db = Database(str(tmp_path / "aarva.db"))
        from aarva.config import load_pipeline_config
        config = load_pipeline_config()
        called = []
        import aarva.services.article_virality as module
        monkeypatch.setattr(module, "_hn_url_search", lambda *a, **k: called.append(1))
        stats = scan_for_virality(config, db)
        assert stats.articles_scanned == 0
        assert called == []

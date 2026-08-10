"""Tests for the curation-platform cross-check ("not too niche" signal).

See docs/session_plan_curation_platform_signal.md. Covers: URL
normalization edge cases, the curation_hits lookup helper, the nightly
crawler's idempotency (feed fetching mocked — no real network calls in
the automated suite; the crawler was smoke-tested against all 6 real
live feeds by hand during implementation), and Stage 4-5-6's
curation_score integration end-to-end against a disposable on-disk DB
with a stub LLM client (no real Gemini spend).
"""
from __future__ import annotations

import json

import pytest

from aarva.config import CurationSource
from aarva.db import Database
from aarva.services.curation_lookup import (
    curation_lookup,
    curation_score_for,
    normalize_url,
)
from aarva.sources.curation_crawler import crawl_curation_sources
from aarva.sources.rss import FeedEntry


class TestNormalizeUrl:
    def test_lowercases_scheme_and_host(self):
        assert (
            normalize_url("HTTPS://Example.COM/Article")
            == "https://example.com/Article"
        )

    def test_strips_trailing_slash(self):
        assert (
            normalize_url("https://example.com/article/")
            == "https://example.com/article"
        )

    def test_strips_fragment(self):
        assert (
            normalize_url("https://example.com/article#section-2")
            == "https://example.com/article"
        )

    def test_strips_all_known_tracking_params(self):
        url = (
            "https://example.com/article"
            "?utm_source=x&utm_medium=y&utm_campaign=z&utm_term=a&utm_content=b"
            "&ref=longreads&ref_src=twitter&referer=foo&referrer=bar"
            "&source=newsletter&mc_cid=1&mc_eid=2&fbclid=3&gclid=4"
        )
        assert normalize_url(url) == "https://example.com/article"

    def test_preserves_non_tracking_query_params(self):
        assert (
            normalize_url("https://example.com/article?id=123")
            == "https://example.com/article?id=123"
        )

    def test_same_article_different_tracking_params_normalize_equal(self):
        a = normalize_url(
            "https://example.com/article/foo?utm_source=newsletter&id=9"
        )
        b = normalize_url("https://example.com/article/foo?id=9&ref=longreads")
        assert a == b

    def test_param_order_does_not_matter(self):
        a = normalize_url("https://example.com/x?id=1&tag=2")
        b = normalize_url("https://example.com/x?tag=2&id=1")
        assert a == b


@pytest.fixture
def curation_db(tmp_path):
    return Database(str(tmp_path / "curation_test.db"))


class TestCurationLookup:
    def test_no_hits_returns_empty_list(self, curation_db):
        assert curation_lookup(curation_db, "https://example.com/nothing") == []

    def test_empty_url_returns_empty_list(self, curation_db):
        assert curation_lookup(curation_db, "") == []
        assert curation_lookup(curation_db, None) == []

    def test_finds_hit_after_normalization_mismatch(self, curation_db):
        with curation_db.connect() as conn:
            conn.execute(
                "INSERT INTO curation_hits "
                "(source_name, url, url_normalized, title) VALUES (?, ?, ?, ?)",
                (
                    "Longreads",
                    "https://example.com/article/?utm_source=x",
                    normalize_url("https://example.com/article/?utm_source=x"),
                    "An Article",
                ),
            )
        # Aarva's own canonical_url for the same article, no tracking params.
        hits = curation_lookup(curation_db, "https://example.com/article")
        assert len(hits) == 1
        assert hits[0]["source_name"] == "Longreads"

    def test_multiple_sources_all_returned(self, curation_db):
        with curation_db.connect() as conn:
            for source in ("Longreads", "Kottke.org"):
                conn.execute(
                    "INSERT INTO curation_hits "
                    "(source_name, url, url_normalized, title) VALUES (?, ?, ?, ?)",
                    (source, "https://example.com/a", "https://example.com/a", "A"),
                )
        hits = curation_lookup(curation_db, "https://example.com/a")
        assert {h["source_name"] for h in hits} == {"Longreads", "Kottke.org"}


class TestCurationScoreFor:
    def test_no_hits_scores_zero(self, curation_db):
        score = curation_score_for(
            curation_db, "https://example.com/x", {"Longreads": 0.8},
        )
        assert score == 0.0

    def test_sums_weights_of_matched_sources_only(self, curation_db):
        with curation_db.connect() as conn:
            for source in ("Longreads", "Kottke.org", "Waxy.org"):
                conn.execute(
                    "INSERT INTO curation_hits "
                    "(source_name, url, url_normalized, title) VALUES (?, ?, ?, ?)",
                    (source, "https://example.com/a", "https://example.com/a", "A"),
                )
        score = curation_score_for(
            curation_db, "https://example.com/a",
            {"Longreads": 0.8, "Kottke.org": 0.6},  # Waxy.org intentionally omitted
        )
        assert score == pytest.approx(1.4)

    def test_source_absent_from_weights_contributes_zero_not_error(self, curation_db):
        """A hit from a source that's since been disabled/removed from
        curation_sources.yaml shouldn't crash the lookup — see the
        docstring on curation_score_for."""
        with curation_db.connect() as conn:
            conn.execute(
                "INSERT INTO curation_hits "
                "(source_name, url, url_normalized, title) VALUES (?, ?, ?, ?)",
                ("NowDisabledSource", "https://example.com/a",
                 "https://example.com/a", "A"),
            )
        score = curation_score_for(curation_db, "https://example.com/a", {})
        assert score == 0.0

    # ─── Topic-similarity fuzzy-match path (session_plan_curation_
    # topic_similarity.md) ────────────────────────────────────────────

    def test_no_embeddings_given_only_exact_match_counts(self, curation_db):
        """Without article_embedding/hit_embeddings, behavior is
        identical to the original exact-match-only function — the
        fuzzy path is purely additive, never required."""
        score = curation_score_for(
            curation_db, "https://example.com/x", {"Longreads": 0.8},
            article_embedding=None, hit_embeddings=None,
        )
        assert score == 0.0

    def test_fuzzy_match_above_floor_counts_at_reduced_weight(self, curation_db):
        import numpy as np

        article_vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        # cosine similarity with article_vec = 0.9 (well above the 0.80 floor)
        similar_vec = np.array([0.9, np.sqrt(1 - 0.9**2), 0.0], dtype=np.float32)
        score = curation_score_for(
            curation_db, "https://example.com/no-exact-match", {"Longreads": 0.8},
            article_embedding=article_vec,
            hit_embeddings=[("Longreads", similar_vec)],
            topic_similarity_floor=0.80,
        )
        assert score == pytest.approx(0.8 * 0.7)  # FUZZY_MATCH_WEIGHT_MULTIPLIER

    def test_fuzzy_match_below_floor_does_not_count(self, curation_db):
        import numpy as np

        article_vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        # cosine similarity = 0.5, below the 0.80 floor
        dissimilar_vec = np.array([0.5, np.sqrt(1 - 0.5**2), 0.0], dtype=np.float32)
        score = curation_score_for(
            curation_db, "https://example.com/no-exact-match", {"Longreads": 0.8},
            article_embedding=article_vec,
            hit_embeddings=[("Longreads", dissimilar_vec)],
            topic_similarity_floor=0.80,
        )
        assert score == 0.0

    def test_exact_match_takes_priority_over_fuzzy_for_same_source(self, curation_db):
        """A source that already exact-matched must not ALSO get
        credited via the fuzzy path — would double-count the same
        editorial signal from one source."""
        import numpy as np

        with curation_db.connect() as conn:
            conn.execute(
                "INSERT INTO curation_hits "
                "(source_name, url, url_normalized, title) VALUES (?, ?, ?, ?)",
                ("Longreads", "https://example.com/a",
                 "https://example.com/a", "A"),
            )
        article_vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        identical_vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        score = curation_score_for(
            curation_db, "https://example.com/a", {"Longreads": 0.8},
            article_embedding=article_vec,
            hit_embeddings=[("Longreads", identical_vec)],
            topic_similarity_floor=0.80,
        )
        # Full weight (exact), NOT full + 0.7*full (would be double-counting).
        assert score == pytest.approx(0.8)

    def test_best_fuzzy_match_per_source_only_not_summed(self, curation_db):
        """Two separate fuzzy hits from the SAME source only count
        once, at the best (highest-similarity) match — stops one
        prolific source from stacking multiple weak partial credits."""
        import numpy as np

        article_vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        hit_a = np.array([0.85, np.sqrt(1 - 0.85**2), 0.0], dtype=np.float32)
        hit_b = np.array([0.95, np.sqrt(1 - 0.95**2), 0.0], dtype=np.float32)
        score = curation_score_for(
            curation_db, "https://example.com/no-exact-match", {"Kottke.org": 0.6},
            article_embedding=article_vec,
            hit_embeddings=[("Kottke.org", hit_a), ("Kottke.org", hit_b)],
            topic_similarity_floor=0.80,
        )
        # Only the single best match's weight counts, not both summed.
        assert score == pytest.approx(0.6 * 0.7)

    def test_fuzzy_matches_from_different_sources_both_count(self, curation_db):
        import numpy as np

        article_vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        similar_vec = np.array([0.9, np.sqrt(1 - 0.9**2), 0.0], dtype=np.float32)
        score = curation_score_for(
            curation_db, "https://example.com/no-exact-match",
            {"Longreads": 0.8, "Kottke.org": 0.6},
            article_embedding=article_vec,
            hit_embeddings=[("Longreads", similar_vec), ("Kottke.org", similar_vec)],
            topic_similarity_floor=0.80,
        )
        assert score == pytest.approx(0.8 * 0.7 + 0.6 * 0.7)


class TestAllHitEmbeddings:
    def test_returns_only_rows_with_matching_embedding_model(self, curation_db):
        import numpy as np
        from aarva.services.curation_lookup import all_hit_embeddings

        vec = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        with curation_db.connect() as conn:
            conn.execute(
                "INSERT INTO curation_hits "
                "(source_name, url, url_normalized, title, embedding, embedding_model) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("Longreads", "https://example.com/a", "https://example.com/a",
                 "A", vec.tobytes(), "gemini-embedding-001-768"),
            )
            # Different model — must be excluded.
            conn.execute(
                "INSERT INTO curation_hits "
                "(source_name, url, url_normalized, title, embedding, embedding_model) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("Kottke.org", "https://example.com/b", "https://example.com/b",
                 "B", vec.tobytes(), "some-other-model"),
            )
            # No embedding at all — must be excluded.
            conn.execute(
                "INSERT INTO curation_hits "
                "(source_name, url, url_normalized, title) VALUES (?, ?, ?, ?)",
                ("Waxy.org", "https://example.com/c", "https://example.com/c", "C"),
            )

        result = all_hit_embeddings(curation_db, "gemini-embedding-001-768")
        assert len(result) == 1
        assert result[0][0] == "Longreads"
        assert np.array_equal(result[0][1], vec)


class TestExtractEmbeddedLinks:
    """digest-post link extraction (session_plan_curation_topic_
    similarity.md) — pulling real embedded article links out of a
    newsletter-issue-style feed entry's HTML body. The extraction was
    additionally smoke-tested against a real, live WITI feed entry
    during implementation (see docs/roadmap.md's 2026-08-10 entry)."""

    def _entry(self, html):
        return FeedEntry(
            canonical_url="https://whyisthisinteresting.substack.com/p/issue-1",
            title="The Issue Edition", byline=None, summary=None,
            published_date=None, raw_content_html=html,
        )

    def test_extracts_real_external_link_with_anchor_text(self):
        from aarva.sources.curation_crawler import _extract_embedded_links

        html = '<p><a href="https://wired.com/story/foo">A Great Wired Story</a></p>'
        result = _extract_embedded_links(
            self._entry(html), "whyisthisinteresting.substack.com",
        )
        assert result == [("https://wired.com/story/foo", "A Great Wired Story")]

    def test_no_content_html_returns_empty(self):
        from aarva.sources.curation_crawler import _extract_embedded_links

        entry = FeedEntry(
            canonical_url="https://example.com/x", title="X", byline=None,
            summary=None, published_date=None, raw_content_html=None,
        )
        assert _extract_embedded_links(entry, "example.com") == []

    def test_skips_empty_or_short_anchor_text(self):
        from aarva.sources.curation_crawler import _extract_embedded_links

        html = (
            '<a href="https://wired.com/a"></a>'
            '<a href="https://wired.com/b">here</a>'  # 4 chars, under the 10-char floor
            '<a href="https://wired.com/c">A Real Article Title</a>'
        )
        result = _extract_embedded_links(self._entry(html), "whyisthisinteresting.substack.com")
        assert result == [("https://wired.com/c", "A Real Article Title")]

    def test_skips_same_domain_self_links(self):
        from aarva.sources.curation_crawler import _extract_embedded_links

        html = (
            '<a href="https://whyisthisinteresting.substack.com/p/issue-1">Read more</a>'
            '<a href="https://wired.com/story/foo">A Real External Pick</a>'
        )
        result = _extract_embedded_links(self._entry(html), "whyisthisinteresting.substack.com")
        assert result == [("https://wired.com/story/foo", "A Real External Pick")]

    def test_skips_known_non_article_domains(self):
        from aarva.sources.curation_crawler import _extract_embedded_links

        html = "".join(
            f'<a href="https://{domain}/whatever">Some Real Looking Title</a>'
            for domain in (
                "substackcdn.com", "youtube.com", "youtu.be", "vimeo.com",
                "twitter.com", "x.com", "instagram.com",
            )
        )
        result = _extract_embedded_links(self._entry(html), "whyisthisinteresting.substack.com")
        assert result == []

    def test_caps_at_max_links_per_entry(self):
        from aarva.sources.curation_crawler import (
            _extract_embedded_links, _MAX_EXTRACTED_LINKS_PER_ENTRY,
        )

        html = "".join(
            f'<a href="https://example{i}.com/x">A Real Looking Article Title {i}</a>'
            for i in range(_MAX_EXTRACTED_LINKS_PER_ENTRY + 10)
        )
        result = _extract_embedded_links(self._entry(html), "whyisthisinteresting.substack.com")
        assert len(result) == _MAX_EXTRACTED_LINKS_PER_ENTRY

    def test_html_entities_in_anchor_text_are_unescaped_correctly(self):
        """Real anchor text often contains HTML entities (curly quotes,
        ampersands) — confirmed against a live WITI feed during
        implementation (e.g. "the Military&#8217;s Tech"). The current
        implementation strips tags but does NOT unescape entities —
        this test documents that as current behavior."""
        from aarva.sources.curation_crawler import _extract_embedded_links

        html = '<a href="https://wired.com/a">The Military&#8217;s Tech</a>'
        result = _extract_embedded_links(self._entry(html), "whyisthisinteresting.substack.com")
        assert result == [("https://wired.com/a", "The Military&#8217;s Tech")]


class TestCurationCrawler:
    """Feed fetching is mocked (monkeypatched fetch_feed) — no real
    network calls in the automated suite. The crawler was independently
    smoke-tested against all 6 real, live production feeds by hand
    during implementation (see docs/roadmap.md's 2026-08-10 entry) —
    these tests cover the insertion/idempotency logic fetch_feed
    itself doesn't touch."""

    def _make_source(self, name, weight=0.5, enabled=True):
        return CurationSource(
            name=name, homepage=None, feed_url=f"https://fake/{name}",
            weight=weight, enabled=enabled, notes=None,
        )

    def test_inserts_new_hits_and_counts_them(self, curation_db, monkeypatch):
        from aarva.sources import curation_crawler as module

        def fake_fetch_feed(feed_url, **kwargs):
            return [
                FeedEntry(
                    canonical_url="https://example.com/1", title="One",
                    byline=None, summary=None, published_date=None,
                ),
                FeedEntry(
                    canonical_url="https://example.com/2", title="Two",
                    byline=None, summary=None, published_date=None,
                ),
            ]

        monkeypatch.setattr(module, "fetch_feed", fake_fetch_feed)

        from aarva.config import load_pipeline_config
        config = load_pipeline_config()
        stats = crawl_curation_sources(
            config, curation_db, sources=[self._make_source("TestSource")],
        )
        assert stats.sources_processed == 1
        assert stats.sources_failed == 0
        assert stats.items_seen == 2
        assert stats.hits_added == 2
        assert stats.hits_already_seen == 0

        with curation_db.connect() as conn:
            rows = conn.execute(
                "SELECT url_normalized FROM curation_hits "
                "WHERE source_name = 'TestSource'"
            ).fetchall()
        assert len(rows) == 2

    def test_recrawl_is_idempotent(self, curation_db, monkeypatch):
        from aarva.sources import curation_crawler as module

        def fake_fetch_feed(feed_url, **kwargs):
            return [FeedEntry(
                canonical_url="https://example.com/1", title="One",
                byline=None, summary=None, published_date=None,
            )]

        monkeypatch.setattr(module, "fetch_feed", fake_fetch_feed)

        from aarva.config import load_pipeline_config
        config = load_pipeline_config()
        source = self._make_source("TestSource")
        crawl_curation_sources(config, curation_db, sources=[source])
        stats2 = crawl_curation_sources(config, curation_db, sources=[source])

        assert stats2.hits_added == 0
        assert stats2.hits_already_seen == 1
        with curation_db.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM curation_hits WHERE source_name = 'TestSource'"
            ).fetchone()[0]
        assert count == 1  # not duplicated

    def test_disabled_source_is_skipped(self, curation_db, monkeypatch):
        from aarva.sources import curation_crawler as module

        called = []

        def fake_fetch_feed(feed_url, **kwargs):
            called.append(feed_url)
            return []

        monkeypatch.setattr(module, "fetch_feed", fake_fetch_feed)

        from aarva.config import load_pipeline_config
        config = load_pipeline_config()
        stats = crawl_curation_sources(
            config, curation_db,
            sources=[self._make_source("Disabled", enabled=False)],
        )
        assert called == []
        assert stats.sources_processed == 0

    def test_one_source_failing_does_not_break_the_others(self, curation_db, monkeypatch):
        from aarva.sources import curation_crawler as module

        def fake_fetch_feed(feed_url, **kwargs):
            if "bad" in feed_url:
                raise RuntimeError("network exploded")
            return [FeedEntry(
                canonical_url="https://example.com/ok", title="Ok",
                byline=None, summary=None, published_date=None,
            )]

        monkeypatch.setattr(module, "fetch_feed", fake_fetch_feed)

        from aarva.config import load_pipeline_config
        config = load_pipeline_config()
        sources = [
            CurationSource(name="BadSource", homepage=None,
                           feed_url="https://fake/bad", weight=0.5,
                           enabled=True, notes=None),
            self._make_source("GoodSource"),
        ]
        stats = crawl_curation_sources(config, curation_db, sources=sources)
        assert stats.sources_failed == 1
        assert stats.sources_processed == 1
        assert stats.hits_added == 1


class _StubLLMClient:
    """Returns a fixed, canned response for every call — no real Gemini
    spend. Matches the mocking pattern already used in this repo for
    TTS safety-block tests (see test history in docs/roadmap.md's
    2026-07-22 TTS-boilerplate entry)."""

    def __init__(self, response: dict):
        self._response = response

    def complete(self, prompt, *, expect_json=True, temperature=None, timeout=120):
        return dict(self._response)

    @property
    def name(self) -> str:
        return "stub"


CANNED_RESPONSE = {
    "rigour": 0.8,
    "posture": 0.8,
    "self_implication": 0.5,
    "piece_type": "article",
    "rigour_rationale": "x",
    "posture_rationale": "x",
    "self_implication_rationale": "x",
    "lens": "test_lens",
    "pillar": "test_pillar",
    "jtbd_primary": "curiosity",
    "jtbd_secondary": None,
    "topic_recency_sensitivity": "low",
    "structural_form": "essay",
    "method_of_inquiry": "reporting",
    "voice_register": "neutral",
    "temporal_lens": "present",
    "cognitive_density": "medium",
    "emotional_register": "measured",
}


@pytest.fixture
def scoring_env(tmp_path, monkeypatch):
    """A real on-disk DB with one publication + one 'ingested' article,
    plus a curation_hits row that matches that article's canonical_url
    from one enabled source and one intentionally-absent-from-config
    source (to prove only configured sources' weights count)."""
    monkeypatch.setenv("AARVA_DB_PATH", str(tmp_path / "aarva.db"))
    db = Database(str(tmp_path / "aarva.db"))

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO publications (name, enabled) VALUES ('Test Pub', 1)"
        )
        pub_id = conn.execute(
            "SELECT id FROM publications WHERE name = 'Test Pub'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO articles "
            "(canonical_url, title, publication_id, full_text, status) "
            "VALUES (?, ?, ?, ?, 'ingested')",
            ("https://example.com/matched-article", "Matched Article",
             pub_id, "Full article text " * 50),
        )
        article_id = conn.execute(
            "SELECT id FROM articles WHERE canonical_url = "
            "'https://example.com/matched-article'"
        ).fetchone()[0]
        # A hit from a configured source (weight counts) ...
        conn.execute(
            "INSERT INTO curation_hits "
            "(source_name, url, url_normalized, title) VALUES (?, ?, ?, ?)",
            ("Longreads", "https://example.com/matched-article",
             "https://example.com/matched-article", "Matched Article"),
        )
        # ... and one from a source NOT in the enabled config (shouldn't count).
        conn.execute(
            "INSERT INTO curation_hits "
            "(source_name, url, url_normalized, title) VALUES (?, ?, ?, ?)",
            ("SomeUnconfiguredSource", "https://example.com/matched-article",
             "https://example.com/matched-article", "Matched Article"),
        )

    return {"db": db, "article_id": article_id}


class TestStage456CurationIntegration:
    def test_curation_disabled_leaves_score_at_zero_and_ranking_unaffected(
        self, scoring_env, monkeypatch,
    ):
        from aarva.config import load_pipeline_config
        from aarva.stages import stage_4_5_6_score

        config = load_pipeline_config()
        monkeypatch.setattr(config.__class__, "curation",
                             property(lambda self: {"enabled": False}))

        stub = _StubLLMClient(CANNED_RESPONSE)
        stage_4_5_6_score.score_all(
            config, scoring_env["db"],
            article_filter_ids={scoring_env["article_id"]}, llm=stub,
        )

        with scoring_env["db"].connect() as conn:
            row = conn.execute(
                "SELECT curation_score, status FROM articles WHERE id = ?",
                (scoring_env["article_id"],),
            ).fetchone()
            score_row = conn.execute(
                "SELECT ranking_score FROM article_scores WHERE article_id = ?",
                (scoring_env["article_id"],),
            ).fetchone()

        assert row["curation_score"] == 0.0  # untouched, default
        assert row["status"] == "scored"
        expected_base = round(0.45 * 0.8 + 0.45 * 0.8 + 0.10 * 0.5, 4)
        assert score_row["ranking_score"] == pytest.approx(expected_base)

    def test_curation_enabled_persists_score_and_bumps_ranking(
        self, scoring_env, monkeypatch,
    ):
        from aarva.config import load_pipeline_config, CurationSource
        from aarva.stages import stage_4_5_6_score

        config = load_pipeline_config()
        monkeypatch.setattr(
            config.__class__, "curation",
            property(lambda self: {"enabled": True, "score_weight": 0.10}),
        )
        # Only "Longreads" is a configured/enabled source — the
        # "SomeUnconfiguredSource" hit must NOT contribute.
        monkeypatch.setattr(
            stage_4_5_6_score, "load_curation_sources",
            lambda: [CurationSource(
                name="Longreads", homepage=None, feed_url="x",
                weight=0.8, enabled=True, notes=None,
            )],
        )

        stub = _StubLLMClient(CANNED_RESPONSE)
        stage_4_5_6_score.score_all(
            config, scoring_env["db"],
            article_filter_ids={scoring_env["article_id"]}, llm=stub,
        )

        with scoring_env["db"].connect() as conn:
            row = conn.execute(
                "SELECT curation_score FROM articles WHERE id = ?",
                (scoring_env["article_id"],),
            ).fetchone()
            score_row = conn.execute(
                "SELECT ranking_score FROM article_scores WHERE article_id = ?",
                (scoring_env["article_id"],),
            ).fetchone()

        # Only Longreads' weight (0.8) counts — the unconfigured
        # source's hit is silently ignored, not summed in.
        assert row["curation_score"] == pytest.approx(0.8)
        expected_base = 0.45 * 0.8 + 0.45 * 0.8 + 0.10 * 0.5
        expected_ranking = round(expected_base + 0.10 * 0.8, 4)
        assert score_row["ranking_score"] == pytest.approx(expected_ranking)
        # Confirm the curation term actually moved the score versus
        # the disabled case — not just a coincidentally-equal value.
        assert score_row["ranking_score"] > round(expected_base, 4)

    def test_curation_enabled_fuzzy_topic_match_bumps_ranking(
        self, scoring_env, monkeypatch,
    ):
        """End-to-end exercise of the topic-similarity path (session_
        plan_curation_topic_similarity.md): an article with NO exact-
        URL hit, but a real stored embedding similar to a curated
        pick's stored embedding, still gets a curation_score bump —
        at the reduced 0.7x weight, not full weight. build_embedding_
        client is stubbed (returns a fixed .name only, never calls
        .embed()) so this test makes no real network/API calls."""
        import numpy as np
        from aarva.config import load_pipeline_config, CurationSource
        from aarva.stages import stage_4_5_6_score

        db = scoring_env["db"]
        model_name = "test-embedding-model"
        article_vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        similar_vec = np.array([0.9, np.sqrt(1 - 0.9**2), 0.0], dtype=np.float32)

        with db.connect() as conn:
            conn.execute(
                "INSERT INTO articles "
                "(canonical_url, title, publication_id, full_text, status, "
                " embedding, embedding_model) "
                "SELECT 'https://example.com/fuzzy-only-article', "
                "'Fuzzy Only Article', publication_id, full_text, 'ingested', "
                "?, ? FROM articles LIMIT 1",
                (article_vec.tobytes(), model_name),
            )
            fuzzy_article_id = conn.execute(
                "SELECT id FROM articles WHERE canonical_url = "
                "'https://example.com/fuzzy-only-article'"
            ).fetchone()[0]
            # No exact-URL hit for this article at all — only a
            # topically-similar embedding on a DIFFERENT URL.
            conn.execute(
                "INSERT INTO curation_hits "
                "(source_name, url, url_normalized, title, embedding, embedding_model) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("Longreads", "https://example.com/a-different-curated-pick",
                 "https://example.com/a-different-curated-pick",
                 "A Different Curated Pick", similar_vec.tobytes(), model_name),
            )

        config = load_pipeline_config()
        monkeypatch.setattr(
            config.__class__, "curation",
            property(lambda self: {
                "enabled": True, "score_weight": 0.10,
                "topic_similarity_floor": 0.80,
            }),
        )
        monkeypatch.setattr(
            stage_4_5_6_score, "load_curation_sources",
            lambda: [CurationSource(
                name="Longreads", homepage=None, feed_url="x",
                weight=0.8, enabled=True, notes=None,
            )],
        )

        class _StubEmbeddingClient:
            name = model_name

        monkeypatch.setattr(
            stage_4_5_6_score, "build_embedding_client",
            lambda cfg: _StubEmbeddingClient(),
        )

        stub = _StubLLMClient(CANNED_RESPONSE)
        stage_4_5_6_score.score_all(
            config, db, article_filter_ids={fuzzy_article_id}, llm=stub,
        )

        with db.connect() as conn:
            row = conn.execute(
                "SELECT curation_score FROM articles WHERE id = ?",
                (fuzzy_article_id,),
            ).fetchone()

        # 0.8 (Longreads' weight) * 0.7 (fuzzy multiplier) — not full
        # weight, since this was a topic-similarity match, not exact.
        assert row["curation_score"] == pytest.approx(0.8 * 0.7)

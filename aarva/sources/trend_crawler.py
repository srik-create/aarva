"""Nightly crawl of trend sources for the delight/timeliness signal —
see docs/session_plan_trend_signal_for_delight.md and, for the
Bluesky/HN sources added in v2, docs/session_plan_trend_signal_v2.md.

v1 scope (locked 2026-08-13, see that doc's top-of-file NOTE): only
Google Trends is crawled, via the `trendspyg` library's RSS path
(fast, not rate-limit-sensitive — verified against live data for US/
IN/GB before wiring). YouTube Trending and a standalone GDELT "trend
source" were both dropped from v1; GDELT is still used, just as the
matching-flow fallback search in aarva.services.trend_matcher, not as
a crawled source here.

v2 (2026-08-20) adds two more source kinds, both real-verified before
wiring (rule 6a): Bluesky `getTrends` (public, no auth) and HN Algolia
`search_by_date` (public, no auth). Each `TrendSource.kind` selects a
handler below; all three return a common `(phrase, raw_metadata)`
shape so the shared translate+insert loop in crawl_trend_sources()
doesn't need to know which source produced a given trend.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date

import httpx
import trendspyg

from aarva.clients.llm import LLMClient, build_llm_client
from aarva.config import PipelineConfig, TrendSource, load_trend_sources
from aarva.db import Database

logger = logging.getLogger(__name__)

BLUESKY_GET_TRENDS_URL = "https://public.api.bsky.app/xrpc/app.bsky.unspecced.getTrends"
# Verified 2026-08-20 against the live endpoint: 25 is the actual server-
# enforced max ("integer too big (maximum 25, got 50)") — the v2 spec's
# own example URL used limit=50, which is wrong; this is the real cap.
BLUESKY_MAX_LIMIT = 25
# Real statuses observed 2026-08-20: 'trending', 'cooling', 'saturating'
# (not documented in the spec, which only anticipated 'trending'/
# 'cooling'/'stale'). Treated as an INCLUDE-list of exactly these two —
# 'saturating' (and any other unrecognized value) is excluded by
# default, same conservative treatment as the documented 'stale'.
_BLUESKY_INCLUDED_STATUSES = ("trending", "cooling")

HN_SEARCH_BY_DATE_URL = "https://hn.algolia.com/api/v1/search_by_date"


_TRANSLATE_PROMPT = (
    "Translate the following short trending search phrase into "
    "English. Reply with ONLY the translated phrase, nothing else "
    "(no quotes, no explanation).\n\nPhrase: {phrase}"
)


def _needs_translation(phrase: str) -> bool:
    """Simple ASCII heuristic — every non-English trend seen in real
    India-region data (2026-08-13) used non-Latin script (Hindi,
    Tamil), so this correctly separates them from English trends
    without adding a language-detection dependency for a binary call."""
    return not phrase.isascii()


def _translate(phrase: str, llm: LLMClient, cache: dict[str, str]) -> str:
    if phrase in cache:
        return cache[phrase]
    try:
        result = llm.complete(
            _TRANSLATE_PROMPT.format(phrase=phrase),
            expect_json=False,
        )
        translated = str(result).strip() or phrase
    except Exception as e:
        logger.warning("Trend translation failed for %r: %s", phrase, e)
        translated = phrase
    cache[phrase] = translated
    return translated


@dataclass
class TrendCrawlStats:
    sources_processed: int = 0
    sources_failed: int = 0
    trends_seen: int = 0
    hits_added: int = 0
    hits_already_seen: int = 0
    translations: int = 0
    per_source: dict[str, int] = field(default_factory=dict)


def _fetch_google_trends(source: TrendSource, config: PipelineConfig) -> list[tuple[str, dict]]:
    trends = trendspyg.download_google_trends_rss(geo=source.region, cache=False)
    results = []
    for trend in trends:
        phrase = (trend.get("trend") or "").strip()
        if not phrase:
            continue
        raw_metadata = {
            "traffic": trend.get("traffic"),
            "published": str(trend.get("published")) if trend.get("published") else None,
            "news_articles": trend.get("news_articles") or [],
            "explore_link": trend.get("explore_link"),
        }
        results.append((phrase, raw_metadata))
    return results


def _fetch_bluesky_trends(source: TrendSource, config: PipelineConfig) -> list[tuple[str, dict]]:
    """docs/session_plan_trend_signal_v2.md concept A. Skips 'stale'
    AND the real-but-undocumented 'saturating' status (see module
    docstring) and any category not in the configured allowlist —
    politics still gets crawled (so it's visible for debugging in
    raw_metadata_json) but never turned into a trend_hits row, since
    the allowlist check runs before the row is even built."""
    allowed_categories = set(config.trends.get(
        "bluesky_allowed_categories",
        ["culture", "science-tech", "entertainment", "sports", "education"],
    ))
    response = httpx.get(
        BLUESKY_GET_TRENDS_URL, params={"limit": BLUESKY_MAX_LIMIT}, timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    results = []
    for t in data.get("trends", []):
        if t.get("status") not in _BLUESKY_INCLUDED_STATUSES:
            continue
        if t.get("category") not in allowed_categories:
            continue
        phrase = (t.get("displayName") or "").strip()
        if not phrase:
            continue
        raw_metadata = {
            "postCount": t.get("postCount"),
            "category": t.get("category"),
            "status": t.get("status"),
            "topic": t.get("topic"),
        }
        results.append((phrase, raw_metadata))
    return results


def _fetch_hn_frontpage(source: TrendSource, config: PipelineConfig) -> list[tuple[str, dict]]:
    """docs/session_plan_trend_signal_v2.md concept A. The points
    threshold is enforced server-side via numericFilters (verified
    2026-08-20 against live data), not re-checked client-side."""
    points_threshold = int(config.trends.get("hn_points_threshold", 200))
    lookback_hours = int(config.trends.get("hn_lookback_hours", 24))
    cutoff = int(time.time()) - lookback_hours * 3600
    response = httpx.get(
        HN_SEARCH_BY_DATE_URL,
        params={
            "numericFilters": f"points>{points_threshold},created_at_i>{cutoff}",
            "tags": "story",
            "hitsPerPage": 30,
        },
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    results = []
    for hit in data.get("hits", []):
        phrase = (hit.get("title") or "").strip()
        if not phrase:
            continue
        raw_metadata = {
            "url": hit.get("url"),
            "points": hit.get("points"),
            "num_comments": hit.get("num_comments"),
            "story_id": hit.get("objectID"),
        }
        results.append((phrase, raw_metadata))
    return results


_FETCHERS = {
    "google_trends": _fetch_google_trends,
    "bluesky_trends": _fetch_bluesky_trends,
    "hn_frontpage": _fetch_hn_frontpage,
}


def crawl_trend_sources(
    config: PipelineConfig,
    db: Database,
    sources: list[TrendSource] | None = None,
    llm: LLMClient | None = None,
) -> TrendCrawlStats:
    """Fetch each enabled trend source (Google Trends via trendspyg's
    RSS path, Bluesky getTrends, HN Algolia search_by_date — dispatch
    by TrendSource.kind), translate non-English trend phrases via
    Gemini, and idempotently insert into trend_hits (same-day
    re-crawls of the same source+phrase are silently skipped via
    trend_hits' unique index on (source_name, trend_phrase,
    date(seen_at)) — see aarva/db.py). One bad source logs a warning
    and is skipped, same "one bad feed shouldn't break the run"
    posture as aarva.sources.curation_crawler."""
    if sources is None:
        sources = load_trend_sources()
    if llm is None:
        llm = build_llm_client(config.llm)

    stats = TrendCrawlStats()
    translation_cache: dict[str, str] = {}
    today = date.today().isoformat()

    for source in sources:
        if not source.enabled:
            continue
        fetcher = _FETCHERS.get(source.kind)
        if fetcher is None:
            logger.warning(
                "Unknown trend source kind %r for %s, skipping",
                source.kind, source.name,
            )
            stats.sources_failed += 1
            continue
        try:
            trends = fetcher(source, config)
        except Exception as e:
            logger.warning("Trend crawl failed for %s: %s", source.name, e)
            stats.sources_failed += 1
            continue

        stats.sources_processed += 1
        stats.trends_seen += len(trends)
        source_new_hits = 0

        with db.connect() as conn:
            for phrase, raw_metadata in trends:
                phrase_en = phrase
                if _needs_translation(phrase):
                    phrase_en = _translate(phrase, llm, translation_cache)
                    stats.translations += 1

                cur = conn.execute(
                    "INSERT OR IGNORE INTO trend_hits "
                    "(source_name, trend_phrase, trend_phrase_en, region, "
                    " raw_metadata_json) VALUES (?, ?, ?, ?, ?)",
                    (source.name, phrase, phrase_en, source.region,
                     json.dumps(raw_metadata)),
                )
                if cur.rowcount > 0:
                    source_new_hits += 1
                else:
                    stats.hits_already_seen += 1

        stats.hits_added += source_new_hits
        stats.per_source[source.name] = source_new_hits
        logger.info(
            "Trend crawl: %s (%s) — %d trends, %d new hits",
            source.name, source.region, len(trends), source_new_hits,
        )

    logger.info(
        "Trend crawl done — %d/%d sources ok, %d trends seen, "
        "%d translated, %d new hits, %d already known (day=%s)",
        stats.sources_processed, stats.sources_processed + stats.sources_failed,
        stats.trends_seen, stats.translations, stats.hits_added,
        stats.hits_already_seen, today,
    )
    return stats

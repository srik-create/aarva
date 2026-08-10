"""Nightly crawl of peer-curator RSS feeds for the "not too niche"
signal — see docs/session_plan_curation_platform_signal.md.

Reuses aarva.sources.rss.fetch_feed for the actual HTTP fetch +
feedparser parsing (same non-article-URL filtering, same lookback-
window logic, same User-Agent/Accept-header handling already tuned for
Aarva's own publication feeds) — a curator's RSS feed is structurally
the same kind of feed Stage 1 already knows how to read. No HTML-
scraping fallback is needed: every aarva/config/curation_sources.yaml
entry verified as a real RSS/Atom feed before being added (see the
spec's rule 6a verification note).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from aarva.config import CurationSource, PipelineConfig, load_curation_sources
from aarva.db import Database
from aarva.services.curation_lookup import normalize_url
from aarva.sources.rss import fetch_feed

logger = logging.getLogger(__name__)


@dataclass
class CurationCrawlStats:
    sources_processed: int = 0
    sources_failed: int = 0
    items_seen: int = 0
    hits_added: int = 0
    hits_already_seen: int = 0
    per_source: dict[str, int] = field(default_factory=dict)  # source -> new hits


def crawl_curation_sources(
    config: PipelineConfig,
    db: Database,
    sources: list[CurationSource] | None = None,
) -> CurationCrawlStats:
    """Fetch each enabled curation source's feed, extract (title, url)
    pairs, normalize, and idempotently insert into curation_hits.
    One bad source logs a warning and is skipped — same "one bad feed
    shouldn't break the run" posture as aarva.sources.rss.fetch_feed
    itself already has for Aarva's own publications."""
    if sources is None:
        sources = load_curation_sources()

    crawl_window_days = int(config.curation.get("crawl_window_days", 14))
    stats = CurationCrawlStats()

    for source in sources:
        if not source.enabled:
            continue
        try:
            entries = fetch_feed(
                source.feed_url,
                max_entries=100,
                lookback_days=crawl_window_days,
                timeout=config.ingestion.http_timeout_seconds,
                user_agent=config.ingestion.user_agent,
            )
        except Exception as e:
            logger.warning("Curation crawl failed for %s: %s", source.name, e)
            stats.sources_failed += 1
            continue

        stats.sources_processed += 1
        stats.items_seen += len(entries)
        source_new_hits = 0

        with db.connect() as conn:
            for entry in entries:
                normalized = normalize_url(entry.canonical_url)
                cur = conn.execute(
                    "INSERT OR IGNORE INTO curation_hits "
                    "(source_name, url, url_normalized, title) "
                    "VALUES (?, ?, ?, ?)",
                    (source.name, entry.canonical_url, normalized, entry.title),
                )
                if cur.rowcount > 0:
                    source_new_hits += 1
                else:
                    stats.hits_already_seen += 1

        stats.hits_added += source_new_hits
        stats.per_source[source.name] = source_new_hits
        logger.info(
            "Curation crawl: %s — %d items, %d new hits",
            source.name, len(entries), source_new_hits,
        )

    logger.info(
        "Curation crawl done — %d/%d sources ok, %d items seen, "
        "%d new hits, %d already known",
        stats.sources_processed, stats.sources_processed + stats.sources_failed,
        stats.items_seen, stats.hits_added, stats.hits_already_seen,
    )
    return stats

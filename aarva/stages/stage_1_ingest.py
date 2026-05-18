"""Stage 1 — Ingestion.

Pulls RSS feeds for every enabled publication, extracts full text for new
articles, writes them to the DB. Idempotent: re-running on the same day skips
articles already ingested.

Day 1 deliverable: this stage works end-to-end. Consolidation (Stage 1.5),
filters (Stage 2), and everything downstream are no-ops for now.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from aarva.config import PipelineConfig, Publication, load_publications
from aarva.db import Database
from aarva.sources.article_extractor import ExtractedArticle, extract_article
from aarva.sources.rss import FeedEntry, fetch_feed

logger = logging.getLogger(__name__)


@dataclass
class IngestionStats:
    publications_processed: int = 0
    entries_seen: int = 0
    already_known: int = 0
    extraction_failed: int = 0
    inserted: int = 0


def _ensure_publication_in_db(db: Database, pub: Publication) -> int:
    return db.upsert_publication(
        name=pub.name,
        rss_url=pub.rss_url,
        homepage=pub.homepage,
        tier=pub.tier,
        enabled=pub.enabled,
        licence_status=pub.licence_status,
        notes=pub.notes,
    )


def _ingest_entry(
    db: Database,
    publication_id: int,
    entry: FeedEntry,
    config: PipelineConfig,
    stats: IngestionStats,
) -> None:
    if db.article_exists(entry.canonical_url):
        stats.already_known += 1
        return

    extracted: Optional[ExtractedArticle] = extract_article(
        entry.canonical_url,
        timeout=config.ingestion.http_timeout_seconds,
        user_agent=config.ingestion.user_agent,
    )

    if not extracted:
        # Store the article anyway so we don't re-try it on every run, but
        # mark it as extraction_failed so Stage 2 ignores it.
        db.insert_article(
            canonical_url=entry.canonical_url,
            title=entry.title,
            byline=entry.byline,
            publication_id=publication_id,
            published_date=entry.published_date,
            word_count=None,
            full_text=None,
            excerpt=entry.summary,
            status="extraction_failed",
        )
        stats.extraction_failed += 1
        return

    article_id = db.insert_article(
        canonical_url=entry.canonical_url,
        title=entry.title,
        byline=entry.byline,
        publication_id=publication_id,
        published_date=entry.published_date,
        word_count=extracted.word_count,
        full_text=extracted.full_text,
        excerpt=extracted.excerpt,
        status="ingested",
    )
    if article_id is not None:
        stats.inserted += 1


def ingest_today(
    config: PipelineConfig,
    db: Database,
    *,
    publication_filter: Optional[set[str]] = None,
) -> IngestionStats:
    """Run Stage 1 ingestion for all enabled publications.

    publication_filter: if provided, limit the run to publications whose names
    are in this set. Useful for debugging a single feed.
    """
    publications = load_publications()
    stats = IngestionStats()

    for pub in publications:
        if not pub.enabled or not pub.rss_url:
            continue
        if publication_filter and pub.name not in publication_filter:
            continue

        publication_id = _ensure_publication_in_db(db, pub)
        stats.publications_processed += 1

        try:
            entries = fetch_feed(
                pub.rss_url,
                max_entries=config.ingestion.max_entries_per_feed,
                lookback_days=config.ingestion.lookback_days,
                timeout=config.ingestion.http_timeout_seconds,
                user_agent=config.ingestion.user_agent,
            )
        except Exception as e:
            logger.warning("Feed fetch failed for %s: %s", pub.name, e)
            continue

        for entry in entries:
            stats.entries_seen += 1
            try:
                _ingest_entry(db, publication_id, entry, config, stats)
            except Exception as e:
                logger.warning("Entry ingestion failed for %s: %s",
                               entry.canonical_url, e)

        logger.info(
            "Processed %s — %d entries seen, %d new, %d existing, %d extraction failures",
            pub.name, len(entries),
            stats.inserted, stats.already_known, stats.extraction_failed,
        )

    return stats

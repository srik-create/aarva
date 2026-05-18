"""RSS / Atom feed fetching for Aarva.

Each feed is fetched once per ingestion run; the parsed entries are returned as
plain Python dicts with normalised keys. Full-text extraction happens
separately in article_extractor.py.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

import feedparser
import httpx
from dateutil import parser as date_parser

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeedEntry:
    """A single article entry from an RSS / Atom feed.

    Just enough fields to feed downstream stages — full text comes later from
    the article extractor.
    """
    canonical_url: str
    title: str
    byline: Optional[str]
    summary: Optional[str]            # often a snippet from the feed itself
    published_date: Optional[datetime]


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = date_parser.parse(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError, OverflowError):
        return None


def _extract_byline(entry: dict) -> Optional[str]:
    """Feeds put authors in three or four different places; try them all."""
    if entry.get("author"):
        return str(entry["author"]).strip()
    authors = entry.get("authors")
    if isinstance(authors, list) and authors:
        names = [a.get("name", "").strip() for a in authors if isinstance(a, dict)]
        names = [n for n in names if n]
        if names:
            return ", ".join(names)
    creator = entry.get("dc_creator") or entry.get("creator")
    if creator:
        return str(creator).strip()
    return None


def fetch_feed(
    rss_url: str,
    *,
    max_entries: int = 30,
    lookback_days: int = 7,
    timeout: int = 30,
    user_agent: str = "Aarva/0.1",
) -> list[FeedEntry]:
    """Fetch and parse a feed. Returns entries published within the lookback window.

    Failures (network errors, malformed feeds) are logged and return [] rather
    than raising — one bad feed shouldn't break a pipeline run.
    """
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True,
                          headers={"User-Agent": user_agent}) as client:
            response = client.get(rss_url)
            response.raise_for_status()
            feed_text = response.text
    except (httpx.HTTPError, httpx.RequestError) as e:
        logger.warning("Failed to fetch %s: %s", rss_url, e)
        return []

    parsed = feedparser.parse(feed_text)
    if parsed.bozo and not parsed.entries:
        logger.warning("Feed parse failed for %s: %s",
                       rss_url, getattr(parsed, "bozo_exception", "unknown"))
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    entries: list[FeedEntry] = []
    for raw_entry in parsed.entries[:max_entries]:
        url = raw_entry.get("link")
        title = raw_entry.get("title")
        if not url or not title:
            continue

        published_date = _parse_date(
            raw_entry.get("published")
            or raw_entry.get("updated")
            or raw_entry.get("pubDate")
        )

        # Skip entries older than lookback window (when we know the date).
        if published_date and published_date < cutoff:
            continue

        entries.append(FeedEntry(
            canonical_url=url.strip(),
            title=title.strip(),
            byline=_extract_byline(raw_entry),
            summary=(raw_entry.get("summary") or raw_entry.get("description") or "").strip() or None,
            published_date=published_date,
        ))

    return entries

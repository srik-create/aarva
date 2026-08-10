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


# URL substrings that flag an entry as "not an article" — typically video pages,
# podcast episodes, image galleries, etc. These are always going to fail
# trafilatura extraction (there's no article text to extract), so we drop them
# at the RSS layer instead of wasting an HTTP fetch + an extraction attempt.
NON_ARTICLE_URL_SUBSTRINGS = (
    "/videos/",
    "/video/",
    "/podcasts/",
    "/podcast/",
    "/gallery/",
    "/galleries/",
    "/photos/",
    "/photo-",
    "/interactive/",
    "/multimedia/",
)


def _is_non_article_url(url: str) -> bool:
    lower = url.lower()
    return any(s in lower for s in NON_ARTICLE_URL_SUBSTRINGS)


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
    # Full HTML body if the feed provides one (feedparser's `content`
    # field), else the same value as `summary`. None if neither is
    # present. Added for docs/session_plan_curation_topic_similarity.md
    # — digest/newsletter-style feed entries (one issue = many picks)
    # embed their real links in here; `summary` alone is usually too
    # short to contain them. Optional with a default so this doesn't
    # break the one existing FeedEntry(...) construction site.
    raw_content_html: Optional[str] = None


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
        # Send an explicit Accept header for RSS/Atom/XML in addition
        # to the User-Agent. Some publishers (e.g., Caixin's gateway
        # API) do strict content-negotiation and return 406 Not
        # Acceptable when no Accept header is sent. Listing the
        # specific MIME types with */* as the fallback is broadly
        # compatible — any well-behaved RSS server matches one of them.
        headers = {
            "User-Agent": user_agent,
            "Accept": (
                "application/rss+xml, application/atom+xml, "
                "application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5"
            ),
        }
        with httpx.Client(timeout=timeout, follow_redirects=True,
                          headers=headers) as client:
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

        # Drop video / podcast / gallery entries — they have no article text
        # for trafilatura to extract, so they'd just clog Stage 1 with
        # extraction failures.
        if _is_non_article_url(url):
            logger.debug("Skipping non-article entry: %s", url)
            continue

        published_date = _parse_date(
            raw_entry.get("published")
            or raw_entry.get("updated")
            or raw_entry.get("pubDate")
        )

        # Skip entries older than lookback window (when we know the date).
        if published_date and published_date < cutoff:
            continue

        raw_content = raw_entry.get("content")
        raw_content_html = (
            raw_content[0].get("value")
            if raw_content and isinstance(raw_content, list)
            else None
        ) or (raw_entry.get("summary") or raw_entry.get("description") or None)

        entries.append(FeedEntry(
            canonical_url=url.strip(),
            title=title.strip(),
            byline=_extract_byline(raw_entry),
            summary=(raw_entry.get("summary") or raw_entry.get("description") or "").strip() or None,
            published_date=published_date,
            raw_content_html=raw_content_html,
        ))

    return entries

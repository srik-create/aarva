"""Nightly crawl of peer-curator RSS feeds for the "not too niche"
signal — see docs/session_plan_curation_platform_signal.md and, for
digest-link extraction + topic-similarity embedding,
docs/session_plan_curation_topic_similarity.md.

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
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from aarva.clients.embedding import build_embedding_client
from aarva.config import CurationSource, PipelineConfig, load_curation_sources
from aarva.db import Database
from aarva.services.curation_lookup import normalize_url
from aarva.sources.rss import FeedEntry, _is_non_article_url, fetch_feed

logger = logging.getLogger(__name__)


# Digest/newsletter-style entries (one feed item = one issue, with the
# real picks embedded as links inside) need their own non-article
# filtering, on top of aarva.sources.rss's existing path-substring
# check — a bare `youtube.com/watch?v=...` or a Substack CDN image URL
# doesn't contain any of THAT check's path substrings (/videos/ etc.),
# so those domains are matched directly instead.
_NON_ARTICLE_DOMAINS = (
    "substackcdn.com",
    "youtube.com", "youtu.be", "vimeo.com",
    "twitter.com", "x.com", "instagram.com",
)

_MIN_ANCHOR_TEXT_LEN = 10
_MAX_EXTRACTED_LINKS_PER_ENTRY = 15

_ANCHOR_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
_TAG_RE = re.compile(r"<[^<]+?>")


def _extract_embedded_links(entry: FeedEntry, source_domain: str) -> list[tuple[str, str]]:
    """Pull (url, anchor_text) pairs out of a feed entry's HTML body —
    for digest/newsletter-style entries (one issue, many embedded
    picks) whose own title/link don't describe any single article.
    Purely additive: the entry's own (title, url) is still recorded
    separately, unchanged, by the caller.

    Filters out: empty/too-short anchor text (drops "Read more" /
    bare image links), same-domain self-links (the issue linking back
    to itself), and known non-article/CDN/video/social domains. Capped
    at _MAX_EXTRACTED_LINKS_PER_ENTRY — no real entry inspected while
    building this came close, but a pathological one shouldn't be able
    to flood curation_hits."""
    html = entry.raw_content_html
    if not html:
        return []

    results: list[tuple[str, str]] = []
    for href, raw_text in _ANCHOR_RE.findall(html):
        text = _TAG_RE.sub("", raw_text).strip()
        if len(text) < _MIN_ANCHOR_TEXT_LEN:
            continue

        href = href.strip()
        domain = urlsplit(href).netloc.lower()
        if not domain:
            continue
        if domain == source_domain or domain.endswith(f".{source_domain}"):
            continue
        if any(domain == d or domain.endswith(f".{d}") for d in _NON_ARTICLE_DOMAINS):
            continue
        if _is_non_article_url(href):
            continue

        results.append((href, text))
        if len(results) >= _MAX_EXTRACTED_LINKS_PER_ENTRY:
            break

    return results


@dataclass
class CurationCrawlStats:
    sources_processed: int = 0
    sources_failed: int = 0
    items_seen: int = 0
    hits_added: int = 0
    hits_already_seen: int = 0
    links_extracted: int = 0
    per_source: dict[str, int] = field(default_factory=dict)  # source -> new hits


def crawl_curation_sources(
    config: PipelineConfig,
    db: Database,
    sources: list[CurationSource] | None = None,
) -> CurationCrawlStats:
    """Fetch each enabled curation source's feed, extract (title, url)
    pairs, normalize, and idempotently insert into curation_hits. For
    digest/newsletter-style entries (one issue, many embedded picks —
    see docs/session_plan_curation_topic_similarity.md), also extracts
    each embedded external link as an additional hit, alongside
    (never replacing) the entry's own (title, url) row. Newly-inserted
    hits are embedded via the configured embedding client (same one
    Stage 1.5 uses) so Stage 4-5-6 can fuzzy-match by topic similarity,
    not just exact URL. One bad source logs a warning and is skipped —
    same "one bad feed shouldn't break the run" posture as
    aarva.sources.rss.fetch_feed itself already has for Aarva's own
    publications."""
    if sources is None:
        sources = load_curation_sources()

    crawl_window_days = int(config.curation.get("crawl_window_days", 14))
    stats = CurationCrawlStats()
    # (source_name, url_normalized, title) for every genuinely new row
    # inserted this run — embedded in one batch after the crawl loop,
    # not per-row, to keep API calls to one per run per source.
    newly_inserted: list[tuple[str, str, str]] = []

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
                candidates = [(entry.canonical_url, entry.title)]
                source_domain = urlsplit(entry.canonical_url).netloc.lower()
                extracted = _extract_embedded_links(entry, source_domain)
                stats.links_extracted += len(extracted)
                candidates.extend(extracted)

                for url, title in candidates:
                    normalized = normalize_url(url)
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO curation_hits "
                        "(source_name, url, url_normalized, title) "
                        "VALUES (?, ?, ?, ?)",
                        (source.name, url, normalized, title),
                    )
                    if cur.rowcount > 0:
                        source_new_hits += 1
                        newly_inserted.append((source.name, normalized, title))
                    else:
                        stats.hits_already_seen += 1

        stats.hits_added += source_new_hits
        stats.per_source[source.name] = source_new_hits
        logger.info(
            "Curation crawl: %s — %d items, %d new hits",
            source.name, len(entries), source_new_hits,
        )

    if newly_inserted:
        try:
            client = build_embedding_client(config.raw.get("embedding", {}))
            vectors = client.embed([title for _, _, title in newly_inserted])
            with db.connect() as conn:
                for (source_name, url_normalized, _title), vec in zip(newly_inserted, vectors):
                    conn.execute(
                        "UPDATE curation_hits SET embedding = ?, embedding_model = ? "
                        "WHERE source_name = ? AND url_normalized = ?",
                        (vec.tobytes(), client.name, source_name, url_normalized),
                    )
        except Exception as e:
            # Embedding failure shouldn't lose the crawl's hits — the
            # rows just stay embedding=NULL (unmatched by the fuzzy
            # path) until a future crawl re-embeds them. Exact-URL
            # matching on these rows is completely unaffected.
            logger.warning(
                "Curation crawl: failed to embed %d new hits: %s",
                len(newly_inserted), e,
            )

    logger.info(
        "Curation crawl done — %d/%d sources ok, %d items seen, "
        "%d links extracted from digest-style entries, %d new hits, "
        "%d already known",
        stats.sources_processed, stats.sources_processed + stats.sources_failed,
        stats.items_seen, stats.links_extracted, stats.hits_added,
        stats.hits_already_seen,
    )
    return stats

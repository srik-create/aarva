"""Stage 2 — Hard filters.

Cheap signal-based rejection: word floor, listicle detection, digest/
collection patterns. Articles failing these checks get
`status='filtered_out'`. Articles already filtered by Stage 1.5 are
not re-evaluated.

The publication allowlist is enforced at ingestion time
(publications.yaml's `enabled` flag), so we don't re-check it here.

This is the last cheap step before LLM-based scoring (Stages 4+5+6),
so keeping the filters tight matters for compute cost.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from typing import Optional

from aarva.config import PipelineConfig
from aarva.db import Database

logger = logging.getLogger(__name__)


@dataclass
class FilterStats:
    candidates: int = 0
    failed_word_floor: int = 0
    failed_listicle: int = 0
    failed_digest_pattern: int = 0
    survivors: int = 0


# Title prefixes / phrases that mark a piece as a digest or collection
# rather than an individual article. Case-insensitive.
_DIGEST_TITLE_PREFIXES = (
    "early edition:",
    "evening edition:",
    "morning brief:",
    "morning briefing:",
    "evening brief:",
    "evening briefing:",
    "daily brief:",
    "daily briefing:",
    "weekly brief:",
    "weekly briefing:",
    "weekly digest:",
    "weekly:",
    "newsletter:",
    "collection:",
    "roundup:",
    "round-up:",
    "round up:",
    "digest:",
    "briefing:",
    "quick hits:",
    "the week:",
    "the week in",
    "week in review",
    "this week in",
    "what we're reading",
    "links roundup",
    "links: ",
)

# Whole-title regex for "title is just a date" — common for daily digest
# pages whose only identifying detail is the publication date.
_DATE_ONLY_TITLE_RES = (
    re.compile(r"^\s*\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\s*$"),         # 2026-06-09
    re.compile(r"^\s*\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\s*$"),       # 9/6/2026
    re.compile(
        r"^\s*(january|february|march|april|may|june|july|august|"
        r"september|october|november|december)\s+\d{1,2},?\s+\d{4}\s*$",
        re.IGNORECASE,
    ),                                                             # June 9, 2026
)


def _is_listicle(title: str, listicle_keywords: list[str]) -> bool:
    title_lower = f" {title.lower()} "
    return any(kw.lower() in title_lower for kw in listicle_keywords)


def _is_digest_or_collection(
    title: str, byline: str | None, publication: str | None,
) -> bool:
    """Conservative regex detector: title starts with a digest /
    collection prefix, OR the whole title is just a date. We do NOT
    use 'byline == publication' as a signal — too many real
    publications (Works in Progress, parts of The Bulwark, Rest of
    World) put the brand name as the default byline on individual
    articles. The LLM piece_type classifier in Stage 4-5-6 catches
    the subtler digest/collection/stub cases that this regex misses.

    The byline / publication arguments are still accepted for
    signature stability with the cleanup script."""
    if not title:
        return False
    title_low = title.strip().lower()
    if any(title_low.startswith(p) for p in _DIGEST_TITLE_PREFIXES):
        return True
    if any(rx.match(title) for rx in _DATE_ONLY_TITLE_RES):
        return True
    return False


def filter_hard(
    config: PipelineConfig,
    db: Database,
    *,
    article_filter_ids: Optional[set[int]] = None,
) -> FilterStats:
    """Run Stage 2: hard filters on articles that survived Stage 1.5.

    article_filter_ids: if provided, only filter articles with these
    IDs (mirrors stage_4_5_6_score.py::score_all's same-named param) —
    used by aarva/ingest_url.py to run this stage on a single ad-hoc
    article without touching other pending 'ingested' articles.
    """
    word_floor = config.filters.word_floor
    listicle_keywords = config.filters.listicle_keywords

    with db.connect() as conn:
        rows = conn.execute("""
            SELECT a.id, a.title, a.byline, a.word_count,
                   p.name AS publication
              FROM articles a
              JOIN publications p ON p.id = a.publication_id
             WHERE a.status = 'ingested'
        """).fetchall()
        candidates = [
            (int(r["id"]), r["title"], r["byline"], int(r["word_count"] or 0),
             r["publication"])
            for r in rows
            if article_filter_ids is None or int(r["id"]) in article_filter_ids
        ]

    stats = FilterStats(candidates=len(candidates))
    to_filter: list[tuple[int, str]] = []  # (id, reason)

    for article_id, title, byline, word_count, publication in candidates:
        if word_count < word_floor:
            to_filter.append((article_id, f"word_count_below_floor_{word_count}<{word_floor}"))
            stats.failed_word_floor += 1
            continue
        if _is_listicle(title, listicle_keywords):
            to_filter.append((article_id, "listicle_keyword_match"))
            stats.failed_listicle += 1
            continue
        if _is_digest_or_collection(title, byline, publication):
            to_filter.append((article_id, "digest_or_collection_pattern"))
            stats.failed_digest_pattern += 1
            continue
        stats.survivors += 1

    if to_filter:
        with db.connect() as conn:
            for article_id, reason in to_filter:
                conn.execute(
                    "UPDATE articles SET status = 'filtered_out' WHERE id = ?",
                    (article_id,),
                )
                logger.debug("Filtered out article %d: %s", article_id, reason)

    logger.info(
        "Stage 2 — %d candidates, %d below word floor, %d listicles, "
        "%d digests/collections, %d survivors",
        stats.candidates, stats.failed_word_floor, stats.failed_listicle,
        stats.failed_digest_pattern, stats.survivors,
    )

    return stats

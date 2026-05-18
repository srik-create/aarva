"""Stage 2 — Hard filters.

Cheap signal-based rejection: word floor, listicle detection. Articles failing
these checks get `status='filtered_out'`. Articles already filtered by
Stage 1.5 are not re-evaluated.

The publication allowlist is enforced at ingestion time (publications.yaml's
`enabled` flag), so we don't re-check it here.

This is the last cheap step before LLM-based scoring (Stages 4+5+6), so
keeping the filters tight matters for compute cost.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from aarva.config import PipelineConfig
from aarva.db import Database

logger = logging.getLogger(__name__)


@dataclass
class FilterStats:
    candidates: int = 0
    failed_word_floor: int = 0
    failed_listicle: int = 0
    survivors: int = 0


def _is_listicle(title: str, listicle_keywords: list[str]) -> bool:
    title_lower = f" {title.lower()} "
    return any(kw.lower() in title_lower for kw in listicle_keywords)


def filter_hard(
    config: PipelineConfig,
    db: Database,
) -> FilterStats:
    """Run Stage 2: hard filters on articles that survived Stage 1.5."""
    word_floor = config.filters.word_floor
    listicle_keywords = config.filters.listicle_keywords

    with db.connect() as conn:
        rows = conn.execute("""
            SELECT id, title, word_count
              FROM articles
             WHERE status = 'ingested'
        """).fetchall()
        candidates = [
            (int(r["id"]), r["title"], int(r["word_count"] or 0))
            for r in rows
        ]

    stats = FilterStats(candidates=len(candidates))
    to_filter: list[tuple[int, str]] = []  # (id, reason)

    for article_id, title, word_count in candidates:
        if word_count < word_floor:
            to_filter.append((article_id, f"word_count_below_floor_{word_count}<{word_floor}"))
            stats.failed_word_floor += 1
            continue
        if _is_listicle(title, listicle_keywords):
            to_filter.append((article_id, "listicle_keyword_match"))
            stats.failed_listicle += 1
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
        "Stage 2 — %d candidates, %d below word floor, %d listicles, %d survivors",
        stats.candidates, stats.failed_word_floor, stats.failed_listicle,
        stats.survivors,
    )

    return stats

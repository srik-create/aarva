"""Housekeeping for the trend-signal layer — see docs/session_plan_
trend_hits_auto_dismiss_stale.md.

Real production incident, 2026-08-20: `python -m aarva.review`'s
"Trending topics" and "Trending Aarva articles" sections both query
`operator_action IS NULL` with no time filter. Every day's `--stage 3`
crawl only ever ADDED unresolved rows — nothing ever swept yesterday's
undecided ones — so the backlog grew to 800+ suggestions the operator
never asked to see again.

dismiss_stale_hits() runs at the start of every `--stage 3` invocation,
BEFORE the forward crawl and reverse-lookup scan, marking anything
still unresolved past the configured cutoff as 'auto_dismissed_stale'
(never deleted — soft-supersede per AGENTS.md rule 12, and a distinct
marker from operator-intent 'dismissed' for future analysis). A topic
that's still genuinely trending gets re-inserted fresh by the same
day's crawl (idempotent per source+phrase+date), so nothing real is
lost — only genuinely stale, ignored suggestions are cleared.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from aarva.config import PipelineConfig
from aarva.db import Database

logger = logging.getLogger(__name__)


@dataclass
class DismissStaleStats:
    trends_dismissed: int = 0
    virality_dismissed: int = 0


def dismiss_stale_hits(config: PipelineConfig, db: Database) -> DismissStaleStats:
    hours = int(config.trends.get("stale_after_hours", 24))
    cutoff = f"-{hours} hours"

    with db.connect() as conn:
        trend_cur = conn.execute(
            "UPDATE trend_hits "
            "   SET operator_action = 'auto_dismissed_stale', "
            "       resolved_at = CURRENT_TIMESTAMP "
            " WHERE operator_action IS NULL "
            "   AND seen_at < datetime('now', ?)",
            (cutoff,),
        )
        virality_cur = conn.execute(
            "UPDATE article_virality_hits "
            "   SET operator_action = 'auto_dismissed_stale', "
            "       resolved_at = CURRENT_TIMESTAMP "
            " WHERE operator_action IS NULL "
            "   AND seen_at < datetime('now', ?)",
            (cutoff,),
        )

    stats = DismissStaleStats(
        trends_dismissed=trend_cur.rowcount,
        virality_dismissed=virality_cur.rowcount,
    )
    logger.info(
        "Auto-dismissed %d stale trends + %d stale virality hits "
        "(older than %dh).",
        stats.trends_dismissed, stats.virality_dismissed, hours,
    )
    return stats

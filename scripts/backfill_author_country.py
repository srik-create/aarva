"""Backfill articles.author_country_code for existing articles.

docs/session_plan_author_provenance_accents.md added per-article author-
provenance classification for TTS accent steering, wired into the daily
pipeline as Stage 8.5 going forward. This backfills the existing catalog
(~5,300 articles as of 2026-07-16, ~$5 total at Gemini's per-call cost).
Idempotent: only articles with author_country_code IS NULL are considered,
so it's safe to re-run — already-classified rows (including 'unknown',
a terminal result, not a retry state) are left alone.

Usage:
    python scripts/backfill_author_country.py
    python scripts/backfill_author_country.py --dry-run
    python scripts/backfill_author_country.py --limit 50
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aarva.config import load_pipeline_config
from aarva.db import Database
from aarva.stages.stage_8c_author_provenance import classify_pending_articles


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report how many articles are pending; don't classify anything.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Cap the number of articles classified this run (for staged rollout).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s  %(name)s  %(message)s",
    )
    logger = logging.getLogger("backfill_author_country")

    config = load_pipeline_config()
    db = Database(str(config.db_path))

    if args.dry_run:
        with db.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM articles "
                "WHERE author_country_code IS NULL AND full_text IS NOT NULL"
            ).fetchone()[0]
        logger.info("%d article(s) pending author-provenance classification", count)
        return 0

    stats = classify_pending_articles(config, db, limit=args.limit)
    logger.info(
        "Backfill complete — %d/%d classified (us=%d uk=%d india=%d unknown=%d), "
        "%d errors",
        stats.classified, stats.candidates,
        stats.us, stats.uk, stats.india, stats.unknown, stats.errors,
    )
    return 0 if stats.errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

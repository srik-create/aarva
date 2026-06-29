"""Backfill embeddings for existing crosscut episodes.

Iterates every crosscut edition in the DB and computes both embedding
variants (pairing_summary + article_mean) via
`aarva.services.crosscut_embeddings.embed_crosscut_episode`. Idempotent:
re-runnable; episodes that already have the embedding from the current
model get a no-op upsert.

Usage:
    python scripts/backfill_crosscut_embeddings.py
    python scripts/backfill_crosscut_embeddings.py --dry-run
    python scripts/backfill_crosscut_embeddings.py --since 2026-06-01

Reads the embedding client from pipeline.yaml's `embedding:` block —
same model and provider as Stage 1.5 so the new crosscut vectors live
in the same space as the article vectors.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow running as a top-level script
if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aarva.clients.embedding import build_embedding_client
from aarva.config import load_pipeline_config
from aarva.db import Database
from aarva.services.crosscut_embeddings import EmbedStats, embed_crosscut_episode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List episodes that would be embedded; don't write anything.",
    )
    parser.add_argument(
        "--since",
        help="Only embed episodes with edition_date >= this date (YYYY-MM-DD).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s  %(name)s  %(message)s",
    )
    logger = logging.getLogger("backfill_crosscut_embeddings")

    config = load_pipeline_config()
    db = Database(str(config.db_path))

    where = ["edition_type = 'crosscut'"]
    params: list = []
    if args.since:
        where.append("edition_date >= ?")
        params.append(args.since)

    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, edition_date, topic_label
              FROM editions
             WHERE {' AND '.join(where)}
             ORDER BY edition_date DESC
            """,
            params,
        ).fetchall()

    if not rows:
        logger.info("No crosscut episodes match the filter.")
        return 0

    logger.info("Found %d crosscut episode(s) to process", len(rows))

    if args.dry_run:
        for r in rows:
            print(f"  edition_id={r['id']}  date={r['edition_date']}  topic={r['topic_label']}")
        return 0

    emb_cfg = config.raw.get("embedding", {})
    client = build_embedding_client(emb_cfg)
    logger.info("Embedding client: %s (dim=%d)", client.name, client.dim)

    totals = EmbedStats()
    for r in rows:
        stats = embed_crosscut_episode(db, client, r["id"])
        totals.pairing_embedded += stats.pairing_embedded
        totals.article_mean_embedded += stats.article_mean_embedded
        totals.skipped_no_text += stats.skipped_no_text
        totals.skipped_missing_article_embeddings += stats.skipped_missing_article_embeddings
        totals.errors += stats.errors
        logger.info(
            "  edition_id=%-4d %s   pairing=%d  article_mean=%d  errors=%d",
            r["id"], r["edition_date"],
            stats.pairing_embedded, stats.article_mean_embedded, stats.errors,
        )

    logger.info(
        "Backfill complete — pairing=%d, article_mean=%d, "
        "skipped_no_text=%d, skipped_missing_article_embeddings=%d, errors=%d",
        totals.pairing_embedded, totals.article_mean_embedded,
        totals.skipped_no_text, totals.skipped_missing_article_embeddings,
        totals.errors,
    )
    return 0 if totals.errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

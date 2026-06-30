"""Re-embed every article + crosscut episode under the current embedding model.

Use this when the embedding model in `pipeline.yaml` has changed —
e.g. the 2026-06-30 switch from BGE-base-local to Vertex AI's
`gemini-embedding-001`. Walks two surfaces:

1. **Articles** with status in (`scored`, `in_basket`, `in_edition`)
   — i.e. anything reachable by the search/candidate flow.
   `articles.embedding` is OVERWRITTEN with the new vector and
   `articles.embedding_model` is stamped with the new model name.
   Old vectors are unrecoverable after this step (they live only on
   the laptop's previous DB snapshot if you need a rollback).
2. **Crosscut episodes** — re-uses
   `aarva.services.crosscut_embeddings.embed_crosscut_episode`, which
   writes one row per (edition_id, source, embedding_model). Old rows
   stay in the table (filtered out by model-name on read); the new
   rows are added under the current model's name.

Idempotent throughout: re-running this skips articles already at the
current model name and upserts crosscut rows via the existing
UNIQUE(edition_id, source, embedding_model) + ON CONFLICT.

Usage:
    python scripts/reembed_to_vertex_ai.py
    python scripts/reembed_to_vertex_ai.py --dry-run
    python scripts/reembed_to_vertex_ai.py --limit-articles 100  # quick test
    python scripts/reembed_to_vertex_ai.py --skip-articles       # just crosscuts
    python scripts/reembed_to_vertex_ai.py --skip-crosscuts      # just articles

After running locally, sync the DB to Render via
`scripts/sync_db_to_render.sh` so the new vectors land in production.
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


def reembed_articles(
    db: Database,
    client,
    *,
    dry_run: bool,
    limit: int | None,
) -> tuple[int, int, int]:
    """Re-embed eligible articles under the current model.

    Returns (already_current, embedded, errors)."""
    logger = logging.getLogger("reembed_articles")
    target_model = client.name

    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, title, excerpt, embedding_model
              FROM articles
             WHERE status IN ('scored', 'in_basket', 'in_edition')
            """
        ).fetchall()

    needs_embed = [r for r in rows if (r["embedding_model"] or "") != target_model]
    already_current = len(rows) - len(needs_embed)
    if limit is not None:
        needs_embed = needs_embed[:limit]

    logger.info(
        "Articles: total=%d  already_current=%d  to_embed=%d (target_model=%s)",
        len(rows), already_current, len(needs_embed), target_model,
    )
    if dry_run or not needs_embed:
        return already_current, 0, 0

    # Mirror Stage 1.5's input shape: title + first 1500 chars of excerpt.
    # Same recipe means the post-switch vectors live in the same
    # semantic space the pipeline produces for new articles going
    # forward.
    embedded = 0
    errors = 0
    for r in needs_embed:
        excerpt = (r["excerpt"] or "")[:1500]
        text = f"{r['title']}. {excerpt}"
        try:
            vec = client.embed([text])[0]   # defaults to RETRIEVAL_DOCUMENT
        except Exception as e:
            logger.warning("article %d embed failed: %s", r["id"], e)
            errors += 1
            continue
        try:
            db.set_article_embedding(
                article_id=int(r["id"]),
                embedding_bytes=vec.tobytes(),
                embedding_model=target_model,
            )
            embedded += 1
            if embedded % 100 == 0:
                logger.info("  progress: %d / %d", embedded, len(needs_embed))
        except Exception as e:
            logger.warning("article %d save failed: %s", r["id"], e)
            errors += 1
    return already_current, embedded, errors


def reembed_crosscuts(
    db: Database,
    client,
    *,
    dry_run: bool,
) -> tuple[int, int, int, int]:
    """Re-embed every crosscut episode under the current model.

    Reuses the existing service to keep the embed logic in one place.
    Returns (episodes_seen, pairing_embedded, article_mean_embedded,
    errors)."""
    from aarva.services.crosscut_embeddings import embed_crosscut_episode
    logger = logging.getLogger("reembed_crosscuts")

    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, edition_date, topic_label
              FROM editions
             WHERE edition_type = 'crosscut'
             ORDER BY edition_date DESC
            """
        ).fetchall()

    logger.info("Crosscut episodes: %d total to re-embed", len(rows))
    if dry_run or not rows:
        return len(rows), 0, 0, 0

    pairing_n = 0
    article_mean_n = 0
    errors = 0
    for r in rows:
        stats = embed_crosscut_episode(db, client, int(r["id"]))
        pairing_n += stats.pairing_embedded
        article_mean_n += stats.article_mean_embedded
        errors += stats.errors
        logger.info(
            "  edition_id=%-4d %s  pairing=%d article_mean=%d errors=%d",
            r["id"], r["edition_date"],
            stats.pairing_embedded, stats.article_mean_embedded, stats.errors,
        )
    return len(rows), pairing_n, article_mean_n, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would happen; don't write.")
    parser.add_argument("--skip-articles", action="store_true",
                        help="Skip the article re-embed pass.")
    parser.add_argument("--skip-crosscuts", action="store_true",
                        help="Skip the crosscut re-embed pass.")
    parser.add_argument("--limit-articles", type=int, default=None,
                        help="Embed at most N articles (useful for smoke tests).")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s  %(name)s  %(message)s",
    )
    logger = logging.getLogger("reembed_to_vertex_ai")

    config = load_pipeline_config()
    db = Database(str(config.db_path))

    emb_cfg = config.raw.get("embedding", {})
    client = build_embedding_client(emb_cfg)
    logger.info(
        "Target embedding: provider=%s name=%s dim=%d",
        emb_cfg.get("provider"), client.name, client.dim,
    )
    if args.dry_run:
        logger.info("DRY RUN — no writes will occur.")

    overall_errors = 0

    if not args.skip_articles:
        already, embedded, errors = reembed_articles(
            db, client,
            dry_run=args.dry_run, limit=args.limit_articles,
        )
        logger.info(
            "Articles: already_current=%d, embedded=%d, errors=%d",
            already, embedded, errors,
        )
        overall_errors += errors
    else:
        logger.info("Skipped articles pass.")

    if not args.skip_crosscuts:
        seen, pairing, article_mean, errors = reembed_crosscuts(
            db, client, dry_run=args.dry_run,
        )
        logger.info(
            "Crosscuts: seen=%d, pairing=%d, article_mean=%d, errors=%d",
            seen, pairing, article_mean, errors,
        )
        overall_errors += errors
    else:
        logger.info("Skipped crosscuts pass.")

    return 0 if overall_errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

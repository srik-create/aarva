"""Publish one or more selected articles as standalone BONUS episodes.

Each picked article becomes its own bonus edition (edition_type='bonus')
with its own hook + why-now context + narrated audio. They appear in
the RSS feed as itunes:episodeType="bonus" — Apple/Spotify show these
as side-content alongside the main daily series.

Usage:
    python -m aarva.publish_articles 2148 1885 1531
    python -m aarva.publish_articles --force 2148      # re-publish even if in_edition

Pairs with aarva.search via the --publish flag, which forwards the
top result IDs into this module.

After this script finishes (or after `--publish` from search), run:
    python -m aarva.daily --stage 10        # convert WAV → MP3, regen RSS
    bash scripts/publish.sh                 # push to gh-pages

The Stage 10 + publish.sh chain is intentionally a separate step so
you can batch multiple `publish_articles` runs and publish all of
them at once.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Optional

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aarva.config import load_pipeline_config
from aarva.db import Database
from aarva.stages import stage_8_hook_context, stage_9_tts


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("publish_articles")


from aarva.cli_utils import BOLD, DIM, RED, GREEN, YELLOW  # noqa: F401


def _load_article(db: Database, article_id: int) -> Optional[dict]:
    """Pull the article + score columns we need to validate the pick."""
    with db.connect() as conn:
        row = conn.execute("""
            SELECT a.id, a.title, a.byline, a.publication_id,
                   a.full_text, a.word_count, a.status,
                   a.canonical_url,
                   p.name AS publication,
                   s.verdict, s.ranking_score
              FROM articles a
              JOIN publications p ON p.id = a.publication_id
              LEFT JOIN article_scores s ON s.article_id = a.id
             WHERE a.id = ?
        """, (article_id,)).fetchone()
    return dict(row) if row else None


def _create_bonus_edition_with_piece(
    db: Database, article_id: int, today: date,
) -> int:
    """Create a new bonus edition row plus its single edition_piece.
    Returns the new edition_id."""
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO editions (edition_date, edition_type) "
            "VALUES (?, 'bonus')",
            (today.isoformat(),),
        )
        edition_id = int(cur.lastrowid)
        conn.execute("""
            INSERT INTO edition_pieces
                (edition_id, article_id, slot, position, review_status)
            VALUES (?, ?, 'bonus', 0, 'approved')
        """, (edition_id, article_id))
    return edition_id


def _validate_pick(article: Optional[dict], force: bool) -> tuple[bool, str]:
    """Decide whether to proceed with publishing this article.
    Returns (ok, reason). Refuses by default if the article is missing,
    has no full text, or has been in a past edition (idempotency).
    --force overrides the in-edition check.
    """
    if not article:
        return False, "article not found"
    if not (article.get("full_text") or "").strip():
        return False, "no full_text — can't narrate"
    if article.get("status") == "extraction_failed":
        return False, "extraction failed — no narratable content"
    if article.get("status") == "in_edition" and not force:
        return False, (
            "already published in a past edition. Use --force to "
            "re-publish (will overwrite the existing audio file)."
        )
    if article.get("verdict") == "FAIL":
        return False, (
            "scored as FAIL (below rigour/posture floor). "
            "Editorial bar protection. Use --force to override."
        )
    return True, "ok"


def publish_one(
    config, db: Database, article_id: int, today: date, force: bool,
) -> Optional[int]:
    """Build a bonus episode for a single article. Returns the
    edition_id on success, None on validation failure."""
    article = _load_article(db, article_id)
    ok, reason = _validate_pick(article, force)
    if not ok:
        print(f"  {RED('✗')} article {article_id}: {reason}")
        return None

    title = (article["title"] or "")[:60]
    pub = article["publication"] or "?"
    print(f"  {YELLOW('→')} article {article_id}  [{pub}]  {title}")

    edition_id = _create_bonus_edition_with_piece(db, article_id, today)
    logger.info("Created bonus edition #%d for article %d", edition_id, article_id)

    # Stage 8 — hook + contextualisation + show_notes for this edition.
    logger.info("Generating hook + context (Stage 8) for edition #%d…",
                edition_id)
    s8 = stage_8_hook_context.generate_for_edition(
        config, db, edition_id=edition_id,
    )
    logger.info("Stage 8 done — %d hooks, %d contexts, %d show_notes generated",
                s8.hooks_generated, s8.contexts_generated,
                s8.show_notes_generated)

    # Stage 9 — narrate.
    logger.info("Narrating audio (Stage 9) for edition #%d…", edition_id)
    s9 = stage_9_tts.generate_for_edition(
        config, db, edition_id=edition_id,
    )
    logger.info("Stage 9 done — %d narrated, %d errors",
                s9.audio_generated, s9.errors)

    # Mark article as in_edition so it doesn't appear in tomorrow's
    # daily selection. The user explicitly published it; double-up
    # would be jarring for listeners.
    with db.connect() as conn:
        conn.execute(
            "UPDATE articles SET status = 'in_edition' WHERE id = ?",
            (article_id,),
        )
    print(f"  {GREEN('✓')} article {article_id} → bonus edition #{edition_id}")
    return edition_id


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("article_ids", type=int, nargs="+",
                    help="One or more article IDs to publish as bonus "
                         "episodes. Each becomes its own standalone episode.")
    ap.add_argument("--force", action="store_true",
                    help="Publish even if the article is already in a past "
                         "edition or scored as FAIL. Use sparingly — will "
                         "overwrite existing audio.")
    args = ap.parse_args(argv)

    config = load_pipeline_config()
    db = Database(config.db_path)
    today = date.today()

    print()
    print(BOLD("═" * 70))
    print(BOLD(f"  Publishing {len(args.article_ids)} bonus episode(s)"))
    print(BOLD("═" * 70))
    print()

    edition_ids: list[int] = []
    for aid in args.article_ids:
        eid = publish_one(config, db, aid, today, force=args.force)
        if eid:
            edition_ids.append(eid)

    print()
    print(BOLD("─" * 70))
    print(f"Published {GREEN(str(len(edition_ids)))} of "
          f"{len(args.article_ids)} requested.")
    if edition_ids:
        print()
        print("Next:")
        print(f"  {YELLOW('python -m aarva.daily --stage 10')}   "
              f"# convert WAV→MP3, regen RSS, render HTML")
        print(f"  {YELLOW('bash scripts/publish.sh')}             "
              f"# push to gh-pages")
    return 0 if edition_ids else 1


if __name__ == "__main__":
    sys.exit(main())

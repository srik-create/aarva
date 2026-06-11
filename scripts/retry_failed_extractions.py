"""Retry article extraction for everything currently marked 'extraction_failed'.

After improving the article extractor (e.g., adding recall-mode fallback for
Aeon, or new site-specific selectors), run this to re-process the articles
that previously failed instead of waiting for new RSS entries.

Articles that now extract cleanly get bumped to status='ingested' so the
pipeline picks them up. Articles that still fail stay at 'extraction_failed'.

Usage (venv active):
    python scripts/retry_failed_extractions.py            # all publications
    python scripts/retry_failed_extractions.py Aeon       # one publication
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the project importable when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aarva.config import load_pipeline_config
from aarva.db import Database
from aarva.sources.article_extractor import extract_article


def main(filter_pub: str | None) -> int:
    config = load_pipeline_config()
    db = Database(config.db_path)

    with db.connect() as conn:
        conn.row_factory = __import__("sqlite3").Row
        q = """
            SELECT a.id, a.canonical_url, a.title, p.name AS pub
              FROM articles a
              JOIN publications p ON p.id = a.publication_id
             WHERE a.status = 'extraction_failed'
        """
        params: tuple = ()
        if filter_pub:
            q += " AND p.name = ?"
            params = (filter_pub,)
        rows = conn.execute(q, params).fetchall()

    print(f"Retrying {len(rows)} failed extractions"
          f"{f' for {filter_pub}' if filter_pub else ''}…")
    print()

    recovered = 0
    still_failing = 0

    for row in rows:
        url = row["canonical_url"]
        article_id = row["id"]
        title = (row["title"] or "")[:60]
        pub = row["pub"]

        result = extract_article(
            url,
            timeout=config.ingestion.http_timeout_seconds,
            user_agent=config.ingestion.user_agent,
        )

        if result and result.word_count > 0:
            with db.connect() as conn:
                conn.execute(
                    """
                    UPDATE articles
                       SET status = 'ingested',
                           full_text = ?,
                           excerpt = ?,
                           word_count = ?
                     WHERE id = ?
                    """,
                    (result.full_text, result.excerpt, result.word_count, article_id),
                )
                conn.commit()
            recovered += 1
            print(f"  ✓ [{pub}] {title}  ({result.word_count} words)")
        else:
            still_failing += 1
            print(f"  ✗ [{pub}] {title}")

    print()
    print(f"Recovered: {recovered}  |  Still failing: {still_failing}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))

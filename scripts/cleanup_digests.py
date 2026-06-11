"""Hard-delete existing digest / collection / stub articles from the DB.

Runs the Stage 2 regex detector against every article (regardless of
current status) and deletes the matches. By default prints what would
be deleted; use --apply to actually delete.

This is irreversible. Use --dry-run first, eyeball the list, then
run with --apply.

Usage:
    python scripts/cleanup_digests.py               # dry-run (default)
    python scripts/cleanup_digests.py --apply       # hard delete
    python scripts/cleanup_digests.py --apply --pub "Just Security"
                                                    # restrict to one pub
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aarva.cli_utils import BOLD, DIM, RED, GREEN, YELLOW
from aarva.config import load_pipeline_config
from aarva.db import Database
from aarva.stages.stage_2_filter import _is_digest_or_collection


def _find_matches(db: Database, pub_filter: str | None) -> list[dict]:
    where = []
    params: list = []
    if pub_filter:
        where.append("LOWER(p.name) LIKE ?")
        params.append(f"%{pub_filter.lower()}%")
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    with db.connect() as conn:
        rows = conn.execute(f"""
            SELECT a.id, a.title, a.byline, a.canonical_url, a.status,
                   p.name AS publication
              FROM articles a
              JOIN publications p ON p.id = a.publication_id
            {where_sql}
        """, params).fetchall()
    return [
        dict(r) for r in rows
        if _is_digest_or_collection(r["title"], r["byline"], r["publication"])
    ]


def _hard_delete(db: Database, article_ids: list[int]) -> dict:
    """Cascade-delete an article plus its scores, cluster memberships,
    edition_pieces, edition_rejections, user_actions, and crosscut
    candidate rows that reference it. Audio files on disk are NOT
    touched here (orphans are harmless; sweep separately if needed)."""
    counts: dict[str, int] = {}
    if not article_ids:
        return counts
    ph = ",".join("?" for _ in article_ids)
    with db.connect() as conn:
        # Order matters where FKs without CASCADE exist.
        for table, col in (
            ("article_clusters", "article_id"),
            ("article_scores", "article_id"),
            ("edition_rejections", "article_id"),
            ("user_actions", "article_id"),
            ("crosscut_pair_candidates", "article_a_id"),
            ("crosscut_pair_candidates", "article_b_id"),
            ("edition_pieces", "article_id"),
        ):
            try:
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE {col} IN ({ph})", article_ids,
                )
                counts[f"{table}.{col}"] = cur.rowcount
            except Exception as e:
                counts[f"{table}.{col}"] = f"ERR: {e}"
        cur = conn.execute(
            f"DELETE FROM articles WHERE id IN ({ph})", article_ids,
        )
        counts["articles"] = cur.rowcount
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--apply", action="store_true",
                    help="Actually delete. Without this, runs as a dry-run.")
    ap.add_argument("--pub", type=str, default=None,
                    help="Restrict to publications whose name contains this "
                         "substring (case-insensitive).")
    args = ap.parse_args(argv)

    config = load_pipeline_config()
    db = Database(config.db_path)
    matches = _find_matches(db, args.pub)

    print()
    print(BOLD(f"Found {len(matches)} digest/collection/stub article(s) "
               f"by title-pattern detection."))
    if not matches:
        print(DIM("Nothing to delete."))
        return 0

    by_pub = Counter(m["publication"] for m in matches)
    print()
    print("By publication:")
    for pub, n in by_pub.most_common():
        print(f"  {n:4d}  {pub}")
    print()

    print("Sample (up to 20):")
    for m in matches[:20]:
        status = m.get("status") or "?"
        print(f"  [{YELLOW(status):>20}]  id={m['id']:5d}  "
              f"[{m['publication']}]  {(m['title'] or '')[:75]}")
    if len(matches) > 20:
        print(f"  ... and {len(matches) - 20} more")
    print()

    if not args.apply:
        print(DIM("(dry-run; nothing deleted. Re-run with --apply to delete.)"))
        return 0

    # Confirm
    try:
        confirm = input(BOLD(
            f"Hard-delete {len(matches)} article(s) and all related rows? "
            f"[type YES to proceed]: "
        )).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return 1
    if confirm != "YES":
        print(DIM("Cancelled."))
        return 0

    counts = _hard_delete(db, [int(m["id"]) for m in matches])
    print()
    print(GREEN("Deleted:"))
    for k, v in counts.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

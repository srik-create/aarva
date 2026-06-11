"""Re-record older crosscut episodes with full article narration.

Background: earlier crosscuts (before the full-article switch) stored
LLM-extracted "key passages" (2–3 paragraphs each) in
edition_pieces.show_notes, and the TTS reads from show_notes. To bring
those episodes in line with the new full-article approach, this script:

  1. Loads every built crosscut edition (skipping today's, which is
     either fresh or being built by the running pipeline).
  2. For each, replaces show_notes on both pieces with the full
     article body from articles.full_text.
  3. NULLs out audio_url + duration_seconds so synthesize_crosscut_
     episode regenerates the WAV from scratch.
  4. Runs synthesize_crosscut_episode on the edition.

After this script completes, run:
    python -m aarva.daily --stage 10        # converts WAV → MP3, regen RSS, re-renders crosscut HTML
    bash scripts/publish.sh                 # push to gh-pages

Run from the project root:
    python scripts/rerecord_crosscut_audio.py
    python scripts/rerecord_crosscut_audio.py --include-today  # also re-record today's crosscut
    python scripts/rerecord_crosscut_audio.py --edition-id 13  # specific edition only
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

# Allow running as a script from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aarva.config import load_pipeline_config
from aarva.db import Database
from aarva.stages.stage_crosscut import synthesize_crosscut_episode

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("rerecord_crosscut")


def _list_targets(db: Database, include_today: bool, specific_id: int | None) -> list[dict]:
    today_iso = date.today().isoformat()
    with db.connect() as conn:
        if specific_id is not None:
            rows = conn.execute("""
                SELECT id, edition_date FROM editions
                 WHERE id = ? AND edition_type = 'crosscut'
            """, (specific_id,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT DISTINCT e.id, e.edition_date
                  FROM editions e
                  JOIN edition_pieces ep ON ep.edition_id = e.id
                 WHERE e.edition_type = 'crosscut'
                 ORDER BY e.edition_date DESC, e.id DESC
            """).fetchall()
    return [
        dict(r) for r in rows
        if include_today or str(r["edition_date"]) != today_iso
    ]


def _replace_show_notes_with_full_text(db: Database, edition_id: int) -> int:
    """For each piece in the crosscut, copy articles.full_text into
    edition_pieces.show_notes. Returns the number of rows updated."""
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT ep.article_id, a.full_text, a.title, LENGTH(a.full_text) AS len
              FROM edition_pieces ep
              JOIN articles a ON a.id = ep.article_id
             WHERE ep.edition_id = ?
             ORDER BY ep.position
        """, (edition_id,)).fetchall()
        n = 0
        for r in rows:
            if not r["full_text"]:
                logger.warning(
                    "Edition %d / article %d (%s): no full_text — skipping",
                    edition_id, r["article_id"], (r["title"] or "")[:50],
                )
                continue
            conn.execute("""
                UPDATE edition_pieces
                   SET show_notes = ?,
                       audio_url = NULL,
                       duration_seconds = NULL,
                       narrator_voice = NULL
                 WHERE edition_id = ? AND article_id = ?
            """, (r["full_text"], edition_id, r["article_id"]))
            logger.info(
                "  edition %d / article %d: show_notes ← full_text (%d chars; was excerpt)",
                edition_id, r["article_id"], r["len"] or 0,
            )
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--include-today", action="store_true",
                    help="Also re-record today's crosscut (default: skip).")
    ap.add_argument("--edition-id", type=int, default=None,
                    help="Re-record this specific crosscut edition only.")
    args = ap.parse_args()

    config = load_pipeline_config()
    db = Database(config.db_path)
    targets = _list_targets(db, args.include_today, args.edition_id)

    if not targets:
        print("No crosscut editions to re-record.")
        return 0

    print(f"Re-recording {len(targets)} crosscut edition(s):")
    for t in targets:
        print(f"  edition #{t['id']}  ({t['edition_date']})")
    print()

    for t in targets:
        eid = int(t["id"])
        logger.info("=" * 70)
        logger.info("Edition #%d (%s) — replacing show_notes with full text",
                    eid, t["edition_date"])
        n = _replace_show_notes_with_full_text(db, eid)
        if n == 0:
            logger.warning("Edition #%d: 0 pieces updated — skipping TTS", eid)
            continue
        logger.info("Edition #%d — running TTS (may take several minutes)", eid)
        stats = synthesize_crosscut_episode(config, db, edition_id=eid)
        logger.info("Edition #%d — done. %d sections synthesised, %d errors, output: %s",
                    eid, stats.sections_synthesized, stats.errors, stats.output_path)

    print()
    print("All targeted crosscut editions re-recorded.")
    print("Next:")
    print("  python -m aarva.daily --stage 10")
    print("  bash scripts/publish.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())

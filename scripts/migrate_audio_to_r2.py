"""One-off: upload every existing MP3 in edition_pieces to Cloudflare R2.

Used once when switching audio hosting from GitHub Pages to R2 — uploads
all historic episodes so the RSS feed's <enclosure> URLs (now pointing
at R2 per tts.r2.public_url_base) resolve for past as well as new
episodes.

Safe to re-run: idempotent via head_object check in R2 (files already
in the bucket are skipped). The first run uploads everything missing;
subsequent runs are near-instant.

Usage:
    # Preview what would be uploaded:
    python scripts/migrate_audio_to_r2.py --dry-run

    # Actually upload:
    python scripts/migrate_audio_to_r2.py

Pre-flight:
  - tts.r2.enabled: true in pipeline.yaml (with bucket + endpoint + URL)
  - AARVA_R2_ACCESS_KEY_ID + AARVA_R2_SECRET_ACCESS_KEY in env
  - boto3 installed (pip install -r requirements.txt)

This script is a thin wrapper over r2_uploader.upload_all_pending —
the same function Stage 10 runs on every publish. Running this script
manually before the next daily ensures the bulk upload doesn't happen
inside the publish window.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow running from project root (`python scripts/migrate_audio_to_r2.py`)
# without setting PYTHONPATH first. Matches the pattern in
# scripts/cleanup_digests.py, scripts/retag_jtbd.py, etc.
if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aarva.config import load_pipeline_config
from aarva.db import Database
from aarva.output.r2_uploader import (
    build_uploader_from_config, upload_all_pending,
)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Backfill historic MP3s to Cloudflare R2.",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be uploaded without actually uploading.",
    )
    ap.add_argument(
        "-v", "--verbose", action="store_true",
        help="Verbose logging (debug level).",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("aarva.migrate_audio_to_r2")

    config = load_pipeline_config()
    db = Database(config.db_path)

    uploader = build_uploader_from_config(config)
    if uploader is None:
        log.error(
            "R2 is not enabled in pipeline.yaml (tts.r2.enabled: true). "
            "Add the r2 block and re-run."
        )
        return 1
    log.info("R2 uploader ready — bucket=%s, endpoint=%s",
             uploader.bucket, uploader.endpoint_url)

    # Dry-run: walk edition_pieces, report what would happen, no uploads.
    if args.dry_run:
        with db.connect() as conn:
            rows = conn.execute("""
                SELECT DISTINCT audio_url
                  FROM edition_pieces
                 WHERE audio_url IS NOT NULL AND audio_url != ''
                   AND audio_url LIKE '%.mp3'
                 ORDER BY audio_url
            """).fetchall()

        aarva_root = config.audio_dir.parent.parent
        would_upload = 0
        already = 0
        missing = 0
        for row in rows:
            audio_url = row["audio_url"]
            local = aarva_root / audio_url
            if not local.exists():
                log.info("  SKIP (local source missing): %s", audio_url)
                missing += 1
                continue
            if uploader.key_exists(audio_url):
                log.debug("  SKIP (already in R2):       %s", audio_url)
                already += 1
                continue
            log.info("  WOULD UPLOAD:                   %s", audio_url)
            would_upload += 1
        log.info(
            "Dry-run summary: would upload %d, %d already in R2, %d source-missing",
            would_upload, already, missing,
        )
        return 0

    # Real run — delegate to the same function Stage 10 uses.
    stats = upload_all_pending(config, db, uploader=uploader)
    log.info(
        "Migration done — %d uploaded, %d already in bucket, "
        "%d source-missing, %d errors",
        stats.uploaded, stats.skipped_already_present,
        stats.skipped_source_missing, stats.errors,
    )
    return 0 if stats.errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

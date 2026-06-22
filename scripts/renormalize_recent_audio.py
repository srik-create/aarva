"""One-off: re-convert WAV → MP3 for recent episodes with the new
loudness-normalized ffmpeg pipeline, then re-upload to R2.

Use case: a previous batch of MP3s was published before the loudnorm
filter landed in audio_converter.py. Listeners hear per-chunk volume
variance on those episodes; we want to regenerate them at the new
target loudness (configured under output.loudness_* in pipeline.yaml).

Strategy:
  1. Look up every edition_piece with a .mp3 audio_url whose edition
     was published on/after the --since date.
  2. For each: locate the .wav source (same directory, .wav extension),
     re-run _convert_wav_to_mp3() with the current config's loudnorm
     params, overwriting the local .mp3.
  3. Re-upload each .mp3 to R2 (put_object overwrites the existing
     object).
  4. The next publish.sh will rsync the new local .mp3s into gh-pages
     too (just in case any listener fetches directly from there).

Safe to re-run — idempotent in spirit (overwrites with same params
produce the same output).

The DB audio_url doesn't change — only the file content does. RSS
feed URLs are unaffected; podcast apps will simply serve newer
content under the same URL on next request.

Usage:
    python scripts/renormalize_recent_audio.py --dry-run --since 2026-06-17
    python scripts/renormalize_recent_audio.py            --since 2026-06-17

Defaults: --since 2026-06-17 (the date the user requested as the
floor; before that, leave the catalog alone).
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

# Allow running from project root.
if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aarva.config import load_pipeline_config
from aarva.db import Database
from aarva.output.audio_converter import _convert_wav_to_mp3
from aarva.output.r2_uploader import (
    build_uploader_from_config, _content_type_for,
)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Re-convert and re-upload MP3s for recent episodes "
                    "with the current loudnorm settings.",
    )
    ap.add_argument(
        "--since", type=str, default="2026-06-17",
        help="Only re-process episodes published on/after this date "
             "(YYYY-MM-DD). Default: 2026-06-17.",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be re-converted without actually doing it.",
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
    log = logging.getLogger("aarva.renormalize_recent_audio")

    try:
        since_date = date.fromisoformat(args.since)
    except ValueError:
        log.error("--since must be YYYY-MM-DD (got %r)", args.since)
        return 1

    config = load_pipeline_config()
    db = Database(config.db_path)

    # Read the same loudnorm config the regular converter uses.
    output_cfg = config.raw.get("output", {}) or {}
    target_lufs = float(output_cfg.get("loudness_target_lufs", -16.0))
    true_peak = float(output_cfg.get("loudness_true_peak_dbfs", -1.5))
    lra = float(output_cfg.get("loudness_range_lu", 11.0))
    log.info(
        "Loudnorm params (from pipeline.yaml): I=%s LUFS, TP=%s dBFS, "
        "LRA=%s LU", target_lufs, true_peak, lra,
    )

    uploader = build_uploader_from_config(config)
    if uploader is None:
        log.warning(
            "R2 is disabled in pipeline.yaml — local files will be "
            "re-converted but NOT re-uploaded. Enable tts.r2.enabled "
            "and re-run if you want to push new MP3s to R2."
        )
    else:
        log.info(
            "R2 uploader ready — bucket=%s, endpoint=%s",
            uploader.bucket, uploader.endpoint_url,
        )

    # Find every DISTINCT .mp3 audio_url whose edition is dated on/after
    # the floor. JOIN through editions for edition_date.
    aarva_root = config.audio_dir.parent.parent
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT DISTINCT ep.audio_url, e.edition_date, e.edition_type
              FROM edition_pieces ep
              JOIN editions e ON e.id = ep.edition_id
             WHERE ep.audio_url IS NOT NULL AND ep.audio_url != ''
               AND ep.audio_url LIKE '%.mp3'
               AND e.edition_date >= ?
             ORDER BY e.edition_date, ep.audio_url
        """, (since_date.isoformat(),)).fetchall()

    if not rows:
        log.info("No MP3s to re-process on/after %s.", since_date)
        return 0

    log.info(
        "Found %d MP3s to re-process on/after %s.", len(rows), since_date,
    )

    converted = 0
    uploaded = 0
    wav_missing = 0
    errors = 0

    for row in rows:
        audio_url = row["audio_url"]
        mp3_path = aarva_root / audio_url
        wav_path = mp3_path.with_suffix(".wav")

        if not wav_path.exists():
            log.warning(
                "  [%s %s] WAV source missing: %s — cannot re-convert",
                row["edition_date"], row["edition_type"], wav_path,
            )
            wav_missing += 1
            continue

        if args.dry_run:
            log.info(
                "  [%s %s] WOULD RE-CONVERT: %s  (wav=%dKB)",
                row["edition_date"], row["edition_type"], audio_url,
                wav_path.stat().st_size // 1024,
            )
            continue

        # Re-convert WAV → MP3 with current loudnorm params (overwrites
        # the existing .mp3).
        try:
            _convert_wav_to_mp3(
                wav_path, mp3_path,
                loudness_target_lufs=target_lufs,
                loudness_true_peak_dbfs=true_peak,
                loudness_range_lu=lra,
            )
        except Exception as e:
            log.warning(
                "  [%s %s] re-conversion failed: %s — %s",
                row["edition_date"], row["edition_type"], audio_url, e,
            )
            errors += 1
            continue

        new_size_kb = mp3_path.stat().st_size // 1024
        log.info(
            "  [%s %s] re-converted: %s  (mp3=%dKB)",
            row["edition_date"], row["edition_type"], audio_url, new_size_kb,
        )
        converted += 1

        # Re-upload to R2 (put_object overwrites; key_exists is bypassed
        # because we explicitly want the new content up there).
        if uploader is not None:
            try:
                uploader.upload_file(
                    mp3_path, audio_url,
                    content_type=_content_type_for(audio_url),
                )
                log.info(
                    "  [%s %s] re-uploaded: %s",
                    row["edition_date"], row["edition_type"],
                    uploader.public_url_for(audio_url),
                )
                uploaded += 1
            except Exception as e:
                log.warning(
                    "  [%s %s] R2 re-upload failed: %s — %s",
                    row["edition_date"], row["edition_type"], audio_url, e,
                )
                errors += 1

    if args.dry_run:
        would = sum(1 for r in rows
                    if (aarva_root / r["audio_url"]).with_suffix(".wav").exists())
        log.info(
            "Dry-run summary: would re-process %d MP3s "
            "(%d WAV sources missing, will skip)",
            would, len(rows) - would,
        )
    else:
        log.info(
            "Done — re-converted %d, re-uploaded %d, %d WAV-missing, %d errors",
            converted, uploaded, wav_missing, errors,
        )
        log.info(
            "Next step: bash scripts/publish.sh  "
            "(rsyncs new local MP3s into gh-pages too, so direct GH "
            "Pages listeners get the new audio).",
        )
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

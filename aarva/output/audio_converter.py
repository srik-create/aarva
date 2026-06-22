"""WAV → MP3 conversion for publishing.

Podcast apps (especially Apple Podcasts) won't accept WAV enclosures in
RSS. We convert the WAVs to MP3 at publish time, leaving the WAVs in place
as archival originals while serving the MP3s in the feed.

Uses ffmpeg via subprocess. ffmpeg is a dependency users install themselves
(`brew install ffmpeg` on macOS).

Default encoding: 64 kbps mono — high enough for clear narration, low
enough that a 15-minute piece is only ~7MB instead of ~80MB.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from aarva.config import PipelineConfig
from aarva.db import Database

logger = logging.getLogger(__name__)


@dataclass
class ConversionStats:
    converted: int = 0
    skipped_already_done: int = 0
    skipped_source_missing: int = 0
    errors: int = 0


def _have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _convert_wav_to_mp3(
    wav_path: Path,
    mp3_path: Path,
    bitrate: str = "64k",
    loudness_target_lufs: float = -16.0,
    loudness_true_peak_dbfs: float = -1.5,
    loudness_range_lu: float = 11.0,
) -> None:
    """Run ffmpeg to convert a WAV to MP3 + loudness-normalize.

    Loudness normalization via ffmpeg's loudnorm filter brings every
    output MP3 to a consistent perceptual loudness (target: -16 LUFS,
    the typical podcast streaming standard for Apple Podcasts and
    Spotify). Without this, per-chunk variance in Gemini TTS output
    levels leaks into the final MP3 as audible volume jumps between
    chunks of the same article. With it, all chunks of all articles
    are at the same perceived loudness ±~0.5 dB, regardless of what
    levels the model originally produced.

    loudnorm parameters:
      I  = target integrated loudness (LUFS). Default -16 = podcast
           streaming convention. EBU broadcast = -23. Spotify (current)
           = -14. Apple Podcasts = -16.
      TP = true peak ceiling (dBFS). -1.5 stays under platform limits
           (Apple recommends -1, Spotify recommends -2) and prevents
           inter-sample peaks that could clip on aggressive decoders.
      LRA = loudness range (LU). 11 is conservative for spoken word
           (broadcast standard is 7-9; podcasts often 11-13). Higher
           = more allowed dynamic range; lower = more uniform.

    Single-pass (no measurement run); ~95% accurate, which is fine for
    spoken-word podcasts. Two-pass (measure-then-apply) is more precise
    but doubles ffmpeg wall-clock per file — not worth it at Aarva's
    scale.
    """
    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    loudnorm_filter = (
        f"loudnorm=I={loudness_target_lufs}"
        f":TP={loudness_true_peak_dbfs}"
        f":LRA={loudness_range_lu}"
    )
    cmd = [
        "ffmpeg",
        "-y",                # overwrite if exists
        "-loglevel", "error",
        "-i", str(wav_path),
        "-af", loudnorm_filter,
        "-codec:a", "libmp3lame",
        "-b:a", bitrate,
        "-ac", "1",          # mono (TTS output is mono; saves space)
        str(mp3_path),
    ]
    try:
        subprocess.run(
            cmd, check=True, capture_output=True, text=True, timeout=300,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"ffmpeg failed for {wav_path}: {(e.stderr or '')[:300]}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ffmpeg timed out for {wav_path}") from e


def convert_all_for_publish(
    config: PipelineConfig,
    db: Database,
    bitrate: str = "64k",
) -> ConversionStats:
    """Ensure every edition piece with audio has an .mp3 alongside its .wav.

    Updates edition_pieces.audio_url to point at the .mp3 path. The .wav
    file is preserved as an archival original.

    Loudness normalization parameters are read from pipeline.yaml's
    output: block (loudness_target_lufs, loudness_true_peak_dbfs,
    loudness_range_lu). Defaults match the function defaults if absent
    from config.
    """
    stats = ConversionStats()

    if not _have_ffmpeg():
        raise RuntimeError(
            "ffmpeg not found on PATH. Install with: brew install ffmpeg"
        )

    output_cfg = config.raw.get("output", {}) or {}
    target_lufs = float(output_cfg.get("loudness_target_lufs", -16.0))
    true_peak = float(output_cfg.get("loudness_true_peak_dbfs", -1.5))
    lra = float(output_cfg.get("loudness_range_lu", 11.0))

    project_root = config.web_dir.parent.parent

    with db.connect() as conn:
        rows = conn.execute("""
            SELECT edition_id, article_id, audio_url
              FROM edition_pieces
             WHERE audio_url IS NOT NULL AND audio_url != ''
        """).fetchall()

    # Dedupe: crosscut episodes have two edition_pieces rows that share
    # the same audio_url. Convert each WAV once; collect (old, new)
    # URL pairs and apply them all in a single transaction at the end.
    seen_wav_urls: set[str] = set()
    url_updates: list[tuple[str, str]] = []   # (new_url, old_url)

    for row in rows:
        audio_url = row["audio_url"]
        if audio_url.endswith(".mp3"):
            stats.skipped_already_done += 1
            continue
        if audio_url in seen_wav_urls:
            # Already in the update batch (crosscut case) — the single
            # UPDATE later will rewrite this row's audio_url too.
            stats.skipped_already_done += 1
            continue

        wav_path = project_root / audio_url
        if not wav_path.exists():
            logger.warning("Audio source missing for edition %d / article %d: %s",
                           row["edition_id"], row["article_id"], wav_path)
            stats.skipped_source_missing += 1
            continue

        mp3_path = wav_path.with_suffix(".mp3")
        new_url = str(mp3_path.relative_to(project_root))

        try:
            _convert_wav_to_mp3(
                wav_path, mp3_path,
                bitrate=bitrate,
                loudness_target_lufs=target_lufs,
                loudness_true_peak_dbfs=true_peak,
                loudness_range_lu=lra,
            )
        except Exception as e:
            logger.warning("Conversion failed for %s: %s", wav_path, e)
            stats.errors += 1
            continue

        # Only mark this WAV "seen" AFTER successful conversion. If
        # ffmpeg failed we want the next iteration to retry rather than
        # silently rewriting peer rows to a non-existent .mp3.
        seen_wav_urls.add(audio_url)
        url_updates.append((new_url, audio_url))
        stats.converted += 1
        mp3_size_kb = mp3_path.stat().st_size // 1024
        wav_size_kb = wav_path.stat().st_size // 1024
        logger.info(
            "Converted edition %d / article %d: %s (%dKB → %dKB, %.0f%% smaller)",
            row["edition_id"], row["article_id"],
            mp3_path.name, wav_size_kb, mp3_size_kb,
            100 * (1 - mp3_size_kb / max(wav_size_kb, 1)),
        )

    # One transaction for all URL rewrites — one connection acquired,
    # one commit, instead of N connections / N commits.
    if url_updates:
        with db.connect() as conn:
            conn.executemany(
                "UPDATE edition_pieces SET audio_url = ? WHERE audio_url = ?",
                url_updates,
            )

    return stats

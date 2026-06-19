"""Cloudflare R2 audio uploader.

Aarva publishes audio MP3s to Cloudflare R2 (S3-compatible object store)
so the RSS feed's <enclosure> URLs point at R2 rather than at the
GitHub Pages site that hosts the HTML + RSS itself. Why:

  - GitHub Pages has a 1 GB published-site soft cap. Aarva ships ~65 MB
    of audio per day, so the cap would bite in ~15 days. R2 has 10 GB
    free (Aarva ≈ 5 months) and $0.015/GB/month after — pennies even
    a year out.
  - R2 has zero egress fees. Apple Podcasts / Spotify / YouTube can pull
    audio at any volume without ever costing us bandwidth.
  - Keeps the HTML + RSS feed itself on GitHub Pages (small footprint
    that fits easily inside the GH Pages cap).

Wire-up:
  - Stage 10 (publish) calls `upload_all_pending(config, db)` after the
    audio_converter has finished WAV → MP3 conversion. The uploader
    walks every edition_piece with a non-empty audio_url, checks
    whether that key already exists in R2, and uploads if not.
    Idempotent — re-runnable without re-uploading.
  - The RSS feed reads `audio_url_base` from `config.output` (a new
    field added alongside `public_url_base`). When set, <enclosure>
    URLs use this base instead of public_url_base — pointing listeners
    at R2 for audio while HTML pages still link to GitHub Pages.

Config (pipeline.yaml):

    tts:
      r2:
        enabled: true
        bucket: aarva-audio
        endpoint_url: https://<account_id>.r2.cloudflarestorage.com
        public_url_base: https://pub-<hash>.r2.dev
        # Future: switch public_url_base to https://audio.aarva.app
        # once the custom domain is wired up in Cloudflare DNS.

Credentials (env vars, never YAML):
    AARVA_R2_ACCESS_KEY_ID
    AARVA_R2_SECRET_ACCESS_KEY

The R2 key is the audio_url verbatim (e.g. `output/audio/2026-06-17/
article_1337.mp3`). Slightly redundant ('output/' prefix), but means
the same path identifies the file locally, in the DB, and in R2 —
no mapping table or per-call path manipulation needed.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from aarva.config import PipelineConfig
from aarva.db import Database

logger = logging.getLogger(__name__)


@dataclass
class UploadStats:
    uploaded: int = 0
    skipped_already_present: int = 0
    skipped_source_missing: int = 0
    errors: int = 0


class R2Uploader:
    """Thin wrapper around boto3 for Cloudflare R2.

    Construct once per publish run; reuses a single S3 client + signature
    cache across all uploads. Lazy-imports boto3 so the rest of Aarva
    doesn't pay the import cost when R2 isn't in use.
    """

    def __init__(
        self,
        endpoint_url: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        public_url_base: str,
    ):
        self.endpoint_url = endpoint_url.rstrip("/")
        self.bucket = bucket
        self.public_url_base = public_url_base.rstrip("/")
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._client = None

    def _load(self):
        if self._client is not None:
            return
        try:
            import boto3
            from botocore.config import Config
        except ImportError as e:
            raise RuntimeError(
                "R2Uploader requires boto3. Install with: pip install boto3"
            ) from e
        # auto region works for R2 (S3-compatible); SignatureVersion v4
        # is required.
        self._client = boto3.client(
            service_name="s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self._access_key_id,
            aws_secret_access_key=self._secret_access_key,
            region_name="auto",
            config=Config(signature_version="s3v4"),
        )
        logger.info(
            "R2Uploader ready — endpoint=%s, bucket=%s",
            self.endpoint_url, self.bucket,
        )

    def key_exists(self, key: str) -> bool:
        """True iff a file with this key is already in the bucket.

        Uses HEAD request via head_object — cheap, doesn't transfer body.
        Any non-404 error is treated as "unknown" (returns False) and
        the upload path retries; this is safer than returning True and
        skipping a needed upload.
        """
        self._load()
        from botocore.exceptions import ClientError
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            logger.warning(
                "R2 head_object unexpected error for key=%s: %s", key, e,
            )
            return False

    def upload_file(self, local_path: Path, key: str,
                    content_type: str = "audio/mpeg") -> None:
        """Upload local file → R2 at the given key. Overwrites if exists.

        Use `key_exists()` upstream if you want idempotency. This call
        itself does NOT check first — callers wanting to avoid the
        re-upload should pre-check.
        """
        self._load()
        with open(local_path, "rb") as f:
            self._client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=f,
                ContentType=content_type,
            )

    def public_url_for(self, key: str) -> str:
        """Build the listener-facing URL for the given R2 key."""
        return f"{self.public_url_base}/{key.lstrip('/')}"


# ─────────────────────────────────────────────────────────────────────────
# Factory + Stage-10 driver
# ─────────────────────────────────────────────────────────────────────────

def _content_type_for(path: str) -> str:
    """MIME for the audio file extension. Defaults to audio/mpeg if
    extension unrecognised."""
    p = path.lower()
    if p.endswith(".mp3"):
        return "audio/mpeg"
    if p.endswith(".wav"):
        return "audio/wav"
    if p.endswith(".m4a"):
        return "audio/mp4"
    return "audio/mpeg"


def build_uploader_from_config(config: PipelineConfig) -> Optional[R2Uploader]:
    """Return an R2Uploader built from `config.tts.r2`, or None if R2 is
    disabled / not configured. Raises ConfigError if R2 is enabled but
    the credentials env vars are missing — better than silently no-op."""
    from aarva.exceptions import ConfigError

    tts_cfg = config.raw.get("tts", {}) or {}
    r2_cfg = tts_cfg.get("r2", {}) or {}
    if not r2_cfg.get("enabled"):
        return None

    bucket = r2_cfg.get("bucket")
    endpoint_url = r2_cfg.get("endpoint_url")
    public_url_base = r2_cfg.get("public_url_base")
    if not (bucket and endpoint_url and public_url_base):
        raise ConfigError(
            "tts.r2.enabled is true but tts.r2.bucket, "
            "tts.r2.endpoint_url, and/or tts.r2.public_url_base are not "
            "all set in pipeline.yaml."
        )

    access_key_id = os.environ.get("AARVA_R2_ACCESS_KEY_ID")
    secret_access_key = os.environ.get("AARVA_R2_SECRET_ACCESS_KEY")
    if not (access_key_id and secret_access_key):
        raise ConfigError(
            "tts.r2.enabled is true but AARVA_R2_ACCESS_KEY_ID and/or "
            "AARVA_R2_SECRET_ACCESS_KEY env vars are not set. Add them "
            "to your shell (or to ~/.zshrc and re-source it). The "
            "values come from the R2 API token you created in the "
            "Cloudflare dashboard."
        )

    return R2Uploader(
        endpoint_url=endpoint_url,
        bucket=bucket,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        public_url_base=public_url_base,
    )


def upload_all_pending(
    config: PipelineConfig,
    db: Database,
    uploader: Optional[R2Uploader] = None,
) -> UploadStats:
    """Iterate every edition_piece with a non-empty audio_url and ensure
    its MP3 is present in R2.

    Designed to run after `audio_converter.convert_all_for_publish` so
    `audio_url` already points at the .mp3 (not the .wav archival
    original). Idempotent: files already in the bucket are skipped via
    a HEAD check, so subsequent runs are cheap.

    If R2 is disabled in config, returns a zero-stats result and no-ops.
    """
    stats = UploadStats()

    if uploader is None:
        uploader = build_uploader_from_config(config)
    if uploader is None:
        return stats   # R2 disabled; silently no-op

    # `audio_url` in DB is relative to the aarva/ package directory
    # (e.g. 'output/audio/2026-06-17/article_1337.mp3'). The local file
    # lives at `<aarva/>/<audio_url>` — derive that from the audio_dir
    # config (`<aarva/>/output/audio/`) by going up two levels.
    aarva_root = config.audio_dir.parent.parent

    with db.connect() as conn:
        rows = conn.execute("""
            SELECT DISTINCT audio_url
              FROM edition_pieces
             WHERE audio_url IS NOT NULL AND audio_url != ''
        """).fetchall()

    for row in rows:
        audio_url = row["audio_url"]
        if not audio_url.endswith(".mp3"):
            # Skip non-MP3 (we only publish MP3 enclosures in the RSS).
            # WAVs that haven't been converted yet are caller's problem.
            continue

        key = audio_url   # see module docstring — key == audio_url verbatim
        local_path = aarva_root / audio_url

        if not local_path.exists():
            logger.warning(
                "R2 upload — source missing: %s (expected at %s)",
                audio_url, local_path,
            )
            stats.skipped_source_missing += 1
            continue

        if uploader.key_exists(key):
            stats.skipped_already_present += 1
            continue

        try:
            uploader.upload_file(
                local_path, key,
                content_type=_content_type_for(audio_url),
            )
        except Exception as e:
            logger.warning("R2 upload failed for %s: %s", key, e)
            stats.errors += 1
            continue

        size_kb = local_path.stat().st_size // 1024
        logger.info(
            "R2 uploaded: %s (%dKB) → %s",
            key, size_kb, uploader.public_url_for(key),
        )
        stats.uploaded += 1

    return stats

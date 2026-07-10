"""Back up the listener DB to R2 after every on-demand build.

The listener DB (see aarva/listener_db.py) has no backup mechanism at
all today — unlike the main DB, nothing ever copies it anywhere. Two
separate bugs have already wiped listener episodes this way
(2026-07-03 sync overwrite, 2026-07-06 -> 2026-07-11 ephemeral disk).
Both are fixed now, but neither fix protects against a *future*
mistake, a Render disk loss, or an outage. This gives every build a
redundant copy independent of Render's disk entirely.

Called from aarva/services/episode_worker.py right after a build
completes. One snapshot per calendar day (dated key) — multiple builds
on the same day overwrite that day's snapshot, so a bad build can't
silently corrupt the only backup from an earlier good build that same
day, while still bounding storage growth.

To restore from a backup (manual — there's no restore endpoint, this
is a last-resort safety net, not a live failover path):
    1. Download the gzipped object from R2
       (_data/aarva-listener-db-backups/aarva-listener-<date>.gz)
    2. gunzip it
    3. Write the result to AARVA_LISTENER_DB_PATH on the server
       (e.g. via a temporary admin endpoint, or direct disk access)
"""
from __future__ import annotations

import gzip
import logging
from datetime import date
from pathlib import Path

from aarva.config import PipelineConfig
from aarva.output.r2_uploader import build_uploader_from_config

logger = logging.getLogger(__name__)

_BACKUP_KEY_PREFIX = "_data/aarva-listener-db-backups"


def backup_listener_db_to_r2(config: PipelineConfig, listener_db_path: Path) -> bool:
    """Gzip + upload the current listener DB file to R2, dated by day.

    Non-fatal: any failure is logged and swallowed, never raised — a
    listener whose build just finished shouldn't see an error because
    of an unrelated backup step. Returns True on success, False
    otherwise (R2 disabled, file missing, upload failed, etc).
    """
    try:
        uploader = build_uploader_from_config(config)
        if uploader is None:
            return False  # R2 disabled — nothing to do

        if not listener_db_path.exists():
            logger.warning(
                "listener_db_backup: %s doesn't exist yet, skipping",
                listener_db_path,
            )
            return False

        raw = listener_db_path.read_bytes()
        compressed = gzip.compress(raw)
        key = f"{_BACKUP_KEY_PREFIX}/aarva-listener-{date.today().isoformat()}.gz"

        uploader._load()
        uploader._client.put_object(
            Bucket=uploader.bucket, Key=key, Body=compressed,
            ContentType="application/gzip",
        )
        logger.info(
            "listener_db_backup: backed up %d bytes (%d gzipped) to %s",
            len(raw), len(compressed), key,
        )
        return True
    except Exception as e:
        logger.warning("listener_db_backup: failed (non-fatal): %s", e)
        return False

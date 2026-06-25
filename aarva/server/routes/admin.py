"""Authenticated admin endpoints.

Today: just the daily-DB-sync receiver. The endpoint expects a gzipped
SQLite snapshot in the request body (produced by `sqlite3 .backup` on
the laptop, then gzipped), authenticates via a bearer token, validates
the payload, and atomic-replaces the DB on the persistent disk.

Tomorrow: any other operator-only hooks (force-deploy, cache flush,
prometheus-style metrics scrape) live here too.

Security model — kept intentionally minimal:
  - Bearer token via `Authorization: Bearer <token>` matched against
    AARVA_RENDER_SYNC_TOKEN (set on Render dashboard).
  - HTTPS-only (enforced by Render + Cloudflare in production).
  - 401 on missing or mismatched token; 403 on bad payload.
  - No CORS (no browser ever calls this).

Why not a separate admin service:
  - The DB to swap lives on this service's disk.
  - One Render instance, one place to update.
  - Re-evaluate if/when we grow to multiple instances.
"""
from __future__ import annotations

import gzip
import logging
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from aarva.server.app import app

logger = logging.getLogger(__name__)


# Hard cap on the request body we'll accept. Sane upper bound to keep a
# misconfigured or malicious caller from filling the disk. The real DB
# tarball is currently ~30 MB; 200 MB gives years of headroom.
_MAX_BODY_BYTES = 200 * 1024 * 1024


def _check_token(request: Request) -> None:
    """Validate the Authorization header against the configured token.

    Raises HTTPException(401) if missing / mismatched. Logged failures
    don't include the bearer value (avoid leaking it via logs)."""
    expected = os.environ.get("AARVA_RENDER_SYNC_TOKEN", "")
    if not expected:
        # Endpoint disabled by default if the env var isn't set — much
        # safer than accepting any caller while the operator's still
        # provisioning the secret.
        logger.warning("/admin/sync-db hit but AARVA_RENDER_SYNC_TOKEN unset")
        raise HTTPException(status_code=503, detail="sync endpoint not configured")

    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    provided = header[len("Bearer "):].strip()
    # Constant-time compare so a token-substring guessing attack can't
    # learn the prefix by timing the request.
    import hmac
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid bearer token")


@app.post("/admin/sync-db")
async def admin_sync_db(request: Request) -> JSONResponse:
    """Receive a gzipped SQLite snapshot and atomic-replace /data/aarva.db.

    Request:
      POST /admin/sync-db
      Authorization: Bearer <AARVA_RENDER_SYNC_TOKEN>
      Content-Type: application/gzip
      Body: gunzip → valid SQLite file (produced by `sqlite3 .backup`)

    Response (200):
      {"status": "ok", "articles": <int>, "bytes": <int>}

    Errors:
      401  invalid / missing bearer token
      403  payload isn't a usable SQLite DB, or article count is suspicious
      413  body exceeds the size cap
      503  AARVA_RENDER_SYNC_TOKEN not set on this instance
    """
    _check_token(request)

    # Read body. FastAPI's request.body() loads it all into memory —
    # acceptable at our ~30 MB scale; revisit with streaming if we
    # grow beyond ~100 MB tarballs.
    body = await request.body()
    if len(body) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    if len(body) < 1024:
        # An empty/tiny payload is almost certainly a misconfigured call.
        raise HTTPException(status_code=403, detail="payload too small")

    # Decompress to a staging path next to the live DB so the eventual
    # atomic mv stays on the same filesystem.
    db_path = Path(os.environ.get("AARVA_DB_PATH", "/data/aarva.db"))
    db_dir = db_path.parent
    db_dir.mkdir(parents=True, exist_ok=True)

    # Use a NamedTemporaryFile in the same dir so os.replace() is atomic.
    fd, staging_str = tempfile.mkstemp(
        prefix="aarva-db-staging-", suffix=".db", dir=str(db_dir),
    )
    os.close(fd)
    staging = Path(staging_str)

    try:
        try:
            decompressed = gzip.decompress(body)
        except (OSError, EOFError) as e:
            raise HTTPException(status_code=403, detail=f"gunzip failed: {e}")

        staging.write_bytes(decompressed)

        # Sanity-check the staged DB: it has to (a) open, (b) have an
        # `articles` table, (c) report a plausible article count. If any
        # of these fail the live DB stays untouched.
        try:
            with sqlite3.connect(str(staging)) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM articles"
                ).fetchone()
            article_count = int(row[0])
        except sqlite3.DatabaseError as e:
            raise HTTPException(status_code=403, detail=f"not a SQLite DB: {e}")
        except sqlite3.OperationalError as e:
            raise HTTPException(
                status_code=403,
                detail=f"staged DB missing expected schema: {e}",
            )

        if article_count <= 0:
            # Receiving an empty DB almost certainly means something
            # broke on the laptop side; don't overwrite live with empty.
            raise HTTPException(
                status_code=403,
                detail="staged DB has 0 articles — refusing to swap",
            )

        # Atomic rename. On the same filesystem this is one syscall —
        # any new sqlite3.connect() after this point sees the new file;
        # any in-flight reads finish against the (still-open) old inode.
        # Our request pattern opens + closes connections per query, so
        # the "in-flight read" window is sub-millisecond.
        os.replace(str(staging), str(db_path))
        logger.info(
            "DB sync ok — %d articles, %d bytes (gzipped), %d bytes (raw)",
            article_count, len(body), len(decompressed),
        )

        return JSONResponse({
            "status": "ok",
            "articles": article_count,
            "bytes": len(body),
        })
    except HTTPException:
        # Clean up staging file before re-raising so we don't leave
        # half-uploaded turds on the persistent disk.
        if staging.exists():
            staging.unlink(missing_ok=True)
        raise
    except Exception as e:
        logger.exception("DB sync failed unexpectedly: %s", e)
        if staging.exists():
            staging.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"sync failed: {e}") from e

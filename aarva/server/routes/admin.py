"""Authenticated admin endpoints.

Today: the daily-DB-sync receiver, and a lost-listener-episode
diagnostic. The sync endpoint expects a small JSON trigger pointing at
a gzipped SQLite snapshot the laptop has already uploaded to R2; the
server fetches from R2 over the fast backbone (vs. the laptop
uploading the body directly over a residential link, which can blow
past Render's 100-second request timeout on the Starter plan). Every
sync automatically checks for listener episodes about to lose their
last recovery chance (see _find_lost_episodes) before overwriting the
jobs table that holds it.

Tomorrow: any other operator-only hooks (force-deploy, cache flush,
metrics scrape) live here too.

Security model — kept intentionally minimal:
  - Bearer token via `Authorization: Bearer <token>` matched against
    AARVA_RENDER_SYNC_TOKEN (set on Render dashboard).
  - HTTPS-only (enforced by Render + Cloudflare in production).
  - 401 on missing or mismatched token.
  - R2 credentials (AARVA_R2_ACCESS_KEY_ID / _SECRET_ACCESS_KEY) live
    in Render env vars, never in source.
  - Refuses to swap if the staged DB has zero articles — a broken
    laptop run can't wipe live data.
"""
from __future__ import annotations

import gzip
import hmac
import json
import logging
import os
import sqlite3
import tempfile
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from aarva.server.app import app

logger = logging.getLogger(__name__)


# Hard cap on the R2 object we'll fetch. Sane upper bound to keep a
# misconfigured caller from wedging us on a multi-gig download. The
# real DB tarball is currently ~30 MB; 200 MB gives years of headroom.
_MAX_PAYLOAD_BYTES = 200 * 1024 * 1024


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
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid bearer token")


def _build_r2_client(pipeline_cfg):
    """Return a boto3 S3 client configured for our R2 bucket, or raise
    HTTPException with the operator-facing reason if anything is missing.

    R2 endpoint + bucket come from pipeline.yaml (already in the
    container); credentials come from env vars (set in Render's
    dashboard, never in source)."""
    r2_cfg = (pipeline_cfg.raw.get("tts", {}) or {}).get("r2", {}) or {}
    bucket = r2_cfg.get("bucket")
    endpoint_url = r2_cfg.get("endpoint_url")
    if not (bucket and endpoint_url):
        raise HTTPException(
            status_code=503,
            detail="R2 not configured in pipeline.yaml",
        )

    access_key = os.environ.get("AARVA_R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("AARVA_R2_SECRET_ACCESS_KEY")
    if not (access_key and secret_key):
        raise HTTPException(
            status_code=503,
            detail="AARVA_R2_ACCESS_KEY_ID / AARVA_R2_SECRET_ACCESS_KEY unset",
        )

    try:
        import boto3
        from botocore.config import Config
    except ImportError as e:
        raise HTTPException(
            status_code=500,
            detail=f"boto3 not available: {e}",
        )

    client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )
    return client, bucket


def _find_lost_episodes(db, listener_db) -> list[dict]:
    """Find completed build_crosscut jobs whose stamped edition doesn't
    exist in either DB — evidence of an episode whose editions/
    edition_pieces rows are gone but whose job record (in the main DB)
    survived. See admin_diagnose_lost_episodes for the full story.

    Shared by that endpoint and admin_sync_db, which runs this FIRST,
    before touching the live DB — a sync overwrites the main DB's
    `jobs` table with the laptop's version, which has no idea about
    builds that happened directly on Render, so this evidence only
    exists in the brief window before each sync."""
    with db.connect() as conn:
        job_rows = conn.execute("""
            SELECT id, payload_json, result_json, created_at, finished_at
              FROM jobs
             WHERE kind = 'build_crosscut' AND status = 'completed'
        """).fetchall()

    lost: list[dict] = []
    for row in job_rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
            result = json.loads(row["result_json"] or "{}")
        except (TypeError, ValueError):
            continue
        edition_id = result.get("edition_id")
        if edition_id is None:
            continue

        with db.connect() as conn:
            exists_main = conn.execute(
                "SELECT 1 FROM editions WHERE id = ?", (edition_id,)
            ).fetchone() is not None
        with listener_db.connect() as lconn:
            exists_listener = lconn.execute(
                "SELECT 1 FROM editions WHERE id = ?", (edition_id,)
            ).fetchone() is not None
        if exists_main or exists_listener:
            continue  # this one's fine — not lost

        entry: dict = {
            "job_id": row["id"],
            "edition_id": edition_id,
            "created_at": row["created_at"],
            "finished_at": row["finished_at"],
            "topic_label": payload.get("topic_label"),
            "why": payload.get("why"),
            "prompt": payload.get("prompt"),
            "requester_email": payload.get("requester_email"),
        }
        for payload_key, out_key in (
            ("article_a_id", "article_a"), ("article_b_id", "article_b"),
        ):
            article_id = payload.get(payload_key)
            entry[out_key] = None
            if article_id is None:
                continue
            with db.connect() as conn:
                arow = conn.execute("""
                    SELECT a.id, a.title, a.byline, a.canonical_url,
                           p.name AS publication
                      FROM articles a
                      JOIN publications p ON p.id = a.publication_id
                     WHERE a.id = ?
                """, (article_id,)).fetchone()
            entry[out_key] = dict(arow) if arow else {"id": article_id, "note": "article not found"}
        lost.append(entry)

    return lost


@app.post("/admin/sync-db")
async def admin_sync_db(request: Request) -> JSONResponse:
    """Pull a gzipped SQLite snapshot from R2, validate, atomic-replace.

    Request:
      POST /admin/sync-db
      Authorization: Bearer <AARVA_RENDER_SYNC_TOKEN>
      Content-Type: application/json
      Body: {"r2_key": "_data/aarva-db.gz"}

    Response (200):
      {"status": "ok", "articles": <int>, "bytes": <int>,
       "lost_episodes_found": [...]}

    Errors:
      400  payload missing r2_key
      401  invalid / missing bearer token
      403  fetched object isn't a usable SQLite DB / has zero articles
      413  fetched object exceeds the size cap
      502  R2 fetch failed (bucket / key / credentials)
      503  AARVA_RENDER_SYNC_TOKEN or R2 config missing on this instance
    """
    _check_token(request)

    # Check for lost listener episodes BEFORE the atomic replace below
    # overwrites the main DB's `jobs` table with the laptop's version
    # — that's the only window this evidence exists in (see
    # _find_lost_episodes). Automatic per sync, not a manual step
    # someone has to remember to run first (2026-07-11: this used to
    # be exactly that, until a lost-episode discovery only worked
    # because the operator hadn't synced yet).
    lost_episodes = _find_lost_episodes(request.app.state.db, request.app.state.listener_db)
    if lost_episodes:
        logger.error(
            "=" * 70 + "\n"
            f"WARNING: {len(lost_episodes)} listener episode(s) about to "
            "lose their last chance at recovery — this sync is about to "
            "overwrite the jobs table that still holds their original "
            "article pairing. See the sync response for details.\n"
            + "=" * 70
        )

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    r2_key = payload.get("r2_key") if isinstance(payload, dict) else None
    if not r2_key or not isinstance(r2_key, str):
        raise HTTPException(status_code=400, detail="payload missing 'r2_key'")

    client, bucket = _build_r2_client(request.app.state.pipeline_cfg)

    # Fetch object from R2.
    try:
        obj = client.get_object(Bucket=bucket, Key=r2_key)
        size = int(obj.get("ContentLength") or 0)
        if size and size > _MAX_PAYLOAD_BYTES:
            raise HTTPException(status_code=413, detail=f"object too large: {size} bytes")
        compressed = obj["Body"].read()
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("R2 fetch failed key=%s: %s", r2_key, e)
        raise HTTPException(status_code=502, detail=f"R2 fetch failed: {e}")

    if len(compressed) > _MAX_PAYLOAD_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    if len(compressed) < 1024:
        raise HTTPException(status_code=403, detail="payload too small")

    # Decompress to a staging path next to the live DB so the eventual
    # atomic mv stays on the same filesystem.
    db_path = Path(os.environ.get("AARVA_DB_PATH", "/data/aarva.db"))
    db_dir = db_path.parent
    db_dir.mkdir(parents=True, exist_ok=True)

    fd, staging_str = tempfile.mkstemp(
        prefix="aarva-db-staging-", suffix=".db", dir=str(db_dir),
    )
    os.close(fd)
    staging = Path(staging_str)

    try:
        try:
            decompressed = gzip.decompress(compressed)
        except (OSError, EOFError) as e:
            raise HTTPException(status_code=403, detail=f"gunzip failed: {e}")

        staging.write_bytes(decompressed)

        # Sanity-check the staged DB: it has to (a) open, (b) have an
        # `articles` table, (c) report a plausible article count. If
        # any fail, the live DB stays untouched.
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
            raise HTTPException(
                status_code=403,
                detail="staged DB has 0 articles — refusing to swap",
            )

        # Atomic rename. On the same filesystem this is one syscall —
        # any new sqlite3.connect() after this point sees the new file;
        # any in-flight reads finish against the (still-open) old inode.
        # Our request pattern opens + closes connections per query, so
        # the in-flight read window is sub-millisecond.
        os.replace(str(staging), str(db_path))
        logger.info(
            "DB sync ok — %d articles, %d bytes (gzipped), %d bytes (raw), key=%s",
            article_count, len(compressed), len(decompressed), r2_key,
        )

        return JSONResponse({
            "status": "ok",
            "articles": article_count,
            "bytes": len(compressed),
            "lost_episodes_found": lost_episodes,
        })
    except HTTPException:
        if staging.exists():
            staging.unlink(missing_ok=True)
        raise
    except Exception as e:
        logger.exception("DB sync failed unexpectedly: %s", e)
        if staging.exists():
            staging.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"sync failed: {e}") from e


@app.get("/admin/diagnose-lost-episodes")
async def admin_diagnose_lost_episodes(request: Request) -> JSONResponse:
    """Find listener-built episodes whose editions/edition_pieces rows
    are gone but whose build-job record survived — evidence of exactly
    the class of bug that hit twice already (2026-07-03 sync overwrite,
    2026-07-06→11 ephemeral disk): both wiped the listener DB, but the
    `jobs` table lives in the *main* DB, which neither bug touched.

    `admin_sync_db` now runs this same check automatically before every
    sync (see _find_lost_episodes) — this endpoint is for checking
    on-demand between syncs, or independent of one.

    Request:
      GET /admin/diagnose-lost-episodes
      Authorization: Bearer <AARVA_RENDER_SYNC_TOKEN>

    Response (200):
      {"status": "ok", "count": <int>, "lost_episodes": [...]}
    """
    _check_token(request)
    lost = _find_lost_episodes(request.app.state.db, request.app.state.listener_db)
    return JSONResponse({"status": "ok", "count": len(lost), "lost_episodes": lost})

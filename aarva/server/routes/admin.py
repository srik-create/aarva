"""Authenticated admin endpoints.

Today: the daily-DB-sync receiver, a lost-listener-episode
diagnostic, and the listener-created-crosscut bonus-promotion pair.
The sync endpoint expects a small JSON trigger pointing at a gzipped
SQLite snapshot the laptop has already uploaded to R2; the server
fetches from R2 over the fast backbone (vs. the laptop uploading the
body directly over a residential link, which can blow past Render's
100-second request timeout on the Starter plan). Every sync runs the
lost-episode diagnostic (see _find_lost_episodes) as a general safety
check — its original rationale (jobs surviving in the main DB while
the sync wiped editions in the listener DB) no longer applies since
the 2026-07-15 jobs-table move put both in the same file, but it's
cheap and still catches other loss scenarios.

promote-bonus / unpromote-bonus (2026-07-27, see
docs/session_plan_promote_listener_created_as_bonus.md) are admin
endpoints rather than a local CLI flag because of the same DB
topology: listener-created crosscuts live in the listener DB on
Render's persistent disk, and there's no live sync bringing that data
back to the operator's laptop (only a one-way, disaster-recovery-only
R2 snapshot). Running the write directly on Render — which already
holds both DB connections in memory — sidesteps the whole problem.
The operator finds the edition_id by browsing the live
/listener-created page, then hits these endpoints (curl, or a small
wrapper script) to promote/unpromote it.

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
import re
import sqlite3
import tempfile
from datetime import date
from pathlib import Path

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from aarva.output.rss_feed import (
    _aarva_app_reference_html,
    _resolve_audio_url_base,
    _xml_esc,
)
from aarva.server.app import app
from aarva.services.queries import load_crosscut_episodes, load_listener_episodes

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
    edition_pieces rows are gone but whose job record survived. See
    admin_diagnose_lost_episodes for the full story.

    UPDATED 2026-07-15: `jobs` moved to the listener DB (see
    docs/session_plan_jobs_to_listener_db.md) — it used to live in the
    main DB specifically so a sync (which atomic-replaces the main DB)
    wouldn't wipe this evidence before it could be read. Now that jobs
    and editions live in the same file, a sync can no longer wipe one
    without the other — this check's original sync-timing rationale is
    moot. Kept anyway as a general-purpose diagnostic (e.g. a listener-
    DB restore from an R2 backup predating a completed build) — cheap
    to run, still correct once pointed at the right file."""
    with listener_db.connect() as conn:
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

    # General safety check, run before every sync (2026-07-11: this
    # used to be a manual step, until a lost-episode discovery only
    # worked because the operator hadn't synced yet — see
    # _find_lost_episodes). Since the 2026-07-15 jobs-table move, the
    # sync no longer threatens this evidence directly (jobs and
    # editions both live in the listener DB, untouched by this atomic
    # replace of the main DB) — kept as a standing diagnostic anyway.
    lost_episodes = _find_lost_episodes(request.app.state.db, request.app.state.listener_db)
    if lost_episodes:
        logger.error(
            "=" * 70 + "\n"
            f"WARNING: {len(lost_episodes)} listener episode(s) found whose "
            "audio finished but whose editions row is gone. See the sync "
            "response for details.\n"
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
    are gone but whose build-job record survived — evidence of the
    same class of bug that's hit three times now (2026-07-03 sync
    overwrite, 2026-07-06→11 ephemeral disk, 2026-07-15 jobs-table
    sync overwrite — the last one is what this check itself was
    fixed for). As of 2026-07-15, `jobs` lives alongside `editions` in
    the listener DB (see docs/session_plan_jobs_to_listener_db.md), so
    this specific job-survives-but-editions-don't split can no longer
    happen via a sync — kept as a general diagnostic regardless.

    `admin_sync_db` runs this same check on every sync (see
    _find_lost_episodes) — this endpoint is for checking on-demand
    between syncs, or independent of one.

    Request:
      GET /admin/diagnose-lost-episodes
      Authorization: Bearer <AARVA_RENDER_SYNC_TOKEN>

    Response (200):
      {"status": "ok", "count": <int>, "lost_episodes": [...]}
    """
    _check_token(request)
    lost = _find_lost_episodes(request.app.state.db, request.app.state.listener_db)
    return JSONResponse({"status": "ok", "count": len(lost), "lost_episodes": lost})


def _load_listener_crosscut_for_promotion(db, listener_db, edition_id: int) -> dict:
    """Look up a crosscut by edition_id for promotion. Checks
    listener_db first (everything since the 2026-07-06 split lives
    there), falling back to the main db (pre-split legacy episodes
    only). Raises HTTPException(404) if edition_id isn't a listener-
    created crosscut in either DB, or HTTPException(400) if it is one
    but has no synthesized audio yet — checked directly via raw SQL
    (not the load_listener_episodes/load_crosscut_episodes helpers,
    which both filter out rows with no audio_url — reusing them here
    would collapse "doesn't exist" and "exists but no audio" into the
    same not-found result). Returns the full row (same shape as those
    helpers) once existence + audio are confirmed."""
    with listener_db.connect() as conn:
        row = conn.execute("""
            SELECT e.user_id, ep.audio_url
              FROM editions e
              JOIN edition_pieces ep ON ep.edition_id = e.id AND ep.position = 0
             WHERE e.id = ? AND e.edition_type = 'crosscut'
        """, (edition_id,)).fetchone()
    if row and row["user_id"] is not None:
        if not row["audio_url"]:
            raise HTTPException(
                status_code=400,
                detail=f"edition {edition_id} has no synthesized audio yet",
            )
        found = load_listener_episodes(listener_db, edition_id=edition_id)
        return found[0]

    # Fallback: pre-split legacy listener episodes in the main db.
    with db.connect() as conn:
        row = conn.execute("""
            SELECT e.user_id, ep.audio_url
              FROM editions e
              JOIN edition_pieces ep ON ep.edition_id = e.id AND ep.position = 0
             WHERE e.id = ? AND e.edition_type = 'crosscut'
        """, (edition_id,)).fetchone()
    if row and row["user_id"] is not None:
        if not row["audio_url"]:
            raise HTTPException(
                status_code=400,
                detail=f"edition {edition_id} has no synthesized audio yet",
            )
        found = load_crosscut_episodes(db, edition_id=edition_id)
        return found[0]

    raise HTTPException(
        status_code=404,
        detail=f"edition {edition_id} is not a listener-created crosscut",
    )


@app.post("/admin/promote-bonus")
async def admin_promote_bonus(request: Request) -> JSONResponse:
    """Promote a listener-created crosscut onto today's (or a given
    date's) /today page, under the "Also today" section. See
    docs/session_plan_promote_listener_created_as_bonus.md.

    Request:
      POST /admin/promote-bonus
      Authorization: Bearer <AARVA_RENDER_SYNC_TOKEN>
      Body: {"edition_id": <int>, "daily_date": "<YYYY-MM-DD>"}  (daily_date optional, defaults to today)

    Response (200):
      {"status": "ok", "position": <int>, "daily_date": "<date>"}

    Errors:
      400  missing/invalid edition_id, or the crosscut has no audio yet
      401  invalid / missing bearer token
      404  edition_id doesn't exist, or isn't a listener-created crosscut
             (editorial crosscuts are refused — they already show on
             /today via a separate path)
    """
    _check_token(request)
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    edition_id = payload.get("edition_id") if isinstance(payload, dict) else None
    if not isinstance(edition_id, int):
        raise HTTPException(status_code=400, detail="payload missing integer 'edition_id'")

    daily_date_str = payload.get("daily_date") if isinstance(payload, dict) else None
    if daily_date_str:
        try:
            daily_date = date.fromisoformat(daily_date_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="daily_date must be YYYY-MM-DD")
    else:
        daily_date = date.today()

    db = request.app.state.db
    listener_db = request.app.state.listener_db

    # Raises 404 / 400 itself if edition_id isn't a promotable
    # listener-created crosscut.
    _load_listener_crosscut_for_promotion(db, listener_db, edition_id)

    with db.connect() as conn:
        row = conn.execute("""
            SELECT MAX(position) AS max_pos FROM daily_bonus_features
             WHERE daily_date = ?
        """, (daily_date.isoformat(),)).fetchone()
        next_position = int(row["max_pos"] or 0) + 1
        conn.execute("""
            INSERT OR IGNORE INTO daily_bonus_features
                (daily_date, featured_edition_id, position)
            VALUES (?, ?, ?)
        """, (daily_date.isoformat(), edition_id, next_position))
        conn.commit()

    logger.info(
        "Promoted edition %d as bonus feature #%d for %s",
        edition_id, next_position, daily_date.isoformat(),
    )
    return JSONResponse({
        "status": "ok", "position": next_position,
        "daily_date": daily_date.isoformat(),
    })


@app.post("/admin/unpromote-bonus")
async def admin_unpromote_bonus(request: Request) -> JSONResponse:
    """Remove a crosscut from a date's "Also today" bonus features.
    Idempotent — un-promoting something that wasn't promoted is a
    no-op, not an error. Does NOT reorder remaining positions (gaps
    are fine; reordering is a future feature per the spec's non-goals).

    Request:
      POST /admin/unpromote-bonus
      Authorization: Bearer <AARVA_RENDER_SYNC_TOKEN>
      Body: {"edition_id": <int>, "daily_date": "<YYYY-MM-DD>"}  (daily_date optional, defaults to today)

    Response (200):
      {"status": "ok", "removed": <bool>, "daily_date": "<date>"}
    """
    _check_token(request)
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    edition_id = payload.get("edition_id") if isinstance(payload, dict) else None
    if not isinstance(edition_id, int):
        raise HTTPException(status_code=400, detail="payload missing integer 'edition_id'")

    daily_date_str = payload.get("daily_date") if isinstance(payload, dict) else None
    if daily_date_str:
        try:
            daily_date = date.fromisoformat(daily_date_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="daily_date must be YYYY-MM-DD")
    else:
        daily_date = date.today()

    db = request.app.state.db
    with db.connect() as conn:
        cur = conn.execute("""
            DELETE FROM daily_bonus_features
             WHERE daily_date = ? AND featured_edition_id = ?
        """, (daily_date.isoformat(), edition_id))
        conn.commit()
        removed = cur.rowcount > 0

    if removed:
        logger.info(
            "Un-promoted edition %d for %s", edition_id, daily_date.isoformat(),
        )
    else:
        logger.info(
            "edition %d was not promoted for %s — nothing to remove",
            edition_id, daily_date.isoformat(),
        )
    return JSONResponse({
        "status": "ok", "removed": removed, "daily_date": daily_date.isoformat(),
    })


def _stat_or_head_byte_length(
    audio_url: str, package_root: Path, audio_url_base: str,
) -> int:
    """Best-effort byte length for an admin-composed RSS item.

    Stats Render's local disk first (works for episodes whose mp3
    hasn't been R2-cleaned yet). Falls back to an HTTP HEAD against
    the public audio URL for older, R2-only episodes. Returns 0 (with
    the caller expected to log a warning) if both fail — mirrors
    rss_feed.py's _audio_byte_length, which never hard-fails feed
    generation over a missing byte count."""
    try:
        path = package_root / audio_url.lstrip("/")
        if path.exists():
            return path.stat().st_size
    except Exception:
        pass
    try:
        url = (
            audio_url if re.match(r"^https?://", audio_url)
            else f"{audio_url_base.rstrip('/')}/{audio_url.lstrip('/')}"
        )
        with httpx.Client(timeout=10) as client:
            resp = client.head(url, follow_redirects=True)
        if resp.status_code == 200:
            length = resp.headers.get("content-length")
            if length:
                return int(length)
    except Exception:
        pass
    return 0


@app.get("/admin/episode-metadata")
async def admin_episode_metadata(request: Request) -> JSONResponse:
    """Fetch RSS-ready metadata for a crosscut edition_id — used by the
    laptop's `python -m aarva.rss_add --from-edition` CLI to graduate
    a listener-created (or main-DB) crosscut into the podcast RSS feed.
    See docs/session_plan_rss_extra_items.md.

    Request:
      GET /admin/episode-metadata?edition_id=<int>
      Authorization: Bearer <AARVA_RENDER_SYNC_TOKEN>

    Response (200):
      {"kind": "crosscut", "guid": "aarva-crosscut-<id>",
       "episode_date": "<date>", "title": "Crosscut: <topic>",
       "description_html": "<html>", "audio_url": "<relative path>",
       "byte_length": <int>, "duration_seconds": <int|None>,
       "author": "Aarva", "subtitle": "Crosscut · <topic>",
       "itunes_episode_type": "full"}

    Errors:
      400  missing/invalid edition_id
      401  invalid / missing bearer token
      404  edition_id doesn't exist, or isn't a listener-created crosscut
             (reuses _load_listener_crosscut_for_promotion's checks —
             also 400s if the crosscut has no synthesized audio yet)
    """
    _check_token(request)
    raw_id = request.query_params.get("edition_id")
    if raw_id is None:
        raise HTTPException(status_code=400, detail="missing edition_id query param")
    try:
        edition_id = int(raw_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="edition_id must be an integer")

    db = request.app.state.db
    listener_db = request.app.state.listener_db
    # Raises 404 / 400 itself if edition_id isn't a promotable
    # listener-created (or legacy pre-split) crosscut.
    cc = _load_listener_crosscut_for_promotion(db, listener_db, edition_id)

    topic = cc.get("topic_label") or "untitled"
    title = f"Crosscut: {topic}"

    # Description composed identically to _crosscut_item_xml
    # (rss_feed.py) so a graduated episode reads the same in a podcast
    # app as an editorial crosscut item does.
    desc_parts = []
    if cc.get("intro_text"):
        desc_parts.append(_xml_esc(cc["intro_text"]))
    if cc.get("bridge_between"):
        desc_parts.append(f"<em>{_xml_esc(cc['bridge_between'])}</em>")
    if cc.get("outro_text"):
        desc_parts.append(_xml_esc(cc["outro_text"]))
    sources = []
    if cc.get("url_a"):
        sources.append(
            f'<a href="{_xml_esc(cc["url_a"])}">{_xml_esc(cc["pub_a"])}: '
            f'{_xml_esc(cc["title_a"])}</a>'
        )
    if cc.get("url_b"):
        sources.append(
            f'<a href="{_xml_esc(cc["url_b"])}">{_xml_esc(cc["pub_b"])}: '
            f'{_xml_esc(cc["title_b"])}</a>'
        )
    if sources:
        desc_parts.append("Sources:<br/>" + "<br/>".join(sources))

    pipeline_cfg = request.app.state.pipeline_cfg
    aarva_app_url = (
        (pipeline_cfg.raw.get("output", {}) or {})
        .get("aarva_app_url", "").rstrip("/")
    )
    if aarva_app_url:
        desc_parts.append(_aarva_app_reference_html(aarva_app_url))
    description_html = "<br/><br/>".join(desc_parts)

    package_root = pipeline_cfg.rss_feed_path.resolve().parent.parent
    audio_url_base = _resolve_audio_url_base(pipeline_cfg)
    byte_length = _stat_or_head_byte_length(
        cc["audio_url"], package_root, audio_url_base,
    )
    if byte_length == 0:
        logger.warning(
            "episode-metadata: could not determine byte_length for "
            "edition %d (audio_url=%s) — stat and HEAD both failed",
            edition_id, cc["audio_url"],
        )

    return JSONResponse({
        "kind": "crosscut",
        "guid": f"aarva-crosscut-{edition_id}",
        "episode_date": cc.get("edition_date"),
        "title": title,
        "description_html": description_html,
        "audio_url": cc["audio_url"],
        "byte_length": byte_length,
        "duration_seconds": cc.get("duration_seconds"),
        "author": "Aarva",
        "subtitle": f"Crosscut · {topic}",
        "itunes_episode_type": "full",
    })

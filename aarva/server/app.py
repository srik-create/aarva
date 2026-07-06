"""Aarva web app — FastAPI entry point.

Local development:
    uvicorn aarva.server.app:app --reload

Production (any host that runs Python + ASGI):
    uvicorn aarva.server.app:app --host 0.0.0.0 --port $PORT

The app is intentionally provider-agnostic — see the Dockerfile for
the canonical build that runs on Render today, Fly.io / Railway / DO
/ VPS tomorrow (per AGENTS.md rule 7b).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from aarva.config import load_pipeline_config
from aarva.db import Database
from aarva.listener_db import ListenerDatabase
from aarva.server.config import load_server_config

logger = logging.getLogger(__name__)


_THIS_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _THIS_DIR / "static"
_TEMPLATES_DIR = _THIS_DIR / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise long-lived resources on startup, clean up on shutdown.

    Stores everything keyed off `app.state` so route handlers can
    reach them via `request.app.state.*`. This is the standard
    FastAPI pattern for shared-resource access.
    """
    server_cfg = load_server_config()
    pipeline_cfg = load_pipeline_config()

    # Honour AARVA_DB_PATH (env override) if set; otherwise use the
    # path baked into pipeline.yaml. The two should normally agree,
    # but env wins because that's how hosting platforms inject the
    # path to the persistent volume.
    db_path = Path(server_cfg.db_path).expanduser().resolve()
    db = Database(str(db_path))

    # Separate file for listener-built episodes — never touched by
    # scripts/sync_db_to_render.sh's atomic-replace of db_path above.
    # See aarva/listener_db.py for why.
    listener_db_path = Path(server_cfg.listener_db_path).expanduser().resolve()
    listener_db = ListenerDatabase(str(listener_db_path))

    logging.basicConfig(
        level=getattr(logging, server_cfg.log_level, logging.INFO),
        format="%(asctime)s  %(levelname)s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logger.info(
        "Aarva server starting — host=%s port=%d db=%s listener_db=%s public=%s",
        server_cfg.host, server_cfg.port, db_path, listener_db_path,
        server_cfg.public_url,
    )

    app.state.server_cfg = server_cfg
    app.state.pipeline_cfg = pipeline_cfg
    app.state.db = db
    app.state.listener_db = listener_db

    # Embedding + LLM clients are built once at startup and reused by
    # the route handlers (in particular the episode-candidate flow,
    # which calls Gemini and BGE on every prompt). Building them
    # per-request would re-load the ~110 MB BGE model from disk on
    # every search; once at boot is much friendlier.
    from aarva.clients.embedding import build_embedding_client
    from aarva.clients.llm import build_llm_client
    app.state.embedding_client = build_embedding_client(
        pipeline_cfg.raw.get("embedding", {})
    )
    app.state.llm_client = build_llm_client(pipeline_cfg.llm)

    # Background worker for on-demand episode builds. Daemon thread
    # polls the jobs table for build_crosscut jobs and runs the full
    # crosscut pipeline (script-gen + TTS + R2 upload + embed). One
    # concurrent build, FIFO. Stuck-job recovery runs at startup
    # inside start_worker(). See aarva/services/episode_worker.py.
    from aarva.services.episode_worker import start_worker
    app.state.episode_worker = start_worker(db, listener_db, pipeline_cfg)

    yield

    logger.info("Aarva server shutting down")
    # Clean stop so an in-progress build can wind down (the loop
    # checks stop_event between jobs; a running job finishes first).
    try:
        app.state.episode_worker.stop(timeout=5.0)
    except Exception as e:
        logger.warning("episode_worker stop failed: %s", e)


app = FastAPI(
    title="Aarva",
    description="The world as your classroom, the finest journalism as your "
                "curriculum. Written by humans. Narrated by AI.",
    lifespan=lifespan,
)


# ─────────────────────────────────────────────────────────────────────────
# Static + templates
# ─────────────────────────────────────────────────────────────────────────

if _STATIC_DIR.exists():
    app.mount(
        "/static",
        StaticFiles(directory=str(_STATIC_DIR)),
        name="static",
    )


# ─────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> JSONResponse:
    """Liveness probe. Returns 200 with a small status payload.

    Any provider's health check (Render, Fly, k8s readiness probes,
    Cloudflare's healthcheck-via-ping) understands this shape. Doesn't
    hit the DB so it's safe to poll at high frequency."""
    return JSONResponse({"status": "ok", "service": "aarva"})


@app.get("/health/db")
async def health_db() -> JSONResponse:
    """Deeper health check: confirms the DB connection works.

    Slower than /health (one round-trip to SQLite). Useful for
    deployment sanity checks; don't wire to high-frequency probes."""
    db: Database = app.state.db
    try:
        with db.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()
        return JSONResponse({
            "status": "ok",
            "articles": int(row["n"]),
        })
    except Exception as e:
        return JSONResponse(
            {"status": "error", "detail": str(e)[:200]},
            status_code=500,
        )


# Route modules are imported here so they register handlers against
# `app`. Each module attaches its own routes via @app.get(...) — kept
# in separate files for clarity as the surface area grows. Imports
# go at the bottom of app.py (not the top) because the routes
# themselves `from aarva.server.app import app` — top-of-file imports
# would create a cycle.
import aarva.server.routes  # noqa: F401, E402  (side-effect import)

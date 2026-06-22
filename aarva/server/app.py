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

    logging.basicConfig(
        level=getattr(logging, server_cfg.log_level, logging.INFO),
        format="%(asctime)s  %(levelname)s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logger.info(
        "Aarva server starting — host=%s port=%d db=%s public=%s",
        server_cfg.host, server_cfg.port, db_path, server_cfg.public_url,
    )

    app.state.server_cfg = server_cfg
    app.state.pipeline_cfg = pipeline_cfg
    app.state.db = db

    yield

    logger.info("Aarva server shutting down")


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
# in separate files for clarity as the surface area grows.
#
# (No route modules yet — added in the next commit.)

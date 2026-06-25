# Aarva web app — production container.
#
# Single-stage build: install Python deps, copy code, run uvicorn.
# Designed to be provider-agnostic per AGENTS.md rule 7b — Render is
# the v0.1 host, but this image runs unchanged on Fly.io / Railway /
# DigitalOcean App Platform / Kubernetes / a bare VPS.
#
# Runtime env vars (set by the hosting platform — never baked in):
#   PORT                    — listen port (Render injects this)
#   HOST                    — bind address (default 0.0.0.0)
#   AARVA_DB_PATH           — SQLite DB path (default /data/aarva.db)
#   AARVA_SERVER_PUBLIC_URL — public-facing URL (e.g. https://aarva.app)
#   AARVA_LOG_LEVEL         — INFO (default) | DEBUG | WARNING
#
# The web app process is read-only on the DB and makes no LLM or R2
# calls itself; audio URLs in the RSS feed point at audio.aarva.app
# (R2) and the pipeline that produces audio runs separately. So the
# container needs neither AARVA_GEMINI_API_KEY nor R2 credentials.

FROM python:3.12-slim

WORKDIR /app

# Install Python deps first so the layer cache survives code changes.
# requirements.txt includes the full pipeline deps; the web server
# itself only imports a subset. We accept the larger image at v0.1
# in exchange for keeping a single requirements file. If image size
# becomes a problem, split into requirements-server.txt later.
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Application code. Only the aarva package is needed at runtime;
# scripts/, docs/, tests/ are excluded via .dockerignore.
COPY aarva/ ./aarva/

# Sensible production defaults. Each can be overridden by the
# hosting platform's env-var injection.
ENV HOST=0.0.0.0 \
    PORT=8000 \
    AARVA_DB_PATH=/data/aarva.db \
    AARVA_LOG_LEVEL=INFO

EXPOSE 8000

# Use sh -c + exec so the env-var expansion at runtime happens AND
# the python process becomes PID 1 (proper signal handling).
CMD ["sh", "-c", "exec uvicorn aarva.server.app:app --host ${HOST} --port ${PORT}"]

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
# The web app serves read-only DB traffic AND runs the /create build
# worker in-process. The build worker calls Gemini for LLM + TTS,
# converts the resulting WAV to MP3 with loudnorm, and uploads the
# MP3 to R2. So the container DOES need:
#   - AARVA_GEMINI_API_KEY or GCP ADC (auth to Gemini)
#   - AARVA_R2_ACCESS_KEY_ID / _SECRET_ACCESS_KEY (audio upload)
#   - ffmpeg on PATH (audio conversion; installed below)
# The earlier assumption that Render only serves and never produces
# audio became stale when /create landed on 2026-06-29.

FROM python:3.12-slim

WORKDIR /app

# System deps for the /create build worker.
#   ffmpeg — used by aarva/output/audio_converter.py to convert the
#            Gemini-TTS WAV output to loudnorm'd MP3 before R2 upload.
#            Without it, episode_worker crashes at convert_all_for_
#            publish with "ffmpeg not found on PATH".
# Cleaning apt cache in the same RUN keeps the layer small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

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

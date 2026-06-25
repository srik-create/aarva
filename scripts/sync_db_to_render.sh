#!/usr/bin/env bash
# Push the local SQLite DB to the live Render service via R2 relay.
#
# Runs as the last step of `scripts/run_daily.sh`. Snapshots the DB
# using SQLite's backup API, gzips it, uploads the gzipped file to R2
# at a fixed key, then POSTs a tiny JSON trigger to /admin/sync-db on
# aarva.app. The server downloads from R2 over its own (fast) backbone,
# validates, and atomic-replaces /data/aarva.db.
#
# Why the R2 relay (vs. POSTing the gzipped DB directly): Render has a
# hard ~100-second request timeout on the Starter plan. A ~30 MB upload
# from a residential connection can exceed that. R2 ingest from a
# residential connection is reliable (boto3-style multipart, retries),
# and R2 → Render is the fast backbone path.
#
# Required env vars (loaded from ~/.aarva.env by run_daily.sh):
#   AARVA_RENDER_SYNC_TOKEN     — bearer token agreed with the server
#   AARVA_R2_ACCESS_KEY_ID      — R2 API key
#   AARVA_R2_SECRET_ACCESS_KEY  — R2 API secret
#
# Optional env vars:
#   AARVA_RENDER_SYNC_URL       — endpoint URL (default https://aarva.app/admin/sync-db)
#   AARVA_DB_PATH               — laptop DB path (default aarva/data/aarva.db)
#   AARVA_R2_ENDPOINT_URL       — R2 S3 endpoint (default below)
#   AARVA_R2_BUCKET             — R2 bucket (default aarva-audio)
#   AARVA_DB_SYNC_R2_KEY        — R2 key for the staging file (default _data/aarva-db.gz)
#
# Manual invocation:
#   bash scripts/sync_db_to_render.sh
#
# Exit codes:
#   0 — synced; response shows the article count on the server
#   1 — config error (missing env vars / missing DB)
#   2 — snapshot failed
#   3 — R2 upload failed
#   4 — server-trigger POST failed

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

DB_PATH="${AARVA_DB_PATH:-aarva/data/aarva.db}"
SYNC_URL="${AARVA_RENDER_SYNC_URL:-https://aarva.app/admin/sync-db}"
R2_ENDPOINT="${AARVA_R2_ENDPOINT_URL:-https://81079726a9b1dae5ed171eea53d54d96.r2.cloudflarestorage.com}"
R2_BUCKET="${AARVA_R2_BUCKET:-aarva-audio}"
R2_KEY="${AARVA_DB_SYNC_R2_KEY:-_data/aarva-db.gz}"

if [ -z "${AARVA_RENDER_SYNC_TOKEN:-}" ]; then
    echo "[sync] ERROR: AARVA_RENDER_SYNC_TOKEN is not set" >&2
    echo "[sync] Add it to ~/.aarva.env. Generate a token with:" >&2
    echo "[sync]   python3 -c 'import secrets; print(secrets.token_urlsafe(32))'" >&2
    exit 1
fi

if [ -z "${AARVA_R2_ACCESS_KEY_ID:-}" ] || [ -z "${AARVA_R2_SECRET_ACCESS_KEY:-}" ]; then
    echo "[sync] ERROR: AARVA_R2_ACCESS_KEY_ID / AARVA_R2_SECRET_ACCESS_KEY not set" >&2
    echo "[sync] These are the R2 credentials already used by the audio uploader." >&2
    exit 1
fi

if [ ! -f "$DB_PATH" ]; then
    echo "[sync] ERROR: DB not found at $DB_PATH" >&2
    exit 1
fi

SNAPSHOT="$(mktemp -t aarva-db-snapshot.XXXXXX).db"
PAYLOAD="$(mktemp -t aarva-db-payload.XXXXXX).gz"
HTTP_STATUS_FILE="$(mktemp -t aarva-sync-status.XXXXXX)"
RESPONSE_FILE="$(mktemp -t aarva-sync-response.XXXXXX)"
# shellcheck disable=SC2064
trap "rm -f '$SNAPSHOT' '$PAYLOAD' '$HTTP_STATUS_FILE' '$RESPONSE_FILE'" EXIT

echo "[sync] snapshotting $DB_PATH …"
# SQLite's .backup acquires the right locks and produces a consistent
# file even mid-write. Output is a single .db file (no WAL sidecars).
if ! sqlite3 "$DB_PATH" ".backup '$SNAPSHOT'"; then
    echo "[sync] ERROR: sqlite3 backup failed" >&2
    exit 2
fi
echo "[sync] snapshot: $(wc -c <"$SNAPSHOT" | tr -d ' ') bytes"

echo "[sync] compressing …"
gzip -c "$SNAPSHOT" > "$PAYLOAD"
echo "[sync] payload: $(wc -c <"$PAYLOAD" | tr -d ' ') bytes (gzipped)"

echo "[sync] uploading to R2 (s3://${R2_BUCKET}/${R2_KEY}) …"
# AWS CLI reads AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (standard
# names). The project uses AARVA_R2_* — inline env mapping below
# translates without polluting the parent shell.
if ! AWS_ACCESS_KEY_ID="$AARVA_R2_ACCESS_KEY_ID" \
     AWS_SECRET_ACCESS_KEY="$AARVA_R2_SECRET_ACCESS_KEY" \
     aws s3 cp "$PAYLOAD" "s3://${R2_BUCKET}/${R2_KEY}" \
         --endpoint-url "$R2_ENDPOINT" \
         --no-progress; then
    echo "[sync] ERROR: R2 upload failed" >&2
    exit 3
fi

echo "[sync] triggering server-side fetch …"
JSON_BODY="$(printf '{"r2_key": "%s"}' "$R2_KEY")"

# Tiny JSON POST — the server takes it from here. Server-side R2 fetch
# happens on Render's backbone and is fast; 60s timeout is generous.
if ! curl -sS \
    -X POST \
    -H "Authorization: Bearer $AARVA_RENDER_SYNC_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$JSON_BODY" \
    --max-time 60 \
    -o "$RESPONSE_FILE" \
    -w "%{http_code}" \
    "$SYNC_URL" > "$HTTP_STATUS_FILE"; then
    echo "[sync] ERROR: curl failed (network / timeout)" >&2
    [ -s "$RESPONSE_FILE" ] && cat "$RESPONSE_FILE" >&2
    exit 4
fi

HTTP_STATUS="$(cat "$HTTP_STATUS_FILE")"
if [ "$HTTP_STATUS" != "200" ]; then
    echo "[sync] ERROR: server returned HTTP $HTTP_STATUS" >&2
    cat "$RESPONSE_FILE" >&2
    echo "" >&2
    exit 4
fi

echo "[sync] OK — server response:"
cat "$RESPONSE_FILE"
echo ""

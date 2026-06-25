#!/usr/bin/env bash
# Push the local SQLite DB to the live Render service.
#
# Runs as the last step of `scripts/run_daily.sh`. Snapshots the DB
# using SQLite's backup API (consistent point-in-time copy, safe even
# if the pipeline is mid-write), gzips it, and POSTs to the
# /admin/sync-db endpoint on aarva.app. The endpoint validates,
# atomic-replaces /data/aarva.db, and returns the new article count.
#
# Required env vars (loaded from ~/.aarva.env in normal operation):
#   AARVA_RENDER_SYNC_TOKEN — bearer token agreed with the Render service.
#                             Generate one with:
#                                 python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
#                             Add to ~/.aarva.env on the laptop AND to
#                             Render's Environment tab.
#
# Optional env vars:
#   AARVA_RENDER_SYNC_URL   — override the endpoint URL (default
#                             https://aarva.app/admin/sync-db).
#   AARVA_DB_PATH           — override the laptop DB path (default
#                             aarva/data/aarva.db relative to repo root).
#
# Manual invocation:
#   bash scripts/sync_db_to_render.sh
#
# Exit codes:
#   0 — synced; response shows the article count on the server
#   1 — config error (missing token / missing DB)
#   2 — snapshot failed
#   3 — upload failed

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

DB_PATH="${AARVA_DB_PATH:-aarva/data/aarva.db}"
SYNC_URL="${AARVA_RENDER_SYNC_URL:-https://aarva.app/admin/sync-db}"

if [ -z "${AARVA_RENDER_SYNC_TOKEN:-}" ]; then
    echo "[sync] ERROR: AARVA_RENDER_SYNC_TOKEN is not set" >&2
    echo "[sync] Add it to ~/.aarva.env. Generate a token with:" >&2
    echo "[sync]   python3 -c 'import secrets; print(secrets.token_urlsafe(32))'" >&2
    exit 1
fi

if [ ! -f "$DB_PATH" ]; then
    echo "[sync] ERROR: DB not found at $DB_PATH" >&2
    exit 1
fi

SNAPSHOT="$(mktemp -t aarva-db-snapshot.XXXXXX).db"
PAYLOAD="$(mktemp -t aarva-db-payload.XXXXXX).gz"
# shellcheck disable=SC2064
trap "rm -f '$SNAPSHOT' '$PAYLOAD'" EXIT

echo "[sync] snapshotting $DB_PATH …"
# SQLite's .backup is the safe way to copy a live DB — it acquires the
# right locks and produces a consistent file even mid-write. Output is a
# single .db file (no WAL/SHM sidecars to worry about).
if ! sqlite3 "$DB_PATH" ".backup '$SNAPSHOT'"; then
    echo "[sync] ERROR: sqlite3 backup failed" >&2
    exit 2
fi
SNAPSHOT_BYTES="$(wc -c <"$SNAPSHOT" | tr -d ' ')"
echo "[sync] snapshot: ${SNAPSHOT_BYTES} bytes"

echo "[sync] compressing …"
gzip -c "$SNAPSHOT" > "$PAYLOAD"
PAYLOAD_BYTES="$(wc -c <"$PAYLOAD" | tr -d ' ')"
echo "[sync] payload: ${PAYLOAD_BYTES} bytes (gzipped)"

echo "[sync] POST $SYNC_URL …"
HTTP_STATUS_FILE="$(mktemp -t aarva-sync-status.XXXXXX)"
RESPONSE_FILE="$(mktemp -t aarva-sync-response.XXXXXX)"
# shellcheck disable=SC2064
trap "rm -f '$SNAPSHOT' '$PAYLOAD' '$HTTP_STATUS_FILE' '$RESPONSE_FILE'" EXIT

# --fail-with-body keeps the response body even on non-2xx so we can
# print the server's error message; -w writes the status code separately
# so we can branch on it.
if ! curl -sS \
    -X POST \
    -H "Authorization: Bearer $AARVA_RENDER_SYNC_TOKEN" \
    -H "Content-Type: application/gzip" \
    --data-binary "@$PAYLOAD" \
    --max-time 120 \
    -o "$RESPONSE_FILE" \
    -w "%{http_code}" \
    "$SYNC_URL" > "$HTTP_STATUS_FILE"; then
    echo "[sync] ERROR: curl failed (network / timeout)" >&2
    [ -s "$RESPONSE_FILE" ] && cat "$RESPONSE_FILE" >&2
    exit 3
fi

HTTP_STATUS="$(cat "$HTTP_STATUS_FILE")"
if [ "$HTTP_STATUS" != "200" ]; then
    echo "[sync] ERROR: server returned HTTP $HTTP_STATUS" >&2
    cat "$RESPONSE_FILE" >&2
    echo "" >&2
    exit 3
fi

echo "[sync] OK — server response:"
cat "$RESPONSE_FILE"
echo ""

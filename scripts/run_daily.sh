#!/usr/bin/env bash
# Aarva daily run — invoked manually by the operator.
#
# Per the decision recorded in docs/project_brief.md (2026-06-26),
# the daily run is NOT scheduled via launchd; the operator runs it
# explicitly when they want today's edition to ship. The plist file
# `scripts/app.aarva.daily.plist` exists as a starting point if
# scheduled runs are ever wanted in the future — load it with
# `launchctl load ~/Library/LaunchAgents/app.aarva.daily.plist`.
#
# Does the full publish cycle:
#   1. Activate the project venv
#   2. Run the pipeline (Stages 1 → 10)
#   3. Sync to gh-pages and push
#   4. Sync DB to Render so aarva.app reflects today's edition
#
# Invocation:
#   bash scripts/run_daily.sh

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

# Stamp every run so the log file is grep-friendly.
echo ""
echo "================================================================"
echo "Aarva daily run — $(date '+%Y-%m-%d %H:%M:%S %z')"
echo "================================================================"

# launchd runs with a minimal PATH — make sure Homebrew, /usr/local, and
# anything that put `claude` or `ffmpeg` on PATH for the interactive shell
# is still available here.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# Load secrets that aren't in the launchd environment. ~/.aarva.env should
# contain lines like:
#   export GEMINI_API_KEY="..."
#   export ANTHROPIC_API_KEY="..."   # optional
# Kept out of the project tree so the key isn't committed by accident.
if [ -f "$HOME/.aarva.env" ]; then
    # shellcheck disable=SC1091
    source "$HOME/.aarva.env"
    echo "[run_daily] loaded secrets from ~/.aarva.env"
else
    echo "[run_daily] WARN: ~/.aarva.env not found; LLM calls will likely fail"
fi

# Activate the venv. We standardise on Python 3.12 for predictability;
# 3.10+ is the floor for the current dependency set.
# shellcheck disable=SC1091
source "$PROJECT_ROOT/.venv/bin/activate"

echo "[run_daily] python: $(which python)  ($(python --version))"
echo "[run_daily] running aarva.daily pipeline…"
python -m aarva.daily

echo "[run_daily] pipeline complete — publishing to gh-pages…"
bash "$PROJECT_ROOT/scripts/publish.sh"

# Push the updated DB to Render so the web app at aarva.app reflects
# today's edition. Skipped silently if AARVA_RENDER_SYNC_TOKEN isn't
# set so dev / hand-runs without web-deploy context still complete.
if [ -n "${AARVA_RENDER_SYNC_TOKEN:-}" ]; then
    echo "[run_daily] syncing DB to Render…"
    bash "$PROJECT_ROOT/scripts/sync_db_to_render.sh"
else
    echo "[run_daily] skipping Render sync (AARVA_RENDER_SYNC_TOKEN unset)"
fi

echo "[run_daily] done — $(date '+%H:%M:%S')"

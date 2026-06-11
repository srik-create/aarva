#!/usr/bin/env bash
# Aarva daily run — invoked by launchd at 8:00am every morning.
#
# Does the full publish cycle:
#   1. Activate the project venv
#   2. Run the pipeline (Stages 1 → 10)
#   3. Sync to gh-pages and push
#
# All output (stdout + stderr) is captured to the log file referenced by
# the launchd plist (~/Library/Logs/aarva-daily.log).
#
# Manual invocation:
#   bash scripts/run_daily.sh
#
# launchd invocation uses the absolute path — see
# ~/Library/LaunchAgents/app.aarva.daily.plist.

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

echo "[run_daily] done — $(date '+%H:%M:%S')"

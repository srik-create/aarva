#!/usr/bin/env bash
# Aarva — finalize an approved edition.
#
# Run AFTER `python -m aarva.review` has approved all pieces in today's
# edition. This runs the remaining stages (8 hooks/contexts → 9 TTS →
# 10 publish) and pushes to GitHub Pages.
#
# Usage: bash scripts/finalize_edition.sh
#
# Pre-conditions:
#   - Stages 1–7 already ran (today's edition exists in the DB)
#   - All proposed pieces have been approved via `python -m aarva.review`
#   - aarva/.venv is built and ~/.aarva.env exists with GEMINI_API_KEY

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

echo ""
echo "================================================================"
echo "Aarva finalize — $(date '+%Y-%m-%d %H:%M:%S %z')"
echo "================================================================"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# Load LLM secrets (GEMINI_API_KEY).
if [ -f "$HOME/.aarva.env" ]; then
    # shellcheck disable=SC1091
    source "$HOME/.aarva.env"
else
    echo "[finalize] WARN: ~/.aarva.env not found; LLM calls will likely fail"
fi

# shellcheck disable=SC1091
source "$PROJECT_ROOT/.venv/bin/activate"

# Sanity check: refuse to finalize if there are still proposed (unapproved)
# pieces in the most recent edition. Better to fail loudly than silently
# ship hooks/audio for pieces the user hasn't seen.
proposed_count=$(sqlite3 aarva/data/aarva.db "
    SELECT COUNT(*)
      FROM edition_pieces ep
      JOIN editions e ON e.id = ep.edition_id
     WHERE e.id = (SELECT MAX(id) FROM editions)
       AND ep.review_status = 'proposed';
")
if [ "$proposed_count" -gt 0 ]; then
    echo "[finalize] ERROR: latest edition still has $proposed_count proposed (unapproved) pieces."
    echo "  Run `python -m aarva.review` first to approve or reject them."
    exit 1
fi

echo "[finalize] running Stage 8 (hooks + why-now contexts)…"
python -m aarva.daily --stage 8

echo "[finalize] running Stage 9 (TTS)…"
python -m aarva.daily --stage 9

echo "[finalize] running Stage 10 (publish)…"
python -m aarva.daily --stage 10

echo "[finalize] pushing to gh-pages…"
bash "$PROJECT_ROOT/scripts/publish.sh"

echo "[finalize] done — $(date '+%H:%M:%S')"

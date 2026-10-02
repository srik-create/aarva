#!/usr/bin/env bash
# Aarva publish script — syncs aarva/output/ to the gh-pages branch + pushes.
#
# Assumes:
#   - You're in the project root
#   - gh-pages branch exists on origin
#   - GitHub Pages is configured to serve from gh-pages branch root
#   - aarva/output/ has the current HTML + RSS + audio you want to publish
#
# Usage:  bash scripts/publish.sh
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

# Ensure we have the latest gh-pages from remote
git fetch origin gh-pages

# Set up a worktree at /tmp/aarva-publish.
#
# macOS periodically cleans /tmp, so git's worktree registration (under
# .git/worktrees/aarva-publish/) can outlive the actual directory. The
# safest sequence: try a force-remove (handles registered + present),
# prune (handles registered + missing), then rm -rf (handles orphaned
# directory with no registration). Each step is a no-op if not needed.
WORKTREE="/tmp/aarva-publish"

git worktree remove --force "$WORKTREE" 2>/dev/null || true
git worktree prune
rm -rf "$WORKTREE"
git worktree add "$WORKTREE" gh-pages

# Sync the worktree to gh-pages tip
cd "$WORKTREE"
git fetch origin gh-pages
git reset --hard origin/gh-pages
cd "$PROJECT_ROOT"

# Sync output directory into the worktree, preserving the "output/" prefix
# because the URLs stored in edition_pieces.audio_url and the feed include
# "output/" — keeping the directory structure aligned avoids 404s.
mkdir -p "$WORKTREE/output/web"

# Copy HTML pages
rsync -a --delete aarva/output/web/ "$WORKTREE/output/web/"

# NOTE: MP3s are no longer copied to gh-pages. Audio is served from R2
# (https://audio.aarva.app/output/audio/...), referenced directly by
# feed.xml's <enclosure url=...> and the HTML pages. The MP3 rsync here
# was a holdover from when GH Pages hosted audio; by 2026-10 it had
# accumulated ~5.7 GB / 776 MP3s on the gh-pages tree and tipped the
# branch over GitHub Pages' 1 GB artifact deployment cap, causing the
# `pages-build-deployment` workflow to time out (observed 2026-10-02 —
# see docs/roadmap.md). A one-off cleanup commit removed the historical
# MP3s from the gh-pages tree; this change prevents them from coming back.

# Copy RSS feed
cp aarva/output/feed.xml "$WORKTREE/feed.xml"

# Copy cover art if present (referenced by the feed's itunes:image tag)
if [ -f aarva/output/cover.png ]; then
    cp aarva/output/cover.png "$WORKTREE/cover.png"
fi

# Generate a simple index.html that redirects to latest.html
cat > "$WORKTREE/index.html" <<EOF
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Aarva</title>
<meta http-equiv="refresh" content="0; url=output/web/latest.html">
<link rel="alternate" type="application/rss+xml" title="Aarva podcast feed" href="feed.xml">
</head>
<body>
<p>Redirecting to <a href="output/web/latest.html">the latest edition</a>…</p>
<p>Subscribe to the <a href="feed.xml">podcast feed</a>.</p>
</body>
</html>
EOF

# Commit + push
cd "$WORKTREE"
git add -A
if git diff --staged --quiet; then
    echo "publish: no changes to publish"
    exit 0
fi
COMMIT_MSG="Aarva publish $(date +'%Y-%m-%d %H:%M')"
git commit -m "$COMMIT_MSG"
git push origin gh-pages

cd "$PROJECT_ROOT"
echo "publish: pushed to gh-pages"
echo "  feed:    https://srik-create.github.io/aarva/feed.xml"
echo "  latest:  https://srik-create.github.io/aarva/output/web/latest.html"

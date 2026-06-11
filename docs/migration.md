# Aarva — Moving to a new Mac

A runbook for the day you want to migrate Aarva to a different machine.
The setup is designed to make this cheap: code lives in git, the only
irreplaceable file is `aarva/data/aarva.db`, and everything else
rebuilds from source.

This runbook assumes you're moving from one Mac to another. The same
steps apply to a clean reinstall on the same machine.

---

## What needs to move, what doesn't

### Critical — must preserve

- **`aarva/data/aarva.db`** — single SQLite file with the entire history
  of ingested articles, scores, editions, pipeline runs. Lose this and
  Aarva loses its memory and would happily re-ingest every article it's
  ever seen.
- **Your GEMINI_API_KEY** — currently in `~/.aarva.env` on the source
  machine. You can also just regenerate a new one from
  [aistudio.google.com/apikey](https://aistudio.google.com/apikey).

### Recoverable from git — no copy needed

- All source code (clone from GitHub)
- All config (`aarva/config/*.yaml`)
- All prompts (`aarva/config/prompts.yaml`)
- The kickoff doc + audit doc + calibration set (`docs/`)
- All scripts (`scripts/`)

### Recoverable from GitHub Pages — no copy needed

- Published HTML editions (`output/web/*.html` on gh-pages)
- Published MP3s (`output/audio/*` on gh-pages)
- The RSS feed (`feed.xml` on gh-pages)
- The podcast cover (`cover.png` on gh-pages)

### Regenerable locally — no copy needed, just rebuild

- **`.venv/`** — virtualenv with all Python deps. Architecture-specific;
  don't try to copy it across machines. Just rebuild on the new Mac.
- **`aarva/output/`** — locally-generated audio + HTML. Can be left
  empty; new editions will populate it. Old editions are already on
  gh-pages so listeners aren't affected.
- ~~Kokoro TTS model files~~ — historical, removed when Gemini TTS
  became the production path. The `aarva/models/` directory no longer
  exists; nothing to download.

### Machine-local — must reconfigure

- **`~/.aarva.env`** — secrets file with `GEMINI_API_KEY`.
- **launchd plist** at `~/Library/LaunchAgents/app.aarva.daily.plist` —
  if/when you flip back to automated mode. Path inside the plist must
  match the new project location.
- **`pmset repeat wakeorpoweron`** — wake schedule. Set with `sudo`.
- **Full Disk Access** (if the project lives under `~/Documents`) —
  grant the bash binary FDA in System Settings. If the project lives
  under `~/Projects/<X>/`, no FDA needed.
- **`claude` CLI** — only needed if you ever flip `llm.provider` back
  to `claude_code`. Install via Anthropic's instructions.

---

## On the OLD Mac, before you migrate

1. **Make sure no pipeline is in flight.** A running ingest or scoring
   stage is mid-write on the SQLite DB; copying it during a write is the
   one way to corrupt the migration.

   ```bash
   # Check there's no running pipeline:
   sqlite3 aarva/data/aarva.db \
     "SELECT id, started_at, status, stage_invoked FROM pipeline_runs
       WHERE status = 'running' ORDER BY id DESC LIMIT 5;"
   ```

   If anything is `running` and you know the process is dead, mark it
   failed manually:

   ```bash
   sqlite3 aarva/data/aarva.db \
     "UPDATE pipeline_runs SET status = 'failed',
         finished_at = CURRENT_TIMESTAMP,
         error_message = 'killed during migration'
       WHERE status = 'running';"
   ```

2. **Commit + push any uncommitted code changes.**

   ```bash
   cd "<project root>"
   git status                 # confirm clean
   git push origin main       # ensure GitHub has everything
   ```

3. **Note your GEMINI_API_KEY.** Copy it from `~/.aarva.env` somewhere
   you'll have it on the new machine. Or generate a fresh key on the
   new machine — same Google account works.

4. **Capture the DB and (optionally) audio archive.** AirDrop is the
   simplest path between two Macs.

   ```bash
   # Bundle just the things you can't regenerate
   cd "<project root>"
   tar czf ~/aarva-migration.tgz \
       aarva/data/aarva.db \
       aarva/output/audio    # optional — old audio is also on gh-pages
   ```

   Then AirDrop / iCloud / USB the tarball to the new Mac.

---

## On the NEW Mac

### 1. Install the base tooling

```bash
# Homebrew if not already installed
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Python 3.12 (current dependency set needs 3.10+; we standardise on 3.12)
brew install python@3.12

# ffmpeg (for WAV → MP3 conversion at Stage 10)
brew install ffmpeg

# git, sqlite3 — usually present, but in case:
brew install git sqlite
```

### 2. Clone the project

```bash
# Pick where you want the project to live. ~/Projects keeps it out of
# the TCC-protected ~/Documents path which avoids launchd headaches.
mkdir -p ~/Projects
cd ~/Projects
git clone https://github.com/srik-create/aarva.git Aarva
cd Aarva
```

(The git remote already encodes `srik-create/aarva` from the source
machine; no URL change needed on the new Mac.)

### 3. Restore the irreplaceable state

```bash
# Unpack the migration tarball from the old Mac
cd ~/Projects/Aarva
tar xzf ~/aarva-migration.tgz   # restores aarva/data/aarva.db and optionally aarva/output/audio
```

Verify the DB is intact:

```bash
sqlite3 aarva/data/aarva.db "SELECT COUNT(*) FROM articles;"
sqlite3 aarva/data/aarva.db "SELECT COUNT(*) FROM editions;"
sqlite3 aarva/data/aarva.db "SELECT MAX(edition_date) FROM editions;"
```

### 4. Build the venv and install deps

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

If `requirements.txt` doesn't exist (it may not be in v0.1), install
manually:

```bash
pip install \
    feedparser httpx trafilatura python-dateutil \
    pyyaml sentence-transformers numpy scikit-learn \
    pillow google-genai anthropic click python-docx
```

### 5. (Skipped — no local model files needed)

Aarva used local Kokoro model files in early v0.1; those were removed
when Gemini TTS became the production path. Stage 9 now uses the same
`google-genai` SDK + GEMINI_API_KEY as the LLM stages, so there's
nothing extra to download.

### 6. Set up secrets

```bash
cat > ~/.aarva.env <<'EOF'
export GEMINI_API_KEY="paste_your_key_here"
EOF
chmod 600 ~/.aarva.env
```

### 7. Smoke test

```bash
source .venv/bin/activate
source ~/.aarva.env

# Feed reachability
python scripts/probe_feeds.py | head -20

# Stage 1 only (cheap, no LLM calls)
python -m aarva.daily --stage 1

# Check the DB after
sqlite3 aarva/data/aarva.db "SELECT status, COUNT(*) FROM articles GROUP BY status;"
```

### 8. (Optional) Set up automation

Only do this once the manual run works end-to-end.

```bash
# Edit the plist's hardcoded paths to match the new project location
sed -i.bak \
  "s|/Users/srikant/Documents/Claude/Projects/Curio v2|$HOME/Projects/Aarva|g" \
  scripts/app.aarva.daily.plist

# Install
cp scripts/app.aarva.daily.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/app.aarva.daily.plist

# Wake schedule
sudo pmset repeat wakeorpoweron MTWRFSU 07:55:00

# Smoke test the wrapper without waiting for 8am
launchctl start app.aarva.daily
tail -f ~/Library/Logs/aarva-daily.log
```

### 9. (Optional) Point Cowork at the new project folder

In Cowork's sidebar, change the selected project folder to
`~/Projects/Aarva`. Your scheduled tasks (under `~/Documents/Claude/Scheduled/`)
will migrate with Cowork itself when you sign in on the new Mac.

---

## On the OLD Mac, after the new one is verified working

Don't decommission immediately — keep the old Mac running for at least
one day after the first successful publish from the new one, so you
have a fallback if anything broke.

When you're confident the new Mac is fully owning Aarva:

```bash
# Disable the launchd job on the old Mac so it doesn't keep firing
launchctl unload ~/Library/LaunchAgents/app.aarva.daily.plist
sudo pmset repeat cancel

# Optional: archive the old project as a tarball, then delete
tar czf ~/Desktop/aarva-old-mac-archive.tgz <project root>
```

If both Macs run the daily job simultaneously you'd get two pushes to
gh-pages with different content — confusing for podcast apps. Don't.

---

## What to verify after migration

| Check | Command | Expected |
|---|---|---|
| Code clone good | `git status` | "nothing to commit, working tree clean" |
| DB intact | `sqlite3 aarva/data/aarva.db "SELECT MAX(edition_date) FROM editions;"` | Today's or yesterday's date |
| Venv working | `python -c "import trafilatura, feedparser, kokoro_onnx, google.genai"` | No import errors |
| Kokoro model present | `ls -lh aarva/models/` | Two files, ~310MB total |
| Secrets loaded | `source ~/.aarva.env && echo "${GEMINI_API_KEY:0:6}…"` | Non-empty prefix |
| Stage 1 runs | `python -m aarva.daily --stage 1` | "Stage 1 done — N publications..." |
| Publish path works | `bash scripts/publish.sh` | "publish: no changes to publish" (because nothing new yet) or "publish: pushed to gh-pages" |
| Feed reachable from listener | `curl -sI https://srik-create.github.io/aarva/feed.xml | head -1` | `HTTP/2 200` |

---

## Total time

Realistic estimate: **30–60 minutes** of attended work, plus 5–10 minutes
for the Kokoro model download (~310MB). The slow part is rebuilding the
venv (a few minutes if network is good, longer if pip wants to compile
anything from source).

The migration itself doesn't require Aarva to publish that day — old
content remains on gh-pages while you set up. If you want to skip a day
of new content, that's fine; if you want to publish on migration day,
the manual flow works from the moment Step 7 succeeds.

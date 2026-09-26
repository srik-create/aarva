**STATUS: In progress — spec awaiting Claude Code
implementation.** Roadmap "In progress" entry added in the same
edit set (rule 17a).

---

# Session plan — make TTS resumability actually work on Render (scratch on `/data`) + stream-concat + worker kill switch

Follow-up to `docs/session_plan_worker_resumability.md` (STATUS:
Shipped 2026-07-14). That fix added per-section scratch on disk
so a resumed build skips already-synthesized sections. It works
locally but has NEVER worked on Render — verified 2026-09-16 in
production while the operator watched a listener-created crosscut
(edition ≥ 1,000,000) loop OOM → restart → re-synthesize all 6
sections from scratch, repeatedly.

Read this doc + `docs/roadmap.md` + `AGENTS.md` +
`docs/project_brief.md` + `docs/session_plan_worker_resumability.md`
before starting.

---

## The bug (diagnosis, verified 2026-09-16)

**Symptom (production, 2026-09-16):** the operator kicked off a
`/create` build for a listener-created crosscut. Render logs
show synthesis reaching the outro; the container then OOM-restarts;
on respawn the worker resumes the job but re-synthesizes from
the intro. Loops indefinitely.

**Root cause: the scratch directory sits on ephemeral container
filesystem, not the persistent `/data` mount.**

Concrete chain (grep 2026-09-16):
- `aarva/stages/stage_crosscut.py:1874` — `audio_dir =
  config.audio_dir / edition_date.isoformat()`.
- `aarva/stages/stage_crosscut.py:1885` — `scratch_dir = audio_dir
  / f"_tts_scratch_{edition_id}"`.
- `aarva/config/pipeline.yaml:7` — `paths.audio_dir:
  aarva/output/audio` (relative path).
- `aarva/config/__init__.py:151` — env var `AARVA_AUDIO_DIR`
  overrides `paths.audio_dir`.
- `render.yaml` — never sets `AARVA_AUDIO_DIR`. The only persistent
  disk paths mounted at `/data` are `AARVA_DB_PATH` (line 25) and
  `AARVA_LISTENER_DB_PATH` (line 36). The 1 GB persistent disk
  (`render.yaml:72-77`) is `mountPath: /data`.

So on Render, `scratch_dir` resolves to `aarva/output/audio/<date>/
_tts_scratch_<edition_id>/` inside the container's ephemeral
filesystem. When Render OOM-kills the container, the whole
ephemeral filesystem is discarded — including all six `.wav`
scratch files. The respawned container reruns
`synthesize_crosscut_episode` with an empty `scratch_dir`, every
`section_path.exists()` at `stage_crosscut.py:1901` returns
False, and all six sections re-synthesize.

**Why the 2026-07-14 verification missed this:** roadmap Probe B
(`kill -9` locally on the operator's laptop) doesn't reproduce a
Render OOM. `kill -9` kills the process but leaves the local
filesystem alone; a Render OOM kills the whole container and
starts a new one with a fresh filesystem. Same class of trap as
the 2026-07-11 finding on `AARVA_LISTENER_DB_PATH` — env var
never wired into `render.yaml` when the listener-DB split shipped
2026-07-06, silently wiping listener episodes every deploy for
five days.

**Compounding factor — where the OOM actually lands:** the
operator's observation ("restarts after the final outro has been
synthesised") points at the concat step, not any single TTS call.
Read the code at `stage_crosscut.py:1935-1960`: `section_pcms:
list[bytes]` accumulates each section's decoded PCM as it's read
back from disk, then a second `combined: list[bytes]` accumulates
PCM + inter-section silence, then `b"".join(combined)` is written
in one `wf.writeframes` call. All six sections' raw PCM lives in
RAM simultaneously at the peak — for a long crosscut (~15 min
audio), that's ~30 MB PCM held twice, plus the concatenated bytes
buffer, on a 512 MB Starter plan sharing memory with FastAPI +
SQLite + the `google-genai` SDK's per-connection state (called
out in roadmap "OOM-frequency investigation" as a suspected top
consumer). This is why the OOM lands at the concat step.

---

## The fix — three concrete changes, one commit

### 1. Move the scratch dir onto `/data`

Add a new env var `AARVA_TTS_SCRATCH_DIR` that overrides where the
per-section scratch WAVs live. Default: unchanged (`audio_dir /
<date> / _tts_scratch_<edition_id>` — keeps local dev identical).
Set on Render to `/data/tts_scratch/`.

Concrete change in `aarva/stages/stage_crosscut.py` at line 1885:

```python
# Per-section scratch dir. Lives on persistent disk in production
# (AARVA_TTS_SCRATCH_DIR) so a Render OOM-and-respawn can find the
# already-synthesized sections. Local dev leaves the env var unset
# and falls back to audio_dir, matching prior behaviour.
scratch_base = os.environ.get("AARVA_TTS_SCRATCH_DIR")
if scratch_base:
    scratch_dir = Path(scratch_base) / f"edition_{edition_id}"
else:
    scratch_dir = audio_dir / f"_tts_scratch_{edition_id}"
scratch_dir.mkdir(parents=True, exist_ok=True)
```

Directory-name change from `_tts_scratch_<id>` to `edition_<id>`
in the env-var branch keeps the `/data/tts_scratch/` root readable
(no leading underscore). Local behaviour unchanged.

Add to `render.yaml` envVars block (after `AARVA_LISTENER_DB_PATH`
at line 36), preserving the block's block-comment discipline:

```yaml
      # Persistent scratch dir for in-flight crosscut TTS. Each
      # section is atomically moved here after synthesis; the dir
      # is cleaned once the combined WAV is written and audio_url
      # is persisted. Without this, the scratch dir would sit on
      # the container's ephemeral filesystem and be wiped by every
      # OOM-restart, forcing every resumed build to re-synthesize
      # all six sections from scratch (observed live 2026-09-16 —
      # see docs/session_plan_tts_scratch_persist.md).
      - key: AARVA_TTS_SCRATCH_DIR
        value: /data/tts_scratch
```

**Disk-footprint math (rule 4 trade-off, signed off 2026-09-16):**
scratch peaks at ~30 MB per in-flight edition (six ~5 MB WAVs).
Cleaned on success at `stage_crosscut.py:1997` (`shutil.rmtree`).
Persistent disk is 1 GB (`render.yaml:72-77`) with the two SQLite
DBs currently ~150-250 MB combined — leaves ~700 MB headroom.
Only one job runs at a time on the single-worker setup, so scratch
never exceeds ~30 MB in practice. Chosen over R2-as-scratch (~50
extra LOC + boto calls per section) because it matches the
existing disk-mount pattern used for both SQLite paths and needs
no new AWS-style state.

### 2. Stream-concat the six sections

Replace the two-list PCM buffering with a single streaming pass
that writes directly into the output `wave.Wave_write`. Cuts peak
memory during concat by ~60 MB (the size of `section_pcms` +
`combined` held simultaneously).

Concrete change in `aarva/stages/stage_crosscut.py`, replacing
lines 1935-1970 (the read-into-`section_pcms` block and the
`combined` accumulation):

```python
# Stream sections into the output WAV directly instead of
# buffering all PCM in memory. Cuts peak RAM by ~60 MB on a
# 15-minute crosscut — critical on Render Starter's 512 MB plan
# where concat was OOM-ing (observed 2026-09-16).
out_path = audio_dir / f"crosscut_{edition_id:04d}.wav"
sample_rate = None
sample_width = None
channels = None
sections_written = 0

# First pass: peek at the first section's WAV header to open the
# output writer with matching params. (We don't know sample_rate
# etc. until we read a section, and wave.open needs them before
# writeframes.)
first_ready = None
for name, _voice, _text, _extra in sections:
    p = scratch_dir / f"{name}.wav"
    if p.exists():
        first_ready = p
        break
if first_ready is None:
    logger.error(
        "Crosscut TTS: no sections produced audio for edition #%d",
        edition_id)
    return stats

with wave.open(str(first_ready), "rb") as wf:
    sample_rate  = wf.getframerate()
    sample_width = wf.getsampwidth()
    channels     = wf.getnchannels()

silence_samples = int(sample_rate * CROSSCUT_INTER_SECTION_PAUSE_MS / 1000)
silence_bytes = b"\x00" * (silence_samples * sample_width * channels)

with wave.open(str(out_path), "wb") as out_wf:
    out_wf.setnchannels(channels)
    out_wf.setsampwidth(sample_width)
    out_wf.setframerate(sample_rate)
    for i, (name, _voice, _text, _extra) in enumerate(sections):
        p = scratch_dir / f"{name}.wav"
        if not p.exists():
            continue  # section-level failure; concat what we have
        with wave.open(str(p), "rb") as wf:
            out_wf.writeframes(wf.readframes(wf.getnframes()))
        sections_written += 1
        if i < len(sections) - 1:
            out_wf.writeframes(silence_bytes)

if sections_written == 0:
    logger.error(
        "Crosscut TTS: no sections written to combined output for "
        "edition #%d", edition_id)
    return stats
```

Downstream code unchanged: `_wav_duration(out_path)`,
`stats.output_path = str(out_path)`, the DB `UPDATE`, and
`shutil.rmtree(scratch_dir, ignore_errors=True)` all follow.

Note: peak RAM during this loop is one section's PCM (~5 MB) at
a time, not six. Silence bytes buffer is tiny (~40 KB).

### 3. Worker kill switch — `AARVA_WORKER_DISABLED`

New env-var gate at the top of `start_worker` in
`aarva/services/episode_worker.py:83`. When set to `"1"` or
`"true"` (case-insensitive), the function logs and returns
immediately without calling `reset_all_running_jobs` and without
spawning the worker thread. Purpose: give the operator a one-click
way to stop the retry loop in production if a similar regression
appears in future, without needing to suspend the whole web
service.

Concrete change in `aarva/services/episode_worker.py`, insert at
the very top of `start_worker`:

```python
def start_worker(...):
    if os.environ.get("AARVA_WORKER_DISABLED", "").lower() in ("1", "true"):
        logger.warning(
            "AARVA_WORKER_DISABLED is set — skipping worker startup. "
            "Any 'running' jobs remain in that state; any 'pending' "
            "jobs will not be picked up until the env var is cleared.")
        return
    # ... existing body unchanged
```

Not added to `render.yaml` (should not persist across deploys) —
the operator sets it manually in the Render dashboard when needed
and unsets it after.

---

## Design rationale

**Why scratch on `/data` rather than the whole `audio_dir`.**
`audio_dir` also holds final combined WAVs (~30-50 MB each) that
`stage_10_publish.py` uploads to R2 and then keeps as local
back-refs. Moving `audio_dir` wholesale would eat the 1 GB disk in
~20-30 editions. Scratch, by contrast, is bounded to one in-flight
edition at a time and is cleaned on success.

**Why an env var rather than a code-level detection of Render.**
Same discipline as `AARVA_DB_PATH` and `AARVA_LISTENER_DB_PATH`
(both at `render.yaml:23-36`). The code stays deploy-target-
agnostic; the deploy YAML holds the deploy-specific paths. Also
lets local integration tests set `AARVA_TTS_SCRATCH_DIR=/tmp/aarva-scratch`
to exercise the code path.

**Why stream-concat and scratch-move in the same commit.** Without
stream-concat, the OOM at the concat step keeps happening — so
even with resumable scratch on `/data`, the loop is: synthesize all
6 → OOM at concat → restart → skip synthesis (great!) → hit
concat → OOM again → restart → skip synthesis → hit concat → …
Rule 21 (one concept per commit) is satisfied by treating the
whole "make listener-created TTS actually complete on Render's
512 MB plan" as one concept — the two changes are both required
for the concept to hold.

The kill switch is a small third piece but sits in the same
concept because it's the operator's escape hatch for the same
class of production incident this fix is closing.

**Why not R2-as-scratch.** Cleaner in principle, but ~50 extra
lines (boto put_object per section, get_object on resume, error
handling on partial uploads), plus network cost per section (six
GET/PUT per build). The `/data` path is 3 lines, matches the
existing pattern, and the scratch never exceeds ~30 MB.

**Why not fix the concat OOM by making the audio shorter.**
Listener-created crosscuts are user-driven (topic-of-the-hour
plus the operator's editorial pairing) — audio length is set by
the content, not tunable per-episode. Cutting a section would
change the product.

---

## Backward compatibility

- **No schema change.** No DB migrations. `edition_pieces.audio_url`
  semantics unchanged.
- **Local dev path unchanged.** Without `AARVA_TTS_SCRATCH_DIR`,
  scratch continues to live under `aarva/output/audio/<date>/
  _tts_scratch_<edition_id>/`. Existing tests that inspect that
  directory keep working.
- **Existing scratch files on Render.** Any leftover
  `aarva/output/audio/<date>/_tts_scratch_*` inside the container
  from the current wedged build are discarded on the next OOM/restart
  anyway; no cleanup needed.
- **In-flight job at deploy time.** Deploy while the loop is
  active: the operator sets `AARVA_WORKER_DISABLED=1` first,
  redeploys, manually flips the wedged job's status to `failed` via
  `sqlite3 /data/aarva-listener.db "UPDATE build_jobs SET status='failed'
  WHERE status IN ('running','pending')"` (needs shell access — see
  Rollout below), then unsets the env var. Second build will pick
  up the scratch layout that now works.
- **Existing crosscuts.** Nothing about post-write behaviour
  changes. The combined WAV lives in the same place; R2 upload +
  `audio_url` persistence are unaffected.

---

## Verification (crucial — must actually simulate Render OOM)

The 2026-07-14 verification was `kill -9` locally, which is why
the regression sat undetected in production for two months. This
time the verification must simulate Render's ephemeral filesystem
being wiped between runs.

1. **Unit test — env var routing.** Set
   `AARVA_TTS_SCRATCH_DIR=/tmp/test-scratch`, call
   `synthesize_crosscut_episode` with a mocked `tts.synthesize`
   that writes 1s of PCM. Assert scratch WAVs land in
   `/tmp/test-scratch/edition_<id>/`, not the audio_dir. Unset
   the env var, rerun, assert scratch lands under `audio_dir/…`.

2. **Ephemeral-fs simulation — the real test.** Two-run
   integration test:
   - Run 1: set `AARVA_TTS_SCRATCH_DIR=/tmp/persistent-scratch`
     and `AARVA_AUDIO_DIR=/tmp/ephemeral-audio`. Call
     `synthesize_crosscut_episode`. Interrupt after 3 sections
     have moved into scratch (mock `tts.synthesize` to raise
     `MemoryError` on the 4th call).
   - Between runs: `shutil.rmtree('/tmp/ephemeral-audio')` — this
     simulates the Render OOM discarding the ephemeral container
     filesystem. Leave `/tmp/persistent-scratch` alone (that's
     `/data` in production).
   - Run 2: call `synthesize_crosscut_episode` again with same
     edition_id, working mock this time. Assert the 3 already-done
     sections log `SKIPPING (already done)`, the remaining 3
     synthesize, and the combined output is produced.
   - If instead you clear `/tmp/persistent-scratch` between runs,
     assert all 6 sections re-synthesize (this is the current
     broken behaviour and proves the test exercises the right
     seam).

3. **Stream-concat memory profile.** Wrap the concat block with
   `tracemalloc.start()` + `tracemalloc.get_traced_memory()`
   snapshots before/after. Synthesize six mock 5 MB WAV sections.
   Assert peak allocation during concat is < 15 MB (one section
   in the read buffer + silence + writer state), not > 60 MB (the
   pre-fix `section_pcms` + `combined` accumulation).

4. **Kill switch.** Set `AARVA_WORKER_DISABLED=1`, call
   `start_worker`, assert no thread is spawned and
   `reset_all_running_jobs` was not called. Unset, call again,
   assert normal behaviour. Log line "AARVA_WORKER_DISABLED is
   set..." appears at WARNING level.

5. **Backward-compat local dev.** With NO env vars set, run a
   scratch-and-resume of the same shape as 2026-07-14's Probe B
   (kill -9 mid-passage_a, rerun). Assert the two already-done
   sections are skipped exactly as before. Confirms local dev
   isn't regressed.

6. **Production smoke.** After deploy, kick off a real
   listener-created crosscut. Watch Render logs. Confirm scratch
   dir is created under `/data/tts_scratch/edition_<id>/` (log
   the resolved path at INFO). If an OOM does happen mid-concat,
   confirm the next attempt logs `SKIPPING (already done)` for
   the 6 finished sections and only re-runs concat. Report the
   log excerpt in the roadmap "Recently completed" entry as
   real-run evidence.

---

## Files that change

- `aarva/stages/stage_crosscut.py` — scratch_dir env-var routing
  (~10 line diff at 1885), stream-concat rewrite (~35 line diff at
  1935-1970). Add `import os` if not already imported at module top.
- `aarva/services/episode_worker.py` — kill-switch gate at
  `start_worker` top (~6 line diff at 83). Add `import os` if not
  already imported.
- `render.yaml` — add `AARVA_TTS_SCRATCH_DIR` envVar with block
  comment (see spec text above).
- `aarva/tests/test_stage_crosscut.py` (or the closest existing
  crosscut TTS test file — pick whichever holds the current
  scratch-dir tests from 2026-07-14) — add verifications 1-5 above.
- `docs/roadmap.md` — "In progress" entry landed in this edit
  set (rule 17a); moved to "Recently completed" at merge time
  with real-run evidence from verification step 6.

---

## Rollout

**Order matters — the loop is active in production RIGHT NOW.**

1. Operator sets `AARVA_WORKER_DISABLED=1` in Render dashboard →
   Render auto-redeploys → worker skips startup on new container →
   no more picking up the wedged job. Loop stops.

2. Operator manually clears the wedged job from listener_db. On
   Render Starter this requires either (a) Shell access if the
   dashboard offers it for this plan, or (b) a one-off admin
   route. Shell access on Starter is uncertain — the operator
   should check the Shell tab in the dashboard first. If
   available: `sqlite3 /data/aarva-listener.db "UPDATE build_jobs
   SET status='failed', last_error='cancelled 2026-09-16 —
   scratch-persist rollout' WHERE status IN ('running','pending')"`.
   If not available: Claude Code adds a POST `/admin/cancel-job/{id}`
   route (~15 LOC) as part of this PR that flips the row to
   `failed` — protected by the same `AARVA_RENDER_SYNC_TOKEN`
   used by `/admin/sync-db`.

3. Merge this PR. Render auto-deploys.

4. Operator unsets `AARVA_WORKER_DISABLED` in Render dashboard
   (or leaves the value blank / removes the entry). Worker starts.

5. Operator re-triggers the listener-created crosscut build. This
   time TTS runs to completion — if an OOM does happen mid-concat,
   the next attempt resumes correctly.

6. Verification step 6 above.

7. Update `docs/roadmap.md` — move the "In progress" entry into
   "Recently completed" under a `### 2026-09-16` (or whatever
   date merges) header with the real-run log evidence.

**Rollback plan.** If step 4 goes wrong (e.g. some edge case with
the stream-concat that only shows up in production audio), set
`AARVA_WORKER_DISABLED=1` again to freeze, then revert the PR.
Local dev is unaffected because the env var isn't set locally.

---

## Rules verified in this handoff

- **AGENTS.md rule 4** (material trade-off pre-approval): user
  signed off 2026-09-16 on (a) using `/data` for scratch rather
  than R2, (b) folding stream-concat into the same commit as the
  scratch move, (c) including the worker kill switch. Disk-footprint
  math (~30 MB peak, 1 GB total, ~700 MB headroom after the two
  SQLite DBs) is documented in Design rationale.
- **AGENTS.md rule 6a** (web-verify vendor claims): only
  vendor-specific claim in this spec is Render's ephemeral
  container filesystem being wiped on restart. Publicly documented
  at Render's persistent-disks docs; verified in production
  2026-09-16 (the observed loop IS the verification). Render
  Starter plan Shell availability is left as "operator should
  check the dashboard" rather than a definite claim — the fallback
  admin route covers the case where Shell isn't there.
- **AGENTS.md rule 12** (preserve history over DELETE): scratch
  dir is `rmtree`d on success as before — no change to that
  posture (scratch is intermediate build state, not editorial
  history). Wedged job rows are marked `failed`, not deleted.
- **AGENTS.md rule 17a** (roadmap-in-same-edit-set + spec-authoring
  subrule): "In progress" row added to `docs/roadmap.md` in the
  same commit that lands this spec.
- **AGENTS.md rule 17c** (STATUS-line discipline): parent doc
  `docs/session_plan_worker_resumability.md` remains STATUS:
  Shipped 2026-07-14 — this is a follow-up doc, not a rewrite of
  that spec's decisions. That spec's Section 3 (OOM-frequency
  investigation) remains open in the roadmap; this fix addresses
  a different failure mode.
- **AGENTS.md rule 17e** (cite-the-source discipline): every code
  reference cites file+line, verified via grep against the current
  tree 2026-09-16 (`stage_crosscut.py:1874,1885,1901,1935-1970,1997`,
  `pipeline.yaml:7`, `config/__init__.py:151`, `render.yaml:23-77`,
  `episode_worker.py:83,89,91`, `episode_jobs.py:236,362`).
- **AGENTS.md rule 20a** (Claude Code git protocol): Claude Code
  commits + pushes + opens PR directly, user gives explicit "merge
  it" before merging.
- **AGENTS.md rule 21** (one concept per commit): the concept is
  "make listener-created crosscut TTS actually complete on Render's
  512 MB plan." Scratch-on-`/data` + stream-concat + kill switch
  are the three components required for that concept to hold —
  splitting them would leave the concept only partially delivered
  after each commit and the loop still-broken.

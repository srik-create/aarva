# Session plan — worker resumability + OOM investigation

Written by Cowork for the next Claude Code session (2026-07-14+).
This supersedes the "item 2/3" placeholder rows in `docs/roadmap.md`
with a proper diagnosis + fix plan.

Read this doc + `docs/roadmap.md` + `AGENTS.md` before starting.

---

## UPDATE 2026-07-14 (later same day) — root cause confirmed

Section 1's diagnostic logs were shipped and Probes A + B were run
for real (locally — a real `/create`-equivalent job, a real `kill -9`
mid-TTS, a real reset-and-resume) rather than waiting on a production
OOM. Result, in terms of this doc's own hypotheses:

- **H1 (stamp doesn't commit) — FALSIFIED.** `stamp_edition_id` fired
  with `rows_affected=1` right after edition creation.
- **H2 (OOM before stamp) — not the mechanism here**; the stamp
  happens well before TTS starts, so timing isn't the issue.
- **H3 (resume branch leaks into setup) — FALSIFIED.** On resume,
  the log showed `checkpoint_edition_id` read correctly and the
  RESUMING branch fired with zero LLM-proposal or edition-creation
  calls — steps 1-3 are genuinely skipped.
- **H4 (per-piece idempotency check is broken) — REFRAMED.** There
  is no such check to be broken. `synthesize_crosscut_episode`
  always re-synthesizes all 6 sections from scratch and only
  persists `audio_url` once, at the very end. This is the actual
  root cause: resume correctly skips steps 1-3, but step 4 (TTS)
  restarts from section 1 every time regardless of prior progress.
  That's what reads to the listener as "the whole thing starts
  over."

**Consequence for this doc's Section 2:** treat H4's fix description
as superseded — the real fix is adding per-section
skip-if-already-synthesized logic to `synthesize_crosscut_episode`
(track progress per-section, e.g. by checking which sections already
have rendered audio before re-synthesizing), not "fixing a broken
check." That work — plus Section 3 (why the OOM happens at all) — is
deferred to a separate session; see `docs/roadmap.md`'s
"Recently completed" 2026-07-14 entry for the full verified detail.

**FIXED, same day.** Per-section resumability shipped: a scratch
directory per edition holds each section's WAV as soon as it's
synthesized (moved in atomically, so a crash mid-synthesize can't
leave a half-written file), and the function skips straight to
reading a section's file if it's already there instead of calling TTS
again. Verified against a real crash-and-resume (not just a read-
through) — see `docs/roadmap.md`'s "Recently completed" 2026-07-14
entry, first bullet, for the full detail. Section 1 (resumability) of
this doc is DONE. Section 3 (why the OOM happens at all) remains open
— see `docs/roadmap.md`'s "In progress" list.

---

## Context — what we know

The listener-facing symptom (reported 2026-07-14):

> "the checkpoint fix doesn't work — it restarts the job from the
> beginning, not from where it stopped. That triggers another OOM,
> and the loop repeats until I manually stop the job."

The user observed multiple full-build restarts on a single `/create`
episode, each hitting OOM. When the container came back up and the
job resumed, it ran the entire flow from step 1 again rather than
jumping to step 4 (TTS) as designed.

**What was already tried and shipped:**

- **PR #49 (2026-07-02):** `stamp_edition_id` writes `edition_id`
  into `jobs.payload_json` after step 2 (`build_episode_script`)
  completes. `_run_job` reads the checkpoint at its top; if set,
  skips steps 1-3 and jumps to TTS.
- **PR #70 (2026-07-14):** Worker resets ALL `running` jobs to
  `pending` on startup unconditionally (instead of only ones older
  than 30 min). Guarantees the resume attempt happens within
  seconds of a Render OOM restart, not 30 minutes later.

Both fixes are in main. Neither addresses the "resume runs from
scratch" bug the user is reporting. That's a separate defect and
this session's job.

**What was manually done to break the loop (2026-07-14):**

Via Render Dashboard Shell:
```
python3 -c "import sqlite3; c = sqlite3.connect('/data/aarva.db'); c.execute(\"UPDATE jobs SET status='failed', ... WHERE status='running' AND kind='build_crosscut'\"); c.commit()"
```

That job stays as `failed` — do not "recover" it as part of testing.

---

## Section 1 — Diagnostic-first approach

Before proposing any fix, get evidence. Two probes together
falsify each of the four hypotheses in Section 2 without waiting
for a production OOM.

### Probe A — Does the checkpoint stamp actually persist?

The simplest, fastest test. **No failure needs to be triggered.**

1. Trigger a `/create` build (locally via `python -m aarva.server`
   or on the live site — either works).
2. Wait ~30-45 seconds for step 2 (`build_episode_script`) to
   complete. The status page's progress text will move from
   "Setting up the build…" to "Writing the intro and bridges…" to
   "Rendering the audio (~15 min)…". When you see the third
   message, step 2 is done and the checkpoint should have been
   stamped.
3. Peek at the `jobs` table:

   ```bash
   python3 -c "
   import sqlite3, json
   c = sqlite3.connect('aarva/data/aarva.db')  # or /data/aarva.db in the Render Shell
   for r in c.execute('SELECT id, status, payload_json FROM jobs ORDER BY id DESC LIMIT 3'):
       p = json.loads(r[2])
       print(f'job {r[0]}: status={r[1]}, edition_id in payload = {p.get(\"edition_id\")!r}')
   "
   ```

Outcomes:
- **`edition_id in payload = <int>`** → the stamp IS working.
  Hypotheses **H1** (stamp doesn't commit) and **H2** (OOM before
  stamp) are both falsified. The resume bug is elsewhere — go to
  Probe B.
- **`edition_id in payload = None`** → the stamp is not landing.
  Either H1 (commit missing) or H2 (OOM lands earlier than the
  stamp call). Fix that first before doing anything else. Add the
  diagnostic logs in the next subsection to distinguish H1 vs H2:
  if `stamp_edition_id` log line fires but the payload has no
  edition_id → H1 (commit not durable). If it never fires → H2
  (stamp not reached).

### Probe B — Does the resume path actually resume?

Only run this if Probe A shows the stamp is working. Simulates a
crash locally without waiting for a real Render OOM.

1. Run the server locally: `python -m aarva.server` (or use the
   equivalent uvicorn command from the Dockerfile).
2. Trigger a build via a local `/create` submission.
3. Once TTS starts (progress: "Rendering the audio…"), forcibly
   kill the process: Ctrl-C, or `kill -9 <pid>` from another
   terminal.
4. Restart the server: same command as step 1.
5. Watch the log. Within ~5 seconds of startup the worker polls,
   claims the reset job, and calls `_run_job` again.

Outcomes:
- **Log shows "RESUMING via checkpoint at edition N"** → the
  resume path IS reading the checkpoint. If the build then still
  re-runs setup steps, we're at **H3** (leaked setup call in
  resume branch) — trace the resume branch call chain.
- **Log shows "starting FROM SCRATCH (no checkpoint)"** → the read
  path isn't seeing the checkpoint even though Probe A confirmed
  it's in the DB. Something between "select from jobs" at the top
  of `_run_job` and the `payload.get('edition_id')` check is
  broken. Likely the `claim_next_pending` query returning a stale
  snapshot, or a caching layer.
- **Log shows RESUMING but TTS re-runs already-done pieces** →
  **H4** (per-piece idempotency check broken). Check whether
  `audio_url` gets committed after each piece finishes.

### Ship these diagnostic logs to make the probes readable

Add each log line below. They cost nothing at rest and turn the
two probes into readable stories:

1. In `aarva/services/episode_jobs.py::stamp_edition_id`, after the
   UPDATE (and after `conn.commit()` if you have to add one):
   ```python
   logger.info(
       "stamp_edition_id: job %d stamped edition_id=%d (rows_affected=%d)",
       job_id, edition_id, conn.total_changes,
   )
   ```
   Confirms the write actually ran and reports how many rows it
   touched (should be 1; 0 means the WHERE clause missed).

2. In `aarva/services/episode_worker.py::_run_job`, at the top
   before the checkpoint check:
   ```python
   logger.info(
       "_run_job: job %d starting — payload keys=%s, checkpoint_edition_id=%s",
       job_id, sorted(payload.keys()), payload.get("edition_id"),
   )
   ```
   The single most useful line — shows exactly what the retry
   sees when it re-claims the job.

3. In the resume branch (~L178):
   ```python
   logger.info(
       "_run_job: job %d RESUMING via checkpoint at edition %d — skipping steps 1-3",
       job_id, edition_id,
   )
   ```

4. In the from-scratch branch (~L187):
   ```python
   logger.info(
       "_run_job: job %d starting FROM SCRATCH (no checkpoint) — running steps 1-3",
       job_id,
   )
   ```

5. In `aarva/stages/stage_crosscut.py::synthesize_crosscut_episode`,
   at the per-piece decision point:
   ```python
   logger.info(
       "TTS piece %d/%d slot=%s audio_url=%r → %s",
       i, n, piece["slot"], piece.get("audio_url"),
       "SKIPPING (already done)" if already_done else "SYNTHESIZING",
   )
   ```
   Confirms per-piece idempotency is (or isn't) firing on retry.

Ship the logs FIRST, then run Probes A and B. Do NOT skip
straight to a fix based on any hypothesis before probing.

**Add these logs (short, one line each):**

1. In `aarva/services/episode_jobs.py::stamp_edition_id`, after the
   UPDATE:
   ```
   logger.info("stamp_edition_id: job %d stamped edition_id=%d (rows_affected=%d)",
                job_id, edition_id, conn.total_changes)
   ```
   Confirms the write went through AND was durable.

2. In `aarva/services/episode_worker.py::_run_job`, at the very top
   (before the checkpoint check):
   ```
   logger.info("_run_job: job %d starting — payload keys: %s, checkpoint_edition_id=%s",
                job_id, sorted(payload.keys()), payload.get("edition_id"))
   ```
   Confirms whether `edition_id` is in the payload when the retry
   attempt claims the job.

3. In the resume branch (line ~178 of episode_worker.py):
   ```
   logger.info("_run_job: job %d RESUMING via checkpoint at edition %d — skipping steps 1-3",
                job_id, edition_id)
   ```

4. In the from-scratch branch (line ~187 of episode_worker.py):
   ```
   logger.info("_run_job: job %d starting FROM SCRATCH (no checkpoint) — running steps 1-3",
                job_id)
   ```

5. In `aarva/stages/stage_crosscut.py::synthesize_crosscut_episode`,
   at the per-piece decision point:
   ```
   logger.info("TTS piece %d/%d slot=%s audio_url=%r → %s",
                i, n, piece["slot"], piece.get("audio_url"),
                "SKIPPING (already done)" if already_done else "SYNTHESIZING")
   ```
   Confirms that per-piece idempotency is actually kicking in on
   retry.

These are all `logger.info` — not verbose. Ship them BEFORE the
next `/create` build. They cost nothing at rest.

**Then wait for the next OOM.** Read the log. The log tells us
exactly which of the four hypotheses in Section 2 is real. Fix
scope becomes obvious. Don't guess in advance.

---

## Section 2 — The four hypotheses to falsify with the logs

Any one of these could be why the checkpoint doesn't work. The
diagnostic logs above are designed to falsify each independently.

### H1. `stamp_edition_id` doesn't durably commit

**How to know:** log 1 shows `rows_affected=0`, OR the log is never
emitted at all (meaning the code path didn't reach the stamp), OR
the stamp fires but the payload read on retry (log 2) shows no
`edition_id` key.

**Likely cause if true:** the `with db.connect() as conn:` context
manager in `aarva/db.py` may not auto-commit on exit. Python's
sqlite3 module does NOT autocommit by default — the caller must
explicitly `conn.commit()` OR the connection wrapper must be
configured with `isolation_level=None`.

**Fix if true:** add `conn.commit()` inside `stamp_edition_id`
before exiting the `with` block. Also audit every other write path
that uses the same wrapper pattern — `update_progress`,
`mark_completed`, `mark_failed`, the raw INSERT/UPDATE in
`_run_job`, etc. Any write that isn't committing durably is a
bug waiting to bite.

### H2. `stamp_edition_id` runs AFTER step 2 but OOM kills the process BEFORE it runs

**How to know:** log 2 on retry shows no `edition_id`; log 1
(from the prior attempt) is either missing entirely from the pre-OOM
log stream or shows the stamp did happen. If the stamp DID happen
but retry still shows no edition_id, we're back to H1.

**Root cause if true:** `build_episode_script` is memory-heavy —
LLM prompts + article full-text loads + embedding math + edition
INSERTs. If the OOM lands INSIDE `build_episode_script` rather
than after it returns, no checkpoint is stamped and the retry
runs the whole thing again → OOM again → loop.

**Fix if true:** move the checkpoint stamp EARLIER in the flow.
Specifically, stamp `edition_id` INSIDE `build_episode_script`,
immediately after the `INSERT INTO editions` completes (not after
the whole function returns). This makes the checkpoint durable
even if a later part of the same function OOMs. Requires threading
a `job_id` parameter into `build_episode_script` — small refactor.

Alternative: bundle the checkpoint into `build_episode_script`'s
own transaction so it's atomic with the edition INSERT.

### H3. Checkpoint is read but the RESUME path itself re-runs setup

**How to know:** log 3 fires ("RESUMING via checkpoint") but the
retry still runs steps 1-3 in observable ways (e.g., another
`crosscut_pair_candidates` row appears, or `build_episode_script`
log lines appear).

**Root cause if true:** something in the resume branch is
accidentally still calling into setup code, OR `synthesize_
crosscut_episode` internally re-does work that should be handled
by step 2.

**Fix if true:** trace the resume branch call chain, find the
leaked setup call, remove it. Not likely if log 3 shows the branch
correctly took the skip path — but worth confirming.

### H4. TTS per-piece idempotency doesn't kick in

**How to know:** log 5 shows "SYNTHESIZING" for pieces that already
have an audio_url from the pre-OOM attempt.

**Root cause if true:** either `audio_url` was never persisted for
finished pieces (Python sqlite3 autocommit issue again — H1's cousin),
OR the per-piece skip check reads a different column than what
gets written, OR the whole `edition_pieces` set is being re-created
by the resumed run (H3 territory).

**Fix if true:** depends on the specific failure mode the log
reveals.

---

## Section 3 — Recurrent OOM (the underlying cause)

Separate from resumability, the actual OOM has been happening
despite PR #49's streaming-TTS fix. Once resumability is verified
working, the next task is to make OOMs stop happening entirely,
not just recover from them well.

**Investigate memory usage:**

Add per-step RSS snapshots to `_run_job`:

```python
import psutil, os
_rss = lambda label: logger.info("RSS at %s: %.0f MB", label, psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024)

_rss("job start")
# ... step 1 ...
_rss("after candidate insert")
# ... step 2 ...
_rss("after build_episode_script")
# ... step 3 ...
_rss("after user_id stamp")
_rss("before TTS")
# ... step 4 (TTS) ...
_rss("after TTS")
# ... step 5 (convert) ...
_rss("after ffmpeg convert")
# ... step 6 (upload) ...
_rss("after R2 upload")
```

Trigger a full build. Read the log. The largest delta between two
adjacent snapshots is the biggest consumer. Then dig into whatever
step that is.

**Known suspects worth pre-checking:**

- **`build_episode_script` loads full article text for both source
  articles into Python memory** — Gemini prompts include the
  articles as context. Each article is often 3-10 KB of text, but
  the whole prompt inflates when combined with system instructions
  and prior turns. Check whether the LLM client accumulates prompt
  history.
- **`synthesize_crosscut_episode` may load all edition_pieces + all
  their text into memory at once** before iterating. Should be
  streaming.
- **`crosscut_embeddings` write-back at end of build_episode_script**
  computes a Matryoshka-truncated embedding and stores it. The
  Gemini embedding call is stateless and shouldn't hold memory,
  but if the client accumulates connection state, that adds up.
- **`google-genai` SDK connection object** — one per client. If we
  instantiate multiple LLM clients across the build (e.g., one for
  intro, one for bridges, one for outro) rather than reusing one,
  each holds its own state.

Likely fixes will be some combination of:

- Discard prompt strings / article text right after each LLM call
- Reuse a single `genai.Client` object across the whole build
- Use `del`, `gc.collect()` at step boundaries to force reclamation
- Move to `excerpt` instead of `full_text` where the LLM only needs
  a summary
- Chunk large iterations that currently load everything in memory

Expect a small memory-diet PR, not a rewrite. Aim: reduce RSS
peak by 50-100 MB to give clear headroom under 512 MB.

---

## Recommended ordering

1. **Ship the Section 1 diagnostic logs first.** One tiny PR, no
   functional changes. Purpose: make the two probes readable.
2. **Run Probe A.** Trigger a normal `/create` build, wait 30-45s,
   check `jobs.payload_json` for `edition_id`. Takes about a
   minute total. Tells you if the stamp is working.
3. **Run Probe B if Probe A passes.** Simulate a crash locally by
   killing the server mid-TTS. Restart. Watch the log. Tells you
   if the resume path reads the checkpoint AND if per-piece TTS
   idempotency fires.
4. **Ship the targeted fix** for whichever hypothesis the probes
   surface. Section 2 spells out the fix for each.
5. **Then Section 3** (memory diet), separately. Even a fully
   working resume + fast-retry loop is a bad user experience — an
   OOM is still visible to the listener as a spinner-stall +
   delayed completion. Making OOMs not happen is the real end
   state.

**Do not attempt to fix all four hypotheses at once without data.**
That was the mistake in PR #49 — a checkpoint-based resume was
built without verifying that the underlying persistence actually
held. Probes A and B mean we don't have to wait for a production
OOM to isolate the bug.

---

## Non-goals for this session

- Do not rewrite the whole worker. The checkpoint architecture is
  sound; something small isn't sticking. Find and fix it, don't
  redesign.
- Do not switch to a background worker service on Render. That's a
  separate migration and expensive by comparison.
- Do not increase the Render instance size (Starter → Standard).
  If we can make it fit in 512 MB we should; upgrading is a
  fallback if the memory diet reveals hard limits.
- Do not attempt the "recovered listener episodes" retrospective
  audit here — that's already covered by other threads.

---

## Files likely to change

- `aarva/services/episode_worker.py` — logging additions (Section 1)
- `aarva/services/episode_jobs.py` — `stamp_edition_id` logging,
  possibly `conn.commit()` add
- `aarva/stages/stage_crosscut.py` —
  `synthesize_crosscut_episode` per-piece logging, possibly
  earlier-checkpoint stamp
- `aarva/db.py` — audit the connection wrapper's commit semantics
- Possibly `requirements.txt` — add `psutil` if not already there
  (for RSS instrumentation)

## Verification

After the fix lands:

1. Trigger a `/create` build.
2. In the middle of TTS, manually SIGKILL the worker (or trigger a
   Render redeploy, or wait for an OOM — whichever's easiest to
   simulate).
3. Watch the log on retry:
   - Log 2 must show `checkpoint_edition_id=<id>` (not None)
   - Log 3 must fire ("RESUMING via checkpoint")
   - Log 5 must show already-done pieces being SKIPPED
   - No new `editions` row should be created; the original edition
     completes.
4. If steps 1-3 all pass but the build STILL OOMs in the same
   place: Section 3 becomes urgent.

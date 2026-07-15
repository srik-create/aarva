# Session plan — move `jobs` table into the listener DB

Written by Cowork for the next Claude Code session (2026-07-15+).
Small, tightly scoped structural fix. Follow the pattern already
established by the listener-episode split (PR #55).

Read this doc + `docs/roadmap.md` + `AGENTS.md` before starting.

---

## DONE 2026-07-15

Shipped as specced, plus two things this spec didn't anticipate —
see `docs/roadmap.md`'s 2026-07-15 "Recently completed" entry for
full detail:

1. `aarva/server/routes/admin.py`'s `_find_lost_episodes` also
   queried `FROM jobs` against the main DB and wasn't in this spec's
   file list — would have broken every sync after this move. Fixed
   in the same PR.
2. `aarva/services/jobs.py` + `aarva/services/editions.py` are a
   second, unrelated job-queue module targeting a same-named `jobs`
   table — confirmed dead code (no live caller), left untouched.

Verified via a real DB-level round trip (enqueue → claim → progress
→ stamp → complete → get, plus the 24h quota check) rather than a
full Gemini/TTS build — this was a structural refactor, not new
logic, so the DB plumbing was the thing that needed proving.

---

## Context — the bug this fixes

Confirmed 2026-07-15 via Render Shell inspection. Full detail:

**Symptom:** listener's `/create` build orphaned mid-TTS after an
OOM restart. The worker's `reset_all_running_jobs` on startup
found zero rows to reset, worker sat idle, listener saw a build
that never completed.

**Cause:** the `jobs` table lives in `/data/aarva.db` — the main
DB that `scripts/sync_db_to_render.sh` atomic-replaces on every
laptop→Render sync. When a sync landed between a `/create` build
starting and the OOM restart, the friend's job row (along with
every other Render-side write to main DB since the previous sync)
was silently wiped. The worker thread continued running TTS
against its in-memory job dict, but when the OOM killed the
process, the recovery path had nothing to find.

**Evidence:**

1. `SELECT id, status FROM jobs ORDER BY id DESC LIMIT 5` on Render
   returned exactly one row — job #3 from 2026-06-29. All other
   /create jobs from the past two weeks had been wiped by
   intervening syncs.
2. `ls -l /data/aarva.db` mtime was 12:35 (matching the day's
   sync), despite listener DB clearly holding an edition
   (`#1000005 'searching for soul in ai music'`) built between
   12:35 and 12:57 today.
3. That listener edition's `edition_pieces` had `audio_url = NULL`
   for both pieces, consistent with a mid-TTS orphan.

**Same class as the 2026-07-06 listener-episode disappearing
bug.** Render-side writes lose data on every sync unless they
live in a file the sync doesn't touch.

---

## Scope — move `jobs` to `/data/aarva-listener.db`

### Decisions locked

1. **Target file: the existing listener DB.** No new file. Extends
   the pattern that already exists for listener episodes: the file
   is the "Render-only writes" surface, and sync never touches it.
2. **Table name: keep `jobs`.** No schema drift beyond the move.
3. **Schema: identical to what's currently in `aarva/db.py`.** Copy
   the CREATE TABLE statement over verbatim into
   `LISTENER_SCHEMA_SQL` in `aarva/listener_db.py`.
4. **Migration: NONE.** The one row currently in main-DB's `jobs`
   table (job #3, 2026-06-29, completed) is not worth carrying
   over. Fresh start on the listener side. All in-flight jobs
   from before this PR are already lost or completed.
5. **Renaming the file / class is out of scope.** `aarva-listener.db`
   is a slight misnomer now that it holds jobs too, but a rename
   is cosmetic churn best done separately.

### Files that must change

- **`aarva/listener_db.py`** — add the `jobs` CREATE TABLE to
  `LISTENER_SCHEMA_SQL`. Copy the exact schema from `aarva/db.py`.
- **`aarva/services/episode_jobs.py`** — every function that
  currently takes a `db: Database` argument for jobs work now
  takes `listener_db: ListenerDatabase` instead. Concretely:
  `ensure_user_for_email` — WAIT: `users` table lives in main DB
  (see below). This one stays on main DB.
  All others (`enqueue_build_job`, `claim_next_pending`,
  `mark_completed`, `mark_failed`, `update_progress`,
  `stamp_edition_id`, `get_job`, `reset_stuck_jobs`,
  `reset_all_running_jobs`) move to listener DB.
- **`aarva/services/episode_worker.py`** — all `db` references
  in the job-lifecycle path change to `listener_db`. The
  `_run_job` function ALSO reads/writes editions and edition_pieces
  from listener_db (already does). Users lookup stays on main DB.
- **`aarva/server/routes/create.py`** — `enqueue_build_job(db,...)`
  becomes `enqueue_build_job(listener_db,...)`. Same for
  `get_job(...)` calls in the status page route.
- **`aarva/server/app.py`** — ensure `app.state.listener_db` is
  used consistently; no `app.state.db` for job operations.

### Subtle: user_id foreign key

The current `jobs` table has `user_id INTEGER REFERENCES users(id)`.
`users` lives in the main DB. When we move `jobs` to the listener
DB, we lose the FK relationship (SQLite doesn't do cross-database
FKs). Two acceptable resolutions:

- **(a) Drop the FK** in the listener DB's `jobs` schema. Keep
  `user_id INTEGER` as a plain column with no reference. Same
  pattern as `edition_pieces.article_id` in the listener DB —
  denormalized cross-DB references are fine here; we accept the
  application enforces consistency.
- (b) Move `users` to the listener DB too. Bigger change, more
  code to update. Not worth it for one FK.

Go with **(a)**. Add a code comment near the `user_id` column
explaining that it references `users.id` in the MAIN db and
integrity is application-level, not DB-level.

### The `ensure_user_for_email` function

This one function in `episode_jobs.py` writes to the `users` table
in the main DB — that's separate from the jobs table and shouldn't
move. Leave it operating against `db` (main). All the other
functions in the file that operate on `jobs` switch to
`listener_db`.

Concretely, function signatures change like:

```python
# Before:
def enqueue_build_job(db: Database, *, prompt, ..., user_id) -> int:

# After:
def enqueue_build_job(
    db: Database,               # for ensure_user_for_email (main DB)
    listener_db: ListenerDatabase,   # for the jobs INSERT
    *, prompt, ..., user_id,
) -> int:
    user_id = ensure_user_for_email(db, requester_email)   # main DB
    # ... quota check reads jobs from listener_db ...
    # ... INSERT INTO jobs goes to listener_db ...
```

Or an even simpler shape: pass `listener_db` only, and have
`enqueue_build_job` accept `user_id` (already resolved by the
caller) rather than looking it up itself. Caller does the
`ensure_user_for_email` in the main DB before calling
`enqueue_build_job`. That way `episode_jobs.py` only ever touches
the listener DB. Cleaner separation.

**Recommend the second shape.** Push `ensure_user_for_email` up
to `create.py`, keep `episode_jobs.py` purely listener-DB-facing.

### Sync endpoint

`aarva/server/routes/admin.py::admin_sync_db` needs NO change. It
still atomic-replaces `/data/aarva.db` (main DB). The listener DB
at `/data/aarva-listener.db` isn't touched by that endpoint. That's
the whole point of the split — sync stays a dumb full-replace on
the main DB, and Render-authored data lives in the file that sync
ignores.

## Verification

1. Trigger a `/create` build on production (or locally). Confirm
   the new job row appears in `/data/aarva-listener.db` NOT
   `/data/aarva.db`:

   ```bash
   python3 -c "
   import sqlite3
   for db in ('/data/aarva.db', '/data/aarva-listener.db'):
       c = sqlite3.connect(db)
       try:
           n = c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
           print(f'{db}: {n} jobs')
       except sqlite3.OperationalError:
           print(f'{db}: no jobs table')
   "
   ```

   Expected: main DB says "no jobs table" (after this PR); listener
   DB shows N ≥ 1 jobs.
2. Run `bash scripts/sync_db_to_render.sh` from the laptop. Then
   re-run the count query. Expected: listener DB job count
   unchanged; main DB still has no jobs table (since we removed
   the table from `aarva/db.py`'s CREATE statements — see below).
3. Force a container restart via Render → Manual Deploy. Confirm:
   `reset_all_running_jobs` on the LISTENER DB fires and logs the
   count. Any orphaned jobs get flipped back to `pending`.
4. Manual kill test (see the earlier session plan
   `session_plan_worker_resumability.md` Probe B): trigger a
   build, kill the process mid-TTS, restart, watch the worker
   claim the job and resume.

### Cleanup on the main DB side

Once jobs moves to listener DB, the `jobs` CREATE TABLE in
`aarva/db.py` becomes dead code. **Remove it** so future readers
don't get confused about which DB owns the table. On startup, if
main DB still has a `jobs` table from before this PR, it doesn't
hurt to leave it — no reader references it after this change.
Optional: add a one-off cleanup that drops it. Not required for
correctness.

---

## Sequencing

Single PR, doable in one Claude Code sitting:

1. Copy `jobs` CREATE TABLE from `aarva/db.py` → `aarva/listener_db.py`.
2. Drop it from `aarva/db.py`.
3. Update `episode_jobs.py` to take `listener_db` everywhere except
   `ensure_user_for_email` (which stays on main DB, or moves to
   the caller). Preferred: move `ensure_user_for_email` out to
   `create.py` so `episode_jobs.py` is purely listener-DB-facing.
4. Update `episode_worker.py` `_run_job` and `start_worker` to
   pass `listener_db` where they previously passed `db`.
5. Update `create.py`'s enqueue + status routes.
6. Verify all four probes above.

No data migration needed. No schema change to the row shape.

---

## Non-goals

- Do NOT rename `aarva-listener.db` in this PR. It's now a slight
  misnomer, but the rename churn (file path, env var,
  documentation) is a separate cosmetic pass.
- Do NOT move `users` to the listener DB. It's laptop-authored
  (well, `ensure_user_for_email` writes it — small caveat, see
  below).
- Do NOT change the `admin_sync_db` endpoint. Sync stays a full
  atomic-replace of the main DB; the listener DB is deliberately
  never touched by sync.
- Do NOT try to migrate job #3 (the single surviving row in
  `/data/aarva.db`). Completed job, no value.
- Do NOT rework the worker resumability logic. That's a separate
  thread and out of scope here.

### `ensure_user_for_email` caveat

This IS a Render-side write to the main DB — every `/create`
submission upserts the requester's users row. Which technically
means the `users` table has the same sync-wipes-data problem as
the old `jobs` table. However, at v1 this is fine because:

- Users rows are re-created on next submission from the same email
  (idempotent upsert)
- No downstream data references users except via user_id — and
  user_id in the moved-to-listener `jobs` table now becomes a
  denormalized cross-DB reference (see "Subtle: user_id foreign
  key" above)
- Fixing users properly = "move users to listener DB too" =
  bigger PR

If listener-request patterns start to include per-user history
that survives across syncs, revisit. For now, accept the
imperfection.

---

## What Cowork owes if this spec has gaps

Same rule as previous session plans: if Claude Code finds a real
ambiguity — the schema move breaks something we didn't anticipate,
the `ensure_user_for_email` refactor is messier than expected —
punt back to Cowork with the specific question. Don't guess.

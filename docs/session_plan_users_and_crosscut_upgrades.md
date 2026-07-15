# Session plan — users persistence + crosscut divergent-view tier + region-specific piece voices

Written by Cowork for the next Claude Code session (2026-07-15+).
Three orthogonal enhancements — each can ship as its own PR in
whatever order works. All three build on the pattern already
established by the listener-DB split (PR #55) and the jobs-to-
listener-DB move (2026-07-15).

Read this doc + `docs/roadmap.md` + `AGENTS.md` before starting.

---

## Section 1 — Move `users` (and `user_sessions`) to listener DB

### Goal

Every `/create` request currently upserts a `users` row via
`ensure_user_for_email`. Same bug class as jobs: `users` lives in
`/data/aarva.db`, which the daily sync atomic-replaces. So user
rows created on Render between syncs get wiped, and any listener
who submitted once and never came back is already gone.

User's ask (2026-07-15): "make sure we store email addresses for
every /create request, so we have that as a database of users."
Plumbing already captures — the durability side is the fix.

### Decisions locked

1. **Target file: existing `/data/aarva-listener.db`.** Same
   argument as jobs — this file is the "Render-only writes"
   surface that sync doesn't touch.
2. **Move both `users` AND `user_sessions`.** `user_sessions` has a
   FK to `users.id`; move together to preserve integrity.
   (If `user_sessions` isn't populated yet — no auth flow live —
   the move is still cheap and forward-compatible.)
3. **Schema: verbatim copy** from `aarva/db.py` into
   `LISTENER_SCHEMA_SQL` in `aarva/listener_db.py`. No column
   changes.
4. **Migration: NONE.** Any users on main DB right now will
   silently disappear on the next sync anyway. Fresh start on
   listener side. `INSERT OR IGNORE` on email means any returning
   listener re-populates their row cleanly.
5. **The `edition_pieces.user_id` / `editions.user_id` /
   `jobs.user_id` columns already live in the listener DB.** All
   three tables moved to the listener DB in prior PRs (listener-
   episode split on 2026-07-06, jobs on 2026-07-15). Their
   `user_id` columns are currently plain `INTEGER` with no FK
   (that's the "no cross-DB FK" pattern we already established).
   Once `users` moves alongside them in this PR, they'll all be
   in the same DB — but keep them as plain `INTEGER` for
   simplicity. Do NOT add an FK back just because the tables now
   sit together; the "no FKs across the listener/main split"
   rule stays. Optional: add a comment on each column noting
   that referential integrity is application-enforced.

### Files that must change

- **`aarva/listener_db.py`** — add `users` + `user_sessions`
  CREATE TABLE statements to `LISTENER_SCHEMA_SQL`. Verbatim copy
  from `aarva/db.py`.
- **`aarva/db.py`** — remove the `users` + `user_sessions` CREATE
  statements after the move is verified. Optional: leave the
  tables dropped from main DB (the sync will bring across an
  empty schema; nothing reads users from main DB after this PR).
- **`aarva/services/episode_jobs.py::ensure_user_for_email`** —
  change signature from `(db, email)` to `(listener_db, email)`.
  Everything else in this function stays identical.
- **`aarva/server/routes/create.py`** — the call to
  `ensure_user_for_email` now passes `listener_db` (from
  `request.app.state.listener_db`) instead of `db`.
- **Grep for `FROM users`, `INTO users`, `REFERENCES users`** across
  the codebase. Any hit outside these files is a callsite that
  needs updating.

### The prospective auth flow

There's a `user_sessions` table in the schema that isn't populated
today (no login flow yet). This move is forward-compatible with
any eventual magic-link auth: sessions live alongside users, both
on the file that survives syncs. When auth lands, no schema move
required.

### Verification

1. Trigger a `/create` build. Confirm the users row lands in
   listener DB, not main DB:
   ```bash
   python3 -c "
   import sqlite3
   for db in ('/data/aarva.db', '/data/aarva-listener.db'):
       c = sqlite3.connect(db)
       try:
           n = c.execute('SELECT COUNT(*) FROM users').fetchone()[0]
           print(f'{db}: {n} users')
       except sqlite3.OperationalError:
           print(f'{db}: no users table')
   "
   ```
2. Run `bash scripts/sync_db_to_render.sh` from the laptop. Re-run
   the count. Listener-DB count should be unchanged.
3. Submit a second /create from the same email. Confirm no
   duplicate row (INSERT OR IGNORE on UNIQUE email).

---

## Section 2 — Crosscut divergent-view tier

### Goal

Current crosscut pair selection scores pairs by topical similarity
(0.45-0.85 band) + structural divergence (lens/pillar/JTBD/
fingerprint axis differences). "Divergence" today = different
angle, not different stance.

User wants a NEW tier layered ABOVE the current logic: first try
to find pairs that hold **divergent views on the same topic**
(different stances, not just different angles). If ≥1 such pair
exists, mix N divergent + M current-logic in the longlist. If no
divergent pairs exist, fall back to current logic only.

Editorial voice unchanged — the intro/bridge/outro still leave
listeners with a question, never a verdict on which view is
"right."

### Decisions locked

1. **Stance detection: additional LLM call per candidate pair.**
   Small Gemini prompt: "Do these two articles argue different
   sides of the same question, or offer different angles on the
   same topic? Return one of: OPPOSING_VIEWS, DIFFERENT_ANGLES."
   Cost: ~$0.001 per pair. Acceptable per user 2026-07-15.
2. **Where in the pipeline this fires.** After the current
   pre-scoring pass (top-30 candidates by structural divergence
   ranked by combined score), each of those 30 gets the stance
   classification LLM call. Pairs marked OPPOSING_VIEWS are
   promoted to a "divergent" bucket; the rest stay in the
   "current-logic" bucket. Both buckets then get the existing
   connection-eval LLM (0-10 quality score); longlist is
   assembled by mixing.
3. **Mixing ratio.** Split the 10-slot longlist 60/40 in favour of
   divergent pairs when available. So if ≥6 divergent pairs pass
   quality-eval, take top 6 divergent + top 4 current-logic. If
   fewer divergent, take all of them + fill the rest from
   current-logic. If zero divergent, fall through to current
   logic only. **"Top" within each bucket means highest-scoring
   by the existing connection-eval 0-10 quality score** —
   don't invent a new ranking metric for the divergent bucket.
   The stance classifier tags pairs; the connection-eval scores
   them; the tag decides which bucket, the score decides order
   within the bucket.
4. **Editorial voice unchanged.** No prompt changes to
   intro / bridge / outro. They keep the "leave the listener with
   a question" formulation established in Section 1 of
   `session_plan_content_quality.md`. Even for divergent-view
   pairs, the copy names the disagreement without endorsing
   either side.
5. **User-visible signal.** The candidate cards on `/create`
   don't need to flag which tier they came from. It's an
   editorial-quality improvement, not a UX-differentiation one.
6. **Fallback signaling in the logs.** Log lines make the tier
   explicit: `"crosscut pair-select: divergent tier found N pairs,
   filled longlist 6/4 (divergent/current-logic)"` OR `"crosscut
   pair-select: no divergent pairs found, using current-logic
   only"`. Helps future debugging.

### Files that must change

- **`aarva/stages/stage_crosscut.py`** — the main pair-detection
  path. Add a new function `_classify_pair_stance` that takes
  two articles + LLM client and returns "OPPOSING_VIEWS" /
  "DIFFERENT_ANGLES" (default to DIFFERENT_ANGLES on any parse
  error). Integrate into the post-pre-score, pre-connection-eval
  path.
- **`aarva/prompts.yaml`** (or wherever the stance-classification
  prompt lands) — new prompt for stance detection. Follow the
  same voice standard as elsewhere (Section 1 of the
  content-quality spec) if the prompt has any voice at all —
  though this one's an internal classifier, so plain instruction
  is fine.
- **`aarva/stages/stage_crosscut.py`** again — the longlist-
  assembly step needs the 60/40-with-fallback mixing logic.

### The stance-classification prompt

Rough shape (adjust for tone/style after web-verifying current
Gemini prompt-format best practices per AGENTS.md rule 6):

```
You classify pairs of journalism articles by their relationship.

Return ONE label:

- OPPOSING_VIEWS: The two articles argue different sides of the
  SAME question. Not just different angles or different subjects
  — the two authors would meaningfully disagree if they met.
  Example: one piece arguing carbon capture is essential to hit
  climate targets; another arguing it's a fossil-fuel-industry
  greenwash. Same question, opposing conclusions.
- DIFFERENT_ANGLES: The articles are about the same topic but
  come at it from complementary or non-overlapping angles —
  they'd nod at each other, not argue. Example: one piece on the
  economics of AI training; another on its cultural impact. Same
  topic, different lenses.

Article A:
Title: {title_a}
Publication: {pub_a}
Excerpt: {excerpt_a[:1500]}

Article B:
Title: {title_b}
Publication: {pub_b}
Excerpt: {excerpt_b[:1500]}

Label:
```

Excerpt clip at 1500 chars: enough to convey the argument's shape
without blowing out the context window. Adjust up if the classifier
consistently mis-classifies short-context.

### Cost math

Current: 30 pre-scored candidates → 30 connection-eval LLM calls.
After: 30 stance classifications + 30 connection-evals = 60 calls
per `/create` build. At Gemini 3 Flash pricing (~$0.001/call for
short prompts), the extra cost is roughly $0.03 per build. Cheap.

Rate-limit safety: 30 additional calls per build spread over ~30
seconds. Well under the Vertex AI RPM ceiling for the project.

### Verification

1. Run a `/create` build for a prompt where you know both a
   divergent-view pair and a same-angle pair exist in the pool
   (e.g. "AI safety debates" — there are both sides). Watch the
   log: expect "divergent tier found N pairs, filled longlist
   X/Y."
2. Inspect the longlist ordering — top divergent pairs should
   appear before current-logic pairs on the candidate cards.
3. Run a build for an evergreen prompt where no divergent pairs
   are likely (e.g. "quietly thoughtful nature writing"). Watch
   the log: expect "no divergent pairs found, using current-logic
   only." Longlist should still populate normally.
4. Manually inspect 2-3 pairs labelled OPPOSING_VIEWS — do they
   actually oppose, or is the classifier over-eager? Tune prompt
   if needed.

---

## Section 3 — Region-specific voices for crosscut pieces

### Goal

Daily-edition articles already use region-specific narrator
accents based on the source publication's `country` tag
(configured in `publications.yaml`, applied via Stage 9's
`_accent_prompt_for`). Crosscut episodes currently use fixed
voices for the two article pieces (`voice_a`, `voice_b` from
`tts.crosscut_voices` config) — no per-publication accent.

User wants the same regional-voice treatment for crosscut piece
narration.

### Decisions locked

1. **Scope: piece_a and piece_b only.** The intro, bridges, and
   outro stay in the neutral Aarva editorial voice (still using
   the `host` voice from `tts.crosscut_voices`). Only the two
   article-piece slots gain per-publication accent steering.
2. **Fallback identical to Stage 9's article narration.**
   Publications without a `country` tag get no accent steer —
   they use the default `voice_a` / `voice_b` as today.
3. **Mechanism: pass an `extra_style` string** to the TTS client
   (same hook Stage 9's `_accent_prompt_for` uses). Nothing new
   in the TTS client's interface — this feature is already there,
   just not wired to crosscut piece TTS.
4. **Voice selection unchanged.** The BASE voice for piece_a
   remains `voice_a`; the accent steer is layered on top via
   `extra_style`. Same for piece_b.

### Files that must change

- **`aarva/stages/stage_crosscut.py::synthesize_crosscut_episode`**
  — currently calls `tts.synthesize(...)` for each section
  (intro, bridge_a, piece_a, bridge_between, piece_b, bridge_b,
  outro). For the piece_a and piece_b sections only:
  1. Look up the piece's `publication_name` (already in the
     `payload["piece_a"]` / `payload["piece_b"]` dict).
  2. Call `_accent_prompt_for(piece_dict, country_map)` from
     `aarva.stages.stage_9_tts` (import at top of file).
  3. Pass the returned string as `extra_style=` on the
     `tts.synthesize()` call. `None` means no accent steer —
     current behaviour preserved for publications without a
     country tag.
- The `_accent_prompt_for` function is already reusable — no
  changes needed inside it.
- **`aarva/config/publications.yaml`** — no change. The `country`
  tags already exist for the publications that have them; this
  PR just teaches the crosscut side to read them.

### Verification

1. Build a crosscut with two pieces from tagged publications —
   e.g. one from The Hindu (country: india) and one from The
   Atlantic (country: us). Listen. Piece_a should have Indian-
   English accent; piece_b should have American-English. Intro/
   bridges/outro should be neutral Aarva.
2. Build a crosscut with one tagged publication and one untagged.
   The tagged piece gets its accent; the untagged piece uses
   default voice_b with no steer.
3. Check TTS log lines — `Crosscut TTS: synthesizing piece_a
   (~N chars, voice=X)` should include the accent style when
   applied.

---

## Sequencing

Three PRs, in whatever order Claude Code prefers. Dependencies:

- **Section 1 (users move)** — independent, self-contained.
- **Section 2 (divergent-view tier)** — independent, self-
  contained. Doesn't touch listener DB.
- **Section 3 (regional crosscut voices)** — independent, self-
  contained. Touches only TTS wiring in stage_crosscut.

Recommended order:

1. **Section 1 first.** Same class as the jobs-to-listener-DB
   move that just shipped 2026-07-15 — closes out the "Render-
   writes-to-main-DB" bug class before it bites for a third time
   in some other table.
2. **Section 2 second.** Bigger editorial impact than Section 3;
   Claude Code will need thought-time on the classifier prompt.
3. **Section 3 last.** Smallest of the three. Almost mechanical.

But nothing strictly forces this order — pick whichever's easiest
for context load.

---

## Non-goals

- **Do not rename `aarva-listener.db`.** It now holds three tables
  that aren't listener-episode-related (jobs, users, user_sessions).
  Rename is churny; leave it.
- **Do not introduce a login flow.** The `user_sessions` table
  moves for forward-compat but stays empty. Magic-link login is a
  separate future thread.
- **Do not touch the current crosscut-selection scoring
  weights** for the current-logic tier. The divergent-view tier
  is layered ABOVE the existing scoring, not a rewrite of it.
- **Do not add analytics on stance-classifier accuracy** in this
  PR. First get it working; measure later.
- **Do not add UI badges** on candidate cards indicating "this
  came from the divergent tier." Editorial quality is invisible
  to the listener by design.

---

## What Cowork owes if this spec has gaps

Same rule as previous session plans: if Claude Code finds a real
ambiguity — the stance classifier is inconsistent, the users move
turns up an unexpected FK, the `_accent_prompt_for` isn't as
reusable as this spec assumes — punt back to Cowork with the
specific question. Don't guess.

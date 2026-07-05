# Session plan — split listener-created episodes into a separate DB

Written 2026-07-03 at the end of a Cowork-mode session; work planned
for the first Claude Code session (2026-07-04+).

## Problem being solved

The `/create` build worker on Render inserts `editions` rows (plus
`edition_pieces` and `crosscut_embeddings`) directly into Render's
`/data/aarva.db`. The daily-run sync from laptop → Render does a full
atomic-replace of `/data/aarva.db` from the laptop's snapshot, so every
listener-created episode built on Render since the last sync gets
wiped by the next sync. Observed: `/listener-created` on aarva.app
shows only 1 of 4 recently-built listener episodes; the other three
were wiped by an intervening `scripts/sync_db_to_render.sh` run.

## Decisions (already made)

1. **Separation strategy.** Listener-created episodes live in a
   dedicated SQLite file `/data/aarva-listener.db` on Render's
   persistent disk. `scripts/sync_db_to_render.sh` never touches
   this file. The main `/data/aarva.db` stays a single-writer replica
   of the laptop's DB (daily pipeline is the only writer).
2. **Article metadata: denormalize.** The two articles a listener
   episode references are already in the main DB's `articles` table
   at build time. We copy the fields the `/listener-created` page and
   the `/crosscut/<id>` detail page display — article title,
   publication name, byline — onto the listener DB's `edition_pieces`
   rows at build time. No cross-DB reads for display.
3. **Search matching: include listener episodes.** `/create`'s
   existing-match candidate flow reads `crosscut_embeddings` from BOTH
   DBs. Use `ATTACH DATABASE` on the read side; scoring is unchanged.
4. **Age gate for news-y prompts.** Behind-the-news and
   future-gazing prompts should only match episodes ≤ 6 days old
   (same as Stage 7's `lens_card_behind` / `lens_card_future`
   `max_age_days=6`). Evergreen prompts don't get a date filter.
   Requires classifying the prompt at search time via a small Gemini
   call — decided this is worth the ~200ms + fractions-of-a-cent
   cost over losing evergreen matching (the alternative we rejected).

## Constraints

- Listener-created episodes MUST NOT appear on `/today`, `/crosscuts`,
  `/categories`, the RSS feed, or anywhere else. Only:
  - `/listener-created` (browse page)
  - `/crosscut/<id>` (detail page, when the id is a listener episode)
  - `/create` results (as "Listen now" existing-match candidates)
- Never commit or push without explicit user sign-off (AGENTS.md 20).
- Branch + PR workflow, one concept per commit (AGENTS.md 21).
- Web-verify anything post-training before drafting runbook content
  (AGENTS.md 6/6a).

## The work — three commits, one PR: `listener-db-split`

### Commit 1: create + wire the listener DB

**New DB file.** Path `/data/aarva-listener.db` in production
(`AARVA_LISTENER_DB_PATH` env var, defaulting to
`aarva/data/aarva-listener.db` on the laptop for local dev).

**Schema.** Mirror the main DB's `editions`, `edition_pieces`, and
`crosscut_embeddings` tables 1:1, PLUS three denormalized columns on
`edition_pieces`:

```sql
ALTER TABLE edition_pieces ADD COLUMN article_title TEXT;
ALTER TABLE edition_pieces ADD COLUMN article_publication TEXT;
ALTER TABLE edition_pieces ADD COLUMN article_byline TEXT;
```

Bootstrap the file + tables on first server startup — extend
`aarva/db.py` (or introduce `aarva/listener_db.py`) with a
`Database` initializer that runs the CREATE TABLE IF NOT EXISTS
statements when the file doesn't exist. Same connection pattern as
the main DB.

**Writes.** `aarva/services/episode_worker.py::_run_job` currently
writes editions/edition_pieces/crosscut_embeddings via
`build_episode_script` + inline SQL to the main DB. Route those
writes to the listener DB when `payload.user_id` is set (which it
always is for /create-driven builds). Concretely:

- Add a `listener_db` state on `app.state` mirroring `db`.
- `build_episode_script` needs a `target_db` parameter that defaults
  to the main DB; when called from the /create worker, pass the
  listener DB. (Same trick for `synthesize_crosscut_episode`, and
  for the `crosscut_embeddings` insert.)
- At the point where we would look up the two articles' titles /
  publication / byline to denormalize, we're still reading from the
  main DB (articles never move) — copy those three fields into the
  listener DB's edition_pieces rows.

**Reads on `/listener-created`.** `load_crosscut_episodes` in
`aarva/services/queries.py` is the current read. Add a
`source_db=...` argument or a sibling function `load_listener_
crosscut_episodes` that queries the listener DB and returns the same
shape, with title/publication/byline coming from the denormalized
columns rather than a join to `articles`.

**Reads on `/crosscut/<id>`.** The detail page currently queries the
main DB. It needs to try the listener DB when the id isn't in the
main DB (or use a routing key — e.g. listener episode ids are
allocated in a distinct range, or we add a small dispatch). Simplest:
try main first, then listener, return whichever finds a match.

**404 the other surfaces.** No code change usually needed —
`load_crosscut_episodes` on the main DB will naturally not return
listener episodes because they're not in it. Verify `/today`,
`/crosscuts`, `/categories`, RSS-feed generator, category pages
don't ATTACH the listener DB.

### Commit 2: existing-match search reads both DBs

`aarva/services/episode_candidates.py::_load_crosscut_vectors` and
its caller `_existing_matches` currently query the main DB's
`crosscut_embeddings`. Extend to also query the listener DB's
`crosscut_embeddings` and merge results.

Cleanest: `ATTACH DATABASE '/data/aarva-listener.db' AS listener` at
the top of the read, then `UNION ALL` the two `crosscut_embeddings`
tables. Alternatively, do two separate queries and merge in Python;
same result, slightly less clever.

Same `embedding_model = ?` filter applies (per the model-name
convention already in use — `gemini-embedding-001-768`).

For the eventual `Candidate` construction, the listener episodes need
`edition_id`, `topic_label`, `intro_text` — all present in listener
DB. Set `kind='existing'` and `edition_id=<listener_id>`; the
`/crosscut/<id>` route (post-Commit-1) handles the listener-DB
lookup.

### Commit 3: prompt classification + 6-day gate

**Classifier.** Add `aarva/services/prompt_classifier.py` — one
function `classify_prompt(prompt: str, llm: LLMClient) -> str`
returning one of `{'behind_the_news', 'future_gazing', 'evergreen'}`.
Uses a small Gemini call with a tight system prompt. Web-fetch
current AI Studio embed-content / generateContent docs before
finalizing the prompt shape.

Prompt sketch (verify against docs before use):

```
Classify this listener prompt into ONE of these categories.
Return only the category name.

behind_the_news  — the prompt asks about current or recent events,
                   or the meaning behind a news story from the last
                   week (elections, wars, breaking scientific
                   announcements, etc.).
future_gazing    — the prompt asks about coming changes,
                   speculation, or forward-looking analysis
                   (e.g. "what's next for AI regulation", "where
                   is the crypto market heading").
evergreen        — timeless questions, patterns, ideas
                   (e.g. "how belief forms", "why we love myth").

Prompt: {prompt}
Category:
```

**Gate in `_existing_matches`.** After classifying, if the class is
`behind_the_news` or `future_gazing`, add
`AND edition_date >= date('now', '-6 days')` to the crosscut query
(both main + listener). Evergreen prompts get no date filter.

Config: expose `search.max_age_days_news = 6` in pipeline.yaml so
this doesn't hard-code.

## Verification (do these before opening the PR)

1. Build 2 test episodes via `/create` on a local dev instance:
   one where you know the topic is news-y ("what's happening with
   AI safety regulation"), one that's evergreen ("what shapes
   personal identity"). Confirm both appear on `/listener-created`.
2. Run `bash scripts/sync_db_to_render.sh`. Confirm both episodes
   still appear on `/listener-created` after the sync.
3. On `/create`, search for a news-y prompt matching the news-y
   episode: appears as existing-match candidate. Search for the
   same news-y prompt 7 days later (or force by editing the
   listener episode's `edition_date`): does NOT appear.
4. Search for an evergreen prompt matching the evergreen episode:
   appears regardless of age.

## What's NOT in scope

- Retroactive recovery of the wiped listener episodes. They're gone
  (audio might still be on R2 but the DB rows are lost).
- Moving other on-Render writes to a separate DB. Only listener
  episodes are affected.
- Postgres migration. Deliberate: the split DB approach buys us
  another year at Aarva's volume without changing infra.

## Other threads for the same session

Independent of the listener-DB split; scoped as separate PRs so
each one can land on its own merits. Ordered by likely effort.

### Thread A — iPhone player: pause-on-navigation

**Symptom.** On aarva.app on iPhone Safari, the audio player pauses
whenever the listener navigates to another page (e.g. tapping a
category link while an article is playing). Same issue on Android
Chrome — expected behavior for a multi-page app: each `<a href>`
click is a full page load, so the `<audio>` element in the current
page is destroyed. iOS Safari additionally blocks any programmatic
`.play()` on the next page without a user gesture, so
sessionStorage-restore + auto-resume doesn't work seamlessly.

**Real fix.** HTMX-style partial navigation: swap only the main
content area on nav, keep the shared player DOM intact so the
audio element survives. Base template already scaffolds the shared
player via `data-track-*` attributes; extending it to intercept
nav clicks and do an HTMX (or fetch + DOM-diff) swap is where the
work is.

**Alternative if HTMX feels too heavy.** Persist playback state
(track src, current time, playing/paused) to sessionStorage before
unload; on the next page, restore the state and show a small
"Resume playing" button that the user taps to continue. Not
seamless — iOS Safari blocks any auto-play — but it does mean the
listener doesn't lose their position. ~30-45 min of work.

Recommendation for the session: try HTMX first. If it turns out to
need more than a small day's work, fall back to the sessionStorage
approach as a stopgap.

**Files likely to change:**
- `aarva/server/templates/base.html` — shared player scaffold,
  add HTMX (or fetch-driven) nav interception
- Any `<a href>` that should navigate rather than full-page-reload
  (`_layout` templates, nav bar, category cards, article cards)

### Thread B — Task #18: Stage 10 loud failure

Stage 10 currently catches `except Exception` around the R2 upload
step in `aarva/daily.py` L365 and only logs a warning. On
2026-07-03 this caused a silent gap: `AARVA_R2_ACCESS_KEY_ID`
wasn't in the shell env when the daily ran (post-reboot,
credentials not yet in `~/.aarva.env`), `upload_all_pending` raised,
the exception was swallowed, RSS shipped pointing at MP3s that
were never uploaded, and Apple/YouTube/aarva.app all failed to
play until a manual `--stage 10` re-run.

Decision to make (bring up in the session):
1. If `tts.r2.enabled=true` and the upload step raises, exit
   non-zero AND skip the RSS-write step. Prevents the feed from
   drifting from truth.
2. Same but keep writing the RSS, just exit non-zero at the very
   end so an operator monitoring exit codes / cron catches it.
3. Print a loud banner + non-zero exit, but let RSS write. (What
   the current warning tries to do, made unmissable.)
4. Leave as-is.

My guess is (1) — feed drift is worse than a delayed publish, and
the operator will re-run when they see the failure. But it's your
call. Small change either way.

**Files likely to change:**
- `aarva/daily.py` — the `except Exception` block in Stage 10
- `aarva/config/pipeline.yaml` — possibly gate behaviour on
  `tts.r2.enabled` explicitly if we want the strict semantic

### Cross-cutting note

All three threads (listener-DB split, iPhone player nav, Stage 10
loud failure) are independent — no PR blocks another. Session
order is your call; my instinct is Thread A first (highest
listener-visible impact), then the listener-DB split, then Thread
B as a wind-down.

## Files likely to change

- `aarva/db.py` (or new `aarva/listener_db.py`) — listener DB init
- `aarva/server/app.py` — attach `listener_db` to `app.state`
- `aarva/server/routes/create.py` — no change probably; wiring goes
  through the worker
- `aarva/services/episode_worker.py::_run_job` — route writes
- `aarva/services/queries.py::load_crosscut_episodes` — read routing
- `aarva/services/episode_candidates.py::_load_crosscut_vectors` +
  `_existing_matches` — dual-DB read + age gate
- `aarva/services/prompt_classifier.py` (new)
- `aarva/stages/stage_crosscut.py::build_episode_script` +
  `synthesize_crosscut_episode` — `target_db` parameter
- `aarva/server/routes/crosscut.py` (wherever `/crosscut/<id>`
  lives) — main-first, listener-fallback lookup
- `aarva/config/pipeline.yaml` — `search.max_age_days_news`
- `docs/project_brief.md` — decision-log row for the split
- `AGENTS.md` — no change expected

## Do NOT change

- The RSS feed generator. Listener episodes must not appear there.
- Sync logic (`scripts/sync_db_to_render.sh`,
  `aarva/server/routes/admin.py::admin_sync_db`). Sync stays a
  simple atomic replace of the main DB.
- Any editorial pipeline stage (Stage 1 through Stage 10) except
  where noted for stage_crosscut.

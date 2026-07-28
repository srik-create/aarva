# Session plan — add ad-hoc extra items to the podcast RSS feed

**STATUS: DONE (2026-07-28).** Implemented as specced, plus one real
bug found and fixed along the way: `_audio_full_url` wasn't actually
a no-op for absolute URLs as this spec assumed — needed for
`rss_add`'s fully-manual mode. See `docs/roadmap.md`'s 2026-07-28
"Recently completed" entry for full details and verification.

Written by Cowork for the next Claude Code session (2026-07-28+).
Follow-up to the promote-bonus admin endpoints that shipped
2026-07-27 (`docs/session_plan_promote_listener_created_as_bonus.md`,
STATUS: DONE) — those surfaced listener-created crosscuts as an
"Also today" section on the `/today` web page only. The podcast RSS
still can't include them, and there's no interface for the operator
to editorially add ANY new episode (listener-created or otherwise) to
the feed. This spec closes that gap with the smallest possible layer:
one new table, one new CLI, one new render loop. Zero website
changes.

Read this doc + `docs/roadmap.md` + `AGENTS.md` +
`docs/project_brief.md` before starting.

**AGENTS.md rule 4 sign-off**: this adds items to the listener-facing
podcast RSS feed — a listener-facing distribution surface, so it
qualifies as an editorial-behaviour change and needed pre-approval.
User approved direction 2026-07-28 in the conversation that produced
this doc. Locked calls below.

---

## Architecture check (rule 17d)

Grep outputs referenced below are from 2026-07-28.

**Reference greps:**

```
$ grep -nE "^def generate_feed|^def _load_all_published_pieces|^def _load_published_crosscuts|^def _item_xml|^def _crosscut_item_xml|^def _audio_byte_length" aarva/output/rss_feed.py
41:def _load_all_published_pieces(db: Database) -> list[dict]:
64:def _load_published_crosscuts(db: Database) -> list[dict]:
193:def _audio_byte_length(audio_url: str, package_root: Path) -> int:
211:def _item_xml(piece: dict, public_url_base: str, package_root: Path,
293:def _crosscut_item_xml(cc: dict, public_url_base: str, package_root: Path,
400:def generate_feed(

$ grep -nE "^def _check_token|@app\.(get|post).*admin" aarva/server/routes/admin.py | head -8
68:def _check_token(request: Request) -> None:
210:@app.post("/admin/sync-db")
351:@app.get("/admin/diagnose-lost-episodes")
430:@app.post("/admin/promote-bonus")
500:@app.post("/admin/unpromote-bonus")

$ grep -nE "^def _load_listener_crosscut_for_promotion" aarva/server/routes/admin.py
379:def _load_listener_crosscut_for_promotion(db, listener_db, edition_id: int) -> dict:

$ grep -nE "CREATE TABLE" aarva/db.py | head -15
21:CREATE TABLE IF NOT EXISTS articles (
50:CREATE TABLE IF NOT EXISTS publications (
116:CREATE TABLE IF NOT EXISTS editions (
158:CREATE TABLE IF NOT EXISTS edition_pieces (
199:CREATE TABLE IF NOT EXISTS edition_rejections (
222:CREATE TABLE IF NOT EXISTS crosscut_pair_candidates (
270:CREATE TABLE IF NOT EXISTS crosscut_embeddings (
300:CREATE TABLE IF NOT EXISTS daily_bonus_features (
312:CREATE TABLE IF NOT EXISTS pipeline_runs (

$ ls aarva/*.py | grep -iE "search|ingest_url|publish|review|crosscut"
aarva/crosscut.py
aarva/ingest_url.py
aarva/publish_articles.py
aarva/review.py
aarva/search.py
```

**Now the three questions:**

1. **Where does the data live?**
   - **RSS feed source(s) today**: the main DB. `generate_feed`
     (`aarva/output/rss_feed.py:400`) loads pieces via
     `_load_all_published_pieces` (`:41`) — daily edition_pieces +
     bonus (`edition_type='bonus'`) — and crosscuts via
     `_load_published_crosscuts` (`:64`), both taking a single
     `Database` (main DB) argument. Listener-created crosscuts
     (edition IDs ≥ 1,000,000 per `aarva/listener_db.py`) live on
     listener_db on Render and are invisible to the current RSS
     path by design (`load_crosscut_episodes` docstring at
     `aarva/services/queries.py:161-166`).
   - **New table** — `rss_extra_items` on the main DB, alongside
     `daily_bonus_features` (`aarva/db.py:300`). Rows are pure RSS
     payload (title, description, audio_url, byte_length, duration,
     guid, pub_date, episode_type). No joins to
     `articles`/`editions`/`publications`. This keeps the feature
     100% RSS-layer with no website coupling.
   - **Listener-created content lookup** — when the operator wants
     to add a listener_db crosscut to the RSS, the CLI fetches
     metadata from Render via a new admin GET endpoint. That
     endpoint reuses `_load_listener_crosscut_for_promotion` at
     `aarva/server/routes/admin.py:379` (verified above) which
     already resolves an edition_id against listener_db first then
     main_db, so the "graduate any crosscut" path works whether the
     source is on Render or on the laptop.
   - **Audio byte length** — the current RSS uses
     `_audio_byte_length` (`aarva/output/rss_feed.py:193`) to stat
     the local mp3 for the enclosure's `length=` attribute (Apple
     validators warn on `length="0"` per its docstring at `:193-201`).
     Listener-created mp3s aren't on the laptop, only on R2. So
     `rss_extra_items` stores `byte_length` explicitly, populated
     either by the admin endpoint (from Render's local disk, where
     the file was written pre-R2-upload) or by the operator in the
     manual-add path.

2. **Where does the operation run?**
   - **Add-item CLI**: on the operator's laptop, alongside
     `aarva/search.py`, `aarva/ingest_url.py`, `aarva/review.py`
     (verified by `ls` above). Writes rows into main_db locally.
   - **Fetch-metadata admin GET**: on Render's FastAPI process,
     same file as the existing admin endpoints
     (`aarva/server/routes/admin.py`), same bearer-token gate
     (`_check_token` at `admin.py:68`, verified above).
   - **RSS render**: on the operator's laptop, Stage 10 of
     `aarva/daily.py` (called via `aarva.daily --stage 10`).
     `rss_feed.py:generate_feed` gets extended to iterate the new
     table.
   - **Publish**: unchanged — the generated `feed.xml` is committed
     to the GH Pages repo and served from
     `srik-create.github.io/aarva` (per
     `docs/project_brief.md:67-69`).

3. **Does the operation have physical access to the data it needs?**
   - **Add-item CLI + main_db write**: yes — main_db is a local
     SQLite file on the laptop.
   - **Fetch-metadata admin GET**: yes — Render has both DBs in
     memory (`request.app.state.db`, `request.app.state.listener_db`
     per `admin.py:470-471`), and the audio mp3 is on Render's local
     disk after synthesis for most listener-created episodes.
   - **RSS render**: yes — reads main_db only, including the new
     `rss_extra_items` table.
   - **No cross-DB writes**: this design deliberately never writes
     to listener_db from the laptop, and never writes to
     `editions`/`edition_pieces` on main_db. Only touches the new
     `rss_extra_items` table. So it doesn't tangle with the daily
     pipeline OR the website's data model.

---

## Locked decisions (with user, 2026-07-28)

1. **RSS-only, zero website impact.** The website's per-page
   surfaces (`/today`, `/crosscut/<id>`, `/listener-created`,
   `/article/<id>`) stay exactly as they are. This feature adds an
   extra RSS `<item>` for a given date; it does NOT surface anywhere
   on aarva.app. If the operator wants a listener-created crosscut
   on the /today "Also today" section too, that's a separate action
   using the existing `/admin/promote-bonus` endpoint (shipped
   2026-07-27, verified at `admin.py:430`).
2. **Auto-prefix `Crosscut: ` when the added episode is a crosscut.**
   Operator never types the prefix manually. The CLI determines the
   kind from either the fetched source (`edition_type='crosscut'`
   → prefix) or an explicit `--kind={crosscut,episode}` flag in the
   fully-manual path.
3. **No new "episode_type" or "edition_type" values.** Both are
   already fully-loaded enums. `rss_extra_items` sits alongside
   these, not inside them. iTunes `<itunes:episodeType>` for extra
   items defaults to `full` (matching the existing crosscut item
   shape at `rss_feed.py:382`), overridable per-row.
4. **Byte length stored on the row, not stat-ed at render time.**
   Because the mp3 isn't on the laptop for listener-created content.
   Non-negotiable — Apple Podcasts validators warn on `length="0"`
   per `_audio_byte_length`'s docstring at `rss_feed.py:193-201`.
5. **Removable is a row delete.** `DELETE FROM rss_extra_items
   WHERE guid = ?` — the next Stage 10 run drops it from feed.xml.
   Reversibility bar is met per rule 4's implicit "material
   trade-off = reversible" preference.
6. **No backfill of the currently orphaned listener-created
   crosscut(s).** The operator will manually add whichever ones
   they editorially want, one CLI invocation each. If bulk
   promotion becomes routine, extend the CLI later; not now.

---

## Schema

New table on main_db, added alongside `daily_bonus_features`
(`aarva/db.py:300`):

```sql
CREATE TABLE rss_extra_items (
    guid              TEXT PRIMARY KEY,
    episode_date      TEXT NOT NULL,
    title             TEXT NOT NULL,
    description_html  TEXT,
    audio_url         TEXT NOT NULL,
    byte_length       INTEGER NOT NULL DEFAULT 0,
    duration_seconds  INTEGER,
    author            TEXT,
    subtitle          TEXT,
    itunes_episode_type TEXT NOT NULL DEFAULT 'full',
    added_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_rss_extra_items_date ON rss_extra_items(episode_date);
```

Notes:
- **`guid`** is the PRIMARY KEY. Format for graduated listener-DB
  crosscuts: `aarva-crosscut-<edition_id>` — matches the existing
  editorial-crosscut GUID convention at `rss_feed.py:314`, so if the
  same episode were ever promoted through both paths (unlikely but
  defensively), podcast apps wouldn't see it as two different
  episodes. Manual add path lets the operator specify a custom GUID
  (e.g., `aarva-extra-<slug>`); if omitted, the CLI generates one
  from a slugified title + episode_date.
- **`episode_date`** is a plain ISO string, matching the
  `editions.edition_date` convention (per `docs/project_brief.md`).
- **`description_html`** stores the already-composed
  `<description>`/`content:encoded` body — assembled by the CLI
  when fetching from listener_db so the render loop stays trivial.
  Nullable in case the operator wants a title-only item.
- **`audio_url`** stores whatever the enclosure needs. For
  graduated listener_db crosscuts, that's the relative path (e.g.
  `output/audio/crosscut/1000011.mp3`) so `_audio_full_url`
  (`rss_feed.py:108`) prepends `audio_url_base` (audio.aarva.app)
  as normal. For fully-manual adds where the mp3 already has a
  full URL, we store the full URL and pass through unchanged (the
  render loop calls `_audio_full_url` which is a no-op when the
  input already has a scheme).
- **`byte_length`** stored per-row because the laptop can't stat
  the R2-hosted mp3. Populated by the admin endpoint (Render can
  stat its own disk) or manually.
- **`duration_seconds`** used for `<itunes:duration>`.
- **`itunes_episode_type`** defaults to `'full'`; can be `'bonus'`
  or `'trailer'` for other cases the operator might want later.

Migration: add table + index in `aarva/db.py` alongside the other
schema. No backfill needed.

---

## Query helper

Add to `aarva/services/queries.py`, sibling to
`load_crosscut_episodes`:

```python
def load_rss_extra_items(db: Database) -> list[dict[str, Any]]:
    """Ad-hoc extra items surfaced only in the podcast RSS feed.
    See docs/session_plan_rss_extra_items.md."""
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT guid, episode_date, title, description_html,
                   audio_url, byte_length, duration_seconds,
                   author, subtitle, itunes_episode_type, added_at
              FROM rss_extra_items
             ORDER BY episode_date DESC, added_at DESC
        """).fetchall()
    return [dict(r) for r in rows]
```

---

## Admin endpoint (Render)

New `GET /admin/episode-metadata` — used by the laptop CLI to fetch
what it needs to compose an `rss_extra_items` row from a listener_db
(or main_db) edition_id.

Placement: `aarva/server/routes/admin.py`, next to
`admin_promote_bonus` (verified at `admin.py:430`). Reuses:
- `_check_token` (`admin.py:68`) for auth.
- `_load_listener_crosscut_for_promotion` (`admin.py:379`) for the
  cross-DB lookup — this function already resolves listener_db first
  then main_db fallback (behaviour verified in the promote-bonus
  ship, per `docs/roadmap.md`'s 2026-07-27 entry — "Promoted
  edition_ids resolve against the listener DB first (everything
  since 2026-07-06), falling back to the main DB for the handful
  of pre-split legacy episodes").

Response shape (composed to match `_crosscut_item_xml`'s field usage
at `rss_feed.py:293-384`):

```json
{
  "kind": "crosscut",
  "guid": "aarva-crosscut-1000011",
  "episode_date": "2026-07-28",
  "title": "Crosscut: <topic_label>",
  "description_html": "<intro><br/><br/><em><bridge></em><br/><br/><outro><br/><br/>Sources:<br/><a href=...>pub_a: title_a</a><br/><a href=...>pub_b: title_b</a>",
  "audio_url": "output/audio/crosscut/1000011.mp3",
  "byte_length": 12345678,
  "duration_seconds": 1620,
  "author": "Aarva",
  "subtitle": "Crosscut · <topic_label>",
  "itunes_episode_type": "full"
}
```

The endpoint:
1. Auth-check with `_check_token`.
2. `edition_id = int(request.query_params["edition_id"])`.
3. Resolve via `_load_listener_crosscut_for_promotion(db,
   listener_db, edition_id)`.
4. Compose `title` with the `Crosscut: ` prefix (locked decision
   2), matching the exact format at `rss_feed.py:317`.
5. Compose `description_html` matching the format at
   `rss_feed.py:322-344` (intro + `<em>bridge</em>` + outro + source
   links). Reuse the same escape / join logic so the two rendered
   items are visually identical in podcast apps.
6. `byte_length` — stat the mp3 from Render's local disk
   (`package_root / audio_url`). If missing (older episode already
   R2-only), fall back to an HTTP HEAD to
   `audio.aarva.app/<audio_url>` and read `Content-Length`. If both
   fail, return 0 and log a warning — the CLI can still write the
   row but should print the same warning.
7. `duration_seconds` from the fetched edition row.

Errors: 401 (bad token), 404 (edition_id not found in either DB),
400 (missing/invalid edition_id).

---

## New CLI — `aarva.rss_add`

New file `aarva/rss_add.py`, alongside `aarva/search.py`,
`aarva/ingest_url.py` (verified via `ls` in the Architecture
check).

Two invocation modes:

**Mode 1 — graduate a listener_db (or main_db) crosscut:**

```bash
python -m aarva.rss_add --from-edition 1000011
```

Steps:
1. GET `https://aarva.app/admin/episode-metadata?edition_id=1000011`
   with `Authorization: Bearer $AARVA_RENDER_SYNC_TOKEN` (same env
   var the sync script already uses per
   `scripts/sync_db_to_render.sh`).
2. Insert the returned payload as an `rss_extra_items` row on the
   local main_db.
3. Print `Added edition 1000011 as RSS extra item (guid=..., date=...).`
   plus a reminder line: `Run \`python -m aarva.daily --stage 10\`
   to publish the updated feed.`

**Mode 2 — fully manual (any mp3 URL):**

```bash
python -m aarva.rss_add \
  --audio-url "https://audio.aarva.app/path/to/file.mp3" \
  --title "Some episode title" \
  --description "Some description text" \
  --duration 1620 \
  --byte-length 12345678 \
  --kind episode  # or --kind crosscut → auto-prefixes "Crosscut: "
  [--episode-date 2026-07-28]  # defaults to today
  [--guid aarva-extra-my-slug]  # defaults to generated
```

Steps:
1. If `--kind crosscut` and title doesn't already start with
   `Crosscut: `, prefix it. Never double-prefix.
2. If `--byte-length` is omitted and the audio URL is https://,
   attempt an HTTP HEAD and read `Content-Length`. Warn but continue
   if it fails (row still written with byte_length=0, feed still
   valid but with the Apple-validator warning).
3. Generate GUID if omitted: `f"aarva-extra-{slug(title)}-{date}"`.
4. Insert the row into main_db.
5. Same "run Stage 10" reminder.

Both modes: idempotent by GUID — re-running with the same GUID
updates the row (INSERT OR REPLACE).

Flag summary:
- `--from-edition <int>` (mode 1)
- `--audio-url <url>` (mode 2 — required)
- `--title <str>` (mode 2 — required)
- `--description <str>` (mode 2 — optional)
- `--duration <int>` (mode 2 — seconds, optional but recommended)
- `--byte-length <int>` (mode 2 — optional, auto-HEAD if absent)
- `--kind {crosscut,episode}` (mode 2 — default `episode`; only
  affects auto-prefix)
- `--episode-date <YYYY-MM-DD>` (mode 2 — default today)
- `--guid <str>` (mode 2 — optional, auto-generated if absent)
- `--author <str>` (mode 2 — optional; default "Aarva")
- `--subtitle <str>` (mode 2 — optional)

Also add `--list` and `--remove <guid>` for basic management:

```bash
python -m aarva.rss_add --list
python -m aarva.rss_add --remove aarva-crosscut-1000011
```

---

## RSS render change

`aarva/output/rss_feed.py` — extend `generate_feed`
(`rss_feed.py:400`) to also iterate `rss_extra_items` and emit each
row as an `<item>`. Reuses existing helpers unchanged.

New helper next to `_item_xml` (`:211`) and `_crosscut_item_xml`
(`:293`):

```python
def _extra_item_xml(row: dict, public_url_base: str, package_root: Path,
                    feed_image: str = "", audio_url_base: str = "",
                    aarva_app_url: str = "") -> str:
    """Render an ad-hoc rss_extra_items row as one RSS <item>. See
    docs/session_plan_rss_extra_items.md.

    Fields are already fully composed on the row (title includes any
    "Crosscut: " prefix; description_html is the full body). This
    render is a straight passthrough, not a compose step.
    """
    audio_url = _audio_full_url(
        row["audio_url"],
        audio_url_base or public_url_base,
    )
    pub_dt = row.get("episode_date")
    if isinstance(pub_dt, str):
        try:
            pub_dt = datetime.fromisoformat(pub_dt).replace(tzinfo=timezone.utc)
        except ValueError:
            pub_dt = datetime.now(timezone.utc)
    pub_date_rss = _format_rfc822(pub_dt) if pub_dt else _format_rfc822(
        datetime.now(timezone.utc)
    )
    duration_str = _format_duration_hhmmss(row.get("duration_seconds"))
    description = row.get("description_html") or ""
    summary_text = _strip_html(description, max_chars=4000)
    image_tag = (
        f'<itunes:image href="{_xml_esc(feed_image)}"/>'
        if feed_image else ""
    )
    return f"""    <item>
      <title>{_xml_esc(row["title"])}</title>
      <itunes:title>{_xml_esc(row["title"])}</itunes:title>
      <link>{_xml_esc(aarva_app_url or public_url_base)}</link>
      <description><![CDATA[{description}]]></description>
      <content:encoded><![CDATA[{description}]]></content:encoded>
      <itunes:summary>{_xml_esc(summary_text)}</itunes:summary>
      <pubDate>{pub_date_rss}</pubDate>
      <guid isPermaLink="false">{_xml_esc(row["guid"])}</guid>
      <enclosure url="{_xml_esc(audio_url)}" length="{row.get("byte_length") or 0}" type="{_xml_esc(_mime_for(audio_url))}"/>
      <itunes:duration>{_xml_esc(duration_str)}</itunes:duration>
      <itunes:author>{_xml_esc(row.get("author") or "Aarva")}</itunes:author>
      <itunes:subtitle>{_xml_esc(row.get("subtitle") or "")}</itunes:subtitle>
      {image_tag}
      <itunes:episodeType>{_xml_esc(row.get("itunes_episode_type") or "full")}</itunes:episodeType>
      <itunes:explicit>false</itunes:explicit>
    </item>"""
```

In `generate_feed`, after the existing daily + crosscut items are
composed, load and render extras:

```python
from aarva.services.queries import load_rss_extra_items
extras = load_rss_extra_items(db)
extra_items = [
    _extra_item_xml(row, public_url_base, package_root,
                    feed_image=feed_image, audio_url_base=audio_url_base,
                    aarva_app_url=aarva_app_url)
    for row in extras
]
```

Merge into the combined item list before sorting by pub_date (the
existing `_item_pub_dt` sort at `rss_feed.py:387` — extras need an
adapter since it looks for `published_date`/`edition_date`; a small
tweak on `_item_pub_dt` OR a per-row `("episode_date", row["episode_date"])`
alias set on the dict before it enters the sort list. Either is
fine; pick whichever reads cleaner in the diff.

Result: extras interleave naturally with existing items by their
`episode_date`, so a listener sees them in chronological order in
their podcast app.

---

## Files that change

- `aarva/db.py` — new `CREATE TABLE rss_extra_items` + index,
  next to `daily_bonus_features` at line 300.
- `aarva/services/queries.py` — new `load_rss_extra_items` helper.
- `aarva/server/routes/admin.py` — new `GET
  /admin/episode-metadata` endpoint. Reuses
  `_load_listener_crosscut_for_promotion` (`admin.py:379`) and
  `_check_token` (`admin.py:68`).
- `aarva/rss_add.py` (new) — the operator CLI. `argparse`-based,
  same shape as `aarva/search.py` (`search.py:568-625`).
- `aarva/output/rss_feed.py` — new `_extra_item_xml` helper; wire
  extras into `generate_feed`'s item list.
- `aarva/tests/test_rss_extra_items.py` (new) — first test file in
  this dir if the tests dir is still empty (per the 2026-07-28
  Independent-CTA spec's note); if not, sibling to the existing
  files. Test cases below.
- `docs/roadmap.md` — after PR merges, add "Recently completed"
  entry. Claude Code owns this per AGENTS.md rule 17.

---

## Verification

1. **Schema migration is idempotent.** `CREATE TABLE IF NOT
   EXISTS` — running `db.init_schema()` twice is a no-op. Confirm
   via a scratch DB.
2. **Admin endpoint returns correct payload for a real
   listener_db edition.** Curl `GET /admin/episode-metadata?edition_id=1000011`
   against Render with the bearer token — confirm `title` starts
   with `Crosscut: `, `description_html` is non-empty and contains
   both source links, `audio_url` is the relative path,
   `byte_length` > 0 (or a warning is printed if the mp3 has already
   been R2-cleaned).
3. **CLI mode 1 — graduate a listener_db crosscut.** Run `python
   -m aarva.rss_add --from-edition 1000011`. Confirm the row lands
   in main_db.rss_extra_items, GUID = `aarva-crosscut-1000011`.
4. **CLI mode 2 — manual add.** Run with `--audio-url`,
   `--title`, `--kind crosscut`, `--duration`. Confirm title gets
   the `Crosscut: ` prefix; row lands. Run once more with `--kind
   episode` and a plain title — confirm no prefix.
5. **Idempotency by GUID.** Run mode 1 twice — second run should
   INSERT OR REPLACE, no duplicate row.
6. **Removal.** `--remove <guid>` deletes the row. Next
   Stage 10 run drops it from `feed.xml`.
7. **RSS render includes the item.** Run `python -m aarva.daily
   --stage 10` (or a scoped `generate_feed` invocation against a
   scratch DB). Grep the resulting `feed.xml` for the GUID —
   confirm an `<item>` block exists with correct `<title>`,
   `<enclosure>`, `<itunes:duration>`, `<pubDate>`.
8. **Ordering.** Manually insert two rows with different
   `episode_date` values. Confirm they appear in the correct
   chronological position relative to real daily / crosscut items
   in the feed.
9. **Website unchanged.** Grep `aarva/server/` for any reference
   to `rss_extra_items` — should be zero (the table is RSS-render-
   only). Load `/today`, `/crosscuts`, `/listener-created` locally
   and confirm nothing surfaces there.
10. **Podcast-app validator smoke test.** After running Stage 10
    with at least one extra item present, paste `feed.xml` into
    https://podba.se/validate (or equivalent) — confirm no new
    warnings compared to a baseline feed without extras.

---

## Non-goals

- **No website changes.** `/today`, `/crosscut/<id>`,
  `/listener-created`, `/article/<id>` are all untouched. Grep-
  verifiable per verification step 9.
- **No changes to `editions`, `edition_pieces`, `articles`, or
  `publications`.** All existing schema stays put.
- **No changes to the daily pipeline's Stage 1-9 flow.** Only
  Stage 10 gains a new item source. The daily edition still gets
  built and rendered exactly as before.
- **No cross-DB writes.** The CLI never writes to listener_db;
  the admin endpoint never writes to either DB. This is strictly
  a metadata read + local main_db write.
- **No admin UI on aarva.app for managing extras.** CLI-only for
  v1. If it becomes routine, a small `/admin/rss-extras` page can
  come later.
- **No bulk backfill.** The operator adds each item explicitly.
- **No modification to the current bonus / crosscut RSS paths.**
  `_load_all_published_pieces` and `_load_published_crosscuts`
  stay untouched. Extras are a third, independent source.

---

## Rollout

- Ship as a single PR touching the six files listed above.
- No env var changes — reuses the existing
  `AARVA_RENDER_SYNC_TOKEN`.
- No Render config changes — the new endpoint is just another
  route in the existing FastAPI app.
- After merge + deploy of the Render side, the operator can start
  running `python -m aarva.rss_add --from-edition <id>` from the
  laptop and then `python -m aarva.daily --stage 10` to publish the
  updated feed.

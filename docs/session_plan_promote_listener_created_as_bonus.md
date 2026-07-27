# Session plan — promote listener-created crosscuts as bonus tracks on /today

Written by Cowork for the next Claude Code session (2026-07-26+).
Small extension to the existing bonus-track mechanism: let the
operator promote specific listener-created crosscuts (those made
via `/create` with a `user_id`) to appear on `/today` as bonus
tracks, ordered, up to N per day.

Read this doc + `docs/roadmap.md` + `AGENTS.md` before starting.

**AGENTS.md rule 4 sign-off**: this surfaces additional listener-
facing content in a new placement. User approved direction
2026-07-26. Copy for the section header is "Also today" (locked;
see below); any deviation requires re-check.

---

## Context

Aarva already has a bonus-track mechanism: `edition_type='bonus'`
rows in `editions`, joined to `edition_pieces` → `articles`. The
`load_bonus_pieces_with_audio` helper (`aarva/services/queries.py`
line 88) surfaces these on `/today` via `home.py`'s route (line
111) and the `bonus_pieces` block in `home.html` (line 77-120).

But that mechanism is **article-shaped** — it assumes a piece has
`article_id`, `title`, `byline`, `publication_name`. Listener-
created crosscuts are **crosscut-shaped**: their audio, topic
label, and subhead sit directly on the `editions` row (via
`aarva/stages/stage_crosscut.py`'s `--crosscut-build` phase),
with no `edition_pieces`. Feeding a listener-created crosscut
through the article-bonus code path would need mixed-shape
rendering — messy.

Cleaner: introduce a lightweight promotion mapping that surfaces
listener-created crosscuts on `/today` under a new "Also today"
section, rendered with the same crosscut card style as today's
editorial crosscut.

**"Listener-created"** = `editions.edition_type='crosscut'` AND
`editions.user_id IS NOT NULL`. Editorial crosscuts (curator-
selected daily crosscut) have `user_id IS NULL` and are already
shown on `/today` via a separate path — do NOT promote those
here; they'd double-render.

---

## Locked decisions (with user, 2026-07-26)

1. **Placement**: dedicated "Also today" section on `/today`,
   using the same peach crosscut-card visual as today's editorial
   crosscut. Sits BELOW today's editorial crosscut and ABOVE the
   existing article-shaped "Bonus today" section (or between if
   both exist).
2. **Volume**: up to N per day, in an explicit ordering. Operator
   controls both count and order.
3. **Interface**: extend `python -m aarva.search` with two new
   flags (`--promote-bonus <edition_id>` and `--unpromote-bonus
   <edition_id>`) plus interactive support (`b <index>` after
   search results). Reuses the search tool's existing pattern.

---

## Schema

New table (lightweight mapping, doesn't mutate `editions`):

```sql
CREATE TABLE daily_bonus_features (
    daily_date          TEXT NOT NULL,
    featured_edition_id INTEGER NOT NULL,
    position            INTEGER NOT NULL,
    added_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (daily_date, featured_edition_id),
    FOREIGN KEY (featured_edition_id) REFERENCES editions(id)
);
CREATE INDEX idx_daily_bonus_features_date
    ON daily_bonus_features(daily_date, position);
```

Rationale:
- **No mutation on `editions`** — listener-created crosscuts stay
  intact. Un-promoting = row delete, no state drift.
- **Same crosscut promoted on multiple days** works naturally
  (different `daily_date` rows).
- **Ordering** via `position` — reorderable without touching
  edition rows.
- **daily_date is a string** matching Aarva's existing
  `editions.edition_date` convention (ISO date). Keeps queries
  uniform.

Migration: add table + index in `aarva/db.py` alongside the other
schema. No backfill needed.

---

## Query helper

Add to `aarva/services/queries.py`, sibling to
`load_bonus_pieces_with_audio`:

```python
def load_featured_listener_crosscuts_for_date(
    db: Database,
    edition_date: date,
) -> list[dict[str, Any]]:
    """Listener-created crosscuts promoted as bonus features
    for the given daily edition date, ordered by position.

    Only returns crosscuts that:
      - have edition_type='crosscut'
      - have user_id IS NOT NULL (listener-created)
      - have a non-empty audio_url on the edition row
        (the crosscut audio, synthesized by --crosscut-tts)
    """
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT e.id AS edition_id,
                   e.edition_date,
                   e.topic_label,
                   e.subhead_hook,
                   e.intro_text,
                   e.outro_text,
                   e.originating_prompt,
                   e.audio_url,
                   e.duration_seconds,
                   e.narrator_voice,
                   f.position
              FROM daily_bonus_features f
              JOIN editions e ON e.id = f.featured_edition_id
             WHERE f.daily_date = ?
               AND e.edition_type = 'crosscut'
               AND e.user_id IS NOT NULL
               AND e.audio_url IS NOT NULL AND e.audio_url != ''
             ORDER BY f.position ASC
        """, (edition_date.isoformat(),)).fetchall()
    return [dict(r) for r in rows]
```

Note: the `crosscut.audio_url` shape is different from
`edition_pieces.audio_url` (which is what the article-bonus
helper returns). The template needs to know this is
crosscut-shaped data.

---

## Route change

`aarva/server/routes/home.py` — extend the `/today` handler
around line 111. After the existing `todays_bonuses` load,
add:

```python
todays_featured_listener_crosscuts = load_featured_listener_crosscuts_for_date(
    db, edition_dt,
)
```

Pass into the template context:

```python
"featured_listener_crosscuts": todays_featured_listener_crosscuts,
```

---

## Template change

`aarva/server/templates/home.html` — add a new section BELOW the
editorial crosscut (currently line 11-62) and ABOVE the JTBD
groups (line 122). Reuse the crosscut card block (line 12-61) as
the visual pattern — same peach card, same play-button styling.

```jinja
{% if featured_listener_crosscuts %}
  <section class="mb-12">
    <p class="editorial text-xs uppercase tracking-widest text-cream-light mb-5 font-medium">
      Also today
    </p>
    {% for cc in featured_listener_crosscuts %}
      <div class="mb-6 p-6 bg-peach rounded-2xl text-ink">
        <p class="text-xs uppercase tracking-widest text-peach-dark font-semibold">
          From a listener
        </p>
        <h2 class="editorial text-2xl font-semibold mt-2 leading-snug">
          <a href="/crosscut/{{ cc.edition_id }}" class="hover:opacity-70 transition-opacity">
            {{ (cc.topic_label or "Two angles") | title_case }}
          </a>
        </h2>
        {% if cc.subhead_hook %}
          <p class="text-ink mt-2 leading-relaxed">{{ cc.subhead_hook }}</p>
        {% endif %}
        {% if cc.originating_prompt %}
          <p class="text-sm text-ink-light mt-2 italic">
            asked: "{{ cc.originating_prompt }}"
          </p>
        {% endif %}
        {# Reuse the shared-player card pattern from base.html. Same
           data-track-* attributes as today's editorial crosscut. #}
        <div class="mt-5" data-player
             data-track-src="{{ cc.audio_url | audio_url(request.app.state.pipeline_cfg.raw.get('tts', {}).get('r2', {}).get('public_url_base', '')) }}"
             data-track-title="{{ (cc.topic_label or 'Crosscut') | title_case }}"
             data-track-link="/crosscut/{{ cc.edition_id }}">
          {# ... same button + progress + time markup as today's crosscut card ... #}
        </div>
      </div>
    {% endfor %}
  </section>
{% endif %}
```

**Ordering on the page:**
1. Editorial crosscut (existing)
2. "Also today" — promoted listener-created crosscuts (new)
3. Existing article-shaped "Bonus today" section (if any)
4. JTBD groups

**Section header copy:** `Also today` — locked. The per-card
attribution eyebrow reads `From a listener`. Both signal that
these are additive picks curated by the operator, without being
noisy about it. Do NOT change either without user re-check
(AGENTS.md rule 4).

---

## CLI extension — `aarva/search.py`

Add three interaction paths that build on the existing search
tool.

### Flag 1: `--promote-bonus <edition_id>`

Non-interactive: promote a specific crosscut's edition_id as
today's bonus feature.

```bash
python -m aarva.search --promote-bonus 108
```

Behavior:
1. Look up `editions.id = <edition_id>`. Verify it's an existing
   row.
2. Verify `edition_type='crosscut' AND user_id IS NOT NULL`
   (must be listener-created). If not → error + exit non-zero:
   `Cannot promote: edition 108 is not a listener-created
   crosscut (edition_type=daily).`
3. Verify `audio_url IS NOT NULL AND audio_url != ''` (must have
   synthesized audio). If not → error + exit non-zero.
4. Insert into `daily_bonus_features` with
   `daily_date = today's date` and
   `position = MAX(position for today) + 1` (or 1 if none yet).
   Use `INSERT OR IGNORE` — re-promoting the same crosscut is a
   no-op with a friendly log message.
5. Print confirmation: `Promoted edition 108 as bonus feature #N
   for today (2026-07-26).`

### Flag 2: `--unpromote-bonus <edition_id>`

Non-interactive: remove a crosscut from today's bonus features.

```bash
python -m aarva.search --unpromote-bonus 108
```

Behavior:
1. DELETE FROM `daily_bonus_features` where `daily_date=today`
   AND `featured_edition_id=<edition_id>`.
2. If nothing was deleted, print a warning: `Edition 108 was not
   promoted for today (2026-07-26).`
3. Otherwise: `Un-promoted edition 108 for today.` Don't
   auto-reorder remaining positions — leave gaps rather than
   silently mutating other rows. Reordering is a separate concern.

### Interactive: `b <index>` after search results

Extend the existing interactive `add` prompt (which already
supports `1,3,7` for adding articles by result-index) to also
accept `b <index>`:

```
Add to today's edition: 1,3,7    (indices) or ids: 9312,9315
Promote as bonus:       b 2,b 5  (listener-created crosscuts only)
Empty to exit.
> b 2
```

`b <index>` runs the same `--promote-bonus` logic on the
crosscut whose index in the results is 2. If that result isn't a
listener-created crosscut, print an error and continue the prompt.

### Search-filter integration

Add a `--listener-only` flag to `search.py` that restricts
results to `editions` rows where `edition_type='crosscut' AND
user_id IS NOT NULL`. Handy shorthand for finding candidates to
promote. Doesn't affect other search flows.

```bash
python -m aarva.search "belief formation" --listener-only
python -m aarva.search --listener-only          # all listener-created, most recent first
```

---

## Non-goals

- **Do NOT promote editorial crosscuts.** They already appear on
  `/today`; promoting them would double-render. The CLI must
  reject them explicitly.
- **Do NOT auto-select** listener-created crosscuts. Fully
  operator-driven. If the operator wants automated selection
  later (e.g. top-quality listener crosscut of the week), that's
  a separate spec.
- **Do NOT touch the article-shaped bonus mechanism.** Both live
  side-by-side on `/today`. Existing bonus editions keep working.
- **Do NOT reorder remaining positions on un-promote.** Leave
  gaps; reordering is a future feature.
- **Do NOT expose promotion on the web UI.** Operator-only CLI.

---

## Files that change

- `aarva/db.py` — new table + index; schema migration.
- `aarva/services/queries.py` — new
  `load_featured_listener_crosscuts_for_date` helper.
- `aarva/server/routes/home.py` — call the new helper, pass into
  template context.
- `aarva/server/templates/home.html` — new "Also today" section.
- `aarva/search.py` — three new interaction paths
  (`--promote-bonus`, `--unpromote-bonus`, `b <index>`,
  `--listener-only`).
- `docs/roadmap.md` — after PR merges, move from In-Progress to
  Recently Completed (Claude Code owns this per AGENTS.md rule
  17).

---

## Verification

1. **DB migration**: run against a fresh DB. Confirm the table +
   index exist. Run against an existing DB — migration adds the
   table without touching existing rows.
2. **CLI, positive path**: create a test listener-created
   crosscut (via `/create` on the local server, then
   `--crosscut-build` and `--crosscut-tts` to make it complete).
   Run `python -m aarva.search --promote-bonus <its_id>`. Confirm
   the promotion row is inserted. Reload `/today`. Confirm the
   "Also today" section appears with the correct topic label +
   subhead + prompt attribution.
3. **CLI, restriction**: try to promote an editorial crosscut
   (`user_id IS NULL`) via `--promote-bonus`. Confirm the CLI
   refuses with a clear error and exits non-zero.
4. **CLI, missing audio**: promote a listener-created crosscut
   that has `audio_url IS NULL`. Confirm refusal.
5. **CLI, un-promote**: `--unpromote-bonus <id>`. Confirm the row
   is deleted. Reload `/today`. The card disappears.
6. **Ordering**: promote 3 different listener-created crosscuts.
   Confirm they appear on `/today` in promotion order. Un-promote
   the middle one; confirm the other two remain in their original
   positions (gaps allowed).
7. **Interactive**: run `python -m aarva.search "..." --listener-only`.
   Type `b 2` at the prompt. Confirm the same promotion behavior
   as `--promote-bonus`.
8. **Regression**: confirm the existing article-shaped
   `Bonus today` section still renders correctly (unchanged
   code path).
9. **Mobile viewport**: render `/today` at 375px in a headless
   browser. Confirm "Also today" section renders cleanly, doesn't
   overflow, doesn't clash with the mini-player at the bottom.
   Attach screenshots to the PR.

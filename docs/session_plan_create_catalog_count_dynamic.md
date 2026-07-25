**STATUS: DONE (2026-07-22).** Implemented exactly as specced. See
`docs/roadmap.md`'s 2026-07-22 "Recently completed" entry.

---

# Session plan — dynamic catalog count on /create loading state

Written by Cowork for the next Claude Code session (2026-07-22+).
Tiny listener-facing fix — the /create page's loading message
currently reads *"Aarva is matching your prompt against ~5,000
articles in the catalog…"* which was accurate when written but
the catalog is now 10k+ and growing. Make the number dynamic.

Read this doc + `docs/roadmap.md` + `AGENTS.md` before starting.

**AGENTS.md rule 4 sign-off**: the copy shape is unchanged —
only the numeric value becomes dynamic. User signed off
2026-07-22.

---

## Context

`aarva/server/templates/create.html` line 31 hardcodes:

```
Aarva is matching your prompt against ~5,000 articles in the
catalog and composing new pairings. Usually 5–10 seconds.
```

The "5,000" was accurate at the time but no longer matches the
current DB. The catalog is what /create's `propose_candidates`
actually searches over — articles with a usable embedding.

---

## Decisions locked

1. **Which count**: articles with a non-null `embedding`. That's
   the true searchable pool (matches what the embedding-similarity
   step in `propose_candidates` actually scans). Not "all
   articles" (some may not yet be embedded); not "published
   only" (`propose_candidates` proposes NEW pairings from
   unpublished articles too, per Phase 2 design).
2. **Rounding**: floor to the nearest 1,000. Never overstate.
   e.g. 10,432 → `~10,000`; 10,987 → `~10,000`; 11,001 →
   `~11,000`. If count < 1,000 (won't happen in practice), show
   `~1,000` as a safety floor so the copy never reads "~0".
3. **Format**: thousands separator ("~10,000" not "~10000").
4. **Recomputation cadence**: compute on every /create render.
   It's a single `SELECT COUNT(*) FROM articles WHERE embedding
   IS NOT NULL` — sub-millisecond query, no caching needed.
5. **Copy shape unchanged** except for the number:
   `Aarva is matching your prompt against ~{count} articles in
   the catalog and composing new pairings. Usually 5–10 seconds.`

---

## Implementation

### `aarva/server/routes/create.py`

The `/create` route currently passes only `{"prompt": q}` to the
template. Extend to include the floored count:

```python
@app.get("/create", response_class=HTMLResponse)
async def create_candidates(request: Request) -> HTMLResponse:
    q = (request.query_params.get("q") or "").strip()
    if not q:
        return RedirectResponse(url="/", status_code=303)

    db = request.app.state.db
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM articles WHERE embedding IS NOT NULL"
        ).fetchone()
    raw_count = int(row["n"] if row else 0)
    # Floor to the nearest 1,000, with a 1,000 safety minimum.
    catalog_size = max(1000, (raw_count // 1000) * 1000)

    return templates.TemplateResponse(
        request, "create.html",
        {"prompt": q, "catalog_size": catalog_size},
    )
```

### `aarva/server/templates/create.html`

Line 31 becomes:

```html
<p class="text-sm text-ink-light mt-1">Aarva is matching your
prompt against ~{{ "{:,}".format(catalog_size) }} articles in
the catalog and composing new pairings. Usually 5–10 seconds.
</p>
```

Jinja's built-in `{:,}` format spec adds the thousands separator.

### Non-goals

- **No caching layer.** The query runs on every /create load;
  it's negligibly cheap on a covering-column count.
- **No live progressive updates** ("counting… now 10,432 →
  10,435"). The loading dialog only shows for 5–10 seconds; the
  number matters as a rough size cue, not a live meter.
- **No change to the /api/candidates path.** That handler does
  the actual search work; the number only appears on the shell
  render.

---

## Verification

1. Load `/create?q=anything`. Confirm the copy reads
   `~10,000 articles` (or whatever the current floor is —
   verify against `SELECT COUNT(*) FROM articles WHERE
   embedding IS NOT NULL` from the DB manually).
2. Simulate a smaller DB (or read the code): count = 999 →
   copy reads `~1,000 articles` (safety floor).
3. Confirm the rest of the sentence is byte-identical to the
   current copy — same wording, same spacing, same em-dash /
   punctuation. Only the number changes.

---

## Files that change

- `aarva/server/routes/create.py` — add the count query + pass
  `catalog_size` into the template context.
- `aarva/server/templates/create.html` — swap the hardcoded
  `~5,000` for the dynamic `~{{ "{:,}".format(catalog_size) }}`.
- `docs/roadmap.md` — after this PR merges, move the item from
  In-Progress to Recently Completed (Claude Code owns this per
  AGENTS.md rule 17).

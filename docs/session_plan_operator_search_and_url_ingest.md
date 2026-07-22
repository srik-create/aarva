**STATUS: DONE (2026-07-22).** Both features shipped in one PR. See
`docs/roadmap.md`'s 2026-07-22 "Recently completed" entry for the
full writeup, including several real spec-vs-reality gaps found and
corrected (domain-matching had no existing helper; `ExtractedArticle`
has no title/byline — those come from RSS metadata that doesn't
exist for an ad-hoc URL; `editions.published_date` isn't actually a
"has this published" flag; `publications` had no `country` column at
all). One deliberate deviation, confirmed with the user before
proceeding: Feature A extends the existing `aarva/search.py` instead
of a new `aarva/find.py` — `search.py` already covered ~90% of the
same ground (lexical+semantic search, filters, ranked display,
interactive picker).

---

# Session plan — operator search + ad-hoc URL ingest

Written by Cowork for the next Claude Code session (2026-07-22+).
Two operator-only tools for augmenting the daily edition. Both
CLI-based (consistent with `python -m aarva.review` and
`python -m aarva.crosscut`). Ship together as one PR — they share
the same "add a specific article to today's edition" mechanism.

Read this doc + `docs/roadmap.md` + `AGENTS.md` before starting.

---

## Context

The daily-review flow currently supports pipeline-picked pieces
(via Stage 7) plus a limited "extras" mechanism (`extra_slots`
with `pub:` / `topic:` filters — a hint that Stage 7 uses to add
one more slot on the next refill). Two gaps:

1. **No way to browse the article DB for good candidates the
   operator remembers or wants to search for.** All candidate
   selection today is either pipeline-picked or filter-hinted;
   there's no "show me all valid articles matching X" query.
2. **No way to ingest an ad-hoc URL.** If the operator finds a
   great article on a publication that isn't set up in
   `publications.yaml` (or one that is but the piece slipped
   past the ingestion window), there's no runtime path to pull
   it in and optionally add it to today's edition.

Both are small extensions that live in `aarva/` alongside
`review.py` / `crosscut.py`, use the same `Database` helper,
and share a common "add to edition" primitive.

---

## Decisions locked (with user, 2026-07-22)

1. **Search:** both semantic (embedding-based) AND keyword
   (literal title/excerpt substring). Same command runs both,
   shows results grouped by source.
2. **Ad-hoc URL from an unknown publication:** prompt at ingest
   time — (a) use a shared "Ad hoc" pub, (b) register the pub
   now (name + optional country tag, DB row only — no
   publications.yaml edit), (c) abort.
3. **Interface:** standalone CLI commands (`python -m aarva.find`
   and `python -m aarva.ingest_url`). No changes to
   `aarva/review.py`'s interactive shell.

---

## Feature A — `python -m aarva.find`

### Goal

Search the local article DB for "valid" candidates that could
be added to today's edition. Show ranked results; let the
operator add one or more directly to today's edition.

### "Valid" = eligible for selection

An article is a valid candidate if ALL of:

1. `articles.status = 'scored'` — has passed extraction +
   scoring stages (i.e., Stages 1 through 4-5-6).
2. Not currently in ANY past PUBLISHED edition's `edition_pieces`
   with `review_status = 'approved'` and the edition's
   `published_date IS NOT NULL`.
3. Not in `edition_rejections` (any edition).
4. Not in `dropped_article_ids` for TODAY's edition (from the
   review-CLI polish PR — same-edition drop exclusion).
5. `articles.full_text IS NOT NULL AND LENGTH(full_text) > 0`.

The same filter set should live in a shared helper — extract
into `aarva/services/candidate_filter.py` so both `find` and
Stage 7 use one definition of "eligible pool." Stage 7 currently
inlines this logic; refactoring is a nice bonus but explicitly
non-mandatory for this PR (keep the change small if the Stage 7
version isn't trivially extractable). Just make sure `find`'s
filter is provably the same set.

### CLI shape

```
python -m aarva.find "climate finance greenwashing"           # search only
python -m aarva.find "..." --limit 20                          # more results
python -m aarva.find "..." --semantic-only                     # opt out of keyword
python -m aarva.find "..." --keyword-only                      # opt out of semantic
python -m aarva.find "..." --add 9028                          # non-interactive add
python -m aarva.find --add 9028                                # skip search; direct add
```

### Two-pass search

1. **Keyword pass:** literal `LIKE %query%` against
   `articles.title` and `articles.excerpt`. Case-insensitive.
   Also split the query on whitespace and require ALL tokens
   match (AND, not OR) — closer to how operators think of
   "these words should be in the article."
2. **Semantic pass:** embed the query via the same Vertex AI
   Gemini Embedding model + config the pipeline already uses
   (768-dim Matryoshka via `aarva/clients/embeddings.py` or
   wherever it lives — DO NOT duplicate the embedding call
   pattern; reuse the existing helper). Cosine similarity
   against `articles.embedding`. Filter to top-K above a
   similarity floor (start with 0.55; tune later).

Merge results, dedupe by article_id (a piece can appear in
both), sort within each source-tag, output as two adjacent
sections OR a single merged list annotated with source. Pick
what reads best; recommend two sections with heading rules.

### Display

Match the visual style of `aarva/crosscut.py`'s longlist
printout — bold `[index]`, coloured metadata, dim summary.
Per row:

```
  [1]  How Cities Are Losing to Concrete   — semantic 0.72
       ProPublica  1,542w / ~10m  2026-07-19  id=9312
       excerpt: "First 220 chars of the article's excerpt as
       a hint at content..."
```

Line 1: title (title_case filter) + source tag (`semantic <score>`
or `keyword`).
Line 2: publication, word count, ~duration, published date,
`id=<int>` so the operator can reference it in `--add`.
Line 3: brief excerpt — 200-250 chars, dim.

### Interactive `add` prompt after results

If invoked without `--add`, after printing results:

```
Add to today's edition: 1,3,7    (indices) or ids: 9312,9315
Empty to exit.
> _
```

Add the chosen article_ids to today's `editions` row's edition
via the shared primitive (§ "Add-to-edition primitive" below).

### Non-goals

- No re-ranking / dedup against existing edition pieces beyond
  the "valid" filter — if the operator wants to add something
  already in the edition, we let them (the primitive will
  handle idempotency).
- No history browsing (past editions of the same query).
- No cross-language search — assumes English tokens.

---

## Feature B — `python -m aarva.ingest_url`

### Goal

Ingest a specific URL into the article DB, then optionally add
it to today's edition. Reuses existing extraction + scoring
code, doesn't recreate any of it.

### CLI shape

```
python -m aarva.ingest_url https://example.com/some/article
python -m aarva.ingest_url <url1> <url2> ...                 # batch
python -m aarva.ingest_url <url> --add-to-edition            # ingest + add
python -m aarva.ingest_url <url> --dry-run                   # extract, don't persist
```

### Per-URL flow

For each URL:

1. **Fetch HTML.** Reuse the existing fetch helper (whatever
   Stage 1 uses — likely `requests` with a timeout + a
   user-agent). Handle 4xx/5xx by printing an error and
   continuing to the next URL, not aborting the whole batch.
2. **Look up publication by domain.** Extract `netloc` from
   the URL. Match against `publications` table's known domains
   (matching however Stage 1 already does it). If matched → use
   that `publication_id`.
3. **Unknown domain — prompt:**
   ```
   Unknown publication domain: foobar.com
     (a) One-off — use shared 'Ad hoc' publication
     (b) Register now (adds a DB row; publications.yaml unchanged)
     (c) Abort this URL, continue with next
   Choice [a/b/c]: _
   ```
   - **(a)** Ensure a single row exists in `publications` with
     `name='Ad hoc'` (create on first use). Use its id.
     Country tag = NULL → default accent.
   - **(b)** Prompt for `name` (freetext) and `country`
     (optional; one of `us`/`uk`/`india` or blank). Insert a
     row in `publications`. Print a reminder:
     `Note: to enable ongoing RSS ingest for this publication,
     add it to publications.yaml manually.` (Explicit non-goal
     for this PR: no YAML edit.)
   - **(c)** Skip this URL; move on.
4. **Extract body.** Call the same extractor Stage 1 uses (do
   not duplicate). If extraction fails (short body, paywalled,
   etc.), print error + skip.
5. **Run the downstream stages inline on the single article:**
   Stage 1.5 (metadata), Stage 2 (structural filters — if the
   article fails a filter, WARN but still let the operator
   decide via a follow-up prompt; ad-hoc URLs may deserve
   manual override), Stage 4-5-6 (scoring), Stage 85 (author
   provenance), embedding generation. If any stage requires an
   LLM call, run it; latency is acceptable for a manual command.
6. **Insert into `articles`** with `status = 'scored'`.
7. **Print the resulting row's summary** — id, title, pub,
   word count, JTBD (if available), one-line excerpt.
8. **If `--add-to-edition`:** call the shared "add to today's
   edition" primitive with the new article_id.

### Terminal-boilerplate strip

If the TTS-boilerplate-strip work has landed (session_plan_
`tts_boilerplate_strip.md`), the `ingest_url` flow reuses that
Stage 1 cleaner automatically — one less thing for this spec to
address.

---

## Shared primitive — "Add article to today's edition"

Both `find --add` and `ingest_url --add-to-edition` need the
same operation: insert an article as a proposed piece in today's
edition. Extract this into `aarva/services/edition_ops.py`
(new file) with signature roughly:

```python
def add_article_to_todays_edition(
    db: Database,
    article_id: int,
    slot: str = "manual_addition",
    position: int | None = None,
) -> Literal["added", "already_present", "no_edition"]:
    ...
```

### Behaviour

1. Find today's edition_id (`SELECT id FROM editions
   WHERE edition_date = date('now') ORDER BY id DESC LIMIT 1`).
   If none exists → return `"no_edition"` (caller prints a
   helpful error). Don't auto-create; Stage 7 owns that.
2. Check whether the article is already in `edition_pieces`
   for that edition (any status). If yes → return
   `"already_present"` (caller prints a no-op message).
3. Compute position — default is `MAX(position) + 1` for that
   edition, or 1 if empty.
4. Insert into `edition_pieces` with:
   - `edition_id`, `article_id`, `slot='manual_addition'`,
     `position`
   - `hook = NULL`, `contextualisation = NULL` (Stage 8's next
     run will fill these — see below)
   - `audio_url = NULL`
   - `review_status = 'proposed'`
5. Return `"added"`.

### Hook / contextualisation generation

Manually-added pieces are inserted with `hook = NULL`. Stage 8
(and Stage 8a for hooks specifically) already skips pieces that
have hooks — need to confirm it PROCESSES pieces with NULL
hooks. If not, add a small flag or rerun path so the operator
can populate them:

```
python -m aarva.daily --stage 8
```

Should be idempotent — pieces with existing hooks are skipped;
NULL-hook manual additions get hooks generated. Verify this is
already the case; if not, small tweak.

### Downstream flow after add

Once the piece is in `edition_pieces` with `review_status =
'proposed'`, it appears in the next `python -m aarva.review`
session and can be approved / rejected / dropped like any other
piece. Manually-added pieces integrate transparently with the
existing review loop.

---

## Non-goals

- **No web UI for either tool.** CLI only per user's decision.
- **No auto-registration of publications into
  `publications.yaml`.** Register-now (option b) inserts a DB
  row only. YAML remains manually curated.
- **No cross-edition adds.** "Today's edition" means today's;
  no `--edition <id>` flag in v1. If the operator wants to
  add to a past edition, they can wait for that need to
  actually arise before we build it.
- **No editable slot name.** All manual adds use
  `slot='manual_addition'`. Front-end templates should render
  this slot with a sensible label (or hide the slot entirely
  and just show as part of the edition body); if any listener-
  facing view depends on slot name, verify before shipping.
- **No dedupe against near-duplicate articles.** If the
  operator adds an article that closely mirrors one already in
  the edition, that's their call — the primitive accepts it.
  Only exact-article-id dedupe is enforced.

---

## Files that change

- `aarva/find.py` (new) — Feature A CLI.
- `aarva/ingest_url.py` (new) — Feature B CLI.
- `aarva/services/candidate_filter.py` (new) — shared "valid
  article" filter used by `find` (and optionally by Stage 7).
- `aarva/services/edition_ops.py` (new) — the shared
  "add article to today's edition" primitive.
- Existing extraction / stage helpers reused via imports —
  do NOT duplicate that code inside the new modules.
- `docs/roadmap.md` — after this PR merges, move the item from
  In-Progress to Recently Completed (Claude Code owns this per
  AGENTS.md rule 17).

---

## Verification

1. **Search — keyword:** `python -m aarva.find "concrete cities"`
   — confirm results include the ProPublica concrete article (or
   whatever's in the DB matching that phrase). No results for
   articles already in a past published edition or in rejections.
2. **Search — semantic:** `python -m aarva.find "urban heat
   islands"` (a phrase the article doesn't literally contain,
   but is topically close) — confirm semantic hits appear.
3. **Search — dedupe:** if an article matches both keyword and
   semantic, it should appear once (with the higher-signal
   source tag).
4. **Search — interactive add:** run without `--add`, pick two
   indices, confirm both end up in today's `edition_pieces` as
   `review_status='proposed'`, slot=`manual_addition`. Then run
   `python -m aarva.review` and confirm they appear in the
   listing.
5. **URL ingest — known pub:** `python -m aarva.ingest_url
   <a-propublica-url-not-yet-ingested>` — confirm article
   inserted, `publication_id` correctly matched.
6. **URL ingest — unknown pub, option (a):** ingest a URL from
   an unknown domain, choose (a). Confirm an "Ad hoc" pub row
   exists (created on first use) and the article's pub_id
   points to it.
7. **URL ingest — unknown pub, option (b):** ingest a URL,
   choose (b), give a name + country tag. Confirm the new pub
   row is inserted and the article's pub_id points to it.
   Confirm the country tag drives accent correctly at TTS.
8. **URL ingest + add:** `python -m aarva.ingest_url <url>
   --add-to-edition`. Confirm the article is in today's
   `edition_pieces` as proposed. Re-run Stage 8, confirm hook +
   contextualisation are populated. Run review, confirm it
   appears with hook.
9. **Idempotency:** re-run the same `find --add <id>` twice.
   Second attempt returns `"already_present"` and prints a
   no-op message. Same for `ingest_url` (URL that's already in
   the DB should short-circuit before re-extracting).

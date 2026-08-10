**STATUS: Shipped 2026-08-10.** See `docs/roadmap.md`'s 2026-08-10
entry for what actually landed, including the real crawl/embedding
verification performed both before and after implementation.

---

# Session plan — curation signal v1.5: digest-post link extraction + topic-similarity matching

Written by Claude Code, 2026-08-10, as a follow-up to the just-shipped
`docs/session_plan_curation_platform_signal.md` (implemented same day
— see `docs/roadmap.md`'s 2026-08-10 entry). Both extensions below
came directly out of real-data investigation the user asked for after
noticing the shipped v1 signal's exact-URL-only matching had an
extremely low hit rate (1 match out of 12,808 real Aarva articles when
tested against a real 6-source crawl).

Read this doc + `docs/roadmap.md` + `AGENTS.md` +
`docs/project_brief.md` before starting, per the standing session-start
protocol.

---

## Context — why this, why now

The shipped v1 signal (`aarva/services/curation_lookup.py`,
`aarva/sources/curation_crawler.py`) matches Aarva articles against
`curation_hits` by exact normalized URL only. Testing it against a
real crawl (131 hits across the 6 configured sources) and the full
real article catalog (12,808 embedded articles) found exactly **one**
exact-URL match. That's far too low a hit rate to meaningfully move
the 72-a-month `too_niche` rejection rate the whole feature exists to
address (see the original spec's data section).

Two independent, real findings came out of investigating why, both
confirmed against live data rather than assumed:

**1. Some sources' feed items are digest/newsletter-issue containers,
not individual article picks.** Why Is This Interesting?'s entire feed
consists of issue titles ("The Park Hyatt Tokyo Edition", "The
Wildberries Edition", "The Saturday Selection, Vol. 116") — the
crawler currently records the ISSUE's own title/URL as the "hit,"
but the issue's title describes the newsletter edition, not any
specific article. The real curated picks (with real, descriptive
titles and real external URLs) are embedded as links *inside* the
issue's HTML body, which the current crawler never opens. Confirmed
by fetching one such issue's `content:encoded` field directly and
extracting its `<a href>` tags — see "Real data: digest extraction"
below. Longreads has the same shape for its weekly "Top 5 Longreads
of the Week" post (2 of 25 items in one real crawl), though its other
23 items are genuine single-article picks with real titles.

**2. Even with clean single-article titles, exact-URL matching alone
is inherently rare**, because it requires Aarva and a curator to have
linked to the literal same piece — a coincidence, not something
happening at any real volume with only ~130 curated items in a
rolling 14-day window against Aarva's much larger, editorially
independent ingestion. Topic-level similarity (does this scored
article cover a similar subject to something a curator picked, even
if not the identical piece) would catch far more of what the signal
is actually trying to measure — "is this general subject reach broad
enough to interest a curious generalist" — without needing to
literally re-derive editorial quality (rigour/posture/self_implication
already do that job independently, at a much heavier 0.45/0.45
weight, and are untouched by this signal).

Both extensions are scoped together in this one doc because the first
directly improves the input data the second measures similarity
against — shipping topic-similarity without fixing the digest-post
noise would mean scoring articles against a mix of real article
titles and meaningless newsletter-issue titles.

---

## Architecture check (rule 17d)

1. **Where does the data live?**
   - `curation_hits` (main_db, already exists — `aarva/db.py`, added
     2026-08-10) gets two new columns: `embedding BLOB`,
     `embedding_model TEXT` — mirrors `articles.embedding` /
     `articles.embedding_model`'s existing storage shape exactly
     (verified: `aarva/db.py`'s `set_article_embedding()` writes
     `UPDATE articles SET embedding = ?, embedding_model = ? WHERE
     id = ?`; `articles.embedding` is read back via `np.frombuffer(row
     ["embedding"], dtype=np.float32)` at
     `aarva/services/episode_candidates.py:342` and `:438`).
   - No new tables. Digest-link extraction produces additional rows
     in the existing `curation_hits` table (same schema, just more
     rows per crawl for sources whose feed entries are container
     posts) — no schema change needed for that half.
   - Config: `aarva/config/curation_sources.yaml` (already exists)
     needs no new fields — link extraction is attempted uniformly for
     every source's entries when the feed provides full HTML content,
     not a per-source opt-in (see "Design — digest-post link
     extraction" below for why a uniform, source-agnostic approach was
     chosen over a per-source flag).
   - `pipeline.yaml`'s existing `curation:` block (`aarva/config/
     pipeline.yaml`, added 2026-08-10) gets one new key:
     `topic_similarity_floor` (default recommended below).

2. **Where does the operation run?**
   - Digest-link extraction: inside `aarva/sources/curation_crawler.py`
     — same place, same `--stage 0` laptop-CLI invocation
     (`aarva/daily.py`) as the existing crawl. No new execution
     surface.
   - Hit embedding: also inside the Stage 0 crawl, via the same
     `aarva.clients.embedding.build_embedding_client` machinery
     Stage 1.5 already uses (`aarva/stages/stage_1_5_consolidate.py`,
     confirmed via `client.embed(docs)` at line 180) — one embedding
     API call per crawl for whatever hits are newly inserted that run.
   - Topic-similarity lookup: inside `aarva/stages/stage_4_5_6_score.py`
     — same place the exact-URL lookup already lives (`curation_score_
     for()`, wired in during the 2026-08-10 implementation). Runs on
     the operator's laptop, same as all of Stage 4-5-6.
   - **No Render/web-app touch** — same as the original spec.

3. **Does the operation have physical access to the data it needs?**
   - Yes. `curation_hits` and `articles` both live in the same local
     main_db file; embeddings for both are computed via the same
     already-configured `embedding:` block in `pipeline.yaml` (confirmed:
     `aarva/stages/stage_1_5_consolidate.py:306-307` reads
     `config.raw.get("embedding", {})` and passes it to
     `build_embedding_client` — no new credential or network dependency
     beyond what Stage 1.5 already requires).
   - Confirmed via `aarva/daily.py`: Stage 1.5 (`stage is None or stage
     == 15`) runs before Stage 4+5+6 (`stage is None or stage in (4,
     456)`) in every full pipeline run, so by the time Stage 4-5-6
     scores an article, that article's own `embedding` column is
     already populated — no extra embedding call needed on the
     article side, only on the (much smaller) curation_hits side.

---

## Real data: digest-post link extraction

Fetched `https://whyisthisinteresting.substack.com/feed` directly and
inspected `entry.content[0].value` (feedparser exposes the full HTML
body for this feed, confirmed — see the per-source content-availability
table below). For the "The Saturday Selection, Vol. 116" entry,
extracting every `<a href="...">anchor text</a>` pair found:

| href | anchor text |
|---|---|
| `substackcdn.com/image/fetch/...` | *(empty)* |
| `wired.com/story/a-civilian-plane-crashed-in-new-mexico...` | "A Civilian Plane Crashed in New Mexico. Was the Military's Tech to Blame?" |
| `kieranvelasquez.substack.com/p/on-the-german-mittelstand` | "On the German Mittelstand" |
| `youtube.com/watch?v=DW0XUsyBBuY` | "The Gen Alpha Melody" |
| `argonaut71.substack.com/p/on-the-loss-of-my-friend` | "On the Loss of My Friend" |
| `whyisthisinteresting.substack.com/p/the-saturday-selection-vol-116` | "Read more" |

Four of these six are genuine, real, individually-titled external
picks — exactly the granularity the exact-URL matcher needs and
currently never sees, since the crawler only ever records the
container entry's own (title, url).

Also checked a non-roundup WITI "edition" post ("The Park Hyatt Tokyo
Edition") — its body contains exactly one external link, credited
"This originally appeared in my Skift column," pointing at
`skift.com/2026/07/21/returning-to-park-hyatt-tokyo-...`. This is
WITI's own essay, syndicated from an original Skift piece — extracting
that link is still correct behavior: it means an original Skift
article was judged interesting enough to adapt/repost, which is a
legitimate "not too niche" signal on that Skift piece specifically.

**Per-source content-field availability**, checked directly against
each of the 6 live feeds:

| Source | Full HTML body available? | Field |
|---|---|---|
| Longreads | Yes | `entry.content[0].value` (~2,400 chars typical) |
| 3 Quarks Daily | Yes | `entry.content[0].value` (~2,500 chars typical) |
| Kottke.org | Yes | `entry.content[0].value` (~1,200 chars typical) |
| Why Is This Interesting? | Yes | `entry.content[0].value` (up to ~12,000 chars) |
| Waxy.org | No | only `entry.summary` (~140 chars, too short to usefully contain multiple picks) |
| Hacker News | No | only `entry.summary` (~280 chars) — moot anyway, HN's own entry.link already points directly at the discussed URL |

Waxy.org and Hacker News simply won't yield extra hits from this — expected and fine, since both are already "direct" format sources where the entry's own (title, url) already is the real pick.

---

## Real data: topic-similarity calibration

Embedded all 131 real hits from a live crawl (titles only, matching
the current `curation_hits` schema) via the production embedding
client (`gemini-embedding-001-768` — confirmed as the exact same model
tag `articles.embedding_model` uses for the real catalog, so vectors
are directly comparable without a re-embed). Computed cosine similarity
(dot product — both sides are L2-normalized, per
`aarva/clients/embedding.py`'s `EmbeddingClient` contract) between
every hit and all 12,808 real, already-embedded Aarva articles.

**Distribution of each hit's single best-matching article, across the
whole catalog:**

| Percentile | Cosine similarity |
|---|---|
| min | 0.676 |
| p10 | 0.715 |
| p25 | 0.735 |
| p50 (median) | 0.754 |
| p75 | 0.784 |
| p90 | 0.814 |
| p95 | 0.842 |
| p99 | 0.888 |
| max | 0.909 |

**Manual inspection of real pairs at different score bands** (titles
only, judged for genuine topical relevance):

- **≥0.83 (top ~10 of 131):** every pair inspected was a clear, correct
  match — several near-duplicate stories (same event, different
  outlets, 0.87-0.91), several genuinely-related-but-distinct pieces
  (e.g. "Thomas Paine, America's Leveller" ↔ "Thomas Paine: The
  Consistent Revolutionary" at 0.836; "Socialists Are Winning Because
  They Listen to People..." ↔ "The Real Reason the Democratic
  Socialists Are Surging" at 0.841).
- **~0.75 (median band):** a genuine mix. Some real topical relevance
  (e.g. "Six Teenagers, a Brutal Murder..." ↔ "The Other Victims of a
  Wrongful Conviction" at 0.752, both wrongful-conviction stories).
  But also clear noise from the digest-post problem above: "The
  Saturday Selection, Vol. 116" (WITI's own container-post title, not
  describing any article) scored 0.756 against "Saturday assorted
  links" (a Marginal Revolution post) — two *generically-named roundup
  posts* matching each other on title-genre, not content.
- **≤0.72 (bottom ~15 of 131):** almost entirely spurious pairs with no
  real topical connection (e.g. "Ikea Complexity Index" ↔ "The Rug Belt
  Atlas and Quiz" at 0.716 — coincidental "quirky index/list" title
  resemblance; "Lou Koller... Punk Rocker" [an obituary] ↔ "Mitch
  McConnell: The Musical" at 0.677).

**Conclusion**: there's no single cliff-edge, but genuine relevance
holds up consistently from the top down to roughly **0.80-0.82**, and
degrades into title-genre-collision noise below that — the exact noise
this doc's digest-extraction fix should reduce, since several of the
worst offenders were bare container-post titles that link extraction
would replace with their real embedded picks' titles.

**Recommended starting floor: 0.80.** Framed as a starting point, not
a final answer — same posture as the existing `DEFAULT_EXISTING_MATCH_
FLOOR = 0.65` in `aarva/services/episode_candidates.py`, which that
file's own comment says was "chosen empirically against the current
19-episode catalog... revisit when catalog grows." This floor should
be re-checked once digest-link extraction is live and a fresh
calibration crawl can be run against the improved data (see
Verification, below).

---

## Locked decisions

1. **Digest-link extraction is uniform, not per-source.** Every
   source's feed entries get the same treatment: if `entry.content`
   (or `entry.summary` as fallback) contains extractable `<a href>`
   links with real anchor text, extract them as *additional*
   `curation_hits` rows, alongside the entry's own (title, url) row
   (which is still recorded exactly as today — this is purely
   additive, never a replacement). No new per-source config flag —
   avoids needing to classify "is this source digest-style," which
   turned out not to be a clean per-source property anyway (WITI mixes
   own-essay posts with genuine roundups; Longreads is 92% direct,
   8% roundup).
2. **Extraction filtering rules**, applied to every candidate
   `<a href>` found in an entry's body:
   - Skip if anchor text is empty or under 10 characters (drops "Read
     more" / "here" / bare image links).
   - Skip if the link's domain matches the feed's own domain (drops
     self-referential links back to the container post itself).
   - Skip if the link's domain is a known non-article host: CDN/media
     hosts (`substackcdn.com` and other asset-CDN patterns), video
     hosts (`youtube.com`, `youtu.be`, `vimeo.com`), and social-embed
     hosts (`twitter.com`, `x.com`) — extends (doesn't replace) the
     existing `_is_non_article_url` substring check already in
     `aarva/sources/rss.py` for path-based patterns
     (`/videos/`, `/podcasts/`, etc.), since a bare `youtube.com/watch`
     URL doesn't contain any of those path substrings.
   - Cap at 15 extracted links per entry (defends against a
     pathological entry; no real entry inspected came close to this).
3. **Topic-similarity matches count at a reduced weight relative to
   exact-URL matches**, reflecting the lower confidence of a fuzzy
   match: **0.7× the source's configured weight**, vs. full weight for
   an exact URL hit. Per source, only the single best-available match
   counts (exact hit takes priority over any fuzzy hit from the same
   source; among fuzzy-only hits from a source, only the
   highest-similarity one counts) — prevents a single prolific source
   (e.g. Kottke's ~58 items per 2-week crawl) from stacking multiple
   weak partial credits and dominating `curation_score`.
4. **`topic_similarity_floor: 0.80`** added to `pipeline.yaml`'s
   existing `curation:` block, next to `score_weight` and
   `crawl_window_days`. Independently tunable from `score_weight` —
   the floor controls what counts as a match at all; the weight
   controls how much a match is worth.
5. **This still composes additively with the exact-URL signal**,
   unchanged from the original spec's design — `curation_score` is
   still a single positive-only number fed into `ranking_score` at
   Stage 4-5-6 time, just now computed from two match types (exact +
   topic) instead of one.

---

## Data model

```sql
ALTER TABLE curation_hits ADD COLUMN embedding BLOB;
ALTER TABLE curation_hits ADD COLUMN embedding_model TEXT;
```

Added the same way every other post-launch column in this codebase
is added — as a `_LEGACY_COLUMN_ADDS` migration entry in
`aarva/db.py._init_schema()` (see the existing pattern immediately
above `articles.curation_score`'s own entry, added the same day this
table was created).

No change to the `PRIMARY KEY (source_name, url_normalized)` — an
extracted sub-link is just another row with its own real URL, subject
to the same idempotent `INSERT OR IGNORE` the crawler already uses.

---

## Scoring integration

Extends `aarva/services/curation_lookup.py::curation_score_for()`
(currently exact-match-only) with a second lookup path. The hit
embeddings MUST be loaded once per `score_all()` run and passed in as
a parameter — not loaded from inside `curation_score_for()` itself,
since that function is called once per article inside `_score_one()`
(`stage_4_5_6_score.py:225-229`), and `_score_one()` runs concurrently
across up to `max_workers` (default 8) threads via
`ThreadPoolExecutor`. Loading inside the function would re-run the
same query up to 8× concurrently per batch, once per article, instead
of once per run — the same mistake the existing `source_weights` load
already avoids (loaded once, before the `ThreadPoolExecutor` block, at
lines 171-176).

```python
# In curation_lookup.py:
def curation_score_for(db, canonical_url, source_weights, *,
                        article_embedding=None, hit_embeddings=None,
                        topic_similarity_floor=0.80):
    """hit_embeddings: pre-loaded list of (source_name, np.ndarray)
    pairs, loaded once per score_all() run via _all_hit_embeddings()
    — never loaded inside this function, which runs once per article
    across concurrent worker threads."""
    exact_hits = curation_lookup(db, canonical_url)
    exact_by_source = {h["source_name"]: source_weights.get(h["source_name"], 0.0)
                       for h in exact_hits}

    fuzzy_by_source = {}
    if article_embedding is not None and hit_embeddings:
        for source_name, hit_vec in hit_embeddings:
            if source_name in exact_by_source:
                continue  # exact match already at full weight, skip fuzzy
            sim = float(np.dot(article_embedding, hit_vec))
            if sim >= topic_similarity_floor:
                weight = source_weights.get(source_name, 0.0) * 0.7
                fuzzy_by_source[source_name] = max(
                    fuzzy_by_source.get(source_name, 0.0), weight
                )

    all_sources = set(exact_by_source) | set(fuzzy_by_source)
    return sum(
        max(exact_by_source.get(s, 0.0), fuzzy_by_source.get(s, 0.0))
        for s in all_sources
    )


def _all_hit_embeddings(db, embedding_model) -> list[tuple[str, np.ndarray]]:
    """Load (source_name, embedding) for every curation_hits row with
    a non-NULL embedding matching the configured model. Called once
    per score_all() run, mirroring the existing source_weights load."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT source_name, embedding FROM curation_hits "
            "WHERE embedding IS NOT NULL AND embedding_model = ?",
            (embedding_model,),
        ).fetchall()
    return [(r["source_name"], np.frombuffer(r["embedding"], dtype=np.float32))
            for r in rows]
```

```python
# In stage_4_5_6_score.py::score_all(), alongside the existing
# curation_enabled / source_weights block (lines 171-176). Needs a
# NEW dependency this file doesn't currently have: an embedding
# client, built only to read its .name tag (matching whichever model
# is currently configured) — gated on curation_enabled so the feature
# being off doesn't add an unnecessary client/credential check.
embedding_client = (
    build_embedding_client(config.raw.get("embedding", {}))
    if curation_enabled else None
)
hit_embeddings = (
    _all_hit_embeddings(db, embedding_client.name) if curation_enabled else []
)
```

At realistic table sizes (hundreds to low-thousands of rows over
months of crawling), a full linear scan per article is fast — no
approximate-nearest-neighbor indexing needed at this scale.

---

## Files that change

- `aarva/db.py` — two new `curation_hits` columns (migration entry).
- `aarva/sources/curation_crawler.py` — digest-link extraction (new
  helper, e.g. `_extract_embedded_links(entry, source_domain)`) +
  embedding the newly-inserted hits' titles via the same embedding
  client Stage 1.5 already builds.
- `aarva/services/curation_lookup.py` — `curation_score_for()` gets
  the fuzzy-match path; new `_all_hit_embeddings()` helper.
- `aarva/stages/stage_4_5_6_score.py` — the candidate SELECT
  (currently `a.id, a.title, a.full_text, a.published_date,
  a.canonical_url, p.name AS publication_name`, lines 143-155) needs
  `a.embedding, a.embedding_model` added — neither is currently
  selected. New import (`build_embedding_client`, not currently used
  in this file) to build an embedding client purely to read its
  `.name` tag, gated on `curation_enabled`. Load hit embeddings once
  per run (mirroring the existing `source_weights` load at lines
  171-176) and pass both that and the scored article's own embedding
  into `curation_score_for` as parameters — not loaded from inside the
  function, since it's called once per article inside `_score_one()`,
  which itself runs concurrently across up to `max_workers` (default
  8) threads.
- `aarva/config/pipeline.yaml` — `curation.topic_similarity_floor`.
- `docs/roadmap.md` — In-progress entry added in this same edit set.

---

## Verification

1. **Extraction smoke test**: re-run the crawler against the same 4
   sources confirmed to expose full content (Longreads, 3 Quarks
   Daily, Kottke.org, WITI) and confirm real sub-links are extracted
   with non-trivial anchor text, self-links and CDN/video links are
   correctly filtered out, and the entry's own (title, url) row is
   still inserted unchanged alongside the new sub-link rows.
2. **Unit tests for the filtering rules**: empty/short anchor text,
   same-domain self-links, each of the new non-article domain
   patterns, the 15-link cap.
3. **Embedding + fuzzy-match integration test**: against a disposable
   DB, insert a `curation_hits` row with a known embedding and an
   `articles` row with a deliberately-similar-but-not-identical
   embedding; confirm `curation_score_for` credits it at 0.7× weight,
   and confirm an exact-URL match on the same source suppresses the
   fuzzy path (no double-counting) per the locked per-source
   best-match-only rule.
4. **Real re-calibration after extraction ships**: re-run the full
   131-hit-style calibration (fresh crawl with extraction live,
   re-embed, re-measure the percentile table and manual spot-checks
   above) to confirm the digest-noise pairs (the "Saturday Selection"
   ↔ "Saturday assorted links" kind of match) have actually
   disappeared from the hit pool, and re-confirm or adjust the 0.80
   floor against the improved data before recommending the operator
   enable it.
5. **Editorial sanity check, same posture as the original spec**: a
   live double-scoring diff (curation on vs off) is the operator's
   post-enablement check, not a pre-merge gate — `curation.enabled`
   stays `false` by default, so merging this doesn't change any
   ranking until explicitly turned on.

---

## Non-goals

- **NOT re-fetching full article pages for Waxy.org/Hacker News.**
  Both already expose the real pick directly via `entry.link`; the
  extra network round-trip a full-page fetch would need isn't
  justified when the direct link already works.
- **NOT an LLM-based topic classifier.** Cosine similarity over
  existing embeddings reuses infrastructure Aarva already pays for and
  already trusts (Stage 1.5, `episode_candidates.py`); adding an LLM
  call per article per curation_hit would be far more expensive for a
  signal that's explicitly a small, positive-only nudge, not a
  primary ranking input.
- **NOT changing the exact-URL match's behavior or weight.** It stays
  exactly as shipped 2026-08-10 — full weight, unchanged priority over
  any fuzzy match on the same source.
- **NOT retroactively re-scoring already-scored articles** — same
  posture as the original spec; only future Stage 4-5-6 runs see the
  improved matching.

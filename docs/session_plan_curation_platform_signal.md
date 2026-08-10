**STATUS: Shipped 2026-08-10.** See `docs/roadmap.md`'s 2026-08-10 entry
for what actually landed — the source list changed materially during
implementation (3 of 5 specced sources turned out broken/inaccessible;
2 replacement candidates from training memory also turned out stale;
final list resolved via direct web search + verification), and the
scoring integration used Option A (persisted on `articles.
curation_score`) per explicit user choice, not this doc's own Option B
recommendation. This doc is kept as the historical record of the
original design — see the roadmap for what shipped.

---

# Session plan — curation-platform cross-check as a "not too niche" signal

Written by Cowork for the next Claude Code session (2026-08-10+).
Addresses the `too_niche` rejection pattern (72 rejections in the
last 30 days, per `edition_rejections` on 2026-08-10). Pulls in an
external signal — whether other curated-longform platforms have
picked the same article — to inform Stage 4-5-6 / Stage 7 scoring.
Distinct from the reviewer-learning-loop Phase 2 mechanism
(`docs/session_plan_reviewer_learning_loop.md`) because it uses
external editorial signal, not just re-processing of Aarva's own
past decisions.

Read this doc + `docs/roadmap.md` + `AGENTS.md` +
`docs/project_brief.md` before starting.

**AGENTS.md rule 4 sign-off**: this changes editorial ranking
behaviour by introducing a new positive-signal weight in Stage
4-5-6 / Stage 7 that reflects other publishers' curation choices.
User approved direction 2026-08-10 in the conversation that
produced this doc — after Cowork's first-pass alternatives (all
internal-only signals: taste centroids, LLM re-judgment, publication
base rates) were rejected as "not great" and the reframe onto
peer-curator signal was accepted. Locked calls below.

**AGENTS.md rule 6a**: this doc lists external URLs and RSS feed
paths. Claude Code MUST web-fetch each source before wiring it into
`curation_sources.yaml`. Cowork verified The Browser exists as a
Ghost-hosted site on 2026-08-10 (https://thebrowser.com/). The rest
are asserted from Cowork's memory and must be verified against
current vendor sites before use. If a UI label, feed path, or
domain has shifted, trust the vendor site over this doc.

---

## Context — why this, why now

**What "too_niche" means operationally.** In
`aarva/services/review_reasons.py:17`, the code
`too_niche` has the label *"Too niche — specialised beyond the
daily reader"*. This is the reviewer flagging an article that,
whatever its merits, is pitched at a narrower audience than a
curious generalist. Concrete examples from the 2026-07-27 →
2026-08-10 rejection log include several articles the reviewer
tagged as "too niche" in the `reason_note` free-text before
Cowork promoted the code to a first-class option: specialist
academic pieces, deep-industry-jargon reporting, niche cultural
in-jokes.

**Why this now — the data.** Query against
`edition_rejections` on 2026-08-10 (last 30 days, `reason NOT
NULL`), grouped by reason:

| reason | count |
|---|---|
| `other` | 94 |
| `too_niche` | **72** |
| `wrong_tone` | 23 |
| `too_long` | 16 |
| `video_dependent` | 5 |
| `transcript` | 4 |
| `listicle` | 1 |

`too_niche` is the top NAMED reason and well above the reviewer
learning loop's Phase 2 minimum-sample threshold of 5
(`docs/session_plan_reviewer_learning_loop.md:132-133`). It's
also structurally different from the other named codes — the
others are shape-of-article ('too long', 'listicle') or
metadata-of-article ('transcript', 'video_dependent'), which
Stage 2 can catch via mechanical rules. `too_niche` is about the
REACH of the article's topic — inherently harder for Stage 2 to
detect from the article alone.

**Path considered and rejected on 2026-08-10.** Cowork first
proposed four internal-only signals: (A) Phase 3 per-reason
taste centroid, (B) LLM niche-score in Stage 4-5-6, (C)
publication-level rejection base rate, (D) Wikipedia page-views
proxy. User rejected these as "not great" — reasoning: they all
recycle Aarva's existing internal signal (past reviewer
decisions, existing embeddings, existing publications), which
makes the current selection bias STICKIER rather than pulling
in new information. Bringing in the reviewer's own past
decisions as a filter risks reinforcing today's blind spots.

User's original proposals: (1) Reddit / social-media popularity
search, (2) mainstream-publication parallel coverage. Reddit was
rejected as editorially wrong for Aarva — per
`docs/project_brief.md:25-31`, Aarva is explicitly *"handpicked
articles meant to delight, indulge curiosity and expand the
mind. Not the anxiety-inducing cycle of breaking news."*
Filtering by Reddit popularity would push Aarva TOWARD trending
content, which is the opposite of what Aarva selects for.
Mainstream parallel coverage (News API-style) was rejected as
too expensive ($449/mo paid tier) for a signal that's editorially
weaker than what peer curators provide.

**Direction landed on — peer-curator cross-check.** The specific
audience Aarva serves — a curious generalist reader who wants
curated longform — is the same audience served by publications
like The Browser, Longreads, Arts & Letters Daily, Kottke.org,
MetaFilter. If an article is picked by these curators, it has by
definition cleared the taste bar of another editor targeting
Aarva's exact audience. That's a much cleaner "not too niche"
signal than either mass-social popularity (wrong audience) or
Aarva's own past decisions (recycled bias). It's also free (RSS
feeds), fast (~one nightly crawl), and editorially peer-aligned.

**Complementary, not replacement.** The Phase 3 per-reason taste
centroid from `docs/session_plan_reviewer_learning_loop.md:236-244`
should still ship — it captures "articles similar to ones the
operator rejected as too niche." That's orthogonal to "articles
that peer curators also picked." Both are useful signals; both
should compose additively in Stage 7's ranking, with independent
weights that can be tuned separately as real data comes in.

**What this does NOT try to fix.** Not tackling the 94 `other`
rejections (a separate promote-new-codes exercise, ongoing).
Not tackling `wrong_tone` (23 — different remediation path,
likely Stage 4-5-6 prompt tuning). Not tackling
`wrong_categorisation` (a new code candidate flagged in the
same 2026-08-10 conversation, needs its own accumulation window
before analysis). This spec is scoped to ONE signal for ONE
reason bucket. The user chose to address `too_niche` first
because it's the largest actionable bucket after `other`.

---

## Architecture check (rule 17d)

Grep outputs referenced below are from 2026-08-10.

**Reference greps:**

```
$ grep -nE "^def fetch_rss|^def ingest_rss|feedparser" aarva/sources/rss.py
15:import feedparser
123:    parsed = feedparser.parse(feed_text)

$ grep -nE "publications\.yaml|rss_url" aarva/config/__init__.py aarva/sources/rss.py aarva/stages/stage_1_ingest.py | head -8
aarva/config/__init__.py:45:    rss_url: str | None
aarva/config/__init__.py:190:    """Load aarva/config/publications.yaml."""
aarva/config/__init__.py:196:            rss_url=p.get("rss_url"),
aarva/sources/rss.py:88:    rss_url: str,
aarva/stages/stage_1_ingest.py:40:        rss_url=pub.rss_url,
aarva/stages/stage_1_ingest.py:130:        if not pub.enabled or not pub.rss_url:

$ grep -nE "CREATE TABLE" aarva/db.py | head -12
21:CREATE TABLE IF NOT EXISTS articles (
50:CREATE TABLE IF NOT EXISTS publications (
116:CREATE TABLE IF NOT EXISTS editions (
158:CREATE TABLE IF NOT EXISTS edition_pieces (
199:CREATE TABLE IF NOT EXISTS edition_rejections (
270:CREATE TABLE IF NOT EXISTS crosscut_embeddings (
300:CREATE TABLE IF NOT EXISTS daily_bonus_features (
312:CREATE TABLE IF NOT EXISTS pipeline_runs (

$ head -18 aarva/config/publications.yaml
# Aarva publication allowlist — v0.1
# Each entry is a publication. Fields:
#   name             — display name
#   rss_url          — RSS / Atom feed URL
#   homepage         — pub homepage (for reference)
#   tier             — A through H per kickoff doc §2
#   enabled          — true / false. Toggling is a one-line change.
```

**Now the three questions:**

1. **Where does the data live?**
   - **Curation-source config**: new file `aarva/config/curation_sources.yaml`,
     modelled on `publications.yaml` (shape verified via head above).
     Each source: name, url, feed_url (RSS if available; scrape path
     otherwise), weight (0.0-1.0), enabled flag, notes.
   - **Curation hits**: new table `curation_hits` on the main DB,
     alongside existing tables at `aarva/db.py` (line 21+). Rows:
     `(source_name, url, url_normalized, title, seen_at)`. Rows land
     from a nightly crawl, one row per (source, url) — dedup on
     `(source_name, url_normalized)` primary key.
   - **Article ↔ curation match**: derived at query time via URL
     normalization join between `articles.canonical_url` and
     `curation_hits.url_normalized`. No new join table — a small
     helper computes matches on the fly, which keeps the schema
     minimal and lets URL-normalization improvements over time
     re-match old hits without a backfill.

2. **Where does the operation run?**
   - **Nightly curation crawl**: on the operator's laptop, alongside
     Stage 1 ingestion. Reuses `aarva/sources/rss.py::feedparser`
     (verified above) for RSS feeds; a small scraper helper for
     non-RSS sources. New CLI subcommand
     `python -m aarva.daily --stage curation` OR wire into Stage 1
     as a sibling loop (Claude Code picks).
   - **Match + scoring integration**: on the operator's laptop
     during Stage 4-5-6 (per-article scoring) — the curation hit
     lookup + bump gets added to the ranking-score composition.
   - **No Render/web-app touch.** The web app reads scored articles
     as-is; curation signal only affects the score DURING assembly.

3. **Does the operation have physical access to the data it needs?**
   - **Crawler + DB write**: yes — main_db is local on the laptop;
     RSS feeds are public URLs; no auth needed for the tier-1 free
     sources (The Browser's PAYWALLED FULL FEED is out of scope for
     v1; see "Locked decisions" #6).
   - **Stage 4-5-6 read**: yes — reads main_db locally.
   - **No cross-DB writes** — curation_hits lives entirely on main_db
     and Aarva's laptop-side pipeline uses it.

---

## Locked decisions (with user, 2026-08-10)

1. **This complements, does NOT replace, Phase 3's `too_niche` taste
   centroid** from `session_plan_reviewer_learning_loop.md:236-244`.
   Both can coexist: the taste centroid captures "articles like ones
   the operator rejected as too niche"; the curation signal captures
   "articles other editorial curators picked." They're orthogonal —
   use both, weighted separately, tune independently.
2. **Signal shape: positive-only.** Getting picked by a curator is a
   positive bump; not being picked is NOT a penalty. Rationale:
   Aarva ingests hundreds of articles a day, most won't cross into
   any curator's picks in the same week. Absence of a hit is not
   evidence of niche-ness. Only presence is used.
3. **Weighted sum across sources.** Each source has a `weight`
   (0.0-1.0). Composite `curation_score = sum(weight for each hit)`.
   Fed into Stage 4-5-6 or Stage 7 as a small additive positive
   term. Starting weights TBD by Claude Code based on realistic
   crawl volume; user tunes after first two weeks of live data.
4. **Sources start conservative — 4-5 sources for v1**, all free.
   Expand list in follow-up PRs as signal quality validates.
5. **Two-way benefit**: same crawl surfaces articles picked by
   peer curators that Aarva HASN'T ingested. Feed those into
   `aarva/ingest_url.py` as "operator suggestions" — a new CLI
   subcommand `python -m aarva.rss_add --curator-suggestions`
   (name TBD, Claude Code picks). Out of primary scope for v1 but
   the crawl data supports it — mention in Non-goals so Claude Code
   knows it's a plausible v1.5 extension, not something to design
   for now.
6. **Paywalled or subscriber-only feeds are out of scope for v1.**
   The Browser's core recommendations are paywalled; only its
   sample/free feed is accessible. Rely on that. If the operator
   later subscribes and wants to feed authenticated content in,
   spec a follow-up.
7. **URL normalization is required.** Curator platforms often link
   through tracking-parameter-heavy URLs (utm_source, ref, etc.);
   Aarva stores canonical_url without those. Normalize both sides
   before match. See "URL normalization" section below.

---

## Data model

New table (main_db, alongside `daily_bonus_features` at
`aarva/db.py:300`):

```sql
CREATE TABLE curation_hits (
    source_name       TEXT NOT NULL,
    url               TEXT NOT NULL,
    url_normalized    TEXT NOT NULL,
    title             TEXT,
    seen_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_name, url_normalized)
);
CREATE INDEX idx_curation_hits_normalized
    ON curation_hits(url_normalized);
```

Rationale:
- PRIMARY KEY on `(source_name, url_normalized)` — one hit per
  (source, article-URL). Re-crawls are idempotent.
- Separate `url` (as-seen from the curator) and `url_normalized`
  (canonical form for matching against Aarva's articles). Keeps the
  original around in case normalization rules change.
- `title` for debuggability + for the "unpicked-by-Aarva" suggestion
  path.
- `seen_at` for freshness — later Stage 4-5-6 integration can
  weight recent hits more.

---

## Curation source list (v1 — verify each before wiring)

**Rule 6a: Claude Code MUST web-fetch each of these before adding to
`curation_sources.yaml`.** If a URL 404s or the RSS shape has
changed, adjust or drop the source and note the change in the
implementation PR's description.

| Source | Homepage | Feed URL (verify!) | Access | Suggested weight | Notes |
|---|---|---|---|---|---|
| The Browser | https://thebrowser.com/ | try `https://thebrowser.com/rss/` (Ghost default) | Sample-only free; core paywalled | 1.0 | Tier-1 editorial peer. Verify what the free feed exposes — may be titles-only. |
| Longreads | https://longreads.com/ | try `https://longreads.com/feed/` | Free | 0.8 | Includes their weekly Top 5 picks. |
| Arts & Letters Daily | https://www.aldaily.com/ | Feed likely at `/feed/rss/` — verify | Free | 0.9 | Three curated picks/day, Aarva-aligned aesthetic. |
| Aeon (Ideas / Essays) | https://aeon.co/ | https://aeon.co/feed.rss (already in Aarva's publications.yaml) | Free | 0.5 | LOWER weight because Aeon is ALSO an Aarva-ingested publication — a hit means "Aeon published it themselves," not cross-curation. Include only if we can distinguish Aeon's original essays from external picks. |
| Kottke.org | https://kottke.org/ | https://feeds.kottke.org/main | Free | 0.6 | One-person curator, high aesthetic alignment. |
| MetaFilter | https://www.metafilter.com/ | https://www.metafilter.com/rss.xml | Free | 0.5 | Broader than Aarva but quality-moderated. |

Skip for v1 (potentially add later per decision #4):

- **Hacker News** — `https://hnrss.org/frontpage?points=200` filters
  to score ≥ 200. Tech-skewed; consider only if targeted broad-
  interest filter can be applied on Aarva's side.
- **/r/TrueReddit** & **/r/InDepthStories** — Reddit RSS still
  works but rate-limited. Small signal, skip for v1.
- **The Sunday Long Read** — newsletter format, RSS path unclear;
  verify separately.

---

## Ingestion flow

Nightly (or as part of Stage 1's daily run):

1. Load `curation_sources.yaml` (helper mirroring
   `load_publications` at `aarva/config/__init__.py:190`).
2. For each source where `enabled: true`:
   - Fetch feed via `aarva/sources/rss.py::feedparser` (already
     imported per grep above) if it's RSS.
   - For non-RSS sources (e.g. Arts & Letters Daily might need
     HTML scrape), a small scraper helper. Use `trafilatura` (already
     a dep — Aarva uses it in ingest_url).
   - Extract (title, url) pairs from the recent items (last ~14 days;
     configurable per source in yaml).
3. For each (title, url), compute `url_normalized` — see below.
4. `INSERT OR IGNORE INTO curation_hits ...`. Idempotent — re-crawls
   don't dup.
5. Log summary: N hits added, M already-seen, K matched to
   existing Aarva articles.

---

## URL normalization

Aarva already stores `canonical_url`. Curator sources link with
tracking params. Normalization rules:

1. Lower-case the scheme + host.
2. Strip common tracking query params: `utm_*`, `ref`, `ref_src`,
   `referer`, `source`, `mc_cid`, `mc_eid`, `fbclid`, `gclid`.
3. Strip URL fragments (`#anchor`).
4. Strip trailing slashes.
5. Optionally: follow HTTP redirects one level to resolve
   shortlinks (e.g. Substack sometimes redirects) — but only if
   the response is fast (<2s timeout).

Store the raw curator URL in `url`; store the normalized form in
`url_normalized`. Match Aarva articles against
`url_normalized` — Aarva's `articles.canonical_url` also gets
normalized on the fly for the join (small helper, no schema change
to `articles`).

---

## Scoring integration

Two integration points, Claude Code picks the cleaner one:

**Option A: Stage 4-5-6 (per-article scoring, `stage_4_5_6_score.py`).**
Add a `curation_score` field to the scored article. Composed as:
```python
curation_score = sum(
    src.weight for src in curation_source_lookup(
        article.canonical_url_normalized
    )
)
```
Add to the article's `ranking_score` with small weight (e.g. `+0.10
* curation_score`). Configurable in `pipeline.yaml`.

**Option B: Stage 7 (assembly, `stage_7_assemble.py`).**
Same computation but applied only during candidate ranking, not
persisted to the article row. Cheaper to iterate on the weight tuning.

**Cowork's recommendation: B first.** Faster to iterate on the
weight without a schema change or a re-score of the whole catalog.
If the signal proves valuable, promote to Stage 4-5-6 (persist
`curation_score` on `articles`) in a follow-up.

Config addition to `pipeline.yaml`:

```yaml
curation:
  enabled: true
  score_weight: 0.10             # multiplier on the sum-of-source-weights
  crawl_window_days: 14          # recent hits only, per source
```

---

## Files that change

- `aarva/config/curation_sources.yaml` (new).
- `aarva/config/__init__.py` — new `CurationSource` dataclass +
  `load_curation_sources()` loader mirroring `load_publications`.
- `aarva/db.py` — new `CREATE TABLE curation_hits` + index.
- `aarva/sources/curation_crawler.py` (new) — orchestrates the
  nightly crawl loop. Reuses `feedparser` from `sources/rss.py`.
- `aarva/services/curation_lookup.py` (new) — the URL-normalize +
  lookup helper that Stage 7 (or Stage 4-5-6) calls.
- `aarva/stages/stage_7_assemble.py` — integrate `curation_score`
  into the ranking composition (option B above).
- `aarva/daily.py` — new `--stage curation` subcommand (or fold into
  Stage 1 — Claude Code picks).
- `aarva/config/pipeline.yaml` — new `curation:` block.
- `docs/roadmap.md` — In-progress entry added in the same edit set
  as this spec (per rule 17a and the 2026-07-28 addition).

---

## Verification

1. **Fetch smoke test:** Run the crawler once against v1 sources.
   Confirm each source returns at least 1 item AND at least one item
   was inserted into `curation_hits`.
2. **URL normalization test:** Unit tests for the normalizer —
   include tracking params, fragments, shortlink expansion, trailing
   slashes. Cover the top 5 tracking-param shapes seen in actual
   curator URLs.
3. **End-to-end match test:** Manually pick an article that's in
   `articles` AND was picked by a curator in a recent hit. Confirm
   `curation_lookup(canonical_url)` returns the hit with the
   correct source weight.
4. **Editorial sanity check:** Before enabling `curation.enabled=true`
   in `pipeline.yaml`, run Stage 7 twice back-to-back on the same
   candidate pool — once with the curation-signal off, once on. Diff
   the top-20 rankings. Confirm the shifts are small (individual
   articles moving 1-3 slots), not swamping other signals. Adjust
   `score_weight` if the shift is too large.
5. **Real daily run:** Enable in `pipeline.yaml`, run a real daily
   pipeline. Confirm no errors, and manually inspect which articles
   received the curation bump — sanity-check the operator agrees
   the bumped articles are indeed "not too niche."
6. **`too_niche` rejection-rate over 2-3 weeks:** track whether the
   fraction of `too_niche` rejections in `edition_rejections`
   decreases materially post-enablement. This is the ultimate
   ground-truth check that the signal is real.

---

## Non-goals

- **NOT authenticated / paywalled feeds** (see locked decision 6).
- **NOT a full "operator-suggested articles from curators"
  discovery UX.** The crawl surfaces hits Aarva hasn't ingested,
  but the surfacing is deferred to a follow-up. Just make sure the
  crawler stores the data (it does — `curation_hits` has the URL
  even without an Aarva article match).
- **NOT autonomous enablement of new curation sources.** Every new
  source is a manual `curation_sources.yaml` addition — same
  posture as `publications.yaml`.
- **NOT retroactive scoring.** Only new Stage 4-5-6 / Stage 7 runs
  benefit. Existing scored articles keep their current
  `ranking_score`. If needed later, a `scripts/rescore_from_curation.py`
  can be written — not in scope.
- **NOT a replacement for the reviewer learning loop's Phase 3
  taste centroids.** Both coexist. This spec adds ONE new signal;
  the Phase 3 spec adds ANOTHER. They compose additively.
- **NOT Reddit/social-media popularity signal.** Explicitly rejected
  in the 2026-08-10 conversation as an editorially-wrong direction
  for Aarva (`docs/project_brief.md:25-31` — anti-anxiety-cycle-
  of-breaking-news, curated over trending).

---

## Rollout

- Ship as one PR touching the files above. Curation crawl OFF by
  default (`enabled: false` in `pipeline.yaml`) so the operator can
  merge without an editorial-behaviour change on day 1.
- Operator manually enables via `pipeline.yaml` after inspecting the
  first crawl's output.
- Weight tuning is a follow-up conversation once real data is in.

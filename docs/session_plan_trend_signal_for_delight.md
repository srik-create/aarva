# Session plan — trend-signal layer for delight + timeliness

Written by Cowork for the next Claude Code session (2026-08-13+).
Adds an external-signal layer that watches trending-topic sources
(Google Trends, YouTube Trending, GDELT), semantically matches each
trend to Aarva's existing article catalog, and surfaces matches to
the operator during the daily review CLI as candidates for the
delight / bonus slot. When a trend has no match in Aarva's own
vector space, a GDELT fallback searches for coverage of the trend
across Aarva's publication allowlist — so the operator can pull in
a trusted-source treatment via `aarva.ingest_url`.

Distinct from `docs/session_plan_curation_platform_signal.md`:
that spec pulls in *peer-curator* signal (The Browser, Longreads,
Kottke) — signal of "other editors picked this." This spec pulls in
*trend* signal (mainstream popularity indices) — signal of "the
wider world is paying attention to this." Both use the same
integration point (Stage 7 / review CLI) with independent weights.

Read this doc + `docs/roadmap.md` + `AGENTS.md` +
`docs/project_brief.md` before starting.

**AGENTS.md rule 4 sign-off**: this changes editorial ranking
behaviour by introducing a new positive-signal path AND surfaces
"trending topics" to the operator during review. User approved
direction 2026-08-13 in the conversation that produced this doc,
including specific source rejections (Weibo and Reddit removed,
prompt language "essayistic" removed).

**AGENTS.md rule 6a**: this doc lists external URLs, library
names, and API endpoints. Claude Code MUST web-fetch each source
(pytrends GitHub, YouTube Data API v3 pricing, GDELT DOC API) and
verify current availability + quota policy before wiring. Cowork
did NOT re-verify these on 2026-08-13 beyond citing YouTube's
current YPP page for ad-policy context; treat all source URLs and
library versions as need-to-verify.

---

## Context — why this, why now

**The operator's ask (2026-08-13):** more delight in the daily
edition, specifically: "how could i add in something on a topic
that is going viral? so it can capture the imagination and
potentially new listeners, but offer info on that topic from a
trusted source?" Example given: a video that went viral in China
(YouTube URL `https://www.youtube.com/watch?v=I1vcbEGmYLg`) —
Cowork couldn't fetch it in the same conversation due to
web-fetch rate limits, so didn't verify its specific content.

**Why viral-topic sourcing needs its own layer.** Aarva's existing
ingestion (~70 publications via RSS per `aarva/config/publications.yaml`)
doesn't watch popularity/trend indices. Something can be enormously
attention-capturing in the wider world without any of Aarva's 70
publications having covered it yet — or having covered it, but not
scored high enough to reach the daily edition. The `delight` JTBD
already exists (`aarva/config/prompts.yaml:127-131` — *"LIGHT, FUN,
PLAYFUL — humour, wit, oddities"*), but it's populated organically
from the ingestion pipeline; nothing surfaces "what the wider world
is talking about that Aarva might have or should get."

**Explicit rejections in the 2026-08-13 conversation.** User
rejected Reddit and Weibo as sources — Reddit for Western-tech
skew, Weibo for scraping fragility + Chinese-language matching
complexity. User approved Google Trends (multi-region including
IN, which reflects Aarva's likely core audience geo), YouTube
Trending (multi-region), and GDELT (global news event tracker,
free API). User also removed the word "essayistic" from the
query-expansion prompt — Aarva's delight/curiosity slots aren't
essayistic-only; the trend match should be shape-agnostic.

**Direction landed on — three-stage flow.** (1) Nightly crawl of
the three approved trend sources; (2) LLM query expansion + semantic
match against Aarva's already-ingested + embedded article catalog
(`articles.embedding` per `aarva/db.py:35`); (3) fallback for
no-match trends: GDELT DOC API search filtered to Aarva's
publication allowlist domains, surfacing candidate URLs the
operator can pull in via existing `aarva.ingest_url`.

**Editorial risk explicitly flagged (rule 4).** "Viral" and
"curated slow" are in tension. `docs/project_brief.md:29-32`
frames Aarva as *"handpicked... not the anxiety-inducing cycle of
breaking news."* The trend-signal layer must NOT nudge Aarva
toward breaking-news patterns. Guardrails locked below: age
filter, lens filter, JTBD filter, trend blacklist.

---

## Architecture check (rule 17d)

Grep outputs referenced below are from 2026-08-13.

**Reference greps:**

```
$ grep -nE "CREATE TABLE|articles\s*\(" aarva/db.py | head -10
21:CREATE TABLE IF NOT EXISTS articles (
50:CREATE TABLE IF NOT EXISTS publications (
116:CREATE TABLE IF NOT EXISTS editions (
158:CREATE TABLE IF NOT EXISTS edition_pieces (
199:CREATE TABLE IF NOT EXISTS edition_rejections (
270:CREATE TABLE IF NOT EXISTS crosscut_embeddings (
300:CREATE TABLE IF NOT EXISTS daily_bonus_features (

$ sed -n '35,36p' aarva/db.py
    embedding       BLOB,      -- float32 numpy bytes, L2-normalised
    embedding_model TEXT,      -- name of the model used (for invalidation on swap)

$ grep -nE "^def add_article_to_todays|^def _apply_decisions" aarva/services/edition_ops.py aarva/review.py
aarva/services/edition_ops.py:21:def add_article_to_todays_edition(
aarva/review.py:415:def _apply_decisions(

$ grep -nE "publications\.yaml|rss_url" aarva/config/__init__.py aarva/sources/rss.py aarva/stages/stage_1_ingest.py | head -6
aarva/config/__init__.py:190:    """Load aarva/config/publications.yaml."""
aarva/sources/rss.py:88:    rss_url: str,
aarva/stages/stage_1_ingest.py:130:    if not pub.enabled or not pub.rss_url:
```

**Now the three questions:**

1. **Where does the data live?**
   - **Trend hits**: new `trend_hits` table on main_db, alongside
     `daily_bonus_features` (`aarva/db.py:300`). Rows: source_name,
     trend_phrase, trend_phrase_en (translated to English if source
     is non-English), region, seen_at, raw_metadata_json,
     matched_article_id (nullable), match_score, operator_action,
     resolved_at.
   - **Source config**: new `aarva/config/trend_sources.yaml`,
     modelled on `publications.yaml` (verified via
     `config/__init__.py:190`). Each source: name, kind, region,
     weight (0.0-1.0), enabled, notes.
   - **Article vector space**: existing `articles.embedding BLOB`
     column (`aarva/db.py:35` — L2-normalised float32 bytes,
     produced by the Vertex AI `gemini-embedding-001` client, 768-dim
     Matryoshka, per `docs/project_brief.md:72`). No schema
     change to `articles` — read-only.
   - **Publication allowlist for the GDELT fallback**: existing
     `publications.yaml` + `publications` table. Reused unchanged.

2. **Where does the operation run?**
   - **Trend crawler**: on the operator's laptop, nightly. New
     stage `--stage trends` OR sibling of Stage 1 (Claude Code
     picks; Cowork's preference: sibling of Stage 1 since the
     cadence is identical).
   - **Query expansion + semantic match**: on the operator's
     laptop, after the crawl. Reuses the existing Vertex AI
     embedding client (`aarva/clients/embedding.py`) and the
     existing LLM client (`aarva/clients/llm.py`).
   - **GDELT fallback search**: on the operator's laptop, only
     for no-match trends. Queries the free GDELT DOC API.
   - **Surface to operator**: in `python -m aarva.review`, new
     "Trending" section at the top of the review CLI.
   - **Add-to-edition**: reuses the primitive
     `aarva/services/edition_ops.py::add_article_to_todays_edition`
     (verified above at `edition_ops.py:21`, shipped 2026-07-22
     per `docs/roadmap.md`).

3. **Does the operation have physical access to the data it needs?**
   - **Crawler + DB write**: yes — main_db is local; trend APIs
     are public.
   - **Semantic match against articles.embedding**: yes — all in
     local main_db.
   - **GDELT fallback**: yes — GDELT DOC API is a free HTTP
     endpoint, no auth. Aarva's publication allowlist domains
     come from `publications.yaml`.
   - **Review CLI extension**: yes — reads/writes main_db locally.

---

## Locked decisions (with user, 2026-08-13)

1. **Three sources only in v1.** Google Trends (multi-region),
   YouTube Trending (multi-region), GDELT (global). Explicitly
   rejected 2026-08-13:
   - **Weibo hot search** — Chinese-language matching adds
     complexity; scraping fragility; declined.
   - **Reddit r/all** — Western-tech skew; declined.
   - **X / Twitter trends** — Basic API tier starts at $200/mo;
     signal not worth the cost.
2. **Positive-only signal.** A trend match is a positive nudge to
   the operator ("consider adding this piece to today's delight
   slot"). Never used as a filter — trends do not penalise
   candidates. This preserves Aarva's editorial baseline.
3. **Semi-automatic, not autonomous.** Trends are surfaced during
   `python -m aarva.review`; the operator explicitly picks add /
   dismiss per trend. No trend gets added to a daily edition
   without an operator keystroke. Preserves the "handpicked"
   thesis in `docs/project_brief.md:29-32`.
4. **Vector-space match FIRST, GDELT fallback SECOND.** For each
   trend: (a) query-expand → embedding-match against Aarva's
   catalog; (b) if no match ≥ threshold, GDELT DOC search
   restricted to allowlist domains from `publications.yaml`.
   GDELT returns candidate URLs, surfaced to operator for
   `aarva.ingest_url`.
5. **LLM query-expansion prompt language.** Locked verbatim
   2026-08-13 after "essayistic" was struck: *"Given the trending
   topic '{trend_phrase_en}', what would relevant articles look
   like? Give 3 alternative descriptive phrasings that capture the
   same underlying interest."*
6. **Editorial guardrails on matched articles.** Trend matches are
   only surfaced when the Aarva article passes ALL of:
   - **Age**: article's `published_date` is ≥ 48 hours old. Keeps
     the "curated slow" posture — a trend proposal reaches into
     something Aarva already selected, not breaking news.
   - **JTBD**: article's `jtbd_primary` is one of `delight`,
     `curiosity`, `smart_escape`, or `keep_ahead`. Excludes pure
     `keep_up_to_date` (news-shaped, wrong slot for trend
     surfacing).
   - **Not already in an edition**: article's status is `scored`,
     not `in_edition`.
   - **Not already trend-surfaced today**: same trend-article
     pair not surfaced in the current 7-day window.
7. **Trend blacklist.** Config-level list of trend phrases to
   never surface. Grow-as-needed. Starter list to include (from
   user's editorial preferences): political-personality names
   that trend for controversy cycles, celebrity-death topics.
   User to fill starter list during first-week tuning; empty at
   ship-time is acceptable.
8. **Cost cap.** LLM spend for the trend layer must stay under
   $5/month at current scale. Verified against estimate below.

---

## Sources config

New file `aarva/config/trend_sources.yaml`, shape mirroring
`publications.yaml`:

```yaml
# Aarva trend sources — v0.1
# Watched nightly by the trend crawler; produces `trend_hits` rows
# that get matched to Aarva's article catalog for delight-slot
# surfacing during review. See docs/session_plan_trend_signal_for_delight.md.

trend_sources:
  # ─── Google Trends ─────────────────────────────────────────────
  - name: google_trends_us
    kind: google_trends
    region: us
    weight: 0.7
    enabled: true

  - name: google_trends_in
    kind: google_trends
    region: in
    weight: 0.8   # Aarva's likely core audience geography
    enabled: true

  - name: google_trends_uk
    kind: google_trends
    region: uk
    weight: 0.7
    enabled: true

  - name: google_trends_global
    kind: google_trends
    region: global
    weight: 0.6
    enabled: true

  # ─── YouTube Trending ──────────────────────────────────────────
  - name: youtube_trending_us
    kind: youtube_trending
    region: us
    weight: 0.6
    enabled: true

  - name: youtube_trending_in
    kind: youtube_trending
    region: in
    weight: 0.7
    enabled: true

  - name: youtube_trending_global
    kind: youtube_trending
    region: global
    weight: 0.5
    enabled: true

  # ─── GDELT (global news events tracker) ────────────────────────
  # Also used as the fallback article-search when a trend has no
  # match in Aarva's own vector space — see "Matching flow" below.
  - name: gdelt_global
    kind: gdelt
    region: global
    weight: 0.6
    enabled: true
```

Weights are hints for how much the operator should trust each
source's suggestions — they compose into the display order in the
review CLI, not into any auto-add logic.

---

## Data model

New table on main_db (add alongside `daily_bonus_features` at
`aarva/db.py:300`):

```sql
CREATE TABLE trend_hits (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name         TEXT NOT NULL,
    trend_phrase        TEXT NOT NULL,            -- as extracted from source
    trend_phrase_en     TEXT,                     -- translated if needed
    region              TEXT,                     -- 'us'/'in'/'uk'/'global'
    seen_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    raw_metadata_json   TEXT,                     -- source-specific extras
    matched_article_id  INTEGER,                  -- populated by matcher
    match_score         REAL,                     -- LLM re-rank 1-5
    operator_action     TEXT,                     -- 'added'|'dismissed'|NULL
    resolved_at         TIMESTAMP,
    FOREIGN KEY (matched_article_id) REFERENCES articles(id)
);
CREATE INDEX idx_trend_hits_seen_at ON trend_hits(seen_at);
CREATE INDEX idx_trend_hits_action  ON trend_hits(operator_action, seen_at);
```

Semantics:
- `operator_action IS NULL` → unresolved; the review CLI surfaces
  these at the top.
- `matched_article_id IS NULL` after matching → no in-catalog
  match; GDELT fallback runs for this trend.
- `raw_metadata_json` preserves source-specific extras (subreddit
  score, YouTube video-id + view count, GDELT tone/theme codes)
  for debuggability and later analysis.

---

## Ingestion flow

New module `aarva/sources/trend_crawler.py`. Orchestrates the
nightly crawl. One handler per `kind`:

- **`google_trends`**: use the `pytrends` library (Cowork asserts
  from memory that `pytrends` is stable and widely used —
  Claude Code must web-verify current status + rate-limit policy
  before adding to `requirements.txt`). Endpoint:
  `pytrends.request.TrendReq(hl='en-US').trending_searches(pn='<region>')`
  or `daily_trending_searches`. Returns top ~20 daily queries per
  region.
- **`youtube_trending`**: YouTube Data API v3,
  `GET videos?chart=mostPopular&regionCode=XX&maxResults=50`. Needs
  an API key (new env var `AARVA_YOUTUBE_API_KEY`; free tier is
  10k quota units/day, `videos.list` costs 1 unit — Claude Code
  must web-verify current pricing). Extract video titles as
  trend phrases.
- **`gdelt`**: GDELT DOC API,
  `https://api.gdeltproject.org/api/v2/doc/doc?query=<...>&format=json&maxrecords=250&timespan=24h`.
  For trend-source use: query GDELT's "top themes" endpoint OR
  `format=timelinevol` on recent global events. Cowork's
  understanding of the exact top-themes endpoint is uncertain;
  Claude Code must web-verify against GDELT's current docs.
- **Translation (non-English sources)**: if `trend_phrase` is not
  English, Gemini call translates → `trend_phrase_en`. Cached by
  `(source_name, trend_phrase)` to avoid re-translation.

Insert each into `trend_hits`. Idempotent by `(source_name,
trend_phrase, seen_at::date)` — re-runs same day skip existing.

Runs as `python -m aarva.daily --stage trends` (new subcommand)
OR folded into Stage 1's ingestion loop (Claude Code's call; the
crawl is fast and lightweight, sibling to Stage 1 is fine).

---

## Matching flow

For each unresolved `trend_hits` row after crawling:

### Vector-space match FIRST

1. **LLM query expansion** — Gemini prompt, locked verbatim per
   decision #5:

   > Given the trending topic '{trend_phrase_en}', what would
   > relevant articles look like? Give 3 alternative descriptive
   > phrasings that capture the same underlying interest.

   Output: JSON with 3 phrasings. Cost: ~$0.0002 per trend.

2. **Semantic retrieval** — for each of the 3 phrasings, embed
   via Vertex AI (`aarva/clients/embedding.py`'s embed_query), then
   cosine-similarity against `articles.embedding` (float32 numpy
   bytes, L2-normalised per `aarva/db.py:35`) with the guardrails
   applied at SQL time:

   ```sql
   -- article filter for trend matching
   WHERE status = 'scored'
     AND embedding IS NOT NULL
     AND published_date <= datetime('now', '-48 hours')
     AND jtbd_primary IN ('delight','curiosity','smart_escape','keep_ahead')
   ```

   Union the top-10 from each phrasing → ~30 candidates. Cheap
   (~$0.0001 in embedding cost).

3. **LLM re-rank** — Gemini scores each candidate 1-5 for how
   well it fits the trend interest. Prompt notes: NO "essayistic"
   framing; shape-agnostic. Cost: ~$0.005 per trend for the
   re-rank call.

4. **Threshold** — best candidate with score ≥ 3.5/5 →
   `matched_article_id` + `match_score` populated. Otherwise
   → NULL, fallback runs.

### GDELT fallback for no-match trends

For each `trend_hits` row where the vector-space match failed:

1. Load the domain list from `publications.yaml`'s `homepage`
   fields (Aarva's allowlist).
2. Query GDELT DOC API for articles matching the trend phrase,
   filtered to those domains:

   ```
   GET https://api.gdeltproject.org/api/v2/doc/doc?
       query=<trend_phrase_en>+(domain:aeon.co OR domain:theatlantic.com OR ...)&
       format=json&maxrecords=25&timespan=14d
   ```

3. GDELT returns URLs. Populate a new column on `trend_hits`:
   `fallback_urls_json` (JSON list of candidate URLs). Add a
   migration for this column in the same PR.
4. Review CLI surfaces these URLs so the operator can pick one
   and feed to `python -m aarva.ingest_url <url> --add-to-edition`.

Add `fallback_urls_json TEXT` to the `trend_hits` schema (edit
the DDL above to include it — this note is Cowork's clarification;
the DDL as written omitted this column, Claude Code should add it
before implementation).

---

## Review CLI extension

Extend `python -m aarva.review` (`aarva/review.py:415` —
`_apply_decisions` is the batch-command dispatcher we hook into).

New section rendered at the top of the review view for the
current daily edition:

```
==== Trending topics (last 24h) ====
[t1] "The great romance slump" (google_trends_us, google_trends_in)
     → Aarva match: article #8342 "Adaptation as translation…" (score 4.2)
     [t1a=add / t1d=dismiss]

[t2] "Nolan Odyssey opening weekend" (youtube_trending_us, gdelt_global)
     → No Aarva match in vector space.
     → GDELT fallback: 3 candidate URLs from allowlist
        - https://www.newyorker.com/culture/…/nolan-odyssey-review
        - https://aeon.co/…/adaptation-and-loss
        - https://www.theatlantic.com/…/nolan-and-homer
     [t2s=show URLs / t2i=ingest first URL / t2d=dismiss]

[t3] "Bank of England rate decision" (gdelt_global)
     → Filtered out: no Aarva match; GDELT fallback URLs found but
       all match keep_up_to_date shape (breaking news — auto-dismissed
       per guardrail #6).
     [t3d=dismiss / t3o=override (surface anyway)]

Enter batch commands like `t1a t2i` — or `all-d` to dismiss all…
```

Actions:
- `tNa`: mark trend N as `operator_action='added'`, call
  `add_article_to_todays_edition(article_id=matched_article_id,
  slot='delight')` (or a new `slot='trending_bonus'` — decision
  below).
- `tNi`: for GDELT-fallback trends, ingest the first (or Nth)
  candidate URL via the existing `aarva.ingest_url` code path,
  then `add_article_to_todays_edition`.
- `tNs`: show the full URL list without picking (for GDELT
  fallbacks).
- `tNd`: mark trend N as `operator_action='dismissed'`.
- `tNo`: override guardrail dismissal — surface the trend even
  if it was auto-filtered.

`all-d` for blanket dismiss stays consistent with the existing
CLI's `all-r`/`all-a` shortcuts (per
`docs/session_plan_review_cli_polish.md:497-502` in the same-
concept discipline).

**Slot decision**: use existing `slot='delight'` if the matched
article's `jtbd_primary` is `delight`; use existing
`slot='bonus'` otherwise; NO new slot type for v1. This avoids a
schema change to `edition_pieces.slot`'s CHECK constraint and
lets us tune the slot mapping in a follow-up if trend-added
pieces need their own visual treatment.

---

## Cost estimate

Per-day, based on ~30 trends surfaced (3 sources × ~10 unique
trends/day after dedup):

- **Google Trends crawl**: free (pytrends). Modest rate-limit
  risk mitigated by the low request count.
- **YouTube Trending crawl**: 3 regions × 1 unit each = 3
  quota units. Well under the 10k/day free tier.
- **GDELT trend + fallback crawls**: free. GDELT has no auth
  and generous rate limits.
- **LLM query expansion**: 30 trends × $0.0002 = **$0.006/day**.
- **LLM re-rank**: 30 trends × $0.005 = **$0.15/day**.
- **Embedding**: 30 trends × 3 phrasings × $0.0001 = **$0.009/day**.
- **Translation (Chinese/other)**: not needed — Weibo removed;
  Google Trends and YouTube regional feeds return English trend
  labels for the English `hl=en` variants; GDELT is auto-
  translated. Zero cost.

**Total: ~$0.17/day = ~$5/month at current scale.** Under the
$5/month cap in decision #8. Grows linearly with trend count.

---

## Files that change

- `aarva/config/trend_sources.yaml` (new)
- `aarva/config/__init__.py` — new `TrendSource` dataclass +
  `load_trend_sources()` loader, mirroring `load_publications`
  (verified at `config/__init__.py:190`).
- `aarva/db.py` — new `CREATE TABLE trend_hits` + indexes;
  include the `fallback_urls_json` column noted in the GDELT
  fallback section.
- `aarva/sources/trend_crawler.py` (new) — nightly crawl loop
  with per-`kind` handlers.
- `aarva/services/trend_matcher.py` (new) — query expansion +
  semantic retrieval + re-rank + GDELT fallback.
- `aarva/review.py` — new "Trending" section in
  `_apply_decisions` and the pre-decision render; new `tN[a|d|i|s|o]`
  batch-command parsing.
- `aarva/daily.py` — new `--stage trends` subcommand OR fold
  into Stage 1 (Claude Code picks).
- `aarva/config/pipeline.yaml` — new `trends:` block with
  thresholds and weights:

  ```yaml
  trends:
    enabled: true
    vector_match_threshold: 3.5    # LLM re-rank 1-5
    gdelt_max_records: 25
    gdelt_timespan: 14d
    article_age_min_hours: 48       # guardrail #6
    allowed_jtbds: [delight, curiosity, smart_escape, keep_ahead]
    blacklist_phrases: []           # starter empty; grow as needed
  ```
- `aarva/clients/llm.py` — no changes (reuse existing).
- `aarva/clients/embedding.py` — no changes (reuse `embed_query`).
- `requirements.txt` — add `pytrends`.
- `docs/roadmap.md` — In-progress entry added in the same edit
  set as this spec (per rule 17a and the 2026-07-28 addition).

---

## Verification

1. **Crawler smoke test**: run once against all v1 sources.
   Confirm at least 5 rows land in `trend_hits` from each
   enabled source.
2. **Query expansion unit test**: for a fixed trend phrase (say
   "the great romance slump"), verify the LLM returns 3
   phrasings and they parse as JSON.
3. **Matcher end-to-end test**: pick a known-good match case —
   e.g. a real article Aarva has on a topic that's currently
   trending — verify the matcher returns it with score ≥ 3.5.
4. **Matcher guardrail test**: pick an article that would match
   semantically BUT was published <48h ago; verify it's excluded.
   Repeat for JTBD (`keep_up_to_date` should be excluded) and
   `status = 'in_edition'`.
5. **GDELT fallback test**: pick a trend that's NOT in Aarva's
   catalog (verify by search first). Confirm GDELT returns
   URLs from allowlist domains and they populate `fallback_urls_json`.
6. **Review CLI test**: run `python -m aarva.review` after a
   real crawl. Confirm the "Trending" section renders. Try each
   action (`tNa`, `tNi`, `tNd`, `tNs`, `tNo`). Confirm actions
   persist to `operator_action` correctly.
7. **Cost check**: after one week of daily runs, sum LLM +
   embedding cost against Vertex AI billing. Confirm under
   $2/week (well under the $5/month cap).
8. **Editorial sanity check**: after 2 weeks of trend surfacing,
   review the `trend_hits` table's `operator_action` distribution.
   If `dismissed:added` ratio > 20:1, the source list or
   thresholds need retuning.

---

## Non-goals

- **NOT autonomous.** No trend is added to a daily edition
  without operator confirmation. Preserves rule 4's editorial
  discipline.
- **NOT breaking-news feed.** Age filter (48h min) + JTBD filter
  keep the "curated slow" posture from `docs/project_brief.md:29-32`.
- **NOT Reddit / Weibo / X.** Explicitly rejected 2026-08-13.
- **NOT a replacement for the curation-platform signal** at
  `docs/session_plan_curation_platform_signal.md`. Both coexist:
  peer-curator signal (that spec) and trend signal (this spec)
  answer different questions; both feed the same
  `add_article_to_todays_edition` primitive with independent
  weights.
- **NOT a full "auto-search publications" fallback.** The GDELT
  fallback covers what Aarva-allowlist coverage exists that
  Aarva hasn't ingested yet. It does NOT scrape individual
  publications' search endpoints.
- **NOT a bonus-episode-shape mechanism.** For v1, trend-added
  articles land in `slot='delight'` or `slot='bonus'` on today's
  daily edition. If trend-added pieces need distinct visual
  treatment on `/today`, spec a follow-up.
- **NOT retroactive.** Only new trends from crawl onwards
  produce hits. Historical trends before this ships are lost.

---

## Rollout

- Ship as one PR touching the files above. Crawl OFF by default
  in `pipeline.yaml` (`trends.enabled: false`) so the merge
  itself doesn't change daily behaviour.
- Operator manually enables via `pipeline.yaml` after inspecting
  the first crawl's output.
- Guardrail thresholds locked at the values above for v1; retune
  after 2-3 weeks of real trend data based on
  operator-action ratios per source.

---

## What Cowork owes upfront (rule 6a + rule 4)

Before Claude Code implements:

1. **Web-verify each source's current state**:
   - `pytrends` GitHub repo status, current API surface, rate
     limits observed by users in the last ~30 days.
   - YouTube Data API v3 free-tier quota (10k units/day)
     confirmed for `videos.list` at 1 unit; region-code
     availability.
   - GDELT DOC API endpoint URL + parameter names; the "top
     themes" or equivalent trend-source endpoint. Cowork's
     understanding here was hand-waved; Claude Code needs to
     confirm the exact endpoint before wiring.

2. **User sign-off on the guardrail thresholds** (decision #6 —
   48h age, JTBD list). Cowork asserts these values were
   discussed 2026-08-13 in principle but not verbally locked at
   the specific "48 hours" / specific JTBD list values. Claude
   Code should raise this to user before implementing if the
   spec surfaces any ambiguity.

3. **User sign-off on the starter trend blacklist** (decision
   #7). Empty at ship-time is acceptable; user fills during
   first-week tuning.

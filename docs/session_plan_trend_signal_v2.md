**STATUS: PARTIALLY SHIPPED.** PR 1 (concept C, the lens-aware
max-age guardrail) shipped 2026-08-20 — see `docs/roadmap.md`'s
2026-08-20 entry. PR 2 (concepts A + B) still pending, with real
scope narrowing found during rule 6a re-verification the same day:
Reddit dropped entirely (confirmed dead — see `docs/project_brief.md`'s
2026-08-20 decision log), Bluesky reverse-lookup deferred pending the
user setting up a bot account. See `docs/roadmap.md`'s "In progress"
section for current PR 2 scope before resuming this spec.

---

# Session plan — trend-signal layer v2 (Bluesky + HN sources, reverse lookup, lens-aware max age)

Written by Cowork for the next Claude Code session (2026-08-20+).
Extends the shipped trend-signal layer
(`docs/session_plan_trend_signal_for_delight.md`, STATUS: Shipped
2026-08-13) with three concepts:

1. **New trend sources** — add Bluesky trends + HN front-page
   alongside Google Trends.
2. **Reverse-lookup** — for Aarva's already-catalogued articles,
   detect current external attention (HN Algolia + Reddit URL-search
   + Bluesky post-search) and surface as boost candidates during
   review.
3. **Lens-aware max-age guardrail on forward matching** — mirror
   Stage 7's `slot_max_age_days` so trend matches to news-y lenses
   (`behind_the_news`, `future_gazing`) can't surface stale articles.

Read this doc + `docs/roadmap.md` + `AGENTS.md` +
`docs/project_brief.md` before starting.

**AGENTS.md rule 4 sign-off**: this changes editorial ranking
behaviour on two paths (forward add + new reverse-lookup) and adds
external-signal sources. User approved direction 2026-08-20 in the
conversation that produced this doc, including specific guardrail
rules per direction:
- Forward matching gains a NEW max-age filter (news-y lenses only)
  in addition to the existing 48h min + JTBD filter.
- Reverse lookup applies JTBD filter only — NO age constraints
  (external virality is trusted signal; Aarva's editorial bar was
  already passed at Stages 2-4-5-6 when the article was scored).

**AGENTS.md rule 6a**: this doc names external URLs and API
endpoints. Cowork verified two on 2026-08-20 with live calls:
- Bluesky `getTrends`:
  `https://public.api.bsky.app/xrpc/app.bsky.unspecced.getTrends`
  — returned structured JSON with `topic`, `displayName`,
  `postCount`, `status`, `category`, no auth required. Endpoint is
  officially "unspecced" so API surface could shift; Claude Code
  should re-verify at implementation.
- HN Algolia:
  `https://hn.algolia.com/api/v1/search?query=<...>&tags=story`
  — returned JSON with `points`, `num_comments`, `created_at`,
  `story_id`, `url`. No auth.

Not re-verified (blocked from Cowork's sandbox) — Reddit's `.json`
endpoints (public listings + `/api/info.json?url=<url>`). Cowork's
understanding: still work externally with a proper User-Agent per
Reddit's post-2023 policy. Claude Code should verify from a real
network before wiring.

---

## Context — why this, why now

**Forward-trend limitation (2026-08-13+):** the shipped trend layer
crawls Google Trends only. Google Trends' data is search-query
based — misses viral cultural moments that don't translate to a
distinct search query. Bluesky and HN cover the essayistic-reader
audience Aarva actually competes for.

**Reverse-lookup gap:** the shipped layer answers "what's the world
talking about → do we have coverage?" It doesn't answer the
opposite: "for content Aarva already selected → is anyone paying
attention to it right now?" That's a real editorial signal for
picking WHICH already-scored piece to elevate today, and it works
even for articles that Stage 7 didn't slot into today's edition.

**Lens-aware max-age gap (bug):** the shipped forward-matcher's
guardrails (`age ≥ 48h min`, `JTBD in {delight, curiosity,
smart_escape, keep_ahead}`) don't include the news-y-lens max-age
cap that Stage 7 itself uses at `stage_7_assemble.py:78,82` — a
6-day cap on `lens=future_gazing` and `lens=behind_the_news`. A
stale article that trend-matches on topic could get surfaced even
though Stage 7 would have rejected it as stale for that lens.

**Editorial risk explicitly considered (rule 4).** External
virality signals are new inputs into a curated-slow product. The
guardrails are asymmetric on purpose (see "Locked decisions"): the
forward path stays conservative (news-cycle guard on both ends), the
reverse path trusts external signal because Aarva's own editorial
bar was applied when the article was originally scored.

---

## Architecture check (rule 17d)

Grep outputs referenced below are from 2026-08-20.

**Reference greps:**

```
$ grep -nE "slot_max_age_days|max_age_days" aarva/stages/stage_7_assemble.py | head -8
67:    # Configurable per slot via assembly.slot_max_age_days in
69:    max_age_days: Optional[int] = None
77:    # assembly.slot_max_age_days.lens_card_future in pipeline.yaml.
78:    SlotSpec("lens_card_future",  lens="future_gazing",       max_age_days=6),
82:    SlotSpec("lens_card_behind",  lens="behind_the_news",     max_age_days=6),
104:    max_age_days=6),
107:    max_age_days=6),

$ grep -nE "^def _load_candidate_articles|jtbd_primary|published_date <=" aarva/services/trend_matcher.py
80:def _load_candidate_articles(
89:    usable embedding. jtbd_primary lives on article_scores, not
99:            SELECT a.id, a.title, a.embedding
100:              FROM articles a
101:              JOIN article_scores s ON s.article_id = a.id
102:             WHERE a.status = 'scored'
103:               AND a.embedding IS NOT NULL
104:               AND a.embedding_model = ?
105:               AND a.published_date <= datetime('now', ?)
106:               AND s.jtbd_primary IN ({placeholders})

$ grep -nE "^CREATE TABLE article_scores|lens" aarva/db.py | head -5
89:CREATE TABLE IF NOT EXISTS article_scores (
99:    lens                        TEXT,
```

Confirmed: `article_scores.lens` (line 99) is where the classifier
result lives. Trend matcher already joins `article_scores` (line
101), so adding a `s.lens NOT IN (...) OR a.published_date >=
datetime('now','-6 days')` condition is a one-clause extension of
the existing SQL.

**Now the three questions:**

1. **Where does the data live?**
   - **New trend sources**: Bluesky and HN feed into the existing
     `trend_hits` table (from
     `docs/session_plan_trend_signal_for_delight.md`). Just new
     `source_name` values + new handlers in the crawler; no schema
     change for concept A.
   - **Reverse-lookup hits**: new table `article_virality_hits`
     on main_db, mirroring `trend_hits` shape but keyed on
     `article_id` instead of `trend_phrase`. Columns:
     `id, article_id, source_name (hn|reddit|bluesky), external_url,
     score, num_comments, seen_at, operator_action, resolved_at`.
     Indexed on `(operator_action, seen_at)` for the review CLI to
     query unresolved hits.
   - **Max-age filter**: no new data. Uses existing
     `article_scores.lens` (verified at `db.py:99`) already joined
     by the trend matcher (verified at `trend_matcher.py:101`).

2. **Where does the operation run?**
   - **Bluesky + HN crawlers**: new `kind` handlers in the existing
     `aarva/sources/trend_crawler.py`. Runs where the current
     Google Trends crawler runs.
   - **Reverse-lookup service**: new `aarva/services/article_virality.py`.
     Runs on the operator's laptop as part of the same `--stage 3`
     (or a sibling stage — Claude Code's call) after the forward
     trend crawl. Iterates recent scored articles, queries HN
     Algolia + Reddit URL-search + Bluesky post-search per URL,
     inserts hits into `article_virality_hits`.
   - **Max-age filter**: pure SQL extension in
     `_load_candidate_articles` at `trend_matcher.py:80-107`. Zero
     new infra.
   - **Review CLI surfacing**: extends the existing "Trending
     topics" section in `python -m aarva.review` with a sibling
     "Trending Aarva articles" section for reverse-lookup hits.

3. **Does the operation have physical access to the data it needs?**
   - **Bluesky/HN crawlers**: yes — Bluesky's `getTrends` and HN
     Algolia are public, no-auth HTTP endpoints (verified today).
   - **Reverse-lookup queries**: yes for HN Algolia (verified);
     yes for Reddit `.json` endpoints from a normal network with a
     User-Agent (blocked from Cowork's sandbox only); yes for
     Bluesky's `searchPosts` endpoint (public, unauthenticated
     rate-limited — sufficient for Aarva's volume).
   - **SQL filter change**: yes — all in local main_db.

---

## Locked decisions (with user, 2026-08-20)

### Forward matching (existing path, extended)

1. **Existing guardrails preserved unchanged**: `48h age MIN`,
   `JTBD IN {delight, curiosity, smart_escape, keep_ahead}`.
2. **NEW: lens-aware max-age filter.** Add to
   `_load_candidate_articles` SQL: articles with
   `article_scores.lens IN ('behind_the_news', 'future_gazing')`
   must also satisfy `a.published_date >= datetime('now', '-6
   days')`. Mirrors the values already in
   `stage_7_assemble.py:78,82`.
3. **Source of truth for max-age values**: read from
   `assembly.slot_max_age_days.lens_card_future` and
   `assembly.slot_max_age_days.lens_card_behind` in
   `pipeline.yaml`, falling back to hardcoded 6 days when
   unset (matching Stage 7's default). One source of truth for
   both stages — if a future PR tunes the 6-day cap for Stage 7,
   the trend matcher automatically inherits.

### Reverse lookup (new)

4. **JTBD filter preserved** (same allowlist as forward). External
   virality doesn't override editorial voice on news-shaped
   content; Aarva's own classifier already ran.
5. **NO age constraints** (per explicit 2026-08-20 user decision).
   An article being trending externally has earned its way in
   regardless of age. Both fresh (< 48h) and stale (> 6 months)
   articles are eligible if there's a virality hit.
6. **Signal thresholds per source** (starter values, retunable):
   - HN: `points ≥ 100` AND posted within last 14 days.
   - Reddit: `score ≥ 500` AND posted within last 14 days.
   - Bluesky: post-search returns ≥ 5 posts mentioning the URL
     within last 14 days.
7. **Scope**: scan articles with `status='scored'`, `embedding IS
   NOT NULL`, `jtbd_primary IN <allowlist>`, published within
   last 90 days. Rationale: caps the API call volume, and beyond
   90 days most articles were either promoted or won't get organic
   attention. Configurable in `pipeline.yaml`.
8. **Semi-automatic same as forward**: operator picks add / dismiss
   per hit via the review CLI. Never auto-adds. Uses the same
   `_apply_trend_decisions`-equivalent flow with
   `review_status='approved'` (per the 2026-08-15 auto-approve fix).

### New trend sources (concept A)

9. **Bluesky trends**: crawled via public unauthenticated endpoint;
   category filter to skip `politics` category matches unless
   category is explicitly allowed via config (starter allowlist:
   `culture`, `science-tech`, `entertainment`, `sports`, `culture`,
   `education` — Bluesky's actual category vocab, verified via
   sample call). This is a per-source guardrail — `politics`
   trends still crawl but don't feed the matcher.
10. **HN front-page**: crawl `https://hn.algolia.com/api/v1/search_by_date`
    with `numericFilters=points>200,created_at_i>...&tags=story` to
    get last 24h's high-scoring stories. Use the story TITLE as
    the trend phrase (LLM query expansion runs the same on it as
    on Google Trends phrases). Optional `story_url` also stored in
    `raw_metadata_json` for possible direct-URL matching later.
11. **Weights**: Bluesky 0.7, HN 0.8 (HN's audience-fit is
    strongest for Aarva). Retunable.

### PR sequencing

12. **Split into two PRs.** Concept C (max-age fix) is small,
    urgent (bug in shipped code), and ships FIRST as a standalone
    PR. Concepts A + B ship together as the second PR (both add
    external-signal surface area, both extend the same crawler +
    review CLI). Same rationale as the 2026-08-15
    trend-adds-auto-approve fix: small correction ships fast,
    bigger design change gets its own review cycle.

---

## Concept C — lens-aware max-age guardrail (SHIPS FIRST)

Change `_load_candidate_articles` in
`aarva/services/trend_matcher.py:80-107` to add a max-age filter
for news-y lenses.

**Before** (current, lines 99-107):
```python
rows = conn.execute(
    f"""
    SELECT a.id, a.title, a.embedding
      FROM articles a
      JOIN article_scores s ON s.article_id = a.id
     WHERE a.status = 'scored'
       AND a.embedding IS NOT NULL
       AND a.embedding_model = ?
       AND a.published_date <= datetime('now', ?)
       AND s.jtbd_primary IN ({placeholders})
    """,
    (embedding_model, f"-{age_min_hours} hours", *allowed_jtbds),
).fetchall()
```

**After** — new clause + parameters. Read the max-age lookup from
`pipeline.yaml`'s `assembly.slot_max_age_days` block; fall back to
Stage 7's default (6 days) when unset. New signature accepts a
`lens_max_age_days: dict[str, int]` mapping (populated by the
caller, e.g. `{"future_gazing": 6, "behind_the_news": 6}`).

Sketch:
```python
# Build a CASE that constrains age only for lenses that have a cap.
lens_clauses = []
lens_params = []
for lens_name, days in lens_max_age_days.items():
    lens_clauses.append(f"(s.lens = ? AND a.published_date >= datetime('now', ?))")
    lens_params.extend([lens_name, f"-{days} days"])

# Article must satisfy: EITHER lens isn't news-y OR the lens-specific
# max-age is met.
lens_filter_sql = (
    "AND (s.lens NOT IN (" + ",".join("?" for _ in lens_max_age_days) + ") "
    "OR " + " OR ".join(lens_clauses) + ")"
) if lens_max_age_days else ""
```

Wire the mapping in the caller (search around `trend_matcher.py:281`)
by reading from `trends_cfg.get("lens_max_age_days")` OR the
existing `assembly.slot_max_age_days` block. Cowork's preference:
read from `assembly.slot_max_age_days` so there's ONE source of
truth for both Stage 7 and the trend matcher.

**Tests:**
- Article with `lens='future_gazing'` and `published_date` 3 days
  ago: passes.
- Article with `lens='future_gazing'` and `published_date` 10 days
  ago: excluded.
- Article with `lens='humans_and_humanity'` and `published_date` 10
  days ago: passes (only news-y lenses get the cap).
- Article with `lens='behind_the_news'`, edge case: exactly at the
  6-day boundary — verify inclusive-or-exclusive matches Stage 7's
  behavior (Stage 7 uses `>=` per its own SQL — same here).

---

## Concept A — Bluesky + HN as trend sources

### Bluesky crawler handler (new `kind='bluesky_trends'`)

Endpoint: `https://public.api.bsky.app/xrpc/app.bsky.unspecced.getTrends?limit=50`.
Response shape verified 2026-08-20: JSON with `trends[]`, each
having `topic`, `displayName`, `description`, `postCount`, `status`
(`trending`/`cooling`/`stale`), `category`.

Handler steps:
1. Fetch endpoint. No auth required.
2. For each `trends[i]` with `status IN ('trending', 'cooling')`
   (skip `'stale'`) and `category` in the allowed set (from
   `pipeline.yaml` `trends.bluesky_allowed_categories`):
3. Extract `displayName` as `trend_phrase` (already English on
   Bluesky's global endpoint).
4. Store `postCount`, `category`, `status` in `raw_metadata_json`.
5. `INSERT OR IGNORE INTO trend_hits (source_name='bluesky',
   trend_phrase=<displayName>, region='global', ...)`.

Rate limit: unauthenticated Bluesky endpoints have generous public
limits (~10s of req/min). One request per day per source is
negligible.

### HN front-page crawler handler (new `kind='hn_frontpage'`)

Endpoint: `https://hn.algolia.com/api/v1/search_by_date`. Query
params: `numericFilters=points>200,created_at_i>{now-24h}&tags=story&hitsPerPage=30`.
Verified 2026-08-20 that base endpoint returns JSON with
`hits[]`, each having `title`, `url`, `points`, `num_comments`,
`created_at_i`.

Handler steps:
1. Fetch endpoint. No auth required.
2. For each hit with `points ≥ threshold` (config, default 200):
3. Use `title` as `trend_phrase`.
4. Store `url`, `points`, `num_comments`, `story_id` in
   `raw_metadata_json`.
5. `INSERT OR IGNORE`.

Rate limit: HN Algolia has no documented public limit for casual
use. One request/day is negligible.

### `trend_sources.yaml` additions

```yaml
- name: bluesky_trends_global
  kind: bluesky_trends
  region: global
  weight: 0.7
  enabled: true

- name: hn_frontpage
  kind: hn_frontpage
  region: global      # HN is inherently global; no per-region variant
  weight: 0.8
  enabled: true
```

### `pipeline.yaml` additions

```yaml
trends:
  # ... existing config unchanged ...
  bluesky_allowed_categories:
    - culture
    - science-tech
    - entertainment
    - sports
    - education
  hn_points_threshold: 200
  hn_lookback_hours: 24
```

---

## Concept B — reverse lookup (article → external virality)

New service `aarva/services/article_virality.py`. New table on
main_db.

### Data model

```sql
CREATE TABLE article_virality_hits (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id          INTEGER NOT NULL REFERENCES articles(id),
    source_name         TEXT NOT NULL,     -- 'hn' | 'reddit' | 'bluesky'
    external_url        TEXT,              -- e.g. HN story URL, Reddit post permalink
    score               INTEGER,           -- points/upvotes on that platform
    num_comments        INTEGER,
    external_created_at TIMESTAMP,
    seen_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    raw_metadata_json   TEXT,
    operator_action     TEXT,              -- 'added' | 'dismissed' | NULL
    resolved_at         TIMESTAMP
);
CREATE INDEX idx_article_virality_hits_action
    ON article_virality_hits(operator_action, seen_at);
CREATE INDEX idx_article_virality_hits_article
    ON article_virality_hits(article_id);
```

### Scan flow

1. Load candidate articles with:
   ```sql
   SELECT a.id, a.canonical_url, a.title
     FROM articles a
     JOIN article_scores s ON s.article_id = a.id
    WHERE a.status = 'scored'
      AND s.jtbd_primary IN ('delight','curiosity','smart_escape','keep_ahead')
      AND a.published_date >= datetime('now', '-90 days')
   ```
   (Note: JTBD filter present, NO age min per locked decision #5;
   the `>= -90 days` is a scan-cost cap, NOT an editorial guardrail.)

2. For each article's `canonical_url`, query the three sources:

**HN Algolia URL search:**
```
GET https://hn.algolia.com/api/v1/search?query=<url-encoded canonical_url>&tags=story&hitsPerPage=5
```
Filter results to `points >= 100 AND created_at_i >= (now - 14 days)`.
For each hit: insert `article_virality_hits` row with
`source_name='hn'`, `score=points`, `num_comments`, `external_url=story_url_on_HN`.

**Reddit URL search:**
```
GET https://www.reddit.com/api/info.json?url=<url-encoded canonical_url>
User-Agent: "aarva/1.0 (by /u/<operator>)"
```
Filter to `data.score >= 500 AND (now - data.created_utc) <= 14 days`.
Insert with `source_name='reddit'`, `external_url=data.permalink`.

**Bluesky post search:**
```
GET https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts?q=<url-encoded canonical_url>&limit=25
```
Count matching posts within last 14 days. If ≥ 5, insert one summary
row with `source_name='bluesky'`, `score=<post_count>`,
`external_url=<first post permalink>`.

### Cost estimate

Scope: ~1k-3k articles in the trailing 90-day window (Aarva
ingests ~15-25 articles/day filtered to scored).

- HN Algolia: 1 request/article. ~3k requests/day. Well within
  informal free limits (no hard cap documented).
- Reddit: 1 request/article, ~3k/day. Reddit's post-2023 rate
  limit for unauthenticated with UA is ~100 requests/minute — so
  batch with 2s pacing → ~5 minutes wall time. Reasonable.
- Bluesky: 1 request/article. Public rate limits allow this.

Batch nightly, cache aggressively (skip articles queried within
last 24h and no hits above threshold last time). Total: $0/mo.

### Review CLI surfacing

Extend `python -m aarva.review` with a NEW section under the
existing "Trending topics" section, titled "Trending Aarva
articles" — one row per unresolved `article_virality_hits`:

```
==== Trending Aarva articles (external virality) ====
[v1] "The great romance slump" (article #8281)
     → HN: 342 points, 187 comments (2026-08-19)
     → JTBD: curiosity, lens: humans_and_humanity, age: 12 days
     [v1a=add to today / v1d=dismiss]

[v2] "AI adaptation as translation" (article #1234)
     → Reddit r/TrueReddit: 823 upvotes, 156 comments
     → Bluesky: 8 mentions (last 14d)
     → JTBD: keep_ahead, lens: future_gazing, age: 4 months
     [v2a=add to today / v2d=dismiss]
```

Actions:
- `vNa`: mark hit as `added`, call
  `add_article_to_todays_edition(db, article_id, slot=<inferred>,
  review_status='approved')` (per the 2026-08-15 auto-approve fix).
- `vNd`: mark hit as `dismissed`.
- Same batch-command shape as the existing `tN` trend actions.

Slot inference: `slot='delight'` if JTBD is `delight`, else
`'bonus'` (matches trend-matcher convention).

---

## Files that change

**PR 1 — concept C (lens-aware max-age fix, ships first):**

- `aarva/services/trend_matcher.py` — extend
  `_load_candidate_articles` SQL with lens-aware max-age filter;
  extend the caller (near line 281) to load
  `assembly.slot_max_age_days` from pipeline.yaml.
- `aarva/tests/test_trend_matcher.py` — new tests for the age-cap
  behavior per lens.
- `docs/roadmap.md` — Recently completed entry at ship time.

**PR 2 — concepts A + B (new sources + reverse lookup):**

- `aarva/config/trend_sources.yaml` — add Bluesky + HN entries.
- `aarva/config/pipeline.yaml` — add Bluesky category allowlist,
  HN threshold, reverse-lookup scan window + thresholds.
- `aarva/sources/trend_crawler.py` — new `bluesky_trends` and
  `hn_frontpage` handlers alongside `google_trends`.
- `aarva/services/article_virality.py` (new) — HN + Reddit +
  Bluesky per-article scan.
- `aarva/db.py` — new `article_virality_hits` table + indexes.
- `aarva/review.py` — new "Trending Aarva articles" section +
  `vNa`/`vNd` batch commands + `_apply_virality_decisions`
  mirroring `_apply_trend_decisions`.
- `aarva/daily.py` — extend `--stage 3` (or add sibling stage) to
  run the reverse-lookup scan after the forward crawl.
- `aarva/tests/test_trend_crawler.py` — tests for both new
  handlers (mocked HTTP).
- `aarva/tests/test_article_virality.py` (new) — tests for the
  reverse-lookup service (mocked HTTP).
- `aarva/tests/test_review.py` — tests for
  `_apply_virality_decisions`.
- `docs/roadmap.md` — In-progress entry (this spec) and
  eventually Recently completed.

---

## Verification

### PR 1 (max-age fix)

1. Unit test: article with `lens='future_gazing'` age 3d → passes;
   age 10d → excluded.
2. Unit test: same for `lens='behind_the_news'`.
3. Unit test: articles with other lenses (`humans_and_humanity`,
   `unclassified`) at 30d age → still pass.
4. Integration test: real matcher run on a scratch DB with mixed
   lens/age data; confirm output count changes as expected.

### PR 2 (sources + reverse lookup)

5. Real API smoke tests: one live call to each of Bluesky trends,
   HN Algolia URL search, HN Algolia by-date, Reddit URL search,
   Bluesky post search. Confirm each returns parseable JSON with
   expected fields.
6. Reverse-lookup real end-to-end: pick an article Aarva has that
   IS currently on HN (verify manually first), run the scan,
   confirm a `hit` row lands with `source_name='hn'` and
   correct score.
7. Reverse-lookup guardrail: verify articles with `jtbd_primary =
   'keep_up_to_date'` are NEVER queried (JTBD filter runs before
   any HTTP calls — saves API budget).
8. Review CLI drive-through: `python -m aarva.review` shows both
   "Trending topics" (existing) and "Trending Aarva articles"
   (new). `vNa` adds the article; verify it lands in
   `edition_pieces` with `review_status='approved'`.
9. Post-2-weeks editorial check: review the `article_virality_hits`
   `operator_action` distribution. If `dismissed:added` ratio is
   > 20:1, retune thresholds.

---

## Non-goals

- **NOT autonomous.** No hit ever gets added without operator
  keystroke.
- **NOT Reddit-general-population-driven.** Reddit URL search
  targets articles Aarva selected, not r/all top posts.
- **NOT a replacement for the peer-curator signal at
  `docs/session_plan_curation_platform_signal.md`.** Peer-curator
  (The Browser, Longreads, Kottke) answers "did another editor
  pick this?"; trend/virality answers "does the wider world care?"
  Both coexist as independent signals.
- **NOT retroactive scoring.** New trends and new virality hits
  only produce signal from the crawl onwards.
- **NOT a full X/Twitter integration.** Basic tier costs $200/mo
  minimum — not worth the signal at Aarva's scale. Reddit and
  Bluesky cover the same territory for free.
- **NOT scanning historical articles beyond 90 days.** Cost cap,
  not editorial. If a 6-month-old article is genuinely trending,
  the daily scan will catch it once — extending the window costs
  API budget without proportional signal.

---

## Rollout

- **PR 1** (max-age fix): ships first, standalone. Small change,
  urgent (bug in current shipped feature). Should ship within a
  day.
- **PR 2** (sources + reverse lookup): ships when ready. Crawl
  and reverse-lookup default OFF in `pipeline.yaml`
  (`trends.bluesky_enabled: false`, `trends.hn_enabled: false`,
  `trends.reverse_lookup_enabled: false`) so merge doesn't change
  daily behavior. Operator flips flags after inspecting first
  crawl output.
- Weight tuning + threshold tuning: 2-week retune based on real
  operator-action ratios.

---

## What Cowork owes upfront (rule 6a + rule 4)

Before Claude Code implements:

1. **Web-verify each source's current state at implementation time:**
   - Bluesky `getTrends` — unspecced endpoint may shift.
   - HN Algolia base URL + rate limits.
   - Reddit `.json` and `/api/info.json?url=` — verify UA policy
     still works.
   - Bluesky `searchPosts` — check unauthenticated rate limits.

2. **User sign-off on thresholds** — the HN 200-points, Reddit
   500-upvotes, Bluesky ≥5-mentions numbers are Cowork's
   guesses. Retunable, but Claude Code should confirm they're
   acceptable starting values.

3. **Optional but valuable**: Cowork's Bluesky category allowlist
   (culture/science-tech/entertainment/sports/education) is
   derived from a single sample call. Verify Bluesky's actual
   category vocab is stable before hardcoding.

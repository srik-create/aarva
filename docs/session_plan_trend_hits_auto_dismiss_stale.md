# Session plan — auto-dismiss stale trend_hits and article_virality_hits

Small direct fix. Follow-up to `docs/session_plan_trend_signal_v2.md`
(STATUS: Shipped 2026-08-20).

Read this doc + `docs/roadmap.md` + `AGENTS.md` +
`docs/project_brief.md` before starting.

---

## The bug (diagnosis)

**Symptom (2026-08-20):** the operator's review CLI is now showing
800+ unresolved trend/virality suggestions. Every day's crawl adds
new hits but yesterday's, and the day before's, and the week
before's — all still unresolved — accumulate indefinitely because
the review CLI queries `operator_action IS NULL` with no time
filter.

Verified via grep 2026-08-20:
- `aarva/review.py:132` (Trending topics section): `WHERE
  th.operator_action IS NULL`. No time filter.
- `aarva/review.py:218` (Trending Aarva articles section): `WHERE
  v.operator_action IS NULL`. No time filter.
- `aarva/services/trend_matcher.py:67` (`_load_unresolved_trends`):
  `WHERE operator_action IS NULL`. Same shape.

Semantics of `operator_action IS NULL` was "the operator hasn't
made a decision on this hit yet." In practice, operator makes
decisions on the trends they want and leaves the rest as-is;
those un-decided rows persist forever.

---

## The fix

At the start of every `--stage 3` run — BEFORE the fresh crawl —
mark any unresolved hits older than a configurable stale-hours
cutoff as `operator_action = 'auto_dismissed_stale'`. Then the
fresh crawl runs and inserts today's hits. Review CLI still
queries `operator_action IS NULL` and picks up only today's fresh
ones.

**Concrete change — three files:**

### 1. `aarva/config/pipeline.yaml`

Extend the existing `trends:` block (line 365, verified 2026-08-20)
with:

```yaml
trends:
  # ... existing config unchanged ...
  # Auto-dismiss unresolved trend_hits + article_virality_hits older
  # than this many hours at the start of every --stage 3 run. Ensures
  # each day's review shows only fresh suggestions. Rows are marked
  # 'auto_dismissed_stale' (not deleted) so future analysis retains
  # the historical record.
  stale_after_hours: 24
```

Default 24h locked with user 2026-08-20.

### 2. New helper — location TBD

Cowork's preference: a new small function
`_auto_dismiss_stale_hits(db, hours: int) -> dict[str, int]` in
`aarva/sources/trend_crawler.py`, called at the top of
`crawl_trend_sources()` (verified entry point at
`trend_crawler.py:186`) before any per-source fetching.

Alternative: put it in `aarva/services/trend_maintenance.py` (new
file) if Claude Code prefers keeping the crawler focused on
crawling. Either is fine — the important invariant is that it runs
BEFORE both the forward crawl and the reverse-lookup scan on every
`--stage 3` invocation.

Function body:

```sql
UPDATE trend_hits
   SET operator_action = 'auto_dismissed_stale',
       resolved_at = CURRENT_TIMESTAMP
 WHERE operator_action IS NULL
   AND seen_at < datetime('now', ?);

UPDATE article_virality_hits
   SET operator_action = 'auto_dismissed_stale',
       resolved_at = CURRENT_TIMESTAMP
 WHERE operator_action IS NULL
   AND seen_at < datetime('now', ?);
```

Both parametrised with `f'-{hours} hours'`. Function returns
`{"trends_dismissed": n, "virality_dismissed": m}` for logging.

Log line at INFO level: `"Auto-dismissed N stale trends + M stale
virality hits (older than <hours>h)."`

### 3. Orchestration — where `--stage 3` calls it

Whichever entry point actually runs on `--stage 3` (likely
`aarva/daily.py` when handling the trends stage) — call
`_auto_dismiss_stale_hits(db, stale_hours)` FIRST, then call the
existing forward crawl, then call the existing reverse-lookup
scan.

Claude Code should pick the specific insertion point after
inspecting how the current --stage 3 sequence is wired. The
invariant is: dismissal runs BEFORE either data-producing step.

---

## Design rationale

**Why 24h default:** you crawl once per day (`--stage 3` at daily
run time). Yesterday's crawl → today's crawl is ~24h. A hit that's
still genuinely trending will get RE-inserted by today's crawler
with a fresh `seen_at` (the crawler is idempotent per
`(source_name, trend_phrase, seen_at::date)` per shipped design)
— so still-trending topics get a fresh evaluation, not silently
dropped.

**Why configurable:** if the operator ever runs `--stage 3` less
frequently (weekends off, missed a day), a 48h or 72h cutoff
prevents mass auto-dismissal of yesterday's actually-useful hits.
`pipeline.yaml`-tunable, no code change.

**Why `auto_dismissed_stale` (not just `dismissed`):** distinct
marker so future analysis can separate operator-intent dismissal
(`'dismissed'`) from time-out dismissal (`'auto_dismissed_stale'`).
`edition_rejections` uses the same discipline — reason codes are
data, not code.

**Why not delete:** preserves history for the eventual reviewer
learning loop equivalent for trends ("what patterns of trends did
the operator ignore?" is a real signal). Same posture as rule 12
(preserve history, soft-supersede over DELETE).

---

## Backward compatibility

- **No schema change.** `operator_action` is already `TEXT` with no
  CHECK constraint (verified in `trend_hits` and `article_virality_hits`
  DDL). New value `'auto_dismissed_stale'` fits.
- **Review CLI unchanged.** The existing `WHERE operator_action IS
  NULL` queries automatically exclude the newly-marked rows.
  Zero code change in `aarva/review.py`.
- **Trend matcher unchanged.** `_load_unresolved_trends` at
  `trend_matcher.py:67` uses the same filter and benefits
  automatically.
- **Historical `trend_hits` rows** (the current 800+ backlog)
  should be swept on first `--stage 3` run after this ships. That's
  the whole point.

---

## Verification

1. **Unit test — dismissal SQL correctness.** Insert 5 unresolved
   trend_hits with mixed `seen_at` (some 12h ago, some 30h ago).
   Call `_auto_dismiss_stale_hits(db, hours=24)`. Confirm the 30h-
   old ones are now `'auto_dismissed_stale'` with `resolved_at` set;
   the 12h-old ones untouched.
2. **Same test for `article_virality_hits`** — parallel case.
3. **Idempotency test:** call the function twice in a row. Second
   call should mark zero additional rows (all stale are already
   dismissed).
4. **Preserves `dismissed` and `added` rows:** insert one of each,
   older than the cutoff. Confirm they're NOT overwritten — only
   `operator_action IS NULL` rows get the update.
5. **End-to-end test:** simulate the 2026-08-20 backlog — insert
   100 unresolved trend_hits older than 24h, run `--stage 3`
   against a scratch DB (mock the crawler HTTP calls). Confirm
   the 100 old rows are auto-dismissed, today's freshly-inserted
   rows remain unresolved, review CLI shows only today's.
6. **Config override test:** set `trends.stale_after_hours: 48`
   in a test pipeline.yaml, confirm rows 30h old are NOT
   auto-dismissed (48h > 30h).

---

## Files that change

- `aarva/config/pipeline.yaml` — add `trends.stale_after_hours: 24`
  under the existing `trends:` block.
- `aarva/sources/trend_crawler.py` (or new `services/trend_maintenance.py`)
  — new `_auto_dismiss_stale_hits` function + call at top of the
  --stage 3 orchestration.
- Wherever `--stage 3` is wired (`aarva/daily.py` most likely) —
  read the new config value and pass it into the dismiss call.
- `aarva/tests/test_trend_maintenance.py` (new) or extend an
  existing trend-signal test file — covers all 6 verification cases
  above.
- `docs/roadmap.md` — Recently completed entry at merge time.

---

## Rollout

- Small PR, self-contained.
- Ship-and-run: first `--stage 3` after merge sweeps today's
  backlog (800+ rows) into `auto_dismissed_stale`. Operator's next
  review is clean.
- Zero risk to already-processed rows (dismissal only touches
  `operator_action IS NULL`).

---

## Rules verified in this handoff

- **AGENTS.md rule 4** (material trade-off): user signed off
  2026-08-20 — 24h default, configurable, hand off to Claude Code.
- **AGENTS.md rule 17e** (cite-the-source discipline): every code
  reference cites file+line, verified via grep against the current
  tree (`aarva/review.py:132,218`, `trend_matcher.py:67,329`,
  `trend_crawler.py:186`, `pipeline.yaml:365`).
- **AGENTS.md rule 17c** (STATUS-line discipline): parent spec
  `docs/session_plan_trend_signal_v2.md` is STATUS: SHIPPED
  2026-08-20. This is a follow-up doc, not a rewrite of that
  spec's decisions.
- **AGENTS.md rule 20a** (Claude Code git protocol): Claude Code
  commits + pushes + opens PR directly, user gives explicit
  "merge it" before merging.

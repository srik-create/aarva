**STATUS: Phase 1 DONE (2026-07-17).** See `docs/roadmap.md`'s
2026-07-17 "Recently completed" entry for the full writeup. One
adaptation: the spec assumed a per-piece interactive prompt; the
actual review CLI takes one batch command line, so the reason prompt
runs once per rejected piece right after that line parses, before the
existing single confirm — not fragmenting the CLI's existing UX.
Phase 2 and Phase 3 are still open, per this doc's own sequencing
(Phase 2 needs 2-3 weeks of Phase 1 data first).

---

# Session plan — reviewer feedback learning loop

Written by Cowork for the next Claude Code session (2026-07-16+).
Substantive new system: turn the reviewer's approve/reject signal
into a learning loop that proposes new filter rules and taste
adjustments over time. Three-phase design; each phase ships as its
own PR.

Read this doc + `docs/roadmap.md` + `AGENTS.md` before starting.

---

## Context

Today's rejection signal is coarse. `edition_rejections` records
that an article was rejected, but not WHY. Stage 7's candidate
pool builds a rejection-centroid vector and slightly penalises
candidates near it — one blob for "rejected articles." That helps
a little but can't distinguish "rejected because it was too long"
from "rejected because it was a listicle." Different rejection
reasons want different remediations.

**User asked (2026-07-16)**: how does the recommendation engine
learn from approve/reject signals, and — crucially — figure out
NEW structural filters to add (transcript-of-audio, video-
dependent, listicle, etc.) that Stage 2 should catch before the
article ever reaches the reviewer?

Answer: capture reasons at rejection time, periodically analyse
patterns per reason, propose new filter rules for the operator
(you) to approve and enable. Three phases.

---

## Phase 1 — Capture rejection reasons

### Goal

Every reject in `python -m aarva.review` records not just "this
was rejected" but "this was rejected because of {reason}." Small
CLI change, small schema change, immediate downstream payoff for
Phases 2 and 3.

### Decisions locked

1. **Reason list — start short.** Seven codes plus free-text:
   - `too_long`
   - `too_short`
   - `wrong_tone`
   - `transcript` (article is essentially a transcript of an
     audio/video interview)
   - `video_dependent` (article's meaning depends on watching
     embedded video content we can't narrate)
   - `listicle` (article is essentially a numbered list with no
     essayistic argument)
   - `other` (accepts free-text `reason_note`)
2. **New schema:**
   ```sql
   ALTER TABLE edition_rejections
     ADD COLUMN reason      TEXT;   -- one of the seven codes
   ALTER TABLE edition_rejections
     ADD COLUMN reason_note TEXT;   -- optional free text
   ```
   `reason` NULL for legacy rows. Backfill leaves them NULL; no
   attempt to reason retroactively.
3. **CLI UX**: after `r` (reject), CLI prints a numbered menu of
   the seven options. User types the number. If `7` (other), CLI
   prompts for a free-text note. Both stored on the new columns.
   Approve (`a`) and drop (`d`) are unchanged.
4. **Reason list is data, not code**. Store the seven codes +
   labels in a small Python constant `REJECTION_REASONS` in
   `aarva/services/review_reasons.py` (or similar). Adding a new
   reason later = one line in that file plus a menu update. NO
   database enum; SQLite doesn't do them well, and a TEXT column
   with app-level validation is the same in practice.
5. **Non-goal**: don't retrofit reasons for historic rejections.
   Only new rejections carry the signal. Phases 2 and 3 accept
   the sparser dataset until it grows.

### Files that must change

- `aarva/db.py` — schema addition to `edition_rejections`
- `aarva/services/review_reasons.py` (new) — the code + label
  constant
- `aarva/review.py` — CLI prompt after `r`. Small block, near the
  existing 'r' branch of `_apply_decisions`.
- One-line update in `edition_rejections` INSERT statement to
  include `reason` and `reason_note`.

### Verification

1. Reject an article via `python -m aarva.review`, choose reason
   `too_long`. Query the row; confirm reason column is set.
2. Reject another with reason `other` + free-text note. Confirm
   both columns populated.
3. Legacy rejections (from before this PR) still work as read;
   `reason = NULL` renders without breaking any downstream logic.

---

## Phase 2 — Periodic pattern extraction

### Goal

A batch script that reads recent rejections grouped by reason,
runs an LLM analysis to identify concrete structural features
that distinguish the rejected group from a comparable approved
group, and outputs a "proposed filter rules" report. Human (you)
reviews and decides which to enable.

Not autonomous. This produces PROPOSALS. Phase 3 enables them.

### Decisions locked

1. **Script**: `scripts/learn_from_rejections.py`. Manual invocation
   or a weekly cron; not automated at v1.
2. **Data window**: default trailing 30 days. Adjustable via
   `--days N` flag.
3. **Minimum sample**: require at least 5 rejections in a reason
   bucket before running analysis for that reason. Below that,
   log "insufficient sample for {reason} — skipping" and move on.
   Prevents overfitting on a single rejection's quirks.
4. **Analysis LLM call, per-reason**: gather up to 10 recent
   rejected articles for that reason + up to 10 approved articles
   from the same window. Send Gemini a structured prompt:

   ```
   You're analysing why a human editor rejected certain journalism
   articles while approving others. The rejected articles were all
   rejected for the reason: {reason_code} ({reason_label}).

   Identify 2-3 CONCRETE, TESTABLE structural or textual features
   that distinguish the rejected articles from the approved ones.
   Each feature must be something a Python function could check
   without human judgement — e.g. word count thresholds, specific
   phrase presence, HTML tag patterns, ratio of headings to
   paragraphs.

   DO NOT propose features that require subjective judgement
   ("well-argued", "engaging", "boring"). Those aren't testable
   automatically.

   Return JSON:
   {
     "proposed_filters": [
       {
         "name": "short-descriptive-slug",
         "description": "One-sentence explanation of what this
                         filter catches.",
         "detection_hint": "Concrete Python-testable logic
                            (regex, word count, tag ratio, etc.)",
         "example_rejected_titles": ["title1", "title2"],
         "example_approved_titles_that_would_survive":
             ["title3", "title4"],
         "confidence": "high | medium | low"
       },
       ...
     ]
   }

   REJECTED articles (all rejected for {reason_label}):
   {rejected_articles_dump}

   APPROVED articles (as reference for what should survive):
   {approved_articles_dump}
   ```

   Each article dump: title + first 800 chars + last 400 chars +
   word count. Keeps the prompt tractable.

5. **Report shape**: script emits Markdown to stdout AND writes
   to `docs/learning_reports/YYYY-MM-DD_rejection_analysis.md`
   (new dir). Per-reason section with proposed filters, sample
   evidence, LLM's confidence tag.
6. **Report is NOT auto-committed** — you review, edit, decide,
   then either commit for the record or delete.
7. **No autonomous enablement.** The script's output is
   proposals only. Phase 3 does the enabling.

### Files that must change

- `scripts/learn_from_rejections.py` (new)
- `docs/learning_reports/` (new directory, gitkeep or first
  report as anchor)
- `aarva/prompts.yaml` (or wherever prompts land) — the analysis
  prompt

### Verification

1. Manually seed a test DB with 8 rejections labelled `listicle`
   (real articles). Run the script with `--days 30 --reason
   listicle`. Confirm the report identifies "headings-to-paragraphs
   ratio" or similar as a proposed filter.
2. Run against real data with `--days 30` (no reason filter).
   Confirm one section per reason bucket, and "insufficient
   sample" logs for reasons that don't have enough data yet.
3. LLM output parses reliably (JSON strict); errors log a warning
   but don't crash the script.

---

## Phase 3 — Enable filters at the right stage

### Goal

Take the proposals from Phase 2 and wire them into the pipeline.
Different reason classes land in different stages:

- **Structural** reasons (`transcript`, `listicle`,
  `video_dependent`, sometimes `too_long`/`too_short`) → Stage 2
  hard filters. Article never reaches the review queue.
- **Qualitative** reasons (`wrong_tone`) → Stage 4 scoring
  adjustments OR per-reason taste centroids for Stage 7 candidate
  scoring. Soft-penalise, don't hard-reject.

### Decisions locked

1. **Stage 2 filter format**: filters live as Python functions in
   `aarva/stages/stage_2_filter.py` (already exists — has word-
   count and listicle filters at v0). New filters get added
   alongside. Each filter is a small pure function `def
   filter_XYZ(article: dict) -> Optional[str]` returning `None`
   if it passes and a rejection-reason string if it fails.
2. **Per-reason taste centroids** (for Stage 7 layer):
   - New table: `taste_centroids(reason TEXT PRIMARY KEY,
     vector BLOB, updated_at DATETIME, article_count INT)`.
   - A helper rebuilds each centroid from the last 30 days of
     rejections tagged with that reason.
   - Stage 7's candidate scoring adds a per-reason penalty:
     for each active reason centroid, compute cosine similarity
     between candidate embedding and the reason centroid, apply
     small negative weight to ranking_score.
   - Default weights: -0.05 per reason centroid at cosine ≥ 0.7.
     Tunable via `pipeline.yaml`.
3. **Enablement flow** — the operator (you) reviews the Phase 2
   report, decides which proposed filters to enable, and:
   - Writes the corresponding filter function in
     `aarva/stages/stage_2_filter.py`, OR
   - Enables the appropriate taste-centroid weight in
     `pipeline.yaml`.
   - Optional: adds a decision-log row to
     `docs/project_brief.md`.
4. **Kill switch**: every new filter registered in an
   `ENABLED_LEARNED_FILTERS` list in pipeline.yaml. Disabling =
   remove from the list, no code deletion required.

### Files that will change

- `aarva/stages/stage_2_filter.py` — add new filter functions as
  each proposal gets enabled (one filter per PR going forward, so
  each is reviewable in isolation)
- `aarva/db.py` — new `taste_centroids` table for per-reason
  centroids
- `aarva/services/taste_centroids.py` (new or extension of
  existing) — helper that (re)builds centroids from
  `edition_rejections` filtered by reason
- `aarva/stages/stage_7_assemble.py` — extend the existing
  taste-centroid scoring to loop over active reason centroids
- `aarva/config/pipeline.yaml` — new `learning:` block:
  ```yaml
  learning:
    enabled_reason_centroids:
      - too_long
      - wrong_tone
      # add reason codes here to activate their centroid in
      # Stage 7 scoring
    centroid_weight: 0.05
    centroid_sim_threshold: 0.7
  ```

### Verification

1. Run Phase 2. Pick one proposal (say, a `listicle` heading-
   ratio detector). Implement the filter function. Enable in
   the list. Re-run Stage 2 on a small test batch. Confirm the
   filter catches known listicles and lets non-listicle articles
   through.
2. Build a per-reason centroid for `wrong_tone`. Enable in
   pipeline.yaml. Run a Stage 7 dry-run against the current pool.
   Confirm ranking_score deltas for candidates near the centroid
   are visible and small (not overwhelming existing signal).

---

## Concrete example — how a `transcript` rejection flows through

Illustrating the whole loop end-to-end.

1. **Reject** (Phase 1): You review edition #82, one piece is
   a Q&A-formatted transcript. You hit `r 4`, then type `4` at
   the reasons prompt to select `transcript`. Row lands in
   `edition_rejections` with `reason='transcript'`.

2. **Accumulate** (Phase 1 continues): Over 2-3 weeks, 6 more
   transcript rejections accumulate. Sample crosses the min
   threshold of 5.

3. **Analyse** (Phase 2): Weekly, you run
   `python scripts/learn_from_rejections.py --days 30`.
   Report includes a `transcript` section. The LLM identifies:
   - "Articles with more than 4 occurrences of speaker-turn
     markers in the pattern `Q:` or `[Name]:` in the first 1500
     chars"
   - "Articles whose first 300 chars contain phrases like
     'in this conversation', 'lightly edited transcript',
     'the following interview'"
   Confidence: high on both.

4. **Enable** (Phase 3): You review the report, add a Stage 2
   filter function:
   ```python
   def filter_transcript(article: dict) -> Optional[str]:
       body = (article.get("full_text") or "")[:1500]
       speaker_turn_re = re.compile(
           r"^(?:Q|A|\[[A-Z][a-z]+\])[:.]", re.MULTILINE)
       if len(speaker_turn_re.findall(body)) >= 4:
           return "filtered: transcript (speaker turns)"
       lead = body[:300].lower()
       if any(phrase in lead for phrase in (
           "lightly edited transcript",
           "the following interview",
           "in this conversation",
       )):
           return "filtered: transcript (intro phrase)"
       return None
   ```
   Wire into the Stage 2 filter chain. Ship as a small PR.

5. **Result**: Future transcript articles get filtered at Stage
   2, never reach your review. You've taught the system a new
   filter through natural rejection use.

---

## Sequencing

Three PRs, in order:

1. **Phase 1 first** (small — schema + CLI). Immediately starts
   accumulating tagged rejections. Zero downstream dependency.
2. **Phase 2 after 2-3 weeks of Phase 1 usage** so there's data
   to analyse. Ship the script and produce the first report.
3. **Phase 3 per-filter** — enable one filter at a time as
   Phase 2 proposals crystallise. NOT one big PR; each filter
   review + enablement is its own small PR.

Phase 1 blocks 2 (no data). Phase 2 blocks 3 (no proposals). But
Phase 3 doesn't need to be one shot — it's a rolling stream of
small filter-enablement PRs going forward.

---

## Non-goals

- **Do NOT auto-enable filters.** Every filter enablement is a
  human-in-the-loop code review.
- **Do NOT retrofit reasons** on historic `edition_rejections`
  rows. Legacy rows keep `reason = NULL`; Phase 2 skips them.
- **Do NOT expand the reason list beyond seven codes + `other`
  in Phase 1.** New reasons crystallise from patterns in `other`
  free-text notes over time and get promoted to first-class
  codes as evidence accumulates.
- **Do NOT try to teach the system Aarva's editorial taste
  wholesale.** The learning loop targets structural + specific-
  qualitative rejection reasons. The bigger editorial voice
  (rigour + posture) is Stage 4's job and stays separate.
- **Do NOT fine-tune a model.** The learning loop uses in-context
  LLM analysis + rule extraction, not model updates. At Aarva's
  volume, that's the right level of investment.
- **Do NOT wire this into `/create`'s listener-facing candidate
  flow directly.** Phase 3's filters improve what gets INTO the
  editorial catalog; the /create search is a separate concern
  and should remain unaware of per-reason centroids.

---

## Related files worth surveying before starting

- `aarva/stages/stage_2_filter.py` — the target for structural
  filters. Sanity-check the existing filter shape before adding
  new ones.
- `aarva/stages/stage_7_assemble.py::_TASTE_CACHE` — the existing
  approval/rejection centroid mechanism Phase 3 extends. Read it
  first; the extension should mirror the current pattern, not
  invent a new one.
- `aarva/db.py::edition_rejections` — schema change starts here.
- `aarva/review.py::_apply_decisions` — the `r` branch is where
  the reason prompt attaches.

---

## What Cowork owes if this spec has gaps

Same rule as previous session plans. If the reason list turns
out to be missing an obvious code once Phase 1 is in use, if
Phase 2's LLM prompt underperforms on real data, or if Phase 3's
centroid weights swamp existing Stage 7 signal — punt back to
Cowork with the specific example. Don't guess.

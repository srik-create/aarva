# Session plan — strip terminal boilerplate before TTS + surface Gemini safety blocks

Written by Cowork for the next Claude Code session (2026-07-18+).
Two related fixes to the article → TTS pipeline caught on 2026-07-18
when two consecutive editions failed at chunk 5 of Gemini TTS on
publication boilerplate content. Both ship as one PR.

Read this doc + `docs/roadmap.md` + `AGENTS.md` before starting.

---

## Context

Two articles in two consecutive daily runs failed identically at
Stage 9 / crosscut TTS:

- **2026-07-18 daily**: article 9028 (ProPublica, "My Maddening
  Quest to Find Out if Blueberries Come From Farms Where Workers
  Were Harmed"). Chunk 5 blocked deterministically. Terminal
  content: production credits list —
  *"Design and development by Zisiga Mukulu. Videos by Mauricio
  Rodríguez Pons. Visual editing by Shoshana Gordon. Food styling
  by Maricela Vega for ProPublica. Opening illustration by Andrew
  B. Myers for ProPublica."*

- **2026-07-22 crosscut ed 97, passage_b**: article 9233 (STAT
  News, "Opinion: My husband's suicide shows there's something
  very wrong with the U.S. insurance industry"). Chunk 5 blocked
  deterministically. Terminal content: author bio + crisis-line
  footer —
  *"If you or someone you know may be considering suicide, contact
  the 988 Suicide & Crisis Lifeline: Call or text 988 or chat
  988lifeline.org…"*

Both were resolved manually by trimming the terminal paragraph
from `articles.full_text` and re-running the stage. Same root
cause both times: **publication boilerplate appended to article
tails deterministically trips Gemini TTS's safety / policy
filter, which returns HTTP 200 with `candidates=None`**. The
current code catches this as a generic exception, wastes 5
retries with exponential backoff (~40s wall time), then fails
the whole piece.

Both classes of terminal boilerplate are also **useless in
audio** — a listener can't dial 988 while listening to a podcast,
and can't tell who did the design work of the underlying web
article. The user's decision (2026-07-22): **strip these before
TTS. Don't preserve them anywhere in Aarva** — listeners who
want that information click through to the original publication
URL.

---

## Fix A — Strip terminal boilerplate at ingestion

### Where in the pipeline

Add a small cleaner step to **Stage 1** (extraction) — inside
the existing body-extraction path, after `full_text` is
assembled but before it's written to `articles.full_text`.

Rationale: cleaning at Stage 1 (rather than transiently at
Stage 9) means every downstream consumer sees the same clean
body — embeddings, JTBD classification, hook generation, and
TTS. Bios and credit lists add noise to embeddings today; this
also incidentally improves recommendation quality.

**Non-goal**: don't preserve the stripped text anywhere. Per
user decision, listeners get crisis-line info / production
credits from the original article at the source publication when
they click through.

### What to strip

Boilerplate paragraphs typically appear at the article tail as
their own paragraph blocks. Detect and remove any TERMINAL
paragraph matching any of these patterns:

1. **Production credits** — paragraphs starting with:
   - `Design and development by`
   - `Illustration(s) by`
   - `Photography by` / `Photos by`
   - `Video(s) by`
   - `Visual editing by`
   - `Design by`
   - `Additional reporting by`
   - `Copy editing by` / `Copy edited by`
   - `Fact-checked by`
   - `Edited by [Name]` (as a standalone terminal line)
2. **Crisis-line / helpline footers** — paragraphs containing:
   - `988 Suicide` / `988 Suicide & Crisis Lifeline`
   - `Samaritans` + phone-number pattern
   - `If you or someone you know`
   - `National Suicide Prevention Lifeline`
   - `Crisis Text Line`
   - `RAINN` / sexual-assault helpline patterns
   - General shape: `If you (or someone you know) …, contact
     [org name] at [phone/URL]`
3. **Author bios** — paragraphs matching:
   - `<Author Name>, [degrees/titles], is a … [profession]`
   - `<Author Name> writes about … for [publication]`
   - `<Author Name> is the author of …`
   - Detection is fuzzier here — use LLM assistance if regex is
     insufficient (see below).
4. **Subscription / newsletter CTAs**:
   - `Sign up for our newsletter`
   - `Subscribe to [publication]`
   - `Support our journalism`
   - `Read more of our coverage`
5. **Corrections / notices** (KEEP these — they carry editorial
   information; do NOT strip):
   - `Correction:` / `Editor's note:` / `Updated on …`
   - Explicitly excluded from the strip list.

### Detection approach

**Primary: regex-based paragraph classifier.** For each paragraph
in `full_text`, in reverse order from the tail:
- Match against the pattern set above.
- If match → strip the paragraph, continue up.
- If no match → stop. Don't strip anything above a non-matching
  paragraph (protects mid-article prose that mentions "988" or
  similar in editorial context).

**Fallback: LLM assist for author-bio detection** (only if regex
misses too many bios in practice). Send just the last 2-3
paragraphs to Gemini Flash with a "is this an author bio or
editorial content?" prompt. Costs a few tokens per article; only
invoke when the regex pass hasn't already trimmed a bio.

Ship v1 with regex only. Add LLM fallback if daily runs show
material bio-detection misses.

### Logging

Log every strip decision at INFO level so operator can audit:

```
Stage 1: article 9233 — stripped 2 terminal paragraphs
  (bio: "Joy Evers (née Hardison), M.D., M.P.H., …")
  (crisis-line: "If you or someone you know may be…")
```

Include a summary at the end of Stage 1:
`Stage 1: stripped terminal boilerplate from N of M articles.`

### Files that change

- `aarva/stages/stage_1_extract.py` (or wherever body extraction
  finalises `full_text`) — add the cleanup pass.
- `aarva/services/terminal_boilerplate.py` (new) — the pattern
  set + the paragraph-classifier function. Isolated so patterns
  can be added over time as new publication footers appear.
- **No schema change.** `articles.full_text` is directly
  overwritten by the cleaner during ingestion.

### Backfill

**Not required.** Historic articles (already ingested with
boilerplate) stay as-is. The failure cases both trigger only at
TTS time; historic articles whose audio already synthesized
successfully don't need re-cleaning. If a future edition happens
to re-select an old article and TTS blocks again, run the
cleaner ad-hoc on that specific row.

Optional: a one-off script `scripts/strip_terminal_boilerplate.py`
that runs the cleaner on all existing rows. Only worth running
if the operator notices recommendation quality improvements
from the cleaner would be worth the reprocessing. Skip for v1.

### Verification

1. Feed the cleaner article 9028's original body (pre-trim,
   recoverable from git or from the original ProPublica URL).
   Confirm the "Design and development by…" paragraph is
   stripped and the previous paragraph ("Over two dozen trade
   groups declined to talk…") is preserved.
2. Feed article 9233's original body (crisis-line + bio version).
   Confirm both trailing paragraphs are stripped and the last
   editorial sentence ("Late at night, fighting sleep to finish
   charts and check the right boxes so claims won't get denied.")
   is preserved.
3. Feed an article whose tail is EDITORIAL content that happens
   to mention "988" (e.g. an article about the 988 hotline as
   its topic). Confirm nothing is stripped — the classifier only
   fires on paragraphs whose FULL SHAPE matches boilerplate, not
   on any paragraph that contains the number 988.
4. Feed an article whose tail is a `Correction:` paragraph.
   Confirm it's preserved (explicit non-strip category).
5. Run a full daily pipeline on a fresh mock day with mixed
   real articles. Confirm no editorial content is lost and TTS
   completes cleanly.

---

## Fix B — Detect Gemini safety blocks properly in the TTS client

### Where

`aarva/clients/tts.py`, `_synth_chunk` method — currently line
436-513 area. The current code does:

```python
parts = response.candidates[0].content.parts   # ← throws when candidates=None
for part in parts:
    if getattr(part, "inline_data", None) and part.inline_data.data:
        pcm = part.inline_data.data
        break
```

When Gemini blocks a request on content policy, `response.candidates`
is `None` (or an empty list depending on SDK version). The
subscript raises `TypeError: 'NoneType' object is not subscriptable`,
which is caught by the generic `except Exception` and reported
as a retryable failure. Five retries later, the piece fails —
with a message that doesn't tell the operator what actually
happened.

### What to change

Before subscripting `candidates`, check the response structure:

```python
# Handle the deterministic content-block case explicitly.
if not response.candidates:
    reason = ""
    try:
        pf = getattr(response, "prompt_feedback", None)
        if pf is not None:
            reason = getattr(pf, "block_reason", "") or ""
    except Exception:
        pass
    raise _NonRetryableTTSError(
        f"Gemini TTS refused synthesis (block_reason={reason!r}). "
        f"This is a deterministic content block — retrying will not help. "
        f"Chunk starts with: {chunk[:120]!r}"
    )

# Also inspect finish_reason on the first candidate.
first = response.candidates[0]
finish = getattr(first, "finish_reason", None)
if finish and str(finish).upper() in ("SAFETY", "PROHIBITED_CONTENT",
                                      "SPII", "BLOCKLIST"):
    raise _NonRetryableTTSError(
        f"Gemini TTS candidate blocked (finish_reason={finish}). "
        f"Chunk starts with: {chunk[:120]!r}"
    )
```

Add a private `_NonRetryableTTSError` class alongside the
existing exception. The retry loop should check for it and
skip the backoff-and-retry logic — fail fast.

### Impact on error reporting

With Fix B in place, the operator sees a log line like:

```
GeminiTTS: refused synthesis (block_reason='SAFETY') — non-retryable.
  Chunk starts with: "If you or someone you know may be considering…"
```

instead of:

```
GeminiTTS chunk synth failed (attempt 5/5): 'NoneType' object is not subscriptable
```

That's the diagnostic difference between "spent 40 seconds
retrying and don't know why it failed" and "one clear line
telling me which chunk to inspect."

### Verification

1. Feed a chunk of raw crisis-line boilerplate (the article
   9233 tail, pre-strip). Confirm the client raises
   `_NonRetryableTTSError` on the first attempt with the block
   reason in the log line.
2. Feed a chunk that fails for a TRANSIENT reason (e.g. simulate
   a network timeout). Confirm the existing retry-with-backoff
   logic still runs — Fix B doesn't collapse legitimate retries.
3. Feed a chunk that succeeds. Confirm no behaviour change.

---

## Non-goals

- **Do not preserve stripped content in `show_notes`, the web
  view, or anywhere else in Aarva.** Explicit user decision
  2026-07-22. Listeners who want crisis-line info / production
  credits click through to the source article.
- **Do not backfill historic articles.** New articles get the
  cleaner going forward; old articles are re-cleaned only if
  they re-fail at TTS.
- **Do not add per-publication boilerplate rules** in v1. The
  pattern set is publication-agnostic — the boilerplate shapes
  we care about (credits, hotlines, bios, CTAs) look the same
  across publications. Revisit if a specific publication has an
  unusual footer shape.

---

## Files that change

- `aarva/stages/stage_1_extract.py` — call the cleaner on
  `full_text` before persisting.
- `aarva/services/terminal_boilerplate.py` (new) — pattern set
  + classifier.
- `aarva/clients/tts.py` — `_synth_chunk` safety-block detection
  (Fix B).
- `docs/roadmap.md` — after this PR merges, move the item from
  In-Progress to Recently Completed (Claude Code owns this per
  AGENTS.md rule 17).

# Session plan — strip The Independent's "Join our commenting forum" CTA

Written by Cowork for the next Claude Code session (2026-07-28+).
Follow-up to `session_plan_tts_boilerplate_strip.md` (shipped
2026-07-22). That work added a paragraph-level boilerplate stripper
for production credits, crisis-line footers, and generic
subscription CTAs. 2026-07-28 a listener-created crosscut failed at
Gemini TTS chunk 5/5 with `PROHIBITED_CONTENT` — The Independent's
"Join our commenting forum" engagement CTA at the article tail
tripped Gemini's safety filter. The existing pattern set doesn't
catch this shape, so the paragraph shipped into TTS unmodified.

This spec adds the Independent-CTA shape (plus a couple of
adjacent publications' patterns worth pre-empting) to the
existing CTA regex. Small focused change to one file.

Read this doc + `docs/roadmap.md` + `AGENTS.md` before starting.

---

## Architecture check (rule 17d)

Grep outputs referenced below are from 2026-07-28.

**Reference greps:**

```
$ sed -n '85,99p' aarva/services/terminal_boilerplate.py
_SUBSCRIPTION_CTA_RE = re.compile(
    r"^\s*("
    r"sign up for our newsletter|"
    r"subscribe to\b|"
    r"support our journalism|"
    r"read more of our coverage"
    r")",
    re.IGNORECASE,
)

_STRIPPABLE_PATTERNS = (
    ("credits", _PRODUCTION_CREDIT_RE),
    ("crisis-line", _CRISIS_LINE_RE),
    ("cta", _SUBSCRIPTION_CTA_RE),
)

$ ls aarva/tests/
(empty)

$ grep -nE "^_NEVER_STRIP_RE|^_PRODUCTION_CREDIT_RE|^_CRISIS_LINE_RE|^_BIO_VERB_RE|^def _classify_paragraph|^def strip_terminal_boilerplate" aarva/services/terminal_boilerplate.py
27:_NEVER_STRIP_RE = re.compile(
32:_PRODUCTION_CREDIT_RE = re.compile(
50:_CRISIS_LINE_RE = re.compile(
73:_BIO_VERB_RE = re.compile(
102:def _classify_paragraph(paragraph: str) -> str | None:
118:def strip_terminal_boilerplate(full_text: str) -> tuple[str, list[tuple[str, str]]]:

$ grep -nE "terminal_boilerplate|strip_terminal_boilerplate" aarva/stages/stage_1_ingest.py
20:from aarva.services.terminal_boilerplate import strip_terminal_boilerplate
90:    full_text, stripped = strip_terminal_boilerplate(extracted.full_text)

$ grep -nE "strip_terminal_boilerplate" aarva/ingest_url.py
223:    from aarva.services.terminal_boilerplate import strip_terminal_boilerplate
224:    return strip_terminal_boilerplate(full_text)
```

Plus `strip_terminal_boilerplate`'s docstring (`aarva/services/terminal_boilerplate.py:118-149`) documents the terminal-only walk: *"Walks backward from the last paragraph… the first genuinely non-matching paragraph stops the walk — nothing above it is ever touched."*

**Now the three questions:**

1. **Where does the data live?**
   - Regex pattern set at `aarva/services/terminal_boilerplate.py:85-93` (`_SUBSCRIPTION_CTA_RE`).
   - Registered in `_STRIPPABLE_PATTERNS` tuple at
     `aarva/services/terminal_boilerplate.py:95-99`.
   - Sibling regex constants `_NEVER_STRIP_RE:27`,
     `_PRODUCTION_CREDIT_RE:32`, `_CRISIS_LINE_RE:50`,
     `_BIO_VERB_RE:73` (cited above).
   - Test coverage: `aarva/tests/` directory exists but is
     empty per `ls` above. No boilerplate tests yet. This
     spec adds the first test file in that dir.

2. **Where does the operation run?**
   - Daily-pipeline ingestion: `aarva/stages/stage_1_ingest.py:20`
     imports `strip_terminal_boilerplate`, `:90` calls it as
     `full_text, stripped = strip_terminal_boilerplate(extracted.full_text)`.
   - Ad-hoc URL ingest via `/create` uses the same cleaner:
     `aarva/ingest_url.py:223-224` imports and calls
     `strip_terminal_boilerplate` on the extracted body.
   - The `articles.full_text` column persistence itself lives
     downstream of these calls; this spec doesn't touch the
     write path, only the pattern that's applied before
     persistence.

3. **Does the operation have physical access to the data it
   needs?**
   - Yes. Regex change is a one-file edit in
     `aarva/services/terminal_boilerplate.py`; imports and
     call-sites are unchanged. No schema, no migration, no
     cross-DB or cross-host concerns.

---

## Locked decisions (with user, 2026-07-28)

1. **Add the Independent CTA shape as a new alternative** in
   `_SUBSCRIPTION_CTA_RE`. Specifically the "join our" family,
   which catches Independent's opener but is narrow enough not
   to match editorial prose.
2. **Backfill NOT required.** Existing article rows with the
   CTA baked into `full_text` stay as-is; the operator manually
   trimmed article 10317 for job 6 on 2026-07-28 (see
   `docs/roadmap.md`'s 2026-07-28 entry). Future ingestion
   catches the CTA at intake.
3. **No LLM-based fallback (yet).** Regex-only stays the v1
   approach per the earlier boilerplate spec. If more publisher-
   specific CTA shapes appear over the next few weeks, then
   re-open the LLM-fallback question.

---

## The change

### `aarva/services/terminal_boilerplate.py`

Currently at lines 85-93:

```python
_SUBSCRIPTION_CTA_RE = re.compile(
    r"^\s*("
    r"sign up for our newsletter|"
    r"subscribe to\b|"
    r"support our journalism|"
    r"read more of our coverage"
    r")",
    re.IGNORECASE,
)
```

Change to:

```python
_SUBSCRIPTION_CTA_RE = re.compile(
    r"^\s*("
    r"sign up for our newsletter|"
    r"subscribe to\b|"
    r"support our journalism|"
    r"read more of our coverage|"
    r"join our commenting forum|"
    r"join thought-provoking conversations|"
    r"join the conversation\b"
    r")",
    re.IGNORECASE,
)
```

Three new alternatives:
- `join our commenting forum` — The Independent's exact opener
  (verified against the blocked chunk in the 2026-07-28 log:
  `Chunk starts with: 'Join our commenting forum\nJoin
  thought-provoking conversations, follow other Independent
  readers…'`).
- `join thought-provoking conversations` — The Independent's
  second sentence in the same block; catches the CTA even if a
  paragraph split occurs mid-way through.
- `join the conversation\b` — pre-empts other publications
  using this common phrasing. Word boundary keeps it from
  matching editorial prose like "join the conversation about
  climate change" mid-paragraph — the pattern only fires on
  paragraphs that START with it (per the `^\s*` anchor in the
  regex) AND only on trailing paragraphs (per
  `strip_terminal_boilerplate` at
  `aarva/services/terminal_boilerplate.py:118-149`, which walks
  backward and stops at the first non-matching paragraph, so
  editorial prose above the tail is never touched).

### Test file (new)

Add `aarva/tests/test_terminal_boilerplate.py` — `aarva/tests/`
exists (per `ls` above) but is empty, so this is the first
test file in that dir. Import
`strip_terminal_boilerplate` (verified at
`aarva/services/terminal_boilerplate.py:118`) or
`_classify_paragraph` (verified at
`aarva/services/terminal_boilerplate.py:102`) — whichever
Claude Code judges the cleaner test surface. Include cases for:

- The exact Independent CTA from the 2026-07-28 log — must be
  classified as `cta` and stripped.
- A short paragraph mentioning "join our commenting forum"
  mid-sentence (not as opener) — must NOT be stripped (the
  `^\s*` anchor protects this).
- Editorial prose mentioning "join the conversation" mid-body
  — must NOT be stripped (paragraph doesn't start with it, AND
  `strip_terminal_boilerplate` only walks the tail).
- A paragraph starting with "Join the conversation:" as a
  terminal CTA — must be stripped.
- Corrections paragraph — must NEVER be stripped. `_NEVER_STRIP_RE`
  is a real guard at `aarva/services/terminal_boilerplate.py:27`
  and `_classify_paragraph` checks it first
  (`terminal_boilerplate.py:107-108`: `if _NEVER_STRIP_RE.search(text): return None`).
- Regression: existing four CTAs (newsletter, subscribe,
  support, read more) still strip correctly.

Concrete test skeleton — Claude Code should adapt to whatever
test framework Aarva uses. Verify what framework the repo
expects before writing: no test files exist yet in `aarva/tests/`
per `ls` output above, so this may need pytest added as a dev
dependency in the same PR. Claude Code should check
`pyproject.toml` / `requirements-dev.txt` (verify what config
files exist before assuming) and pick accordingly.

### Rollout

- Ship as a single small PR touching just
  `aarva/services/terminal_boilerplate.py` (+ 3 alternatives)
  and the new test file.
- No behaviour change for articles already ingested — cleaner
  only runs at ingestion time. Deploy sends the fix live for
  the next ingestion cycle.
- Not urgent (single-listener impact was already patched
  manually 2026-07-28), but low-risk and prevents recurrence.

---

## Non-goals

- **No backfill** of existing articles with the CTA baked in.
  Only touches new ingestion. If a specific old article causes
  a future TTS failure, trim it manually — same shape as the
  2026-07-28 patch.
- **No LLM-based classification** — regex-only stays v1.
- **No changes to `_PRODUCTION_CREDIT_RE`, `_CRISIS_LINE_RE`,
  `_BIO_VERB_RE`, or `_NEVER_STRIP_RE`** — those four regex
  constants exist at `aarva/services/terminal_boilerplate.py`
  lines 32, 50, 73, 27 respectively (grep-cited above) and
  keep working as-is.
- **No admin dashboard** for adding new patterns. When more
  CTA shapes appear, they get added via small PRs like this.
- **No changes to `aarva/stages/stage_1_ingest.py`** — the
  cleaner call is already wired at line 90
  (`full_text, stripped = strip_terminal_boilerplate(extracted.full_text)`,
  grep-cited above); only the regex pattern set changes.

---

## Files that change

- `aarva/services/terminal_boilerplate.py` — three regex
  alternatives added to `_SUBSCRIPTION_CTA_RE`.
- `aarva/tests/test_terminal_boilerplate.py` (new). The
  `aarva/tests/` dir exists (per `ls` output in the
  Architecture check above) but is empty; this is the first
  test file there.
- `docs/roadmap.md` — after PR merges, move from In-Progress
  to Recently Completed (Claude Code owns this per AGENTS.md
  rule 17).

---

## Verification

1. Run the new test file — all cases pass.
2. Manually invoke `_classify_paragraph` on the exact
   Independent CTA text from the log — returns `'cta'`. Entry
   point verified at `aarva/services/terminal_boilerplate.py:102`.
   Alternatively invoke `strip_terminal_boilerplate` (public
   entry point at `terminal_boilerplate.py:118`) on a
   full_text ending with the CTA and confirm it's removed.
3. Manually invoke on a paragraph containing "join our
   commenting forum" mid-sentence — returns `None` (not
   stripped) because the `^\s*` anchor requires the phrase at
   paragraph start.
4. Run one daily-pipeline ingestion cycle against a small
   test set that includes an Independent article — confirm the
   CTA paragraph is stripped from `full_text` before
   persistence, and no other content is lost.
5. After merge + deploy, watch the next ingestion of an
   Independent article. `full_text` on the resulting row
   should NOT contain "Join our commenting forum".

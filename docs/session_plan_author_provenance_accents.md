**STATUS: DONE (2026-07-16).** Shipped per this spec with a few
adjustments — see `docs/roadmap.md`'s 2026-07-16 "Recently completed"
entry for the full writeup. Summary of deviations:
- Wired as `--stage 85` in `daily.py` (not a literal "8c") since the
  CLI's `--stage` is `type=int`; mirrors the existing "Stage 1.5" →
  `15` convention.
- `aarva/services/queries.py` needed no changes — it only serves
  already-synthesized audio, not TTS-time accent selection.
- The draft prompt below under-specified one leak: inferring
  provenance from the article's *topic* (e.g. US-domestic-policy
  articles on ProPublica got `us` with no actual author-residence
  evidence). Fixed in `stage_8c_author_provenance.py`'s prompt with
  an explicit rule against topic/dateline-based inference; re-verified
  against the same real articles that had triggered it.
- One narrow residual edge case not fixed: non-person "channel"
  bylines (e.g. "Aeon Video") describing a third party's nationality
  in the body can still get misattributed. Bounded by the unknown/
  publication-tag fallback; not worth the complexity to close further
  right now.

---

# Session plan — author-provenance-based accents (per-article, not per-publication)

Written by Cowork for the next Claude Code session (2026-07-16+).
Medium-sized enhancement to Stage 9's per-piece accent steering.
Moves from publication-based to author-provenance-based accent
selection, with publication tag as fallback.

Read this doc + `docs/roadmap.md` + `AGENTS.md` before starting.

---

## Context

Today's accent-steering logic lives in
`aarva/stages/stage_9_tts.py::_accent_prompt_for`. It looks up the
article's publication in a country map built from
`publications.yaml` and applies one of three accent styles
(us/uk/india) via TTS `extra_style`. Publications without a
`country:` tag fall through to the default neutral American voice.

This is coarse. Two problems:

1. **Under-covers**: publications like The Diplomat aren't tagged
   (they publish authors from everywhere), so an article by
   Akhilesh Pillalamarri — clearly Indian, clearly writing about
   India — gets the default American accent.
2. **Over-covers**: if we naively tagged The Diplomat as, say,
   `us`, every article would get an American accent — but a
   Chinese author writing for The Diplomat gets a wrong-fit accent.
   Same problem in the other direction for Himal Southasian.

**Real signal**: where does the AUTHOR live/work, not what
publication they write for. That's what should drive the accent
choice.

## Constraint the user set (2026-07-16)

**Do NOT infer provenance from the author's name alone.** Diaspora
authors are common. Neel Mukherjee has an Indian name but lives in
London — his voice should be British-accented, not Indian.
Akhilesh Pillalamarri also has an Indian name but is based in
Washington and writes about South Asia — but his provenance is
actually Indian (born + raised in Andhra Pradesh, English-medium
educated, writes as a South Asian voice). Both examples show that
NAME alone is misleading; you need ACTUAL PROVENANCE evidence.

**Only assign a regional accent when there's real evidence of
where the author currently lives / grew up / works.** If evidence
is absent or unclear, fall back to the publication tag; if that's
also missing, use the default. Being conservative here matters —
a wrong accent is worse than a neutral default.

---

## Design

### Signal sources for provenance, in order of reliability

1. **Explicit author bio** — many articles include a bio footer
   ("Akhilesh Pillalamarri is a lecturer at Johns Hopkins SAIS"
   or "Neel Mukherjee lives in London"). Aarva's body-extraction
   captures this if it's in the article HTML.
2. **First-person biographical body text** — "growing up in
   Delhi", "here in London", "when I moved back to Lagos". These
   are strong evidence of where the author lives / grew up.
3. **Publication + explicit affiliation** — "Delhi correspondent
   for The Guardian", "Aarva's Bombay-based reporter." Suggests
   provenance even when no bio exists.
4. **Byline location tags** — dateline / place-of-publication in
   the byline area. Less reliable but worth using when nothing
   else is available.

If NONE of these fire, provenance = `unknown`. Do not guess from
the name.

### Schema

New column on `articles`:

```sql
ALTER TABLE articles ADD COLUMN author_country_code TEXT;
-- Values: 'us' | 'uk' | 'india' | 'unknown' | NULL
-- NULL = not yet classified (pre-migration)
-- 'unknown' = classifier ran but couldn't determine
```

`unknown` is DIFFERENT from `NULL` — it means "we tried and
couldn't tell." NULL means "we haven't tried yet." Backfill turns
NULLs into concrete values (including 'unknown').

### New classifier — Stage 8c

A new stage that runs after body extraction (Stage 1) and before
TTS (Stage 9). Structurally similar to
`stage_9_tts.py::_pick_narrator_voice`'s gender-detection prompt
— a small dedicated Gemini call per article, cached on the
articles row.

Runs once per article at ingestion time. Backfilled for existing
articles via a one-off script.

**Prompt (draft — refine before shipping):**

```
You classify the CURRENT PROVENANCE of a journalism article's
author, based ONLY on evidence in the byline, author bio, and
article body. This is used to select a TTS accent for the audio
version.

Return ONE label:

- us       — Author currently lives, works, or grew up primarily
             in the United States. Evidence must be explicit:
             "based in New York", "American commentator", "grew
             up in Ohio", university/employer in the US, or
             clear first-person markers ("here in California").
- uk       — Same standard, for the United Kingdom.
- india    — Same standard, for India specifically. Not "South
             Asia" — for regional writers we still need explicit
             India signal.
- unknown  — Evidence is absent, ambiguous, or the author is
             clearly from elsewhere (France, China, Nigeria,
             Australia, diaspora writer whose provenance isn't
             one of us/uk/india, etc.).

CRITICAL RULES:
- Do NOT infer from the author's name. Names indicate heritage,
  not current provenance. Neel Mukherjee → NOT india unless his
  bio says he lives in India. Akhilesh Pillalamarri → NOT india
  unless his bio confirms India (he lives in the US → us if
  evidence supports, else unknown).
- Do NOT infer from the publication. This is about the AUTHOR.
- Prefer 'unknown' when in doubt. A wrong-accent voice is worse
  than a neutral default.

Article byline: {byline}
Article body (first 2000 + last 1000 chars, to capture opening
and any author-bio footer):
{body_excerpt}

Provenance:
```

Excerpt shape (first 2k + last 1k): captures the opening (often
includes datelines or "the author lives in…" leads) and the
footer (where author bios typically sit). Middle chunk is body
text — usually less informative for provenance.

### Precedence at TTS time

`_accent_prompt_for(piece, country_map)` becomes:

```python
def _accent_prompt_for(piece, country_map):
    # 1) Author provenance wins if it's a known accent code.
    author_cc = piece.get("author_country_code")
    if author_cc in ("us", "uk", "india"):
        return _COUNTRY_TO_ACCENT_PROMPT[author_cc]
    # 2) Fall back to publication tag.
    pub_cc = country_map.get(piece.get("publication_name") or "")
    if pub_cc:
        return _COUNTRY_TO_ACCENT_PROMPT.get(pub_cc)
    # 3) No steer.
    return None
```

Rule: **author provenance strictly overrides publication tag**.
The publication tag is the fallback for when we don't know the
author.

### Backfill

One-off script `scripts/backfill_author_country.py`:

- Walk every article where `author_country_code IS NULL`.
- For each, run the classifier prompt with the extracted body.
- Persist the result (including 'unknown' — that's a valid
  outcome, not a "try again later" state).
- Rate-limit and batch as Stage 4-5-6's rate-limiter already
  does; reuse the same `RateLimiter` pattern.
- Log progress every 100 articles.

At Aarva's ~5,300-article catalog and ~$0.001 per Gemini call,
backfill costs ~$5 and takes ~30 minutes at 30 RPM.

---

## Files that will change

- `aarva/db.py` — add `author_country_code` column to `articles`
  (both `CREATE TABLE IF NOT EXISTS` + a migration ADD COLUMN
  guard for pre-existing DBs).
- `aarva/stages/stage_8c_author_provenance.py` (new) — the
  classifier. Small: one function `classify_author_provenance(
  article, llm) -> str`. Mirrors the shape of
  `_pick_narrator_voice` in stage_9_tts.py.
- `aarva/prompts.yaml` (or wherever prompts land in this repo) —
  the classifier prompt. Follow the same conservative-tone
  guidance as elsewhere.
- `aarva/daily.py` — wire Stage 8c into the daily pipeline after
  Stage 1.5 consolidation, before Stage 9 TTS. Idempotent — skip
  articles that already have `author_country_code` set.
- `aarva/stages/stage_9_tts.py::_accent_prompt_for` — precedence
  rule change above.
- `aarva/services/queries.py` — any query that loads
  `edition_pieces` for TTS needs to also load
  `articles.author_country_code` and denormalize it into the
  piece dict (mirror the existing `publication_name` handling).
  Same for listener DB's `edition_pieces` — that side already
  denormalizes `article_publication` etc., add
  `author_country_code` alongside.
- `aarva/listener_db.py` — add `author_country_code` to
  `edition_pieces`' denormalized columns (mirror the pattern for
  article_title, article_publication, article_byline).
- `scripts/backfill_author_country.py` (new) — the one-off.

## Verification

1. Pick 5 known-provenance test articles across the range:
   - Clearly India-based Indian author (Akhilesh Pillalamarri
     writing for The Diplomat — bio says Washington-based
     scholar of South Asia, but grew up in Andhra Pradesh; edge
     case, likely `us` given current-provenance rule).
   - Clearly UK-based Indian-heritage author (any Neel Mukherjee
     piece for The Guardian or NYRB).
   - Clearly US-based author for a UK publication (e.g., an
     American writing in The Guardian).
   - Chinese author for The Diplomat — should return `unknown`,
     not `india` from name confusion.
   - No-byline article — should return `unknown`.
2. Run the classifier on each. Verify the returned code matches
   the expected provenance.
3. Run backfill on a slice of 50 recent articles. Manually
   inspect 5 of the classifications. Confirm no obviously-wrong
   assignments (name-based mistakes).
4. Trigger a full TTS on a test edition where all five test
   articles appear. Listen. Verify the accents match expectations.
5. Confirm precedence: an article from a `country: uk`-tagged
   publication where `author_country_code = 'india'` uses the
   Indian accent, not the British one.

---

## Non-goals

- **Do NOT add support for new accent codes** in this session
  (Chinese, French, Nigerian, Latin American, etc.). Only us / uk
  / india — matching the current accent map. Adding codes is a
  separate PR (needs new TTS voice prompts + testing).
- **Do NOT infer provenance from author name alone.** Fully
  covered in the constraint above but worth restating: if the
  classifier returns a country code based purely on name-onomastics
  vibes, that's a bug.
- **Do NOT retag any publications** in `publications.yaml`. The
  publication tag stays as a fallback for authors we can't classify.
  If a publication should be re-tagged for OTHER reasons, that's a
  separate discussion.
- **Do NOT run classifier per-request** (e.g., in the `/create`
  build flow). It runs once per article at ingestion + backfill,
  result cached in DB. TTS reads the cached value.
- **Do NOT weight author bio scraping enhancements** into this
  PR. The classifier uses whatever `articles.full_text` already
  contains. If the extractor is missing author-bio footers on
  some publications, that's a separate improvement to Stage 1
  extraction.

---

## What Cowork owes if this spec has gaps

Same rule as previous session plans. If the classifier turns out
to over-return `unknown` on real articles (too conservative), or
if there's a schema conflict between `articles.author_country_code`
and existing code paths, punt back to Cowork with the specific
example.

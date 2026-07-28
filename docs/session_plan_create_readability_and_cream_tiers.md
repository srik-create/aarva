# Session plan — /create card readability + site-wide cream brightness hierarchy

Written by Cowork for the next Claude Code session (2026-07-27+).
Two related listener-facing fixes bundled into one PR (both approved
via Cowork mockups 2026-07-27):

1. **Fix `.hook` misapplication on /create card** — the pairing
   description on `/create?q=…` renders `c.why` (multi-sentence
   prose) with the Anton-uppercase `.hook` pull-quote treatment,
   which reads as a shouty blob at that length. Change to
   sentence-case Inter prose with the red left-border kept as the
   editorial signal. Also italicise the two source-article titles
   inline (traditional editorial convention).
2. **Introduce a three-tier cream brightness hierarchy** — cream
   text on dark surfaces currently renders uniformly at 100% (via
   `text-cream-text` `#F0E5D0`). Dial reading-scale text down so
   the page feels calmer while display type still anchors it:
   100% for the wordmark + Anton page H1s, 85% for card topics /
   article titles, 75% for body prose. Metadata / muted classes
   unchanged.

Read this doc + `docs/roadmap.md` + `AGENTS.md` before starting.

**AGENTS.md rule 4 sign-off**: listener-facing visual change. User
approved direction 2026-07-27 via three rounds of Cowork mockups
(description-treatment options, subheading-treatment options,
three-tier brightness hierarchy).

---

## Architecture check (rule 17d)

1. **Where does the data live?**
   - `.hook` CSS class definition: `aarva/server/templates/base.html`
     lines 125-135.
   - Cream tokens (Tailwind config): `aarva/server/templates/base.html`
     lines 48-50.
   - `.hook` class usages: server-rendered Jinja templates under
     `aarva/server/templates/` (verified via grep 2026-07-27; only
     one misapplication found, see below).
   - No DB state, no listener_db, no cross-host data flow.
2. **Where does the operation run?**
   - Server-side Jinja rendering in the FastAPI process on Render.
     Static CSS via Tailwind CDN in-browser. Client-side rendering
     of tokens picks up the new Tailwind config on next page load.
3. **Does the operation have physical access to the data it needs?**
   - Yes. All files are in the git repo, edited on the branch, and
     deployed via the existing FastAPI + Render pipeline. No CLI
     tools, no cross-DB writes, no admin endpoints needed.

---

## Context

**On the `.hook` audit (grep-verified 2026-07-27):**

- `.hook` is defined in `base.html:125-135` as Anton, 18px,
  uppercase, red-left-border, warm-off-white — designed for
  single-sentence editorial pull-quotes on article cards.
- All `.hook` class usages in server templates:
  - `home.html:162` — article card, `piece.hook` (short) — OK
  - `home.html:221` — bonus card, `piece.hook` — OK
  - `article.html:44` — article detail, `piece.hook` — OK
  - `category_detail.html:34` — article row, `piece.hook` — OK
  - `publication_detail.html:45` — article row, `piece.hook` — OK
  - `landing.html:5` — short editorial tagline (14 words) — OK
  - `_candidates_fragment.html:43` — `c.why` (multi-sentence
    connection summary) — **MISAPPLIED**; this is the one to fix.
- Static bake-outs in `aarva/output/web/*.html` also use `.hook`,
  but those are historical past-edition renders, not touched by
  future template edits.

**On the cream hierarchy:** the redesign (2026-07-25) shipped
uniform 100% `#F0E5D0` cream on all dark-surface text. That's
punchy for headline-scale display type but too bright for
long-form reading. The three-tier proposal — 100% / 85% / 75% —
keeps the display anchor while calming the reading text.

---

## Locked decisions (with user, 2026-07-27)

### Fix 1 — /create card readability

1. **Description block (`c.why`)** — swap `.hook` for sentence-case
   Inter prose, keeping the red left-border as the editorial
   "here's why this pairing works" signal.
2. **Subheading (two source-article titles)** — swap
   `<em class="editorial not-italic">` (which currently renders as
   Anton uppercase) for `<em>` in normal italic (Inter italic).
   Traditional editorial convention: article/book titles
   italicised inline.

### Fix 2 — cream brightness hierarchy

New Tailwind tokens added, `cream-text` kept at 100% for full-
brightness display anchors:

- `cream-text` `#F0E5D0` (100%) — **kept**. Used for: wordmark,
  Anton page H1s, anywhere display type needs full punch.
- `cream-title` `rgba(240, 229, 208, 0.85)` — **NEW**. Used for:
  card topics (e.g. `<h2 class="editorial text-2xl">` on
  `/create` cards), article/episode titles at card-scale.
- `cream-body` `rgba(240, 229, 208, 0.75)` — **NEW**. Used for:
  body prose (multi-sentence descriptions, article transcript
  text on dark surfaces, mini-player track title).
- `cream-light` `rgba(240, 229, 208, 0.65)` — **kept**. Used for:
  bylines, "asked:" attribution, medium-emphasis secondary text.
- `cream-muted` `rgba(240, 229, 208, 0.45)` — **kept**. Used for:
  timestamps, dividers, `×` separators, tertiary metadata.

Class-swap mapping — every current `text-cream-text` usage gets
re-categorised into one of the three tiers. Concrete list in the
"Class re-mapping" section below.

---

## Implementation

### `aarva/server/templates/base.html` — Tailwind config

Add two new tokens in the `theme.extend.colors` block (line ~44):

```js
'cream-text':   '#F0E5D0',                        // 100% — display anchors (unchanged)
'cream-title':  'rgba(240, 229, 208, 0.85)',      // NEW — card topics, article titles
'cream-body':   'rgba(240, 229, 208, 0.75)',      // NEW — body prose, mini-player title
'cream-light':  'rgba(240, 229, 208, 0.65)',      // unchanged
'cream-muted':  'rgba(240, 229, 208, 0.45)',      // unchanged
```

### `aarva/server/templates/_candidates_fragment.html`

Two changes on this template:

**Line 32** (card topic — H2 headline):
```jinja
{# BEFORE #}
<h2 class="editorial text-2xl mt-2 leading-snug text-cream-text">{{ c.topic_label | title_case }}</h2>
{# AFTER #}
<h2 class="editorial text-2xl mt-2 leading-snug text-cream-title">{{ c.topic_label | title_case }}</h2>
```

**Lines 34-38** (subheading — two article titles + bylines):
```jinja
{# BEFORE #}
<p class="text-sm text-cream-light mt-3 leading-relaxed">
  <em class="editorial not-italic">{{ c.title_a | title_case }}</em>{% if c.byline_a %}, by {{ c.byline_a }}{% endif %}
  <span class="text-cream-muted mx-1">×</span>
  <em class="editorial not-italic">{{ c.title_b | title_case }}</em>{% if c.byline_b %}, by {{ c.byline_b }}{% endif %}
</p>

{# AFTER #}
<p class="text-sm text-cream-light mt-3 leading-relaxed">
  <em class="text-cream-body">{{ c.title_a | title_case }}</em>{% if c.byline_a %}, by {{ c.byline_a }}{% endif %}
  <span class="text-cream-muted mx-1">×</span>
  <em class="text-cream-body">{{ c.title_b | title_case }}</em>{% if c.byline_b %}, by {{ c.byline_b }}{% endif %}
</p>
```

Note: dropping `class="editorial not-italic"` off the `<em>`
tags means:
- Inter font (not Anton) via the default body inheritance
- Native italic (was suppressed by `not-italic`) — the italic is
  the editorial-convention signal
- Warm cream at 75% via `text-cream-body`

**Line 43** (description / connection summary):
```jinja
{# BEFORE #}
<p class="hook mt-5 whitespace-pre-line">{{ c.why }}</p>

{# AFTER #}
<p class="text-cream-body mt-5 leading-relaxed whitespace-pre-line border-l-2 border-red-accent pl-4">{{ c.why }}</p>
```

Sentence-case Inter body prose (via `text-cream-body`),
`leading-relaxed`, red left-border kept.

### Class re-mapping for the cream hierarchy

Category | Current | New
---|---|---
Wordmark "AARVA" (header masthead) | `text-cream-text` | `text-cream-text` (unchanged)
Page H1 (Anton huge display, e.g. `/today` heading, `/create` prompt) | `text-cream-text` | `text-cream-text` (unchanged)
Card topics / article titles on dark cards (e.g. `_candidates_fragment.html:32` H2) | `text-cream-text` | `text-cream-title`
Body prose on dark cards / pages (e.g. `_candidates_fragment.html:43` description; article-detail transcript body if it lives on dark bg) | `text-cream-text` | `text-cream-body`
Mini-player track title | `text-cream-text` | `text-cream-body`
Body of a section that's not a title — the "Every day, a handpicked selection…" paragraph on landing | `text-cream-light` (already) | keep as-is (60% is fine for that role)

Walk through every current use of `text-cream-text` in
`aarva/server/templates/**/*.html` and re-categorise. Rough grep
count based on 2026-07-27 audit: ~15-20 template sites. Each is a
one-liner change. Roughly:

- `base.html`: wordmark link (line ~213, cited via grep 2026-07-27)
  — stays `cream-text`.
- `home.html`: any H1 stays `cream-text`; article-card topics on
  dark surfaces (if any exist post-JTBD-restore — most cards are
  pastel `text-ink` now per `session_plan_restore_jtbd_card_colors.md`
  STATUS line) → `cream-title`. Long-prose paragraphs (rare on
  home) → `cream-body`. Walk-through required — grep the file
  before editing.
- `crosscut.html` / `crosscut_detail.html`: episode title H1 stays
  `cream-text`; intro/outro body → `cream-body`. Walk-through
  required.
- `article.html`: article title H1 stays `cream-text`; transcript
  body (if on dark surface) → `cream-body`. NOTE: per
  `session_plan_restore_jtbd_card_colors.md` STATUS line
  2026-07-27, article-detail pages are wrapped in a single
  JTBD-colored card with `text-ink` — meaning most article-detail
  content is NOT cream. Verify current class usage before
  changing anything.
- `_candidates_fragment.html`: covered above.
- `create.html`: prompt echo `<h1>` stays `cream-text`; explainer
  paragraph → `cream-body`. Walk-through required.
- **Nav drawer** (`base.html:290-297`, verified via grep
  2026-07-27): the `<nav>` wrapper carries `text-cream-light`
  and individual link `<a>` tags inherit. Link labels stay at
  cream-light — no change needed. Hover state goes to
  `cream-text` (100%) which is intentional punch on hover,
  leave as-is. The "Menu" header at line 282 uses
  `text-cream-text` on a small `.editorial` label — small
  enough that 100% stays fine, leave as-is (or optionally drop
  to `cream-title` for consistency; Claude Code judges at
  implementation time).
- **Mini-player track title** (`base.html:539`, verified via
  grep 2026-07-27): currently `text-sm text-cream-text
  font-medium truncate`. Change to `text-cream-body`.
- **Footer** (`base.html:347`, verified via grep 2026-07-27):
  already uses `text-sm text-cream-muted`. No change needed.

The goal is: display type stays punchy, reading text calms down.
Any element whose role is "small metadata" or "muted secondary"
already uses `cream-light` / `cream-muted` and doesn't need
touching.

---

## Non-goals

- **No changes to red accent, palette, or JTBD colors.** Only the
  cream tokens are touched. Pastel JTBD cards are unaffected
  because their text is `text-ink` dark, not cream.
- **No changes to typography** beyond dropping Anton uppercase
  from the /create card's description block and subheading.
  Wordmark, page H1s, card topics all stay Anton uppercase.
- **No changes to the `.hook` class itself.** Its intended use
  (short single-sentence hooks on article cards) still works
  correctly. Only removing its ONE misapplication on
  `_candidates_fragment.html:43`.
- **No changes to legacy static pre-rendered editions** in
  `aarva/output/web/*.html`. Those are historical bake-outs.
- **No changes to email templates or RSS feed.** Same reason.

---

## Files that change

- `aarva/server/templates/base.html` — add `cream-title`,
  `cream-body` tokens in Tailwind config. Update mini-player
  track-title class from `cream-text` to `cream-body`.
- `aarva/server/templates/_candidates_fragment.html` — three
  changes (H2 class, subheading em+class, description class).
- `aarva/server/templates/home.html` — walk-through, targeted
  swaps only where cream-body/cream-title fit.
- `aarva/server/templates/article.html` — same walk-through.
- `aarva/server/templates/crosscut.html` (or equivalent detail
  template) — same walk-through.
- `aarva/server/templates/create.html` — same walk-through.
- `aarva/server/templates/category_detail.html`,
  `publication_detail.html`, `listener_created.html`,
  `editions.html` — same walk-through.
- `docs/roadmap.md` — move from In-Progress to Recently Completed
  after PR merges (Claude Code owns this per rule 17).

---

## Verification

1. `/create?q=how belief forms` — render the same query the user
   tested. Confirm the card's H2 topic reads at 85% cream, the
   italic article titles read at 75%, the description reads at
   75% with red left-border and sentence-case Inter prose, and
   nothing renders as Anton uppercase blob.
2. `/today` — confirm the "AARVA" wordmark + any Anton page H1
   still render at full 100% cream. Confirm pastel JTBD cards
   are unaffected (still dark-ink text on pastel).
3. `/article/:id` — page hero should either stay full-cream (if
   the article-detail is a dark surface) OR use dark ink on
   pastel (post-JTBD-restore). Either way, confirm no jarring
   brightness contrast between the hero and body.
4. Mini-player at the bottom of the viewport — track title
   should render at 75% cream, not 100%. Play button + progress
   fill unchanged (still red).
5. Nav drawer + hamburger menu — links unchanged (still
   `cream-light`).
6. Headless-browser screenshots at 375px iPhone viewport for
   each of the above pages, attached to the PR.

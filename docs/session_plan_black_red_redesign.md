# Session plan — black + red redesign (Variant A refined)

**STATUS: DONE (2026-07-25).** Implemented as specced, with two
noted deviations (no per-episode doodles on `/crosscuts`, no filter
chips — see `docs/roadmap.md`'s 2026-07-25 "Recently completed"
entry for why). See that entry for the full verification summary.

---

Written by Cowork for the next Claude Code session (2026-07-22+).
Site-wide visual redesign of aarva.app: swap the warm-cream palette
for near-black + warm off-white + pure red, replace the Fraunces+Inter
type pair with Anton (uppercase display) + Inter (body), add per-page
AI-generated hero doodles.

Read this doc + `docs/roadmap.md` + `AGENTS.md` before starting.

**AGENTS.md rule 4 sign-off**: this is a listener-facing redesign.
User approved the direction via mockups in Cowork on 2026-07-22.
The approved mockup is the reference — see the "Reference mockup"
section below for exact colors, fonts, and structural patterns.
DO NOT deviate from the mockup without checking back.

---

## Context

Aarva's current palette (warm-brown "night" background, cream text,
JTBD-differentiated pastel cards: peach, sky, lemon, mint, blush,
lavender) is being replaced with a much bolder black + warm off-white
+ pure red aesthetic inspired by lamalama.com. This is a full palette
+ typography + layout-pattern swap — not a full ground-up UX redesign
(nav structure, information architecture, routes all stay the same).

Approved variant: **Variant A refined with warm off-white text**.
Anton condensed uppercase display, Inter for body, warm off-white
`#F0E5D0` on near-black `#0A0A0A`, pure red `#FF2A2A` as the sole
accent color used sparingly (Create button, eyebrow labels, hook
pull-quote border, play button, progress fill, doodle strokes).

---

## Reference mockup — the visual source of truth

The approved mockup (rendered in Cowork 2026-07-22) shows four
pages:

1. **/today (landing)** — masthead "AARVA" (Anton uppercase),
   Create bar with red Create button, red eyebrow "Daily edition"
   above a huge Anton headline ("SYSTEMS IN CONTROL"), hero doodle
   card, crosscut card with red-tinted border, then list-row
   articles with red-dot play buttons.
2. **/crosscut/:id (detail)** — back arrow, red "Crosscut · 32 min"
   eyebrow, large Anton title, hero doodle, player, editorial intro
   as body copy, then two source-article list rows.
3. **/article/:id (detail)** — back arrow, red publication+JTBD
   eyebrow, Anton title, byline in muted cream, hero doodle,
   **hook rendered as a red-left-bordered pull quote in Anton
   uppercase** (this is new — replaces the current Fraunces italic
   hook), transcript body in Inter, player.
4. **/crosscuts (list)** — red "The archive" eyebrow, Anton
   heading, filter chips (red outlined pill for active, muted
   outlined pill for inactive), then a stack of episode cards each
   with its own doodle + eyebrow date + Anton title.

Do NOT invent new patterns beyond what the mockups show. If a page
template exists that isn't in the mockups (e.g. `/categories`,
`/publications`, `/listener-created`, `/editions`), apply the same
patterns: header + back arrow + red eyebrow + Anton headline + hero
doodle + content grid or list. Match the mockup's spacing rhythm.

---

## Locked decisions

### Colors

Replace the current Tailwind color config in `base.html` line 32-64
with:

```js
colors: {
  'night':        '#0A0A0A',   // page background — near black
  'night-soft':   '#141414',   // cards, elevated surfaces
  'night-border': 'rgba(240, 229, 208, 0.15)',  // subtle dividers
  'cream-text':   '#F0E5D0',   // primary text — warm off-white
  'cream-light':  'rgba(240, 229, 208, 0.65)',  // secondary text
  'cream-muted':  'rgba(240, 229, 208, 0.45)',  // metadata, tertiary
  'red-accent':   '#FF2A2A',   // sole accent color
  'red-accent-fade': 'rgba(255, 42, 42, 0.15)',  // borders/tints
  // JTBD pastels REMOVED entirely — no bg-peach, bg-sky, etc.
  // 'ink' / 'ink-light' / 'paper' can also be removed
  //   (they were text colors on the old pastel cards).
},
```

Since the class NAMES (`night`, `cream-text`) stay the same, template
files that use `bg-night` / `text-cream-text` need no changes. The
JTBD-pastel references (`bg-peach`, `text-ink`, etc.) DO need to
change — replace with `bg-night-soft text-cream-text` uniformly.

### Fonts

Replace the Fraunces+Inter Google Fonts import with:

```html
<link href="https://fonts.googleapis.com/css2?family=Anton&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
```

Keep the `--font-sans` / `--font-serif` naming in the Tailwind
config but repoint:

```js
fontFamily: {
  'sans':      ['Inter', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'sans-serif'],
  'display':   ['Anton', 'Impact', 'Oswald', 'sans-serif'],
  // 'serif' removed — Fraunces no longer loaded.
},
```

Update the `.editorial` utility (currently uses Fraunces) to use
Anton + uppercase:

```css
.editorial {
  font-family: 'Anton', Impact, Oswald, sans-serif;
  text-transform: uppercase;
  letter-spacing: 0.01em;
  font-weight: 400;   /* Anton is a single-weight display font */
}
```

Remove the `.hook` utility's italic Fraunces (see "Hook pattern"
below — it becomes a red-bordered pull-quote instead).

### Typographic scale

| Role                   | Class / usage                | Size / spec |
|------------------------|------------------------------|-------------|
| Wordmark ("AARVA")     | header masthead              | Anton, 24-28px, letter-spacing 0.02em |
| Page headline (H1)     | `.editorial` + `text-5xl`    | Anton, 44-56px, line-height 0.95, letter-spacing 0.005em |
| Section headline (H2)  | `.editorial` + `text-2xl`    | Anton, 32-40px |
| Card headline (H3)     | `.editorial` + `text-xl`     | Anton, 20-24px |
| Body text              | `font-sans`                  | Inter 400, 14-16px, line-height 1.55-1.65 |
| Eyebrow labels         | `.eyebrow` (new utility)     | Inter 600, 10-11px, uppercase, letter-spacing 0.24-0.28em, color `text-red-accent` |
| Byline / metadata      | `font-sans` + `text-cream-light`  | Inter 400, 12-13px |
| Hook pull-quote        | `.hook` (revised)            | Anton 22px, uppercase, red-2px-left-border, padded left 14px |

### The new `.eyebrow` utility

Small tracked-out uppercase label in red — used everywhere content
needs framing ("Daily edition", "Crosscut", "The archive", publication
names, dates). Reused across every template. Suggested:

```css
.eyebrow {
  font-size: 10px;
  letter-spacing: 0.28em;
  text-transform: uppercase;
  color: #FF2A2A;
  font-weight: 600;
  margin-bottom: 8px;
}
```

### Hook pattern (major change)

Currently `_candidates_fragment.html`, `article.html`, and possibly
others render the article's hook as italic Fraunces (via `.hook`).
In the new design, hooks become:

```html
<p class="hook">The greenest thing a city can do is refuse to pour another slab.</p>
```

```css
.hook {
  font-family: 'Anton', sans-serif;
  font-size: 22px;
  line-height: 1.15;
  text-transform: uppercase;
  color: #F0E5D0;
  letter-spacing: 0.005em;
  border-left: 2px solid #FF2A2A;
  padding-left: 14px;
  margin: 0 0 18px;
}
```

**Watch out**: some hooks are long (60-100 chars). Anton at 22px
uppercase can push line count. Confirm on real content that hooks
don't wrap to 4+ lines on iPhone 375px width. If they do, drop the
size to 18-20px OR keep line count sanity by testing against the
5 longest hooks in the DB before landing.

### JTBD colors — removed

The current design uses per-JTBD card colors (`bg-peach` for
crosscut, `bg-lemon` for curiosity, `bg-mint` for smart-escape, etc.).
In the new design, ALL cards are the same near-black surface with
the same subtle border. JTBD is signaled by TEXT alone (the eyebrow
label). `card_color_for_jtbd` and related helpers in
`aarva/server/jtbd_meta.py` become no-ops or get retired.

Concrete change: every `bg-peach text-ink` (or any JTBD pastel)
becomes `bg-night-soft text-cream-text border border-night-border`.
The featured/crosscut card gets an accent variant:
`bg-night-soft border border-red-accent-fade`.

Category `/category/<slug>` pages, which currently color-theme
each page after its JTBD, lose the color theme entirely. Category
identity comes through the eyebrow label + hero doodle.

### Player styling

- Play button (`data-play-button` circular) — background `red-accent`,
  icon in near-black. Both in the mini-player and in-page cards.
- Progress track — cream `rgba(240,229,208,0.15)` background.
- Progress fill — `red-accent`.
- Time labels — `cream-muted`.

The mini-player at the bottom of the viewport keeps its `position:
fixed` + GPU-layer promotion (from the iOS player-bugs PR); only
its color palette changes.

### Hero doodles (per fixed nav page)

Nine SVG doodles, one per fixed nav page + a shared "detail" fallback.
Stored in `aarva/server/static/doodles/`:

| File                       | Used on                       | Theme sketch |
|----------------------------|-------------------------------|--------------|
| `today.svg`                | `/today` (landing)            | Two figures with arrow between them — the daily pairing |
| `crosscuts.svg`            | `/crosscuts` list             | Two overlapping X-shapes crossing in the middle |
| `crosscut_detail.svg`      | `/crosscut/:id` (all)         | Two figures back-to-back with a bridge between |
| `article_detail.svg`       | `/article/:id` (all)          | Open book with a wavy line coming out |
| `categories.svg`           | `/categories`                 | Grid of small distinct doodle marks |
| `category_detail.svg`      | `/category/:slug` (all)       | Reuse `categories.svg` — one per page is enough |
| `publications.svg`         | `/publications`               | Stack of overlapping rectangles like paper piles |
| `publication_detail.svg`   | `/publication/:slug` (all)    | Single newspaper sheet with a red banner |
| `listener_created.svg`     | `/listener-created`           | A speech bubble intersecting a lightbulb |
| `editions.svg`             | `/editions`                   | Calendar with days marked |
| `create.svg`               | `/create` (loading state?)    | Optional — could fit above the "Looking for the right pairings" card |

All doodles: **single-weight red line drawings**, roughly 200×112
viewBox for 16:9 aspect, stroke `#FF2A2A`, stroke-width `1.5-1.6`,
`stroke-linecap="round"`, `stroke-linejoin="round"`, no fills, no
gradients. See the mockups for exact style — cars/figures for
"systems in control", cityscape for "concrete cities", geometric
gestures for archive tiles.

**Generation approach**: prompt Gemini (Aarva's standing rule —
Claude LLM is coding-only; all other LLM work goes through Gemini)
to produce SVG code directly for each theme. **Text-to-SVG, not
text-to-image + vectorization** — the ask is "return the SVG source
for a single-weight red line drawing of X in a 200×112 viewBox."
Iterate on 2-3 drafts per doodle until it feels right. SVGs need to
stay small (< 2KB each), scale cleanly, and be editable if we want
to tweak later. The mockup's existing sketches are a good starting
reference for style.

**Prompt template** (adapt per doodle):
```
Produce SVG source only (no explanation, no markdown fence) for a
single-weight line drawing on transparent background.

- viewBox: 0 0 200 112
- stroke: #FF2A2A, stroke-width 1.5, stroke-linecap round,
  stroke-linejoin round
- no fills except where a shape is deliberately filled in the
  same red (rare)
- style: gestural / sketchy, not geometric-clean. Confident single
  strokes, no cross-hatching.

Subject: {THEME_DESCRIPTION}

Return ONLY the <svg>...</svg> element.
```

Reuse whichever Gemini client Aarva already uses for other LLM
work (`aarva/clients/llm.py` or similar). Save each returned SVG
to `aarva/server/static/doodles/<slug>.svg`. Manually inspect each
before committing — the LLM occasionally emits invalid SVG or
overshoots the viewBox; reject and re-prompt when that happens.
No runtime generation — doodles are curated static assets.

Store the SVG source as-is (not compiled to PNG). Serve inline via
`{% include "doodles/today.svg" %}` OR as a static file. Recommend
static file (cacheable, browser can prefetch).

### Layout patterns

Beyond palette + font, the mockup introduces a few structural
patterns that get applied site-wide:

1. **Hero-per-page**: every listener-facing page (except detail
   pages already covered by a shared doodle) opens with a hero
   doodle card between the eyebrow/H1 and the page's main content.
2. **Eyebrow rhythm**: red uppercase eyebrow always precedes an
   H1 or a card's H3. Never appear without a heading below them.
3. **List-row articles instead of pastel cards**: on `/today`, past
   editions, publication pages, category pages — articles render
   as list rows with a `border-bottom: 1px solid` cream-15% divider,
   NOT as filled cards. The "cards" in the current design were
   pastel-coloured; the new design uses divider lines to separate
   articles in a list.
4. **Featured content gets the card treatment**: the today's
   crosscut on `/today` and any "hero" item on other pages gets a
   proper `bg-night-soft border border-red-accent-fade rounded-2xl
   p-4` card. Everything else is a list row.
5. **Filter chips** (for `/crosscuts` filters, `/publications`
   filters, `/categories` filters if any): red-outlined pill for
   active state (`border border-red-accent text-red-accent`), muted
   outlined pill for inactive (`border border-cream-muted text-cream-muted`).

---

## Implementation staging

Ship as **one big PR** (all-at-once). Reason: the changes are
interdependent. Removing JTBD colors makes no sense without the new
palette. The hook pattern needs Anton to work. Partial states will
look broken.

Order-of-work inside the PR:

1. **Foundational** — `aarva/server/templates/base.html`:
   - Update the Tailwind `theme.extend.colors` block with the new
     palette.
   - Swap Google Fonts import (Anton + Inter, drop Fraunces).
   - Update `<style>` block: `.editorial` → Anton uppercase; new
     `.eyebrow` utility; revised `.hook`; player color updates.
   - Update `<meta name="theme-color">` to `#0A0A0A`.
   - Header masthead — Anton uppercase wordmark ("AARVA").
   - Update the Create button and Create bar colors to red.
2. **Doodles** — generate + commit ~10 SVGs into
   `aarva/server/static/doodles/`. Reference the "Hero doodles"
   table above for exact themes.
3. **Templates** — go template-by-template updating each to the
   new patterns. In this order:
   - `home.html` (landing) — biggest surface, most patterns visible
   - `crosscut.html` (detail)
   - `article.html` (detail)
   - `crosscuts_list.html`
   - `categories.html` + `category.html` (detail)
   - `publications.html` + `publication.html` (detail)
   - `listener_created.html`
   - `editions.html`
   - `create.html`, `_candidates_fragment.html`
   - Any modals, drawer, PWA modal, etc.
4. **Cleanup** — remove now-unused Tailwind color tokens (`peach`,
   `lemon`, etc.), the Fraunces link, `.hook` italic, and
   `jtbd_meta.card_color_for_jtbd` if not used elsewhere.
5. **PWA icon + apple-touch-icon + podcast cover** — regenerate
   the full icon set to match the new palette. Same PR (user
   confirmed 2026-07-22). See "PWA icon regeneration" section
   below for spec.

### PWA icon regeneration

Regenerate all icon assets to the new palette. Same PR.

**Design:**
- Background: `#0A0A0A` (matches page bg).
- Wordmark: "AARVA" in Anton uppercase, warm off-white `#F0E5D0`,
  centred vertically and horizontally, letter-spacing 0.02em (same
  as the header masthead). Weight appropriate to the icon size —
  the letters should feel confident and readable at 60×60px on a
  home-screen grid.
- Single red accent: a small red dot (`#FF2A2A`, ~6-8% of icon
  width) after the final "A" — e.g. "AARVA●" — as the one red
  moment. Do NOT add red anywhere else. The dot is the same
  visual language as the eyebrow labels and doodle strokes on the
  main site — one accent, used with intent.
- Safe zone: keep the wordmark within a ~12% margin from each
  edge so iOS' rounded-corner mask and Android's adaptive-icon
  crop don't clip the letters.

**Assets to regenerate** (all in `aarva/server/static/icons/`):
- `apple-touch-icon.png` — 180×180. Also used by the Media
  Session API on the iPhone lock screen (see
  `session_plan_ios_player_bugs.md`) — updated palette here fixes
  that surface at the same time.
- `icon-192.png` — 192×192 (PWA manifest).
- `icon-512.png` — 512×512 (PWA manifest + Media Session
  high-res artwork).
- `icon-maskable-512.png` — 512×512, with generous safe zone
  for Android's adaptive-icon mask (Android Chromium PWA).
- `favicon.ico` — 32×32 + 16×16 (multi-size ICO).
- `favicon-32.png`, `favicon-16.png` — modern browser variants.

**Podcast cover** (`aarva/output/cover.png`, 3000×3000):
- Regenerated via `scripts/generate_logo.py`. Update the script
  to use the new palette (same "AARVA●" wordmark, same colors,
  scaled up). This is what Apple Podcasts / Spotify / RSS
  ingesters display. Not user-visible on the web app but visible
  wherever the podcast is subscribed to.

**Generation approach:**
- `scripts/generate_logo.py` today produces cover.png. Extend or
  duplicate it to output all PWA sizes from one master. Anton
  should be installable from the same Google Fonts URL used
  in-app, or embedded locally in the script.
- Alternatively: render one large master SVG with the wordmark +
  red dot, then export each size via CairoSVG or Pillow. Cleaner
  than raster-per-size.

**Manifest & meta updates** (`aarva/server/static/manifest.json`
+ `base.html`):
- `manifest.json` — `theme_color: '#0A0A0A'`, `background_color:
  '#0A0A0A'`. Update icon entries to reference the new files at
  the new sizes.
- `base.html` line 141 — `<meta name="theme-color"
  content="#0A0A0A">` (already covered in step 1 of the ordered
  work above).
- `apple-mobile-web-app-status-bar-style` stays
  `black-translucent` — still correct against the new palette.

**Verification for icons:**
1. Install the PWA on iPhone via "Add to Home Screen". Confirm
   the home-screen icon shows the new black + red wordmark.
2. Play a track. Lock the phone. Confirm the lock-screen Now
   Playing artwork uses the new icon.
3. On Android Chrome, install PWA. Confirm the adaptive-icon
   crop doesn't clip the "AARVA" text.
4. Load aarva.app in a fresh browser tab. Favicon in the tab bar
   is the new icon.
5. Subscribe to the RSS feed from Apple Podcasts / Overcast.
   Confirm the podcast-cover art reflects the new design.

### Files that change

Big list. Cluster them for the PR description:

- `aarva/server/templates/base.html` (colors, fonts, styles, header)
- `aarva/server/templates/home.html`
- `aarva/server/templates/crosscut.html`
- `aarva/server/templates/article.html`
- `aarva/server/templates/crosscuts_list.html`
- `aarva/server/templates/categories.html`
- `aarva/server/templates/category.html`
- `aarva/server/templates/publications.html`
- `aarva/server/templates/publication.html`
- `aarva/server/templates/listener_created.html`
- `aarva/server/templates/editions.html`
- `aarva/server/templates/create.html`
- `aarva/server/templates/_candidates_fragment.html`
- `aarva/server/static/doodles/*.svg` (new — ~10 files)
- `aarva/server/static/icons/*` (regenerated — apple-touch-icon,
  icon-192, icon-512, icon-maskable-512, favicon variants)
- `aarva/server/static/manifest.json` (theme_color +
  background_color + icon entries)
- `aarva/output/cover.png` (regenerated podcast cover, 3000×3000)
- `scripts/generate_logo.py` (updated to new palette; may need
  extension to emit all PWA sizes from one master)
- `aarva/server/jtbd_meta.py` (retire `card_color_for_jtbd`)

---

## Verification (mockup gate applies)

Per AGENTS.md rule 4 + the mockup-gate discipline established for
iPhone changes: before landing, render EVERY listener-facing page
in a headless browser at iPhone 375px viewport width. Screenshot
each and paste into the PR description. Compare against the
approved mockup — colors, fonts, spacing, eyebrow placement.

Concretely, verify each page renders correctly:

1. `/` → `/today` — headline, hero doodle, crosscut card, list rows.
2. `/crosscut/:id` — pick an existing crosscut. Confirm hero doodle,
   Anton title, editorial intro, source-article list rows.
3. `/article/:id` — pick a piece with a hook. Confirm the hook
   renders as a red-bordered pull-quote in Anton uppercase, and the
   transcript body reads well in Inter.
4. `/crosscuts` — filter chips visible, episode cards each with
   their own doodle.
5. `/categories` — grid.
6. `/category/:slug` — category eyebrow + article list.
7. `/publications` — publication list.
8. `/publication/:slug` — pub eyebrow + article list.
9. `/listener-created` — listener-created episode list.
10. `/editions` — past-editions list.
11. `/create?q=test` — loading state uses new palette; the
    catalog-count copy still reads correctly.

Also check:

- **Mini-player** — red play button, red progress fill, sits at the
  bottom, still respects `position: fixed` + GPU-layer promotion.
- **iOS lock screen** — audio still plays; the Media Session API
  wiring from the earlier PR still works.
- **Dark mode / light mode toggle** — Aarva doesn't have a light
  mode; the new design assumes dark only. Confirm no leftover
  light-mode-only styles break.

Ship a PR with:
- Screenshots for every listed page.
- A note about any deviation from the approved mockup, with
  reasoning.

---

## Non-goals

- **No changes to information architecture** — nav routes, page
  structure, URL scheme all stay the same.
- **No changes to Aarva's editorial voice / narration** — the
  redesign is visual only.
- **No per-episode or per-article doodles** in v1. Only per-page
  fixed doodles. Per-episode generation is a future project if
  the design lands well.
- **No new PWA icon design** in this PR. If the current icon looks
  wrong against black+red, flag it and open a separate follow-up.
- **No email template redesign** — Resend transactional emails
  keep their current shape. Redesign there is a separate project.
- **No RSS feed / Apple Podcasts cover art redesign** — same reason.
- **No light-mode support** — the design is dark-only by nature.

---

## Backfill / migration

None required. This is purely template + static-asset work. No
schema changes, no data changes, no DB migration.

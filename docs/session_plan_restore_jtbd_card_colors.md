# Session plan — restore per-JTBD card colors on the black + red design

Written by Cowork for the next Claude Code session (2026-07-26+).
Follow-up to `session_plan_black_red_redesign.md` (shipped
2026-07-25). That PR removed the per-JTBD card colors — user
looked at the result and found the /today page felt flat without
per-category visual distinction. Restore the pastel card fills
against the near-black page background, keeping the rest of the
black+red aesthetic intact.

Read this doc + `docs/session_plan_black_red_redesign.md` +
`docs/roadmap.md` + `AGENTS.md` before starting.

**AGENTS.md rule 4 sign-off**: listener-facing visual change. User
approved direction 2026-07-26 — full pastel card fills, original
palette, applied to JTBD-tagged article cards. No mockup gate
this time because the palette IS the original Aarva palette (just
reintroduced against a new page bg) — user already knows how it
reads.

---

## Context

The 2026-07-25 redesign traded per-JTBD pastel card fills for
uniform `bg-night-soft` dark cards. The trade was intentional at
the time (single accent, cleaner surface) but landed as too
uniform in practice — JTBD is a core organising principle of the
daily edition (behind-the-news / curiosity / smart-escape /
delight / future-gazing / crosscut), and eyebrow labels alone
didn't carry that distinction visually.

Original palette (still in the `-dark` shade tokens; only the
`bg-*` fill tokens were removed):
- `peach` `#E6C8B0` — crosscut + CTA
- `sky` `#BFCEDB` — future-gazing / behind
- `lemon` `#DDCF85` — curiosity
- `mint` `#BCD0BF` — smart-escape
- `blush` `#DFB7BE` — delight
- `lavender` `#C8C2D8` — behind-the-news
- `paper` `#E8DFCE` — neutral fallback

Same JTBD → color mapping as pre-redesign. `aarva/server/jtbd_meta.py`
had `card_color_for_jtbd` — restore this if it was retired by the
redesign PR.

---

## Locked decisions (with user, 2026-07-26)

1. **Full tinted card fills.** Bring back the original pastel
   palette as full backgrounds on JTBD-tagged article cards.
2. **Text on pastel goes dark ink.** Anton headings on pastel
   cards render in `text-ink` (dark warm brown), not
   `text-cream-text` (warm off-white). Anton is color-flexible,
   font stays Anton.
3. **Category pages get their theme color back.** Currently
   `/category/<slug>` pages are undifferentiated post-redesign —
   restore per-JTBD theming.
4. **Dark cards stay for non-JTBD surfaces.** Nav drawer, hero
   doodle backgrounds, article-detail transcript area, "Also
   today" listener-created section, mini-player, footer — all
   stay `bg-night-soft` + `text-cream-text` as landed by the
   redesign.
5. **Featured/crosscut card treatment.** The editorial crosscut
   card on `/today` keeps its peach fill (matching the historic
   design). Add a subtle red-tinted border
   (`border border-red-accent-fade` or `2px border-red-accent`)
   so the "featured" signal reads without introducing a second
   dominant color.

---

## Palette additions

Update the Tailwind color config in
`aarva/server/templates/base.html` — restore the pastel + ink
tokens that were removed in the 2026-07-25 redesign PR:

```js
colors: {
  // Existing black + red palette (from 2026-07-25 redesign) — KEEP
  'night':        '#0A0A0A',
  'night-soft':   '#141414',
  'night-border': 'rgba(240, 229, 208, 0.15)',
  'cream-text':   '#F0E5D0',
  'cream-light':  'rgba(240, 229, 208, 0.65)',
  'cream-muted':  'rgba(240, 229, 208, 0.45)',
  'red-accent':   '#FF2A2A',
  'red-accent-fade': 'rgba(255, 42, 42, 0.15)',

  // Restored — pastel JTBD card fills
  'peach':        '#E6C8B0',
  'peach-dark':   '#9C5733',
  'sky':          '#BFCEDB',
  'sky-dark':     '#2C5878',
  'lemon':        '#DDCF85',
  'lemon-dark':   '#6E5E15',
  'mint':         '#BCD0BF',
  'mint-dark':    '#3F5F44',
  'blush':        '#DFB7BE',
  'blush-dark':   '#8C3D4F',
  'lavender':     '#C8C2D8',
  'lavender-dark':'#4F4279',
  'paper':        '#E8DFCE',

  // Restored — dark ink text for text-on-pastel
  'ink':          '#2D2418',
  'ink-light':    '#6E5F4B',
},
```

Verify against the historical config: the original palette had
`-dark` variants for inline text on pastel (e.g. eyebrow labels
on the pastel card in the pre-redesign world). Those `-dark`
tokens may or may not have been kept by the redesign PR — read
the current `base.html` to see and add anything missing.

---

## Template restorations

The redesign PR replaced `bg-peach text-ink` (and equivalents for
other JTBDs) with `bg-night-soft text-cream-text` uniformly. Now
we walk that back — **but only for JTBD-tagged article cards**,
not for every card.

### `aarva/server/templates/home.html`

The `grouped_pieces` loop (was line ~122 in the pre-redesign
template) rendered each JTBD group with `bg-{{ group.card_color }}`
and `text-ink`. Restore:

```jinja
{% for group in grouped_pieces %}
  <section class="mb-12">
    <h2 class="editorial text-xs uppercase tracking-widest text-{{ group.header_color }} mb-5 font-medium">
      {% if group.slug %}
        <a href="/category/{{ group.slug }}" class="hover:opacity-70 transition-opacity">
          {{ group.label }} →
        </a>
      {% else %}
        {{ group.label }}
      {% endif %}
    </h2>
    <div class="space-y-5">
      {% for piece in group.pieces %}
        <article class="bg-{{ group.card_color }} p-6 rounded-2xl text-ink">
          {# rest of the card — publication line, title, byline, hook, player #}
        </article>
      {% endfor %}
    </div>
  </section>
{% endfor %}
```

Anton headings inside these cards use `text-ink` (dark brown).
Byline / metadata use `text-ink-light`. Publication eyebrow uses
`text-{{ group.header_color }}` (e.g. `text-peach-dark`,
`text-sky-dark`) — the per-JTBD-`-dark` variant.

The play button on pastel cards renders as `bg-night text-{{ group.card_color }}`
— dark button, pastel-tinted icon. This was the pre-redesign
treatment and it looks correct against the pastel card.

The `card_color` and `header_color` values come from
`aarva/server/jtbd_meta.py`'s `card_color_for_jtbd` /
`header_color_for_jtbd`. Restore that mapping if the redesign
PR retired it.

### Bonus pieces on `home.html`

The article-shaped `bonus_pieces` section (line ~77 in the
pre-redesign template) also had per-JTBD pastel fills — restore
in the same way.

### `/category/<slug>` pages

Restore the per-JTBD theming — the page's header, body eyebrow
labels, and article cards should all reflect the category's
JTBD color. Follow the pre-redesign template's pattern for
`category.html`.

### Article detail page

The article-detail hero card (currently `bg-night-soft`) gets the
JTBD's pastel fill — matching what a listener saw on the /today
card that led them here. The transcript body BELOW the hero stays
on page bg (near-black + `text-cream-text`) — that's fine.

Verify: does the article-detail page look coherent with pastel
hero + dark body? Or does it feel visually split? If split,
consider extending the pastel to a full card wrapper. Test on
one long article before deciding.

### Crosscut card — featured accent

`home.html`'s crosscut card (line ~11-61 in the current
template) keeps its peach fill. Add a red-tinted border to
signal "featured":

```jinja
<section class="mb-12 p-6 bg-peach border border-red-accent-fade rounded-2xl text-ink">
  ...
```

The listener-created "Also today" section (from
`session_plan_promote_listener_created_as_bonus.md` — shipping
separately) mirrors the same pattern: peach fill + red-tinted
border. Consistency.

---

## Contrast sanity check

The redesign's `.eyebrow` utility currently uses
`color: #FF2A2A` — red. On the new pastel card fills, verify
that red uppercase eyebrow reads WCAG-AA against each pastel:

- red on peach `#E6C8B0` — LIKELY fine but check
- red on sky `#BFCEDB` — CHECK carefully; sky is blue-toned
- red on lemon `#DDCF85` — LIKELY fine
- red on mint `#BCD0BF` — CHECK; mint is green-toned
- red on blush `#DFB7BE` — CHECK; blush is pink-toned, could clash
- red on lavender `#C8C2D8` — CHECK
- red on paper `#E8DFCE` — LIKELY fine

For any pastel where red-on-pastel fails contrast OR clashes
visually, fall back to the per-pastel `-dark` variant for
on-card eyebrows (e.g. `text-peach-dark` on peach cards). The
pre-redesign design used exactly this pattern — each pastel
had a matching `-dark` for inline text.

Codify as CSS utilities or per-JTBD template variants — pick
whichever is less invasive.

---

## What stays dark

Everything in this list keeps the redesign's black+red treatment:

- Page background (`bg-night`)
- Header masthead + nav
- Hero doodle backgrounds (each doodle is red-on-dark, matches
  page bg)
- Article detail page transcript body (long-form reading area)
- Article detail page hook pull-quote (red-bordered Anton on
  near-black)
- "Also today" listener-created crosscut section (peach card
  per the promote-bonus spec)
- Mini-player bar (bottom of viewport)
- Footer
- Nav drawer (side sheet)
- PWA modals
- Filter chips on `/crosscuts` (red-outlined pills)

---

## Non-goals

- **No changes to fonts, headings, or the red accent color.**
  This is purely the pastel restoration.
- **No changes to hero doodles.** They stay red-line-on-black.
- **No changes to the icon set / PWA icon.** Already regenerated
  in the redesign PR with the new "AARVA●" mark.
- **No changes to Fraunces reintroduction.** Anton stays as the
  display font.
- **No listener-visible palette customisation.** Palette is
  editorial; not a user preference.

---

## Files that change

- `aarva/server/templates/base.html` — Tailwind color palette
  additions (pastel + ink tokens).
- `aarva/server/templates/home.html` — restore per-JTBD `bg-*
  text-ink` on grouped-pieces cards + bonus_pieces cards +
  crosscut card's red-tinted border.
- `aarva/server/templates/category.html` — restore per-JTBD
  color theme on category detail pages.
- `aarva/server/templates/article.html` — pastel hero card
  reflecting the article's JTBD.
- `aarva/server/templates/publication.html` — article cards
  in the publication listing reflect their JTBD colors
  (mirror pre-redesign behaviour).
- `aarva/server/jtbd_meta.py` — restore `card_color_for_jtbd`
  and `header_color_for_jtbd` if the redesign PR retired them.
- `docs/roadmap.md` — after PR merges, move from In-Progress to
  Recently Completed (Claude Code owns this per AGENTS.md rule
  17).

---

## Verification

1. `/today` — daily-edition cards each render in their JTBD
   pastel color. Anton headings dark, bylines dark. The editorial
   crosscut card has its red-tinted border.
2. `/category/<slug>` — each category page renders in its JTBD
   color. Article cards on the category page also reflect that
   color (or share a single card style; whichever the pre-
   redesign pattern was — check via git history).
3. `/article/<id>` — the hero card (if any) reflects the
   article's JTBD color. Transcript body remains on page bg.
4. `/publications` and `/publication/<slug>` — articles in the
   list reflect their JTBD colors.
5. Contrast check: on each of the 7 pastels, capture a
   screenshot with red-eyebrow visible. If any fail
   readability, swap to the per-pastel `-dark` variant. Note
   in the PR description which pastels needed the fallback.
6. Mobile viewport (375px) — pastel cards don't clash
   uncomfortably against near-black. Should read as bold color
   blocks rather than muddy.
7. Regression: confirm nothing that stayed dark (per "What
   stays dark" above) accidentally picked up a pastel.

# Session plan — search suggestions (dropdown on focus + no-results fallback)

Written by Cowork for the next Claude Code session (2026-07-15+).
Two related enhancements to the header prompt input ("create an
episode on anything"). Both landed here after the 2026-06-29
search-decision was made — see `docs/project_brief.md`'s decision
log row for the base UX.

Read this doc + `docs/roadmap.md` + `AGENTS.md` before starting.

---

## Context

The header prompt is a single input on every page with placeholder
"create an episode on anything." Submitting takes the listener to
`/create?q=…` which either renders existing crosscut matches
+ Gemini-proposed new pairings, or a "Nothing matched closely
enough" empty state with three example prompts.

Two gaps in the current experience:

1. **Listeners with a blank cursor don't know what they can ask
   for.** The single-line placeholder gives no signal about
   whether Aarva accepts topics, feelings, opinions, juxtapositions,
   etc. Some listeners hesitate to type anything at all.
2. **When no results come back**, the current fallback is a
   generic three-example line. It doesn't offer a semantic
   near-miss ("did you mean X?") or a categorized set of prompt
   archetypes ("here's the kind of thing you could ask").

This spec covers both.

---

## Feature A — Dropdown on focus (when the box is empty)

### DONE 2026-07-16

Shipped as specced. The 6 examples live in a new shared constant
(`aarva/services/prompt_suggestions.py`), registered as a Jinja
global so `base.html` (every page) and Feature B can both read it
without duplication.

Verified with a real headless-browser run (Playwright, installed
locally for this) against a live local server — every verification
bullet in this section passed directly, not just inferred from the
server-rendered HTML. See `docs/roadmap.md`'s 2026-07-16 "Recently
completed" entry for the full list of what was checked.

### Goal

When a listener clicks into the prompt input and it's empty, show
a small dropdown / panel underneath listing 4-6 example prompts
across different kinds of things they could ask. Clicking one
pre-fills the box (does NOT submit — user can edit before hitting
enter/Create). Typing dismisses the dropdown. Clicking outside
dismisses it.

### Decisions locked

1. **Trigger:** input focus AND input value is empty. If the input
   already has text (user came back to a partially-typed query),
   don't show the dropdown.
2. **Content:** a curated list of 4-6 example prompts, deliberately
   spanning the different KINDS of things Aarva can find:
   - **Topic-driven**: "new perspectives on the iran war"
   - **Feeling-driven**: "i'm feeling down — give me something to
     cheer me up"
   - **Juxtaposition-driven**: "jazz and AI"
   - **Question-driven**: "how belief forms"
   - **Opinion-driven**: "opposing views on carbon capture"
   - **Vibe-driven**: "quietly thoughtful nature writing"
   The list is short so listeners scan the pattern quickly and
   see they can ask in different registers. Six is a soft cap;
   four or five is fine.
3. **Interaction:**
   - Click an example → prompt input pre-fills with that string.
     Cursor at end. Dropdown closes. User can hit Enter (Create)
     or edit first.
   - Start typing → dropdown closes; listener is on their own path.
   - Click outside the input → dropdown closes.
   - Escape key → dropdown closes.
4. **Not personalised, not rotated per-session.** The same 4-6
   examples show every time. Rotation is a Nice-to-Have later.
5. **No LLM call.** Static list. Zero cost per focus event.
6. **Copy is exact and lowercase.** These prompts read the way a
   listener would type them. Aarva's editorial voice on the
   OUTPUT is polished; the INPUT should feel casual + accessible.

### The example list — locked

Six examples, one line each, in this order:

1. `new perspectives on the iran war`
2. `i'm feeling down — give me something to cheer me up`
3. `jazz and ai`
4. `how belief forms`
5. `opposing views on carbon capture`
6. `quietly thoughtful nature writing`

Deliberately mixes serious/current-affairs, mood/emotion,
juxtaposition, evergreen/philosophical, opinion-plural, and
aesthetic. Signals "you can ask across all these registers."

### Files likely to change

- `aarva/server/templates/base.html` (or wherever the header
  prompt input lives — likely a shared partial). Add a
  `<div id="prompt-suggestions" hidden>` right below the input.
- Small JS (inline in the same template or in a new
  `aarva/server/static/prompt-suggestions.js`) that:
  - Attaches `focus` and `input` handlers to the prompt input.
  - Shows/hides the suggestions div based on focus + empty
    conditions.
  - Handles click-outside-to-dismiss and Escape.
  - Populates the input on suggestion click and dismisses the
    dropdown.
- CSS: styling for the dropdown that matches the site's cream/
  dark-mode palette. Small, unobtrusive, doesn't obscure the
  prompt input.

### Verification

- Load any page. Click into the header prompt input. Dropdown
  appears with 6 examples.
- Click one → input pre-fills, dropdown disappears, cursor at end
  of input.
- Type any character → dropdown disappears.
- Click outside → dropdown disappears.
- Focus, then don't type, then Escape → dropdown disappears.
- Reload while input has stale text (browser autofill / partial
  query) → dropdown does NOT appear on focus.

---

## Feature B — No-results fallback: near-miss + retry

### Goal

Currently the empty state on `/create?q=…` when nothing matches
shows a static generic line. Two ways to improve it, and this
spec does BOTH (they're additive, not either/or):

1. **Suggest a semantic near-miss.** If any existing catalog
   entry (crosscut episode OR article) is even weakly related to
   the prompt, offer it as "did you mean" or "here's the closest
   thing we have on the shelf." Uses the same embedding-space
   search that powers `/create`'s existing-match candidate flow,
   just with the current similarity floor relaxed for this fallback.
2. **Show the same 6 example prompts as Feature A**, framed as
   "here are other things you could ask." Redundant with the
   focus dropdown but useful in-context — a listener who
   just got no results is more receptive to trying a different
   direction than one who hasn't typed yet.

### Decisions locked

1. **Trigger:** `/create?q=…` returns 0 candidates in the current
   flow (both existing-match and new-pairing tiers came back
   empty).
2. **Near-miss detection:** re-run the existing-match query with
   a lower similarity floor. Current is 0.65 (per
   `DEFAULT_EXISTING_MATCH_FLOOR` in
   `aarva/services/episode_candidates.py`). Fallback tries 0.45.
   Take the top 1-2 results (crosscut episodes and/or articles).
   If nothing at 0.45 either, skip the near-miss section
   entirely and just show the examples.
3. **Near-miss framing:** "The closest thing we have to what you
   asked is:" followed by the crosscut/article title as a link.
   Not "did you mean" — that implies typo, which isn't the case
   here.
4. **Examples framing:** below the near-miss (if any), a small
   section titled "Or try one of these:" listing the six examples
   from Feature A as clickable chips. Clicking submits directly
   (goes to `/create?q=…`) — no pre-fill-then-submit two-step.
5. **No LLM call.** Both the near-miss lookup and the examples
   are already-cached or static. Zero incremental cost.

### Files likely to change

- `aarva/services/episode_candidates.py` — extend `propose_candidates`
  to also return a `near_miss` field (list of 0-2 crosscut/article
  hits at similarity ≥ 0.45 when the main tier returned nothing).
  OR keep `propose_candidates` unchanged and add a sibling
  `find_near_miss(prompt, floor=0.45, k=2)` that the route calls
  when candidates is empty.
- `aarva/server/routes/create.py::api_candidates` — when
  `candidates` is empty, call the near-miss lookup and pass its
  result into the template context.
- `aarva/server/templates/_candidates_fragment.html` — extend the
  empty-state block to render (a) near-miss link if present,
  (b) the 6 examples as clickable chips.
- **DRY on the examples list.** Feature A and Feature B use the
  same 6 examples. Extract into a single source of truth — e.g.
  a Python constant in a new small module `aarva/services/
  prompt_suggestions.py` (or similar) — that both the JS-side
  (via a `<script>` variable injected by the template) and the
  server-side empty-state template read from. Don't hardcode the
  list in two places.

### Verification

- Submit a prompt that clearly won't match anything (e.g.
  "sabbatical soft-boiled egg gastronomy"). Empty state appears
  with:
  - Either a near-miss link OR no near-miss section (depending on
    whether anything at 0.45 was found).
  - The 6 example chips, each clickable.
- Clicking a chip navigates to `/create?q=<that prompt>` and
  produces real candidates.
- Submit a prompt with a real near-miss (e.g. "the new left in
  american politics" when we have an "populist realignment"
  crosscut). Near-miss link renders correctly.
- No client-side JS errors on submit or interaction.

---

## Sequencing

Independent PRs. Recommended order:

1. **Feature A first.** Contained to the header partial + a small
   JS block + one shared constant. Immediate visual improvement
   on every page. Low risk.
2. **Feature B second.** Uses the shared example list from
   Feature A, so extract-into-a-constant work is already done.
   Requires a small tweak in `episode_candidates` for the
   near-miss lookup.

Both are self-contained; if Claude Code prefers to bundle them
into one PR, that's fine — same file gets touched (the shared
constant) either way.

---

## Non-goals

- **Do NOT rotate the example prompts randomly per session.**
  Static list. Rotation is a follow-up if data suggests it helps.
- **Do NOT personalise the suggestions based on the listener's
  history.** No user-tracking beyond what already exists.
- **Do NOT auto-submit on suggestion-click in Feature A.** Pre-
  fill and let the user hit Enter. Preserves listener agency.
- **Do NOT do fuzzy typo correction on the prompt.** The near-miss
  is embedding-space semantic, not lexical. If the listener typed
  "iran war" and we have "Iran conflict" content, embedding
  similarity should catch it without any typo-correction step.
- **Do NOT add analytics on suggestion-click rates** in this
  session. Ship first; measure once there's real listener
  volume.
- **Do NOT change the 6-example list itself in this session.**
  If the list needs tuning, that's a separate follow-up after
  observing which ones get clicked.

---

## What Cowork owes if this spec has gaps

Same rule as previous session plans: if Claude Code finds a real
ambiguity — the header prompt input is embedded differently than
expected, the near-miss floor of 0.45 turns out to surface junk,
the DRY-the-list requirement conflicts with the JS/Python split
in weird ways — punt back to Cowork with the specific question.
Don't guess.

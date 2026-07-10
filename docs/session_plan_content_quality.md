# Session plan — content-quality + share + listener transparency

Written by Cowork for the next Claude Code session (2026-07-11+).
Covers the "content quality" batch of improvements the user listed
on 2026-07-11. Voice standard is locked and non-negotiable across
all items in Section 1; Sections 2-5 build on it. Section 6 (outro
music) is scoped but blocked on an external asset.

Read this doc + `docs/roadmap.md` + `AGENTS.md` before starting.

---

## Section 1 — Voice standard for ALL commentary and subtitles

This is the anchor for the rest of the spec. Every LLM prompt that
produces listener-facing copy MUST target the voice defined here.
Any item in Sections 2-5 that generates copy inherits this standard.

### The voice — locked

**Target reader:** any smart generalist, including someone who is
not a college graduate. Not the person who reads philosophy for fun.

**Reference comparison:** J.K. Rowling in adult mode, not Salman
Rushdie or V.S. Naipaul. Warm, specific, easy to follow, occasionally
pointed. No philosophical density. No hedging phrases. No abstract
nouns where a concrete image works.

**Voice tests** (a prompt output should pass ALL of these):

- Would this land with a curious 18-year-old with no college
  background? If not, simplify.
- Read it aloud in one breath. Does any sentence trip? Break it.
- Is there a concrete image in the first sentence? A dress, a
  painting, a person, a place, an act. If it opens on an abstract
  noun ("resonance", "framework", "phenomenon"), rewrite.
- Do any sentences start with "This piece / This episode examines /
  argues / traces / grapples with"? Cut those verbs. Prefer active
  verbs the reader would use in conversation.
- Are there any words a 12-year-old wouldn't know? If yes, either
  swap them or introduce them naturally.

**Words + phrases to avoid** (non-exhaustive; use judgment):

- "resonance", "juxtaposition", "interrogates", "grapples with",
  "unpacks", "the discourse", "the fabric of", "the essence of",
  "what it means to be"
- Long nested clauses; nominalisations (prefer verbs to abstract
  nouns — "understand" not "understanding of")
- Passive voice unless it's the natural rhythm

**Words + phrases to reach for:**

- Concrete verbs and specific nouns
- Direct questions the listener might already be asking
  ("Why do we remember some pictures and forget others?")
- Contractions where natural ("what's", "we're", "it's")
- Everyday connectives ("and", "but", "so") over "moreover" /
  "additionally" / "consequently"

### Concrete calibration example — locked

For today's Bare Skin × Zurbarán crosscut, the target-voice hook is:

> "Why do we remember some pictures and forget others? A fashion
> designer and a Spanish painter from centuries ago give the same
> answer: the ones that stick with us are the ones that feel a
> little off. This episode looks at what 'a little off' means in
> couture and in old religious art — and what that says about the
> way we really see."

Compare to the internal editorial-facing draft it replaces:

> "There is a striking resonance between the way modern designers
> use technology to create 'bodies without bodies' and the way a
> 17th-century master used spatial 'wrongness' to inspire faith,
> suggesting that the most powerful images are those that refuse
> to be perfectly plausible."

**What changed, and why:**

| Original | Rewrite | Why |
|---|---|---|
| "There is a striking resonance between..." | "Why do we remember some pictures and forget others?" | Opens on a listener-relatable question, not an abstract observation. |
| "the way modern designers use technology to create 'bodies without bodies'" | "a fashion designer" | Concrete noun. The jargon phrase adds nothing for a listener. |
| "the way a 17th-century master used spatial 'wrongness'" | "a Spanish painter from centuries ago" | Same — dropped date-specificity + jargon that clogs the sentence. |
| "images that refuse to be perfectly plausible" | "look a little off" | Same idea, everyday language. |
| One 47-word sentence | Three sentences, longest 30 words | Reads out loud without tripping. |

The rewrite is 55 words vs the original's 47, but every word is
comprehensible. Length is not the metric; readability is.

### Files to change for Section 1

All prompt files that produce listener-facing copy:

- `aarva/prompts.yaml` (or wherever prompts live — the file that
  Stage 8a / 8b / stage_crosscut reference)
  - Stage 8a hook prompt
  - Stage 8b contextualisation prompt
  - `stage_crosscut.py`'s intro prompt
  - `stage_crosscut.py`'s bridge prompts (bridge_a, bridge_between,
    bridge_b, outro)
  - `stage_crosscut.py`'s topic_label prompt
  - `stage_crosscut.py`'s connection-eval / rationale prompt (the
    "why-listen" that shows on candidate cards during /create)
  - Any other prompt that generates copy displayed to listeners

Each prompt should get a system-message addition that codifies the
voice standard. Reference this doc's Section 1 in each prompt's
comment so future edits inherit the standard.

**Also update `AGENTS.md`** with a new rule under "Editorial voice"
(rule 8-ish neighborhood) that says: "All listener-facing copy —
hooks, contextualisation, crosscut intros / bridges / topic labels
— MUST target the smart-generalist voice defined in
`docs/session_plan_content_quality.md` §1. Any new prompt that
produces copy for listeners inherits the standard."

### Verification for Section 1

1. Pick 3 recent articles + 2 recent crosscuts. Re-run Stage 8a +
   8b (or the crosscut equivalent) for them with the new prompts.
   Diff old vs new copy.
2. For each rewrite, apply the voice tests above. Every rewrite
   should pass all 5.
3. Read each rewrite out loud in one breath. If you trip, the prompt
   needs another pass.
4. Show 2-3 of the rewrites to the user before rolling out globally,
   in case the calibration is off in either direction.

---

## Section 2 — Better crosscut sub-headings

### Goal

Current sub-heading on `/crosscuts` cards and `/crosscut/<id>` pages
is `title_a × title_b` — just the two article titles concatenated.
That surfaces nothing about the connection or the "why listen".

Add a listener-facing one-sentence hook that draws out what the
crosscut is looking to address with the two articles, in the
Section-1 voice. The two article titles can stay as smaller
secondary text below the hook.

### Decisions locked

- **New field on the `editions` table (for crosscut episodes):**
  `subhead_hook TEXT NULL`. NULL for old episodes; new crosscuts
  populate it at build time. `stage_crosscut.py` generates it via a
  dedicated Gemini prompt at the same point in the pipeline where
  `topic_label` gets generated.
- **Prompt behaviour:** one sentence, 20-40 words, Section-1 voice.
  Poses a listener question or names the shared insight; does NOT
  restate the article titles.
- **Backfill:** run a one-off script that generates
  `subhead_hook` for every existing crosscut using the same prompt.
  Idempotent — skips rows where `subhead_hook` is already set.
- **Rendering:** on `/crosscuts` browse cards and the
  `/crosscut/<id>` detail page, show `subhead_hook` as primary
  sub-heading text. Keep `title_a × title_b` as smaller italic
  metadata under the hook.

### Files likely to change

- `aarva/db.py` — new column
- `aarva/stages/stage_crosscut.py` — new prompt + persist
- `aarva/prompts.yaml` — add the sub-heading-hook prompt
- `scripts/backfill_subhead_hooks.py` — new one-off backfill
- `aarva/server/templates/crosscuts_list.html` and
  `aarva/server/templates/crosscut_detail.html` — surface the hook
- `aarva/services/queries.py` — include `subhead_hook` in the
  crosscut load queries
- Also the listener-DB split: mirror the column on the listener DB's
  `editions` table (see `aarva/listener_db.py` — the split already
  landed, so this needs a schema migration on that side too)

### Verification

- 5 recent crosscuts have their new sub-heading hooks generated
  and rendered. Each passes the Section-1 voice tests.
- The two article titles still appear (smaller, secondary) so
  listeners can still see the source pieces.
- Old crosscuts without a `subhead_hook` fall back cleanly to
  `title_a × title_b` (don't leave a blank slot).

---

## Section 3 — Search-created crosscuts reflect the search prompt

### Goal

When a listener builds a crosscut via `/create`, the topic label,
sub-heading hook (Section 2), and the intro/bridge text should
acknowledge the originating prompt directly. Right now the prompt is
captured in the job payload during candidate selection and then
silently discarded — the finished episode reads as if it came from
the editorial daily pipeline rather than the listener's specific
question.

### Decisions locked

- **The prompt is available in the job payload already**
  (`payload.prompt` or similar — check the current schema). Wire it
  into `build_episode_script` as a parameter, defaulting to `None`
  for daily-pipeline crosscuts.
- **When `prompt` is provided**, the prompt string is passed into
  the Gemini prompts for:
  - `topic_label` — the label should reference the prompt's frame
    (e.g. a prompt about "new perspectives on the iran war"
    produces a topic label like "New Angles on the Iran War" not
    "Diplomacy and Deterrence").
  - `subhead_hook` — the hook should tie the two articles' shared
    insight back to what the listener asked about.
  - `intro_text` — the intro's opening sentence should acknowledge
    the listener's question ("You asked about X. Here are two
    pieces that come at it from different angles.").
  - Bridge and outro prompts don't need the prompt directly, but
    can reference it via the topic label they receive as context.
- **The prompt is stored on the edition** (new
  `editions.originating_prompt TEXT NULL` column, same
  new-column-on-both-DBs treatment as Section 2). NULL for
  editorial daily crosscuts; set for `/create`-built crosscuts.
  Enables Section 4's display.

### Files likely to change

- `aarva/db.py` and `aarva/listener_db.py` — new column on both
- `aarva/services/episode_worker.py::_run_job` — pass
  `payload.prompt` through to `build_episode_script`
- `aarva/stages/stage_crosscut.py` — accept `originating_prompt`
  parameter; wire into the LLM prompts noted above; persist it on
  the edition row
- `aarva/prompts.yaml` — extend the four prompts to conditionally
  weave in the prompt when provided
- Existing search-created listener episodes: **no backfill needed**
  (the prompt wasn't stored, so we can't recover it; going forward
  is fine)

### Verification

- Build 2 test listener episodes via `/create` with distinct
  prompts. Confirm the resulting topic label + subhead + intro all
  reference the prompt's frame in Section-1 voice.
- Build 1 daily-pipeline crosscut. Confirm nothing about it
  mentions any prompt (it shouldn't — that path passes `None`).

---

## Section 4 — Show search query on `/listener-created` listings

### Goal

On the `/listener-created` browse page, show each episode's
originating prompt underneath the topic label. Lets visitors see
what other listeners have been asking Aarva, and understand the
relationship between the prompt and the episode Aarva built for it.

### Decisions locked

- Uses the `editions.originating_prompt` column added in Section 3.
  Section 3 must land first (or in the same PR).
- **Display verbatim** — the raw prompt string as the listener
  typed it, no LLM paraphrase, no cleanup beyond basic HTML
  escaping. If the listener typed it in lowercase with a typo,
  that's what visitors see. Feels human.
- **Placement:** small italic text under the topic label, above
  the two article titles. Prefix with a subtle "asked:" label so
  it's clear this is what the listener asked for.
- **Fallback:** if `originating_prompt` is NULL (older episodes
  from before Section 3 landed), just don't render the "asked:"
  line — no fallback placeholder.

### Files likely to change

- `aarva/services/queries.py` — include `originating_prompt` in the
  listener-episode load query
- `aarva/server/templates/listener_created.html` — render the
  "asked: <prompt>" line under the topic label

### Verification

- Two listener episodes with prompts set → each shows the prompt
  correctly on `/listener-created`.
- One listener episode with NULL prompt → renders cleanly without
  the "asked:" line.

---

## Section 5 — Share functionality

### Goal

Listeners can share individual articles + crosscut episodes from
aarva.app. Sharing produces good previews wherever it lands (X,
LinkedIn, WhatsApp, iMessage, Slack).

### Decisions locked

- **Two share paths**, both visible in a single share button:
  - **Web Share API** on mobile (`navigator.share`) — surfaces the
    system share sheet with all of the user's messaging apps.
  - **"Copy link" fallback** universally — for desktop browsers
    that don't support Web Share, and as a manual option.
- **No platform-specific buttons** (no "Share on X" / "Share on
  LinkedIn" buttons). Web Share on mobile covers all of those; the
  Copy Link button covers desktop.
- **Open Graph + Twitter Card meta tags** on every article and
  crosscut page so wherever the link lands, the preview renders:
  - `og:title` = article title / crosscut topic label
  - `og:description` = the hook (article) or subhead_hook (crosscut)
  - `og:image` = the podcast cover (or a per-episode cover if we
    ever generate one — punt for now, use the static Aarva cover)
  - `og:url` = the canonical page URL
  - `og:type` = "article" for daily/bonus, "music.song" for
    crosscut (or just "article" everywhere for simplicity)
  - `og:site_name` = "Aarva"
  - `twitter:card` = "summary_large_image"
  - Twitter equivalents that mirror the OG values
- **Button placement:** below the audio player on both article and
  crosscut detail pages. Small, unobtrusive.

### Files likely to change

- `aarva/server/templates/base.html` — extend the `<head>` block
  with OG/Twitter meta tags driven by per-page context
- `aarva/server/templates/article.html` — populate the meta context
  for article pages, add share button component
- `aarva/server/templates/crosscut_detail.html` — same for crosscut
- Small JS snippet (inline or a new `aarva/server/static/share.js`)
  that: tries `navigator.share` first, falls back to
  `navigator.clipboard.writeText` on the current URL, shows a small
  "Copied" toast on success
- `aarva/server/routes/articles.py` and
  `aarva/server/routes/crosscuts.py` — pass the meta values to the
  templates

### Verification

- Share button on an iPhone → opens the iOS share sheet with all
  installed messaging apps as options.
- Share button on a desktop browser → clicking it copies the URL
  and shows a "Copied" toast.
- Paste the URL into iMessage, Slack, X, LinkedIn drafts — each
  shows a preview with the correct title, description, and cover
  image.

---

## Section 6 — Outro music (blocked on audio asset)

### Goal

Small musical outro at the end of every episode's audio: ~2-3
seconds, 4 santoor notes plus a harmonised vocal saying "Aarva".
Signals the end of an episode and creates a nice transition into
whatever plays next.

### Status: blocked

**Waiting on the audio asset.** User will generate it externally
(likely Suno for the full clip, or ElevenLabs Sound Effects for the
santoor + ElevenLabs voice for the "Aarva" vocal) and drop the
resulting WAV or MP3 into `aarva/assets/outro.wav`. Sample rate
should match the TTS output (24kHz mono 16-bit) so ffmpeg can
concatenate without resampling.

### Work Claude Code does once the asset lands

- Add the file to the repo at `aarva/assets/outro.wav`
- Extend `aarva/output/audio_converter.py` (or wherever the ffmpeg
  loudnorm pass is) to concatenate the outro to every article-level
  audio during Stage 10. Idempotent — check for a marker (e.g. a
  small silence tag or file-size-based sentinel) so re-running
  Stage 10 doesn't append the outro twice.
- Verification: play a re-converted MP3, confirm the outro plays at
  the end at appropriate volume (may need `-af volume=` adjustment
  to match the loudnorm target of -16 LUFS).
- Backfill: run Stage 10 conversion for existing episodes so
  everything gets the outro. Trade-off: this changes existing MP3
  file contents; podcast apps might re-download. Acceptable.

### Not in this session

Do NOT start Section 6 until the user drops `aarva/assets/outro.wav`
into the repo. If the user asks about it, remind them the asset is
the blocker.

---

## Sequencing recommendation

The dependencies aren't tight, but there's a natural order:

1. **Section 1 first** (voice prompts). It's the anchor for
   everything else. Ship it, verify with a few re-runs, get user
   sign-off on the calibration. Only proceed once the user is
   happy with the voice.
2. **Section 3** (search-aware crosscuts) alongside **Section 2**
   (crosscut sub-headings). Both add columns to the editions table
   and touch stage_crosscut prompts, so bundling avoids two rounds
   of DB migration + backfill.
3. **Section 4** (show prompt on listener-created) — small, uses
   the column Section 3 added.
4. **Section 5** (share) — self-contained, can go anywhere; do it
   after content quality is settled since the meta tags will pull
   from the improved copy.
5. **Section 6** — when the asset lands, not before.

That's roughly one PR per section. Total ~4-6 PRs for Sections 1-5.

---

## Non-goals for this session

- Don't touch Stage 4 / 5 / 6 scoring prompts — those are internal
  editorial-judgement prompts, not listener-facing copy. The voice
  standard doesn't apply there.
- Don't rewrite existing listener-facing copy retroactively for
  Section 1 (i.e., don't re-generate hooks for every historical
  article). Only new copy from now on. If the user wants
  retroactive rewrites later, that's a separate task.
- Don't add analytics for share button use. Not v1.
- Don't add "share to X specifically" or "share to LinkedIn
  specifically" buttons. Web Share + Copy Link is the full v1.
- Don't generate per-episode cover art. Static Aarva cover for OG
  images.
- Don't do the outro music without the audio asset.

---

## Related roadmap items

- The ephemeral-disk listener-DB bug (roadmap item #1 in "In
  progress") is a strict prerequisite for Section 3 + 4 landing
  cleanly — the schema additions need to land on both the main DB
  and the listener DB, and the listener DB needs to survive
  deploys. Fix that first. It's a one-line render.yaml change.
- Stage 10 loud-failure work (previously roadmap item, now shipped
  as PR #56) — unrelated to this session, but noted so nobody
  double-plans.

---

## What Cowork owes if this spec has gaps

If Claude Code finds something ambiguous in this doc — the voice
calibration feels off after a real re-run, the schema decisions
turn out to conflict with something in the codebase, the share
meta tag values need a different structure — punt back to Cowork
with the specific ambiguity. Don't guess; the whole point of this
session-plan approach is that the coding session doesn't have to
carry design ambiguity.

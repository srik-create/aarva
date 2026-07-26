# Session plan — upgrade text LLM from gemini-3-flash-preview → gemini-3.1-pro-preview

**STATUS: DONE (2026-07-26).** Model swapped, verified end-to-end on
a real full daily run (stages 1-8 + 85, zero model errors). One
follow-up fix was needed and made as part of this change: the
crosscut pre-scoring formula in `aarva/stages/stage_crosscut.py` was
divergence-dominated and surfaced weakly-connected pairs that Flash
had been scoring generously; Pro correctly scored them near zero.
Re-weighted to be similarity-led + dropped the persistence floor
4→3. See `docs/roadmap.md`'s 2026-07-26 "Recently completed" entry
for full details.

Written by Cowork for the next Claude Code session (2026-07-22+).
Single-line model swap in `aarva/config/pipeline.yaml` upgrading
the text LLM from mid-tier Flash to flagship Pro. Affects every
text-generation call in Aarva: article scoring, JTBD tagging,
structural filters, hook writing, contextualisation, author
provenance, crosscut pair-detection + stance classification,
crosscut intro/bridge/outro writing, and the /create
`propose_candidates` flow.

Read this doc + `docs/roadmap.md` + `AGENTS.md` before starting.

---

## Context

Aarva's text LLM is set in `aarva/config/pipeline.yaml` (line 375)
as `gemini-3-flash-preview`. Everything text-generation-related
reads this via `build_llm_client(config.llm)` — one shared client,
one shared model. Verified 2026-07-22 by grep across the code.

**Current call sites (all resolve to the same `llm.model`):**
- `aarva/stages/stage_2_filter.py` — structural filters (transcript
  detection, listicle detection, etc.)
- `aarva/stages/stage_4_5_6_score.py` — quality/JTBD scoring
- `aarva/stages/stage_8_hook.py` (and stage 8a) — hook + context
  writing
- `aarva/stages/stage_8c_author_provenance.py` — author provenance
  classification
- `aarva/stages/stage_crosscut.py` (multiple sites, line 1221 and
  1529) — pair detection, stance classifier, intro/bridge/outro
  writing, passage summaries
- `aarva/server/routes/create.py` → `propose_candidates` — the
  /create listener-facing pair proposer

**Not affected** (uses a different model):
- Stage 9 TTS (`aarva/clients/tts.py`) — uses
  `gemini-3.1-flash-tts-preview` per `pipeline.yaml`'s `tts.model`.
  Pro-tier text models don't do TTS; leave this alone.
- Embedding (`gemini-embedding-001`) — separate concern; leave alone.

---

## Decision locked (with user, 2026-07-22)

- **Upgrade wholesale to `gemini-3.1-pro-preview`.** One line
  change. All Stage 1-8 + crosscut + /create text-gen inherits.
- **No A/B run.** Gains on ingestion + scoring compound across
  the article pool over days — one-day comparisons don't capture
  the meaningful shift. User will observe drift in edition
  quality over the following week.
- **Rationale.** Gemini 3.1 Pro is the current flagship on
  Vertex AI (July 2026): ~2× reasoning perf over Gemini 3 Pro,
  #1 on 12+ of 18 benchmarks, 94.3% GPQA Diamond, 1M context.
  Pricing is $2/1M input + $12/1M output for ≤ 200K contexts.
  At Aarva's volume (~15 articles/day × ~5-10 LLM calls each)
  daily cost is roughly $1-2 → $30-60/month. Acceptable trade
  for editorial-quality improvements to hooks, crosscut writing,
  and stance classification.

---

## The change

### `aarva/config/pipeline.yaml`

Line 375:

```yaml
  # BEFORE
  model: gemini-3-flash-preview

  # AFTER
  model: gemini-3.1-pro-preview
```

Update the comment block above line 375 to reflect the switch —
same style as the "Switched 2026-06-12 from gemini-2.5-flash →
gemini-3-flash" comment already there. Add a new dated line:

```yaml
  # Model. Switched 2026-06-12 from gemini-2.5-flash → gemini-3-flash-preview.
  # Switched 2026-07-22 from gemini-3-flash-preview → gemini-3.1-pro-preview
  # for flagship Pro-tier reasoning across ingestion + scoring + crosscut
  # writing. Docs / benchmarks: see session_plan_llm_upgrade_gemini_3_1_pro.md.
  # Pricing: $2/1M input + $12/1M output for ≤ 200K context (Vertex AI).
```

### `aarva/clients/llm.py`

Line 377 has `DEFAULT_MODEL = "gemini-2.5-flash"` — used only if
`pipeline.yaml` doesn't set the model (should never happen in
production). Bump it too, for consistency:

```python
DEFAULT_MODEL = "gemini-3.1-pro-preview"
```

Update the comment at line 325-326 that references the old
`gemini-3-flash` / `gemini-3-flash-preview` discovery incident —
add a note that the current model is `gemini-3.1-pro-preview`.

### Availability check

Before shipping, confirm `gemini-3.1-pro-preview` resolves on
Vertex AI in the `global` location where Aarva's LLM client is
configured. Quickest check:

```bash
python3 -c "
from aarva.config import load_pipeline_config
from aarva.clients.llm import build_llm_client
cfg = load_pipeline_config()
cfg.llm.model = 'gemini-3.1-pro-preview'
llm = build_llm_client(cfg.llm)
resp = llm.generate_content('One sentence: describe the Aral Sea.')
print(resp)
"
```

If it errors with a ModelService.ListModels 404, try alternative
strings in this order (Google has shipped preview SKUs under
inconsistent names before):
1. `gemini-3.1-pro-preview`
2. `models/gemini-3.1-pro-preview`
3. `gemini-3.1-pro`
4. `gemini-3-1-pro-preview` (hyphen variant)

Whichever resolves, use that literal string. Log which variant
worked in the pipeline.yaml comment so we remember.

### Rollback path

If Pro causes issues (rate limits, latency spikes, unexpected
cost, or worse editorial output), revert is one line:

```yaml
  model: gemini-3-flash-preview
```

Push, deploy, done. No data migration needed.

---

## What will improve (qualitative expectations)

- **Hook quality (Stage 8, Stage 8a)**: hooks currently
  occasionally miss the "why the listener should care" angle.
  Pro should draw out the sharpest 1-sentence framing more
  reliably.
- **Crosscut pair detection**: better at recognising genuine
  thematic connections vs surface-level topical matches.
- **Stance classification (opposing vs different-angles)**:
  currently very few pairs classify as OPPOSING_VIEWS —
  arguably because the classifier is conservative. Pro's
  stronger reasoning may find more genuinely divergent pairs.
- **Crosscut intro/bridge/outro writing**: the connective
  editorial voice is the most listener-audible artefact.
  Sharper reasoning + better rhythm expected.
- **Structural filters (Stage 2)**: fewer misclassifications
  (listicles slipping through, transcripts flagged incorrectly).
- **Author provenance (Stage 85)**: currently has a
  known-edge case with non-person "channel" bylines (per
  `session_plan_author_provenance_accents.md`). Pro may handle
  that edge case better.
- **JTBD scoring (Stage 4-5-6)**: more consistent JTBD
  assignments.

None of these are guaranteed, and none can be A/B-verified in a
single day. Observe over 5-7 days of editions.

---

## Cost monitoring

- Add nothing extra for tracking beyond what Vertex AI's billing
  console already provides.
- If daily spend exceeds ~$5, that's a signal something changed
  (either volume ramped or long-context calls started tripping
  the 200K-token pricing tier).
- Set a soft mental threshold of $100/month. Above that, revisit
  — either use Pro selectively for hooks / crosscut only and
  Flash for the noisy bulk stages, or step back down.

---

## Non-goals

- **No A/B run.** User's call.
- **No mixed-model architecture** (Pro for creative stages, Flash
  for bulk stages). Simpler to change one line; if cost bites,
  revisit as a separate spec.
- **No changes to prompts.** Same prompts, better model. If Pro
  reveals prompt-fragility (unlikely — Pro should be more
  robust, not more fragile), address in a follow-up.
- **No changes to TTS or embedding model.** Different concerns.
- **No env-var switch.** Change the yaml directly so the git log
  shows when the swap happened. `AARVA_LLM_MODEL` remains a valid
  override for local experimentation.

---

## Files that change

- `aarva/config/pipeline.yaml` — model string + dated comment.
- `aarva/clients/llm.py` — `DEFAULT_MODEL` + comment update.
- `docs/roadmap.md` — move item from In-Progress to Recently
  Completed after the PR merges (Claude Code owns this per
  AGENTS.md rule 17).

## Verification

1. Run the availability check above against Vertex AI. Confirm
   `gemini-3.1-pro-preview` resolves. Log which literal string
   worked.
2. Run one daily edition end-to-end:
   `python -m aarva.daily`. Confirm all stages complete without
   Vertex AI errors — no unexpected model-not-found or
   permission errors.
3. Spot-check the resulting edition: read 3 hooks, listen to
   the crosscut intro/bridge/outro. Compare against a recent
   pre-Pro edition for a subjective feel.
4. Check the Vertex AI billing console 24h after the switch.
   Confirm the day's spend is in the $1-3 range, not $10+.
5. If everything looks fine, commit. User observes drift over
   the following week.

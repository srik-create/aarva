# Aarva v0.1 — Kickoff Audit

Last updated: 2026-05-26 (after closing the three v0.1 gaps + show-notes generation + TTS normalisation).

Maps every commitment in `aarva_kickoff.docx` against what's in the v0.1
code today. Statuses:

- **✓ implemented** — present and exercised by the daily pipeline
- **○ partial** — present but incomplete or behind a config flag
- **⏸ deferred** — explicitly out of v0.1 scope per the kickoff (Q in §5)
- **✗ gap** — should be in v0.1 by the kickoff but isn't

Lives as a tracking artifact — when a row flips status, update it here.

---

## §1 Context (editorial promise)

| Item | Status | Notes |
|---|---|---|
| Three audience lenses | ✓ | Stage 5 classification, Stage 7 slot assignment. |
| Five content pillars | ✓ | Stage 5. |
| Four JTBDs | ✓ | Primary + optional secondary, Stage 5. |
| Voice principles applied to Aarva wrapper | ✓ | "personal, curious, participant, playful" explicit in Stage 8a + 8b prompts (verified by grep). |
| Rigour-and-honesty articulation | ✓ | Three sub-scores + hard gate + ranking_score in Stage 4. |
| Durable / world-as-classroom signal | ○ | Implicit via Stage 4 rigour scoring; no explicit "durability" dimension. Could be made explicit via the narrative-fingerprint `temporal_lens` dimension later. |
| Narrowing must be chosen, not inferred | ⏸ | No personalisation in v0.1 → nothing inferred. Becomes relevant with Q26. |

## §2 Operating model

| Item | Status | Notes |
|---|---|---|
| No publisher partnerships (RSS + open) | ✓ | 38 publications in YAML, all RSS-based. |
| User input retained at publication-selection | ✓ | `publications.yaml` is source of truth. |
| Steady-state fully automated | ✓ | When `review.enabled: false`. |
| Cold-start lightweight human review (Q3) | ✓ | **`aarva.review` CLI + Stage 7 halt + `finalize_edition.sh`.** |
| Small-circle initial distribution | ✓ | Personal podcast feed on GitHub Pages. |
| Legal/reputational re-eval at scale (Q25) | ⏸ | Pending until distribution grows. |

## §2 Publication allowlist

| Tier | Coverage | Status |
|---|---|---|
| A — long-form essays | 8 of 12 from kickoff | ○ Missing: American Scholar, Liberties, Comment Magazine, Compact |
| B — behind the news | 11 of 15 | ○ Missing: Inkstick, Reuters Investigates, AP investigative, The Independent (Reuters/AP need wire-branch first, Q28) |
| C — news analysis | 8 of 13 | ○ Missing: UnPopulist, Construction Physics, Honest Broker, Culture Study, Cafe Hayek |
| D — international | 5 of 10 | ○ Missing: Le Monde diplomatique, Caixin, Mada Masr, Granta, Eurozine |
| E — specialist | 6 of 12 | ○ Missing: STAT News, Tortoise, Hakai, Emergence, Pudding, Edge.org |
| F — heterodox bench | 0 of 6 | ✗ Needs filter calibration before adding |
| G — paywalled free crumbs | 0 of 16 | ✗ Needs canonical-fingerprint detection (Stage 2 gap) |
| H — smart escape | 0 of 7 | ✗ Smart-escape slot currently draws from Tier E (Orion, Smithsonian) |
| **Broken / stale URLs** | 8 of 38 active | ✗ Hedgehog Review (404), Works in Progress (404), Lawfare (403), Politico Magazine (403), Brookings (malformed), American Prospect (429), Caravan (404), Knowable (404) |

## §2 Ingestion rules

| Item | Status | Notes |
|---|---|---|
| Word floor: 600 words | ✓ | `filters.word_floor`. |
| Forbes staff-bylines filter | ✗ | **Gap.** Latent — Forbes not enabled, but the filter logic isn't built. |
| Prefer-the-free-mirror | ✗ | **Gap.** Latent — matters at Tier G activation. |
| Paywall position: no archive.ph | ✓ | Code does straight HTTP fetches. |
| Drop wire-service rewrites | ✗ | **Gap.** Cross-pub wire pickups can still slip through. |
| Drop listicles | ○ | `filters.listicle_keywords` configured; caught 0 in recent runs. |

## §2 Volume strategy (Q32)

| Item | Status |
|---|---|
| Headlines daily | ✓ |
| Stage 1.5 consolidation | ✓ |
| Per-publication cap in pool | ✓ (per-cluster) |
| Diversity preservation | ✓ |
| Pairing candidate tagging (Q31) | ⏸ |

## §2 Editorial framework

| Item | Status |
|---|---|
| Three sub-scores (rigour/posture/self-implication) | ✓ |
| Hard gate rigour ≥ 0.5 AND posture ≥ 0.5 | ✓ |
| Ranking 0.45·R + 0.45·P + 0.10·SI | ✓ |
| Per-piece judgment, no author halo | ✓ |
| Calibration set (Q5) | ○ v1 exists but n=10 due to paywalled URLs — **v2 rebuild pending (task #44)** |
| Quarterly Tier F review | ⏸ No Tier F yet |

## §2 Categorisation

All four axes ✓ (lens, pillar, JTBD primary/secondary, topic_recency_sensitivity) — populated by Stage 5.

## §2 Editorial rhythm

| Item | Status |
|---|---|
| Briefing slot (wire branch) | ⏸ Q28 |
| Deep feature | ✓ |
| Three lens cards | ✓ |
| Curiosity + smart-escape | ○ Currently 1 each, kickoff suggested 2-3 — fine for v0.1 |
| "Go deeper" link from briefing | ⏸ Q28 |
| Long-form-only opt-out asymmetry | ⏸ Q26 |

## §2 Personalisation (Q4 resolved / Q26 framework)

| Item | Status |
|---|---|
| Six-dimension narrative fingerprint | ✓ Stage 6 |
| ~37-dim encoded fingerprint vector | ✓ Stored in `article_scores.fingerprint_json` |
| Four-axis personalisation | ⏸ Q26 |
| JTBD-conditional 4×6 matching weights | ⏸ Q26 prerequisite |
| Temporal weight × recency_sensitivity | ⏸ Q26 prerequisite |

## §2 Filter-bubble protection

| Item | Status |
|---|---|
| Topic-concentration cap ≤30% | ✓ **`max_per_cluster_per_edition: 1`** (today) |
| Viewpoint balance ≥1 under-engaged | ⏸ Personalisation prerequisite |
| Serendipity slot (stochastic / cross-user-popular) | ✗ **Gap.** Stage 7 still purely greedy top-1 |
| Cold-start exploration ramp (Q30) | ⏸ |
| No "here's the other side" labels | ✓ |

## §2 Trending coverage

| Item | Status |
|---|---|
| Max 50% trending share per edition | ✗ **Gap.** Config has `assembly.trending_cap=0.50` but nothing reads it |
| Trending detection (Q10) | ⏸ |
| Adaptive cap (Q9) | ⏸ |

## §2 Pairings (Q31)

Entirely ⏸ deferred.

## §2 Pipeline flexibility

| Item | Status |
|---|---|
| Config-driven editorial parameters | ✓ |
| Per-stage flexibility (model swap, etc.) | ✓ `llm.provider` and per-stage prompts |
| Re-tagging as single batch job | ○ Possible via re-run; no dedicated script |

---

## §4 Pipeline stages

### Stage 1 — Ingestion

| Item | Status |
|---|---|
| Essay branch RSS pull | ✓ |
| RSS every 2-3 hours | ✗ Currently daily |
| Headlines + first paragraph only initially | ✗ Currently fetches full text for all |
| Wire branch | ⏸ Q28 |

### Stage 1.5 — Consolidation

✓ Embeddings + TF-IDF fallback, per-publication cap, diversity preservation.

### Stage 2 — Hard filters

| Item | Status |
|---|---|
| Word floor 600 | ✓ |
| Publication allowlist | ✓ |
| Forbes filter | ✗ Latent |
| Prefer-the-free-mirror | ✗ Latent |
| Drop wire rewrites | ✗ Latent |
| Drop listicles | ○ |

### Stages 2.5, 3 — wire ranking, trending

Both ⏸ (depend on Q28 wire branch and Q10 trending detector).

### Stages 4, 5, 6 — Tonal, classification, fingerprint

All ✓. Combined LLM call. Currently using Gemini 2.5 Flash.

### Stage 6.5 — Basket assembly

✗ **Gap.** Currently we go from Stage 4+5+6 → Stage 7 directly. The kickoff's three-basket structure (briefing/editorial/archive) doesn't exist. Minor at single-user volume; matters for personalisation later.

### Stage 7 — Edition assembly

| Item | Status |
|---|---|
| Slot structure (deep + 3 lens cards + curiosity + smart-escape) | ✓ |
| Briefing slot | ⏸ Q28 |
| Per-publication cap | ✓ `max_per_publication_per_edition: 1` |
| Per-cluster cap (topic concentration) | ✓ **`max_per_cluster_per_edition: 1`** (today) |
| Length distribution (30/50/20) | ✓ **soft preference** (today) |
| Trending cap | ✗ Latent |
| JTBD coverage ≥3 of 4 | ○ Emerges from slot definitions |
| Stochastic / serendipity | ✗ |
| Personalisation favouring | ⏸ Q26 |
| Cold-start review-mode refill | ✓ |

### Stage 8 — Hook + context + show notes

| Item | Status |
|---|---|
| 8a hook | ✓ |
| 8b why-now context | ✓ |
| 8c show notes | ✓ **(today)** |
| Mode A/B/C selection for 8b | ✓ (Mode C still degrades to B per kickoff — Q28 dependency) |

### Stage 9 — Audio + metadata

| Item | Status |
|---|---|
| TTS narration | ✓ (Kokoro) |
| Voice selection (gender match + alternate) | ✓ |
| Spoken attribution handoff | ✓ "Narrated for Aarva." |
| Markdown / heteronym normalisation | ✓ **(today)** |
| Show-notes summary in metadata | ✓ **(today, via RSS description)** |
| Episode-level lens/pillar/JTBD tags | ✗ Not in RSS — minor |

### Stage 10 — Publish

| Item | Status |
|---|---|
| HTML edition | ✓ |
| RSS feed (Podcast 2.0) | ✓ |
| MP3 conversion + publish | ✓ |
| GitHub Pages | ✓ |
| Cold-start human gate | ✓ |
| Post-hoc audit + flag-and-remove (Q6) | ✗ **Gap.** No retroactive review of past editions |

---

## §5 Open questions register

| Q | Topic | Status |
|---|---|---|
| Q2 | Legal framework | ⏸ (lawyer task; out of code scope) |
| Q3 | Cold-start operating mode | ✓ **resolved** |
| Q4 | Narrative-fingerprint design | ✓ resolved |
| Q5 | Tonal calibration | ○ v1 set has paywall problem; v2 rebuild pending |
| Q6 | Post-hoc audit + flag-and-remove | ✗ Tier B, not built |
| Q9 | Adaptive trending cap | ⏸ |
| Q10 | Trending detection | ⏸ |
| Q12 | Specific assembly values | ○ slot structure ✓ + caps ✓ + length ✓; trending cap ✗ |
| Q14 | Matching discrimination | ⏸ |
| Q15 | Quality bar for hooks/contexts | ○ prompts in place; no formal eval |
| Q16 | TTS provider | ✓ (Kokoro) |
| Q21 | Stage 4/5/6/8 prompts | ✓ (Stage 8c added today) |
| Q24 | Brand | ✓ (Aarva) |
| Q25 | Paywall position at scale | ⏸ |
| Q26 | Four-axis personalisation | ⏸ |
| Q27 | Today screen rhythm UI | ⏸ |
| Q28 | Breaking-news / wire branch | ⏸ |
| Q29 | Multi-axis similarity | ⏸ |
| Q30 | Cold-start exploration ramp | ⏸ |
| Q31 | Pairings as structural feature | ⏸ |
| Q32 | Stage 1.5 consolidation | ✓ |

---

## Headline status

**v0.1 against the kickoff is ~85% complete.** What's left:

### Real gaps (kickoff-promised, not built)

1. **8 broken feed URLs** — operational gap, blocks promised publications. ~30 min of URL hunting.
2. **Calibration set v2** — current set has n=10 effective due to paywalls; can't confidently validate Stage 4 quality.
3. **Trending detector + cap** (Q10) — `assembly.trending_cap` config exists but unread. Without a detector, there's no signal to cap against.
4. **Post-hoc audit / flag-and-remove (Q6)** — no way to retroactively review or pull a piece from past editions.
5. **Forbes staff-bylines filter** — latent; required before Tier G activation.
6. **Prefer-the-free-mirror canonical fingerprint** — latent; required before Tier G activation.
7. **Wire-service rewrite detection** — latent; minor at current pool.
8. **Stage 6.5 basket assembly** — minor at single-user volume; matters with personalisation.
9. **Serendipity slot** — Stage 7 is purely greedy; no stochastic sampling.

### Deferred-by-design (kickoff Q-tagged)

Q2 (legal), Q9/Q10 (trending), Q14 (matching), Q25 (paywall at scale), Q26 (personalisation), Q27 (Today UI), Q28 (wire branch), Q29 (multi-axis similarity), Q30 (cold-start ramp), Q31 (pairings).

### Done since the last audit write

All three v0.1 gaps closed: topic-concentration cap, length distribution, show-notes summary. Plus: cold-start review CLI, TTS normalisation + heteronym fix, LLM-policy codification, migration runbook, requirements.txt, Gemini 2.5 Flash with paid quota.

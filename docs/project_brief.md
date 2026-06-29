# Aarva — Project brief

Source of truth for orientation. Read this at the start of any session
(human or AI) before doing material work. Companion docs:

- `AGENTS.md` — rules of engagement for AI agents
- `docs/roadmap.md` — what's next, what's deferred, recent commits
- `docs/aarva_architecture_v1.md` — deep technical reference (schema, stages)

**Last updated:** 2026-06-26

---

## What Aarva is

Aarva is a daily AI-narrated podcast pipeline that curates, narrates,
and publishes editions of longform journalism. Every day the pipeline
pulls articles from ~70 publications, scores them for editorial
rigour, picks ~8 pieces for the daily edition (each filling a defined
editorial slot — deep feature, lens cards, curiosity, smart escape,
delight), pairs two articles into a Crosscut episode, generates
intros + context + audio narration via Gemini, and publishes as a
podcast (Apple/Spotify/YouTube) plus an HTML+RSS site.

**Editorial vision** (from `pipeline.yaml`'s feed_description):

> The world as your classroom, the finest journalism as your
> curriculum. Written by humans. Narrated by AI. Every day, a
> selection of handpicked articles meant to delight, indulge
> curiosity and expand the mind. Not the anxiety-inducing cycle of
> breaking news.

**The Crosscut concept**: each day's edition includes one paired-
listening episode where two articles with a non-obvious connection
play back-to-back with bridges between them. Designed to surface
patterns and surprising links.

---

## Who it's for

Listeners who want curated longform journalism delivered as audio.
Aarva replaces "scrolling for interesting reads" with a steady daily
of pre-curated, narrated pieces. Audience is small today; pricing
model TBD.

---

## Architecture in one paragraph

Python codebase, SQLite DB (`aarva/data/aarva.db`). Pipeline stages
run independently via `python -m aarva.daily --stage N`:

1. **Stage 1** Ingest — RSS feeds → articles
1.5. **Stage 1.5** Consolidate — local BGE embeddings, similarity-based dedup
2. **Stage 2** Filter — hard filters (word floor, listicle keywords, digest detector)
4-5-6. **Stage 4+5+6** Score — Gemini call per article: rigour, posture, self-implication, lens, JTBD, piece_type
7. **Stage 7** Assemble — slot-fill the daily edition; halts for review if review.enabled
8. **Stage 8** Hook + context — Gemini-generated intro / contextualisation / show notes per piece
9. **Stage 9** TTS — Gemini Native Audio synthesises per-piece + per-chunk MP3s
10. **Stage 10** Publish — MP3 conversion + loudness normalization + R2 upload + HTML/RSS render

Crosscut is a parallel sub-pipeline: detect candidate pairs from
recent scored articles → user picks one → generate intro+bridges+
outro → TTS as one continuous 20-30 min episode.

**Hosting today**:
- Audio: Cloudflare R2 (custom domain `audio.aarva.app`)
- HTML + RSS: GitHub Pages (`srik-create.github.io/aarva`)
- LLM: Gemini 3 Flash Preview via Vertex AI (ADC, gibran.ai's GCP project)
- TTS: Gemini 3.1 Flash TTS Preview
- Embeddings: local BGE-base (`sentence-transformers`)

**Web app under construction**: FastAPI server in `aarva/server/`,
target deploy to Render.com at `aarva.app`.

---

## Standing user preferences (the human running Aarva)

Some are already in `AGENTS.md`; restated here for orientation.

- **Brevity by default.** Brief responses; no over-explaining; no
  restating the question.
- **Branch + PR workflow.** Don't commit to main directly except for
  trivial doc fixes. Always: feature branch → push → PR → merge → pull.
- **Pre-approve material trade-offs.** Surface options before
  picking, especially around editorial behaviour, schema changes,
  third-party services.
- **Gemini for all non-coding LLM.** Claude is reserved for the AI
  coding agent itself. Pipeline LLM work goes through
  `aarva.clients.llm` with the Gemini backend.
- **Web-search anything post-training.** API endpoints, library
  versions, service pricing — don't trust 2024-2025 memory.
- **External service choices need explicit user check** (AGENTS.md
  rule 7a). Don't bury hosting / email / payments decisions in an
  implementation plan.
- **Design for portability** (AGENTS.md rule 7b). Env vars for all
  config, SDKs behind thin wrappers, Dockerfile as canonical build
  so swapping providers is config-only.
- **The user is non-developer.** All code edits go through the AI
  agent; the user runs commands in their terminal. The agent
  shouldn't push to GitHub directly (the user does this manually
  after sign-off).

---

## Decisions log

Chronological. Includes the rationale + the reversibility note so
future-us knows whether to revisit.

### Editorial / product

| When | Decision | Rationale | Reversible? |
|---|---|---|---|
| Project inception | Editorial bar: rigour ≥ 0.5, posture ≥ 0.5 | Quality floor for what gets narrated | Yes — tune in pipeline.yaml |
| Project inception | One daily edition + one crosscut per day | Singular curated experience | Yes — schema permits more (deferred multi-crosscut work documented in roadmap) |
| Project inception | Anti-source-dominance cap: 1 piece per pub per edition | Diversity of sources | Yes — pipeline.yaml |
| Project inception | Cross-edition publication cooldown: -0.12 / -0.08 / -0.05 / -0.03 / -0.02 decay across last 5 dailies | Avoid same 4-5 pubs every day | Yes |
| Project inception | Topic-concentration cap: 1 piece per Stage-1.5 cluster | No "5 takes on the same news event" | Yes |
| Project inception | Taste-centroid bias from approval / rejection embeddings | Light personalisation hint | Yes — `taste_bias_weight: 0` to disable |
| Project inception | No first person in editorial copy; no LLM-tell vocabulary | Aarva is a curatorial voice, not a personality | Embedded in `prompts.yaml` + AGENTS.md rule 9a/9b |
| 2026-06-13 | Stage 4-5-6 enforces `piece_type='article'` (filters digests / collections / videos) | Catalog-grade content only | Yes — relax piece_type check |
| 2026-06-13 | JTBD priority order (delight > smart_escape > keep_ahead > keep_up_to_date > curiosity) | Prevent curiosity becoming catch-all | Yes — prompts.yaml |
| 2026-06-18 | Per-publication `country: us|uk|india` tag for TTS accent steering | Indian Hindu pieces shouldn't sound American | Yes — country tag is optional per pub |
| 2026-06-18 | Explicit dates in Stage 8 contextualisation (not "last week") | Future-listener-proof for old episodes | Yes — prompts.yaml change |
| 2026-06-26 | Rejected articles are hard-blocked across editions | User was seeing same articles re-suggested daily | Yes — DELETE FROM edition_rejections WHERE article_id = ? |
| 2026-06-26 | `lens_card_future` and `lens_card_behind` capped to last 6 days | News-y slots lose meaning fast | Yes — `assembly.slot_max_age_days` override |

### Tech / infrastructure

| When | Decision | Rationale | Reversible? |
|---|---|---|---|
| Pre-session | BGE-base (local) for article embeddings | Free, no API, decent quality | Yes — embedding client is configurable |
| Pre-session | SQLite as primary DB | Single-file, no ops | Migrating to Postgres ≈ 2 days of work if scale demands |
| Pre-session | Gemini for pipeline LLM | Cost + quality vs. Claude API | Yes — `provider: anthropic_api` for fallback |
| 2026-06-12 | Gemini 2.5 Flash → Gemini 3 Flash Preview | Free-tier quota relief + better quality | Yes — config-only |
| 2026-06-13 | LLM auth: API key → ADC + Vertex AI | gibran.ai data-residency requirement + bypasses AI Studio spending caps | YAML-only switch; api_key path preserved as fallback |
| 2026-06-13 | TTS model: 2.5 Flash → 3.1 Flash | 2.5 broken on Vertex 'global' endpoint; 3.1 quality is genuinely better | Yes — config-only |
| 2026-06-13 | Vertex location: `global` (not single-region EU) | Only location serving Gemini 3 Flash for this project | Could switch when more regions enabled |
| ~2026-06-22 | Audio hosting: GH Pages → Cloudflare R2 | GH Pages 1 GB soft cap + R2 has zero egress | Easy — config-only `tts.r2.enabled` flag |
| 2026-06-25 | TTS pace target: 140 WPM via inline tag | Listener-feedback informed | Yes — prompts.yaml |
| 2026-06-25 | Loudness normalize MP3s to -16 LUFS via ffmpeg loudnorm | Per-chunk volume variance was audible | Yes — `output.loudness_target_lufs` |
| 2026-06-26 | R2 URL: `pub-xxx.r2.dev` → `audio.aarva.app` | r2.dev rate limit broke YouTube ingestion | Yes — config-only `tts.r2.public_url_base` |
| 2026-06-29 | TTS `max_chunk_chars` 2500 → 1800 | Listener feedback: voice quality drifts audibly within each ~3-min chunk and resets at the chunk boundary. Smaller chunks (~2 min) keep each request short enough to stay before the drift, at the cost of ~40% more chunks → slightly more API calls and more chunk transitions. The chunker still packs paragraphs first / sentences as fallback / never splits mid-sentence. | Yes — `tts.max_chunk_chars` in pipeline.yaml |
| 2026-06-26 | Daily run is invoked manually by the operator (not via launchd) | User prefers explicit control over each daily run — a moment to glance at the previous day's output / decide to skip a day / pause without fighting an automation. The pipeline is not so time-sensitive that automation is worth the loss of agency. `scripts/app.aarva.daily.plist` exists as a starting point if scheduled runs are ever desired but is not actively loaded. | Yes — `launchctl load ~/Library/LaunchAgents/app.aarva.daily.plist` activates the scheduled run |

### Web app

| When | Decision | Rationale | Reversible? |
|---|---|---|---|
| 2026-06-26 | Web framework: FastAPI + Jinja templates + HTMX | Python-native; matches existing codebase; minimal frontend complexity for v1 | Could switch to React SPA later if needs grow |
| 2026-06-26 | Hosting: Render.com (NOT Cloudflare Workers) | Workers can't run Python efficiently (10ms CPU limit, no persistent disk, no PyTorch) | Dockerfile is canonical; swap providers without code change |
| 2026-06-26 | Auth: email-on-request for crosscut notifications only | No accounts for v1; capture email only at the moment of crosscut request | Adding full auth later is purely additive (magic-link infra already in services/users.py) |
| 2026-06-26 | ~~Search UX: NL chatbox primary, structured filters secondary~~ | Superseded 2026-06-29 by the episode-creation reframe (see below). | — |
| 2026-06-26 | ~~Crosscut-on-demand UX: automatic suggestion on every search~~ | Superseded 2026-06-29 — the listener never sees a "search" surface; the prompt directly proposes episode candidates. | — |
| 2026-06-29 | **Search collapses into episode creation.** The listener-facing input is framed as creation, not retrieval: a prominent prompt on every page with the placeholder "create an episode on anything". Submitting takes the listener to a candidate page showing up to 3 candidate episodes. Mix of (a) existing crosscut episodes whose embedding matches the prompt above a similarity threshold (shown as "Listen now"), and (b) new pairings proposed by Gemini from articles in the vector space (shown as "Create this episode" — picking one queues a build job that runs the existing crosscut pipeline). The word "crosscut" is hidden from listeners; everything is just an "episode". Articles are not a top-level result type — they're ingredients. Built episodes re-enter the public catalog at /crosscut/<id> and become searchable by future prompts. | One unified UX instead of two (search results page + separate request flow). Closer to listener intent ("make me something to listen to about X"). Articles-as-results would have meant exposing an interim browse surface that doesn't lead to listening; this jumps straight to the episode framing. | Could re-add an explicit article-search surface later; the candidate-generation service is a pure function of the prompt so the input UI is replaceable |
| 2026-06-29 | **Build worker: in-process thread on the existing Render web service.** A background thread inside the FastAPI process polls the existing `jobs` table for `kind='build_crosscut'` rows and runs `stage_crosscut.build_episode_script` against the picked pairing. Single concurrent build at v1; FIFO order; startup pass resets stuck `running` jobs older than 30 min back to `pending`. | Zero extra Render cost (no separate Background Worker service). Build throughput at v1 is bounded by listener volume; one concurrent build is plenty. A worker crash also crashes the web app — acceptable at v1 reliability targets, revisit if build volume rises. | Yes — moving to a Render Background Worker service is a config change (~+$7/month) and `aarva/services/episode_jobs.py` is shaped so the worker loop is independent of the FastAPI process |
| 2026-06-29 | **Email provider for episode-ready notifications: Resend.** When an on-demand episode finishes building the requester gets an email link to the public episode page. | Free tier covers ~3k emails/month, single `RESEND_API_KEY` env var, modern API, easy DKIM/SPF on `aarva.app`. Postmark is more bulletproof but overkill at v1; SES needs more setup; SMTP-via-gibran.ai has worst deliverability. | Yes — email sender is behind a thin wrapper, swap to Postmark / SES / SMTP later by changing the wrapper's backend |
| 2026-06-29 | **Listener-created episodes live on a dedicated surface, separated from the editorial catalog.** Multiple crosscuts per day are now allowed (schema constraint loosened — only `edition_type='daily'` keeps the one-per-day uniqueness; the `editions.user_id` column distinguishes pipeline-generated `NULL` from listener-generated `SET`). `/today` and `/crosscuts` continue to show ONLY pipeline-generated crosscuts (`user_id IS NULL`), keeping the editorial flow uncluttered. A new `/listener-created` page shows the listener-generated set, newest first. The per-episode detail page `/crosscut/<id>` works for either type — looked up by primary key, no user_id filter. | The listener's first encounter shouldn't be the daily edition polluted with strangers' on-demand prompts. The editorial catalog stays curated; the listener-created catalog grows in its own space and is still surfaced via search (the candidate service matches against both). | Yes — `user_id` already exists in the schema; reverting means removing the filter clauses from `/today` + `/crosscuts` and deleting the `/listener-created` route |
| 2026-06-29 | **Listener builds capped at 2 per email per 24 hours.** `enqueue_build_job` counts non-failed `build_crosscut` jobs for the requester's `user_id` within the trailing 24h window; over the cap → `BuildQuotaExceeded` → the route renders `create_quota_exceeded.html` with a 429. Failed / cancelled jobs don't burn a slot. | Each accepted build costs ~$0.80 in Gemini TTS — a small cap keeps cost predictable while the service is in early access. 2/day lets a curious listener try a second pairing the same day without inviting abuse. Rolling window (not midnight reset) avoids gaming the boundary. | Yes — `DEFAULT_BUILDS_PER_24H` constant in `aarva/services/episode_jobs.py`; tune upward when payment / paid tiers exist |
| 2026-06-26 | Domain: `aarva.app` (registered, Cloudflare DNS) | Owned by user; clean | Standard DNS |
| 2026-06-26 | Tailwind via CDN (no build step) | Phase-1 simplicity | Migrate to a Tailwind build later if needed |

---

## Open questions / things to revisit

(Items in here either don't have an immediate trigger or need more
information / a creative decision before they can be acted on.)

- **Logo design.** Header currently uses a text wordmark. Need a real
  mark before Phase 4 (deploy). Could share with the podcast cover
  art (`scripts/generate_logo.py`) or be separate.
- **Hook quality (Stage 8a).** User feedback: hooks should pull out
  the most critical "why this matters" in one sentence, not just
  describe the piece. Pipeline prompt rewrite — deferred from web
  app phase 1.
- **Crosscut listener-notification mechanism.** When a user requests
  an on-demand crosscut and it takes ~15 min to render, how do we
  notify them when ready? Email is current plan; could also be
  in-page polling, browser push, shareable status URL.
- **Multi-crosscut per day.** Current schema enforces one. User has
  asked to support multiple. Documented in roadmap; deferred until
  after web app phase 1.
- **Cloudflare Pages alternative for HTML+RSS.** Currently HTML/RSS
  serve from GitHub Pages; once web app lands at aarva.app, those
  static artifacts could move to Cloudflare Pages or be served by
  the FastAPI app directly. Decision deferred to web app phase 4
  (deploy).
- **What to do with the back catalog of pre-improvements audio.**
  TTS quality improvements (loudnorm, 140 WPM) only apply
  prospectively + to 17-June-onwards re-normalized files. The pre-
  June-17 catalog is at the old quality. Decision: leave alone
  (per AGENTS.md rule 11 — don't re-narrate without explicit ask).

---

## Standing user preferences inferred from this session

These are observed-pattern preferences. AI sessions should treat them
as defaults unless overridden:

- **Honest assessment over reassurance.** When something's broken,
  say it's broken. When a recommendation has caveats, say what they
  are.
- **Concrete before abstract.** "Here are 3 options with trade-offs"
  beats "let me think about how to approach this."
- **Track time-sensitive items.** The user values seeing deferred
  work surfaced when adjacent work begins.
- **Cleanliness of commit history.** One-concept-per-commit (rule
  21), explicit sign-off (rule 20), branch+PR for anything non-trivial.

---

## How to use this doc (for AI agents)

At session start: read `AGENTS.md` + `docs/project_brief.md` (this
file) + `docs/roadmap.md`. Then proceed.

When making a meaningful decision: add a row to the appropriate
decisions log table in the same commit/PR that makes the change. The
decision and the code change land together.

When deferring something: add it to `docs/roadmap.md`'s "Deferred"
section AND mention it in the open-questions section here if it's
broader than a single feature.

When making changes that affect this doc's architecture summary or
standing decisions: update the doc same commit. Never let it drift.

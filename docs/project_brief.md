# Aarva — Project brief

Source of truth for orientation. Read this at the start of any session
(human or AI) before doing material work. Companion docs:

- `AGENTS.md` — rules of engagement for AI agents
- `docs/roadmap.md` — what's next, what's deferred, recent commits
- `docs/aarva_architecture_v1.md` — deep technical reference (schema, stages)

**Last updated:** 2026-07-22

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
- Embeddings: Vertex AI `gemini-embedding-001` (768-dim Matryoshka)

**Web app live at `aarva.app`**: FastAPI server in `aarva/server/`,
hosted on Render.com (Dockerfile-canonical). Two phases shipped:

- Phase 1 — browse-by-X surface (Today, Editions, Categories,
  Crosscuts, Publications, Listener-created), per-JTBD pastel
  cards, persistent sticky audio mini-bar with state across
  page navigations, PWA installable.
- Phase 2 — listener-initiated episode creation. Prompt input on
  every page, candidate page with 3 pairings (mixed existing-
  match + Gemini-proposed new), background worker builds picked
  pairings via the existing crosscut pipeline, email-when-ready
  (currently stub, Resend pending env-var set on Render).

DB lives on Render's persistent disk at `/data/aarva.db`; the
laptop pipeline syncs it nightly via the R2 → /admin/sync-db
relay (see `docs/deploy.md`).

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
| 2026-07-17 | **Reviewer feedback learning loop, Phase 1: capture a reason code on every reject.** `python -m aarva.review` now prompts for one of seven codes (too_long/too_short/wrong_tone/transcript/video_dependent/listicle/other+note) after a reject, stored on new `edition_rejections.reason`/`reason_note` columns. Reason list lives as data in `aarva/services/review_reasons.py`, not a DB enum. Legacy rows stay `reason=NULL` — no retroactive backfill. | Today's rejection signal is a single undifferentiated blob (one rejection-centroid vector); different rejection reasons want different remediations (a hard Stage 2 filter for structural reasons vs. a soft taste-centroid penalty for qualitative ones). See `docs/session_plan_reviewer_learning_loop.md`. | Yes — `reason`/`reason_note` are both nullable and unused by any other code path yet; removing the CLI prompt or the columns is a no-op for everything else |
| 2026-07-18 | **A drop (`Nd`) in the review CLI now excludes the article from the whole edition, not just the dropped slot.** New `editions.dropped_article_ids` column; Stage 7's candidate pool is filtered against it (merged with the rejected-articles set). Also added an `Nu` un-approve command — approved pieces are now visible in the CLI listing (✓ marker) so they have an index to un-approve by. Blanket shortcuts (`all-a`/`all-r`/blank) only sweep proposed pieces, so approved ones stay frozen unless explicitly referenced. | Dropped articles were reappearing in a different slot of the SAME edition the reviewer just dropped them from — not the intent of "not right now." Un-approve closes a real gap: the only prior fix for a mis-click approve was a manual SQL update. See `docs/session_plan_review_cli_polish.md`. | Yes — both are additive; `dropped_article_ids` defaults to an empty list and `Nu` is opt-in per piece |
| 2026-07-17 | **Author-provenance-based TTS accent steering** — supersedes the 2026-06-18 publication-only country tag as the primary signal for TTS accent choice; the publication tag stays as fallback. New `articles.author_country_code` (`us`/`uk`/`india`/`unknown`/`NULL`), classified once per article by a small Gemini call reading byline + body evidence, explicitly never the author's name. Precedence at TTS time: known author provenance > publication tag > default. Full catalog (8,238 articles) backfilled 2026-07-17. See `docs/session_plan_author_provenance_accents.md`. | Publication-only tagging under-covers unaffiliated publications (The Diplomat) and over-generalizes pan-regional ones (Himal Southasian); diaspora authors need country-of-residence, not name-inferred heritage — the 2026-06-18 row's "Indian Hindu pieces shouldn't sound American" goal is better served per-author than per-publication. | Yes — `author_country_code` is nullable per article; clearing it reverts a piece to publication-tag-only behavior |
| 2026-07-22 | **Strip terminal publication boilerplate (production credits, crisis-line footers, author bios, subscription CTAs) from article text at ingestion — not preserved anywhere.** New `aarva/services/terminal_boilerplate.py` regex classifier, wired into Stage 1. New articles only, no backfill. | Two articles in two days deterministically tripped Gemini TTS's safety filter on this exact kind of tail content (a suicide-crisis-line footer, a production-credits list) — useless in audio either way, since a listener can't dial a hotline mid-episode. Explicit user decision: listeners who want it click through to the source article. See `docs/session_plan_tts_boilerplate_strip.md`. | Yes — new articles only; an already-ingested article's `full_text` is untouched unless it re-fails at TTS and gets manually re-cleaned |
| 2026-07-22 | **Operator search + ad-hoc URL ingest — two new CLI tools.** `python -m aarva.search --for-edition --add-to-edition` (extends the existing `aarva/search.py` rather than a new `aarva/find.py`) and `python -m aarva.ingest_url <url>` (new) both add articles to today's daily edition manually, bypassing Stage 7's automatic selection, via a shared `aarva/services/edition_ops.py` primitive. Ad-hoc URLs from unknown publications can register a one-off DB-only publication row (with an optional country tag for TTS accent — required adding a `publications.country` DB column, since publication-level accent steering was previously 100% `publications.yaml`-driven). See `docs/session_plan_operator_search_and_url_ingest.md`. | Two gaps in the daily-review flow: no way to browse the DB for candidates the operator remembers, and no way to pull in a URL from a publication that isn't RSS-configured or that slipped past the ingestion window. | Yes — both are additive CLI tools; no changes to the automatic Stage 7 pipeline's behavior |
| 2026-08-10 | **Curation-platform "not too niche" signal (+ v1.5 topic-similarity extension) enabled in production.** `pipeline.yaml`'s `curation.enabled` flipped `false` → `true` per explicit user request, to test starting the next daily run — earlier than the "operator inspects a crawl's output first" default both specs called for. Real production `curation_hits` was still empty at the time of flipping (all crawl testing had run against disposable DB copies) — the operator needs to run `python -m aarva.daily --stage 0` for real before/as part of the next run for the signal to have anything to match against. See `docs/session_plan_curation_platform_signal.md` and `docs/session_plan_curation_topic_similarity.md`. | User wants to see it in action on real data now rather than wait through a separate inspection period. | Yes — `curation.enabled: false` reverts to the original zero-behavior-change default; no schema/data changes to undo |
| 2026-08-13 | **Trend-signal layer (delight/timeliness) shipped with a materially reduced source list vs. the original spec.** `docs/session_plan_trend_signal_for_delight.md` proposed Google Trends + YouTube Trending + GDELT as three independent crawled sources. Rule 6a verification found: `pytrends` archived/unmaintained (switched to `trendspyg`); GDELT's DOC 2.0 API has no "what's trending" endpoint at all — it's purely search-driven, so it was dropped as a source and kept only as the matching flow's fallback search; YouTube Trending dropped from v1 by user decision given the new `AARVA_YOUTUBE_API_KEY` GCP setup step it would require. v1 ships with exactly one crawled source: Google Trends, 3 real regions (US/IN/GB — the spec's 4th "global" region isn't a real Google Trends concept). | Both were genuine gaps in the spec's Architecture-check-adjacent research, not implementation choices — GDELT's endpoint literally doesn't exist, and pytrends being dead would have shipped a crawler on an abandoned dependency. Confirmed with user before building rather than silently substituting. | Yes — adding YouTube/other sources later is additive (new `TrendSource` rows + a new crawler handler), not a redesign |
| 2026-08-13 | **Trend-signal layer has no `trends.enabled` config flag — running `--stage 3` is itself the sole opt-in.** Shipped with the flag first (mirroring curation-signal's rollout pattern), briefly had a bug where the flag was read but never checked, then removed the flag entirely per user decision: Stage 3 is already explicit-only (not in a full pipeline run), so a second toggle on top of that is redundant. Whatever a `--stage 3` run finds now always surfaces in the next `python -m aarva.review`. | User's stated reasoning: "running stage 3 manually is enough of a toggle for it to be on. i don't need to have another one." | Yes — re-adding a config gate is a small, additive change if ever wanted |
| 2026-08-15 | **Trend adds via `tNa`/`tNi` in review now insert as `review_status='approved'`, not `'proposed'`.** Real production incident: 2 trend-added pieces vanished from a published edition because Stage 7's rebuild-refill deletes every `edition_pieces` row that isn't `'approved'` when refilling slots after a normal iterative-review pass — the trend adds were still `'proposed'` and never got picked up by a second review round. `add_article_to_todays_edition` (`aarva/services/edition_ops.py`) gained a `review_status` parameter (default `'proposed'`, unchanged for `aarva.search`/`aarva.ingest_url`); `_apply_trend_decisions` passes `'approved'` explicitly. See `docs/session_plan_trend_adds_auto_approve.md`. | A trend add is the operator's own explicit approval gesture (they already saw the match before typing `tNa`) — unlike a normal Stage 7 slot-pick, which is the pipeline's proposal that review exists to judge. Propose-then-approve was a redundant second gate that silently dropped the piece if skipped. | Yes — `review_status` parameter is additive; reverting the trend-add call site to the default `'proposed'` restores the old (buggy) behavior |
| 2026-08-20 | **Trend-signal v2's reverse-lookup scope narrowed by real rule-6a verification: Reddit dropped entirely, Bluesky reverse-lookup deferred.** `docs/session_plan_trend_signal_v2.md` assumed Reddit's unauthenticated `.json` URL-search still worked with a proper User-Agent, and Bluesky's `searchPosts` was public/unauthenticated. Both were live-tested: Reddit returns 403 (OAuth closed Nov 2025, unauthenticated `.json` shut down May 30 2026 — confirmed dead, no workaround); Bluesky `searchPosts` also now requires authentication as of mid-2026 (`getTrends`, the forward-source endpoint, is unaffected and still fully public). Reddit removed from the design outright. Bluesky reverse-lookup deferred — the user is setting up a dedicated Bluesky bot account + app password (Claude Code can't complete Bluesky's signup CAPTCHA + email verification itself) and will revisit once that account exists. | Verifying external dependencies for real, not from training memory or the spec author's assumptions, caught two genuine dead ends before any code was written against them — exactly what AGENTS.md rule 6a exists to prevent. | Yes — both are additive-later: Reddit could theoretically return via a paid API tier (not pursued, same reasoning that already rejected X/Twitter's $200/mo tier); Bluesky reverse-lookup is a small wiring addition once the bot account + app password exist |
| 2026-08-20 | **No new `pipeline.yaml` toggle flags for trend-signal v2's new sources or reverse lookup.** Bluesky and HN join `trend_sources.yaml` using the same per-source `enabled: true/false` field Google Trends regions already use — no parallel `trends.bluesky_enabled`/`trends.hn_enabled` flags. Reverse lookup folds into the same `--stage 3` invocation as the forward crawl+match, no separate flag or stage. | Direct continuation of the 2026-08-13 decision that removed the original blanket `trends.enabled` flag: "running stage 3 manually is enough of a toggle for it to be on. i don't need to have another one." A per-source flag in `trend_sources.yaml` already exists for exactly this purpose — a second `pipeline.yaml` flag on top would be the identical redundant-toggle mistake already corrected once. | Yes — a config flag is a small additive change if ever wanted later |

### Tech / infrastructure

| When | Decision | Rationale | Reversible? |
|---|---|---|---|
| Pre-session | Project lives at `~/Projects/Aarva/` (NOT `~/Documents/...`) | The repo was originally under `~/Documents/Claude/Projects/Curio v2/`, but macOS Documents-folder permissions + iCloud sync interfered with launchd-driven automation (files became aliases; scripts couldn't be invoked). Moving outside Documents bypassed both. The rename from "Curio" to "Aarva" landed alongside the move. | Yes — move back any time, but Documents-folder permission issues would return |
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
| 2026-06-30 | **Embeddings: local BGE-base → Vertex AI `gemini-embedding-001` (768-dim Matryoshka).** Production server (Render Starter, 512 MB) OOM'd when `LocalEmbeddingClient` lazy-loaded PyTorch + the BGE model on the first prompt. Switched the embedding stack to Vertex AI's API-based Gemini Embedding model — same Vertex AI ADC project as the LLM + TTS. New `VertexAIEmbeddingClient` in `aarva/clients/embedding.py` with asymmetric `RETRIEVAL_QUERY` / `RETRIEVAL_DOCUMENT` task-type plumbing (listener prompts vs. indexed content). Native output is 3072-dim; truncated to 768 via Matryoshka to keep the existing DB blob shape (no layout migration). All ~5,100 articles + the crosscut catalog re-embedded via `scripts/reembed_to_vertex_ai.py`. `sentence-transformers` + PyTorch dropped from runtime `requirements.txt` (LocalEmbeddingClient stays in tree as offline-dev fallback). Two other candidate fixes considered + rejected: Render Standard upgrade ($25/mo for 2 GB, doesn't help if more in-memory ML lands later); HuggingFace Inference API for runtime queries only (free-tier credits are tight, added latency, new vendor dep). | Aligns with the standing "Gemini for all non-coding LLM" preference; removes PyTorch from the production image (cold-starts faster, ~700 MB lighter); cost is fractions of a cent per call + < $1 one-time re-embed. | Yes — `embedding:` block in pipeline.yaml is provider-switchable; `local` config is kept commented out in the file as the rollback path |
| 2026-06-30 | ~~**Gemini auth (LLM + embedding): ADC/Vertex → api_key/AI Studio.**~~ Superseded same day — see next row. Reasoning at the time: `/create` was crashing on `DefaultCredentialsError` (Render isn't a GCP env, ADC never bootstrapped), and flipping to api_key would fix it without adding a GCP SA key on Render. Kept the class rename (`VertexAIEmbeddingClient` → `GeminiEmbeddingClient`) and the api_key code path in `aarva/clients/embedding.py` — those are permanent improvements even after the config flipped back. | — | — |
| 2026-06-30 | **Reverted the same-day auth flip back to ADC/Vertex on both `llm:` and `embedding:` blocks.** The api_key analysis addressed only reason #1 of the 2026-06-13 mandate (data-residency, covered by paid-tier no-train terms) and missed reason #2 — AI Studio's per-project spending caps, which the daily-pipeline volume plus /create traffic would hit. Vertex on the gibran.ai GCP project doesn't have that ceiling. Correct fix for the Render side is Option A from the original recommendation: provision a GCP service-account key as a Render Secret File and set `GOOGLE_APPLICATION_CREDENTIALS=/etc/secrets/gcp-sa.json`. See docs/deploy.md for the runbook. | Restores the 2026-06-13 architecture: one auth path, same code, same GCP project across laptop + Render. The api_key code path remains available as a one-YAML-line fallback — useful for offline experiments or emergencies but not the production path. | Yes — `auth_mode: api_key` on either block is still supported; both `gcp_project` + `gcp_location` and the `GEMINI_API_KEY` env-var lookup remain intact. |
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

### Process / AI-agent workflow

| When | Decision | Rationale | Reversible? |
|---|---|---|---|
| 2026-07-17 | **Claude Code git protocol carve-out (AGENTS.md rule 20a).** Claude Code commits, pushes, and opens PRs directly (including a `Co-Authored-By: Claude Sonnet 5` trailer) — the conversation itself is the sign-off, not a separate "say commit"/"say push" gate. An explicit "merge it" is still required before merging. Cowork continues to follow rule 20 (sign-off gates) and rule 22 (no AI attribution) exactly as written. | Matches how Claude Code has actually operated on this repo all along; the user confirmed keeping it rather than retrofitting the stricter Cowork-oriented protocol onto Claude Code sessions. | Yes — revert rule 20a in AGENTS.md to fold Claude Code back under rule 20/22 |

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
- ~~**Crosscut listener-notification mechanism.**~~ Resolved
  2026-06-29: email primary (Resend, currently stubbed until the
  API key lands on Render) + a `/build/<job_id>` status page the
  listener can keep open for live progress. PWA push deferred —
  iOS Safari needs the app installed to home screen first, and
  the v1 surface ships without a service worker.
- ~~**Multi-crosscut per day.**~~ Resolved 2026-06-29: the
  partial UNIQUE index on `editions(edition_date, edition_type)`
  was narrowed so only `edition_type='daily'` keeps the
  one-per-day singleton. Crosscut now permits one
  pipeline-generated row per day plus any number of listener-
  generated rows. See decision log row 2026-06-29.
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
  21), branch+PR for anything non-trivial. Explicit sign-off (rule 20)
  applies to Cowork; Claude Code follows its own carve-out (rule 20a)
  — commit+push+PR directly, conversation is the sign-off, explicit
  "merge it" before merging.

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

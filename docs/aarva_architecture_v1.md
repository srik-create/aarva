# Aarva — v0.1 Architecture spec

This is the build-ready architecture for Aarva v0.1. It is consciously narrower than the kickoff doc's full architecture — v0.1 ships the daily-edition pipeline end-to-end, deferring personalisation, the wire branch, pairings, the Discover UI, and the native app to v0.2+.

## Design commitments

The full editorial design is in `aarva_kickoff.docx`. The v0.1 build holds to these specific commitments from it:

1. **Modular by stage.** Each pipeline stage is an independent module with a defined interface. Swapping an implementation (e.g., a different LLM provider, a different TTS engine, a different consolidation algorithm) is a single-file change.
2. **Configuration over code.** All editorial parameters — publication allowlist, JTBD-conditional matching weights, prompt text, slot structure, basket size, tonal thresholds, TTS provider — live in human-readable YAML config files. No code changes needed to tune behaviour.
3. **State is queryable.** Articles, scores, fingerprints, edition history are all in SQLite. Every decision the pipeline makes is logged and inspectable.
4. **Stages run independently.** Each stage can be invoked alone for testing/debugging. The pipeline orchestrator wires them together but doesn't hide them.
5. **Provider-agnostic interfaces.** `LLMClient` and `TTSClient` abstractions mean Claude Code, Anthropic API, OpenAI, ElevenLabs, Piper, F5-TTS are all swap-by-config.

## What's in v0.1, what's deferred

**In v0.1:**
- Ingestion of ~15–20 publications from the allowlist (the open / free-RSS ones — Group A, B, C, E, partial D)
- Stage 1.5 consolidation (naïve headline-similarity dedup)
- Stage 2 hard filters (length, listicle, allowlist)
- Combined Stage 4 + 5 + 6 LLM scoring (one prompt per article producing tonal scores + classification + fingerprint)
- Stage 7 edition assembly (slot-structured, no personalisation, same edition for all listeners)
- Stage 8a + 8b LLM (hooks + why-now contextualisation)
- Stage 9 TTS (Piper for v0.1, provider-agnostic)
- Output: web page (responsive HTML, uses v2 prototype visual identity) + podcast RSS feed
- Daily run via `python -m aarva.daily` or cron

**Deferred to v0.2+:**
- Personalisation (4-axis model, JTBD-conditional weights at runtime)
- Wire branch (breaking-news subsystem, briefing slot)
- Pairings (detection + UI)
- Filter-bubble protections (topic cap, viewpoint balance, serendipity slot)
- Multi-axis similarity (Q29)
- Discover screen
- Native iOS/Android app
- Three-basket architecture (just one editorial basket in v0.1)
- Paywalled-pubs free-crumb handling (Group G)
- Group H smart-escape pubs (added in v0.2)
- Drift detection
- Cold-start exploration ramp

## File and directory layout

```
aarva/
├── README.md
├── config/
│   ├── publications.yaml          # Allowlist with per-pub fields
│   ├── pipeline.yaml              # Stage parameters, thresholds, slot structure
│   ├── prompts.yaml               # Versioned prompt text for each LLM stage
│   ├── matching_weights.yaml      # 4×6 JTBD-conditional weight matrix
│   └── voices.yaml                # TTS voice configuration
├── sources/
│   ├── __init__.py
│   ├── rss.py                     # RSS fetcher
│   └── article_extractor.py       # Full-text extraction (readability-like)
├── stages/
│   ├── __init__.py
│   ├── stage_1_ingest.py
│   ├── stage_1_5_consolidate.py
│   ├── stage_2_filter.py
│   ├── stage_4_5_6_score.py       # Combined tonal + classification + fingerprint
│   ├── stage_7_assemble.py
│   ├── stage_8_hook_context.py
│   └── stage_9_tts.py
├── clients/
│   ├── __init__.py
│   ├── llm.py                     # Abstract + concrete: Claude Code, Anthropic API
│   └── tts.py                     # Abstract + concrete: Piper, F5-TTS, ElevenLabs, OpenAI
├── output/
│   ├── __init__.py
│   ├── web_renderer.py            # Generates HTML edition page
│   └── rss_feed.py                # Generates podcast RSS feed
├── data/                          # SQLite database + audio files (gitignored)
│   ├── aarva.db
│   └── audio/
│       └── YYYY-MM-DD/
├── daily.py                       # Top-level orchestrator
├── tests/
│   ├── test_consolidate.py
│   ├── test_filter.py
│   └── ...
└── scripts/
    ├── run_calibration.py         # Run Stage 4 against calibration set v1
    ├── try_voice.py               # Generate a sample audio with each TTS voice
    └── seed_publications.py       # Load publications.yaml into DB
```

## Data model (SQLite)

```sql
-- One row per article ever ingested
CREATE TABLE articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_url TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    byline TEXT,
    publication_id INTEGER REFERENCES publications(id),
    published_date DATETIME,
    ingested_date DATETIME DEFAULT CURRENT_TIMESTAMP,
    word_count INTEGER,
    full_text TEXT,
    excerpt TEXT,                   -- first paragraph for consolidation
    status TEXT                     -- 'ingested', 'filtered_out', 'scored', 'in_basket', 'in_edition'
);

CREATE TABLE publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    rss_url TEXT,
    homepage TEXT,
    tier TEXT,                      -- A/B/C/D/E/F/G/H per allowlist
    enabled INTEGER DEFAULT 1,
    licence_status TEXT,
    notes TEXT
);

-- Stage 1.5 clusters
CREATE TABLE event_clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    centroid_embedding BLOB,
    created_date DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE article_clusters (
    article_id INTEGER REFERENCES articles(id),
    cluster_id INTEGER REFERENCES event_clusters(id),
    is_best_version INTEGER DEFAULT 0,
    similarity_to_centroid REAL,
    PRIMARY KEY (article_id, cluster_id)
);

-- Stage 4 + 5 + 6 output
CREATE TABLE article_scores (
    article_id INTEGER PRIMARY KEY REFERENCES articles(id),
    rigour REAL,
    rigour_rationale TEXT,
    posture REAL,
    posture_rationale TEXT,
    self_implication REAL,
    self_implication_rationale TEXT,
    verdict TEXT,                   -- 'PASS' | 'FAIL'
    ranking_score REAL,
    lens TEXT,                      -- 'future_gazing' | 'humans_and_humanity' | 'behind_the_news' | 'unclassified'
    pillar TEXT,
    jtbd_primary TEXT,
    jtbd_secondary TEXT,
    topic_recency_sensitivity REAL,
    fingerprint_json TEXT,          -- full 6-dim fingerprint as JSON
    scored_date DATETIME DEFAULT CURRENT_TIMESTAMP,
    prompt_version TEXT
);

-- Editions (one per day)
CREATE TABLE editions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    edition_date DATE UNIQUE NOT NULL,
    published_date DATETIME DEFAULT CURRENT_TIMESTAMP,
    web_url TEXT,
    rss_episode_url TEXT
);

CREATE TABLE edition_pieces (
    edition_id INTEGER REFERENCES editions(id),
    article_id INTEGER REFERENCES articles(id),
    slot TEXT,                      -- 'deep_feature' | 'lens_card_future' | 'lens_card_humans' | 'lens_card_behind' | 'curiosity' | 'smart_escape' | 'delight'
    position INTEGER,
    hook TEXT,
    contextualisation TEXT,
    audio_url TEXT,
    duration_seconds INTEGER,
    PRIMARY KEY (edition_id, article_id)
);

-- Run log: one row per pipeline invocation
CREATE TABLE pipeline_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    finished_at DATETIME,
    status TEXT,                    -- 'running' | 'success' | 'failed'
    articles_ingested INTEGER,
    articles_after_consolidation INTEGER,
    articles_after_filter INTEGER,
    articles_passed_tonal INTEGER,
    edition_id INTEGER REFERENCES editions(id),
    error_message TEXT
);
```

## Stage interfaces

Each stage exposes a single function with a typed signature:

```python
# stage_1_ingest.py
def ingest_today(config: PipelineConfig, db: Database) -> list[Article]:
    """Pull RSS from all enabled publications. Return new articles ingested today."""

# stage_1_5_consolidate.py
def consolidate(articles: list[Article], config: PipelineConfig) -> list[Article]:
    """Cluster by event, pick best-version per cluster, return surviving articles."""

# stage_2_filter.py
def filter_hard(articles: list[Article], config: PipelineConfig) -> list[Article]:
    """Apply hard filters: length, listicle detection, allowlist. Return surviving."""

# stage_4_5_6_score.py
def score_all(articles: list[Article], llm: LLMClient, config: PipelineConfig) -> list[ScoredArticle]:
    """Run combined LLM prompt on each article. Return scored articles."""

# stage_7_assemble.py
def assemble_edition(scored: list[ScoredArticle], config: PipelineConfig) -> Edition:
    """Slot-fill the daily edition under constraints. Return Edition object."""

# stage_8_hook_context.py
def generate_hooks_and_context(edition: Edition, llm: LLMClient, config: PipelineConfig) -> Edition:
    """Generate hook (8a) and why-now contextualisation (8b) for each piece in the edition."""

# stage_9_tts.py
def generate_audio(edition: Edition, tts: TTSClient, config: PipelineConfig) -> Edition:
    """Generate audio file for each piece. Return Edition with audio URLs."""
```

Top-level orchestrator (`daily.py`):

```python
def main():
    config = load_config()
    db = Database(config.db_path)
    llm = build_llm_client(config.llm)
    tts = build_tts_client(config.tts)

    run = db.start_pipeline_run()
    try:
        ingested = stage_1_ingest.ingest_today(config, db)
        consolidated = stage_1_5_consolidate.consolidate(ingested, config)
        filtered = stage_2_filter.filter_hard(consolidated, config)
        scored = stage_4_5_6_score.score_all(filtered, llm, config)
        edition = stage_7_assemble.assemble_edition(scored, config)
        edition = stage_8_hook_context.generate_hooks_and_context(edition, llm, config)
        edition = stage_9_tts.generate_audio(edition, tts, config)
        web_renderer.render_edition(edition, config)
        rss_feed.update_feed(edition, config)
        db.finish_pipeline_run(run, success=True, edition=edition)
    except Exception as e:
        db.finish_pipeline_run(run, success=False, error=str(e))
        raise
```

## Provider abstractions

### LLMClient

```python
class LLMClient(ABC):
    @abstractmethod
    def complete(self, prompt: str, expect_json: bool = True) -> str | dict:
        ...

class ClaudeCodeClient(LLMClient):
    """Invokes `claude -p <prompt>` as subprocess. Uses user's subscription."""

class AnthropicAPIClient(LLMClient):
    """Standard API client. Used if subscription path hits limits or for parallelism."""
```

Default for v0.1: ClaudeCodeClient. Falls back to AnthropicAPIClient by config.

### TTSClient

```python
class TTSClient(ABC):
    @abstractmethod
    def synthesize(self, text: str, voice_id: str, output_path: Path) -> AudioFile:
        ...

class PiperClient(TTSClient):
    """Local Piper binary. Runs on CPU. Voice files downloaded once."""

class F5TTSClient(TTSClient):
    """Local F5-TTS. Higher quality, requires Python + PyTorch + Apple Silicon."""

class ElevenLabsClient(TTSClient):
    """Cloud API. Best quality. Costs money."""

class OpenAITTSClient(TTSClient):
    """Cloud API. Good quality, cheaper than ElevenLabs."""
```

Default for v0.1: PiperClient. Swap by editing `config/voices.yaml`.

## Output formats

### Web page (`output/web/edition-YYYY-MM-DD.html`)

Self-contained HTML page using the `aarva_prototype_v2.html` visual identity. Renders:
- Edition date + volume
- Each piece as a card: hook (italic question), title, byline, why-now contextualisation, audio player, "go to source" link
- Pieces grouped by slot (deep feature first, then lens cards, then curiosity/escape)
- Audio plays inline via HTML5 `<audio>` element

### Podcast RSS feed (`output/feed.xml`)

Standard podcast RSS 2.0 with iTunes namespace. Each edition is one episode. Episode title = "Aarva — [Date]". Each piece is a chapter in the episode (using Podcast Index chapters extension) so listeners can skip between pieces.

User adds the feed URL to their podcast app once; new editions appear automatically.

## Configuration files

### `config/publications.yaml`

```yaml
- name: Aeon
  rss_url: https://aeon.co/feed.rss
  tier: A
  enabled: true
  licence_status: cc_licensed
  notes: Long-form essays & ideas
  ingestion_method: rss

- name: ProPublica
  rss_url: https://www.propublica.org/feeds/propublica/main
  tier: B
  enabled: true
  licence_status: republishable
  notes: Investigative; explicit republish licence
  ingestion_method: rss

# ... (15-20 publications for v0.1)
```

### `config/pipeline.yaml`

```yaml
ingestion:
  schedule: daily_at_05_00_local
  max_articles_per_publication: 30

consolidation:
  similarity_threshold: 0.85
  max_per_publication: 3
  embedding_model: text-embedding-3-small

filters:
  word_floor: 600
  listicle_keywords: ['top 10', 'best of', 'X things']
  allowlist_required: true

scoring:
  combined_prompt_version: v1.0
  llm_temperature: 0.1
  pass_threshold:
    rigour: 0.5
    posture: 0.5

assembly:
  slots:
    deep_feature: 1
    lens_card_future: 1
    lens_card_humans: 1
    lens_card_behind: 1
    curiosity: 1
    smart_escape: 2     # bumped — lineups skewed cerebral; gives reviewer two
                        # light options to keep or drop
    delight: 1          # post-v0.1 addition — see "Post-v0.1 changes" below
  length_distribution:
    short: 0.30
    medium: 0.50
    long: 0.20
  trending_cap: 0.50
  max_per_publication_per_edition: 1
  publication_cooldown_editions: 5    # see Post-v0.1 changes

output:
  web_dir: output/web/
  audio_dir: output/audio/
  rss_path: output/feed.xml
  public_url_base: https://example.com/aarva/
```

### `config/matching_weights.yaml`

The 4×6 JTBD-conditional weight matrix (locked in Q4, but irrelevant for v0.1 since no personalisation runs in v0.1 — included here so v0.2 can switch on personalisation without architecture change).

### `config/prompts.yaml`

Versioned prompt text — Stage 4+5+6 combined prompt, Stage 8a, Stage 8b. Drawn from `aarva_prompts_v1.md`. Versioning lets us A/B test prompt changes against the calibration set without losing the old version.

## Calibration loop

A script `scripts/run_calibration.py` runs the Stage 4+5+6 prompt against the 32 pieces in `aarva_calibration_set_v1.md`, compares the verdict to the user's labels, and reports agreement rate + per-piece disagreement detail. We iterate the prompt until agreement ≥ 85%.

The script also takes a `--prompt-version` flag so we can run old and new versions and compare.

## Build sequence (10 days realistic, 7 days aggressive)

| Day | Deliverable | Verifiable by |
| --- | --- | --- |
| 1 | Project scaffold, config files, DB schema, RSS ingestion working for 5 publications | `python -m aarva.daily --stage 1` ingests articles to DB |
| 2 | Stage 1.5 consolidation + Stage 2 filters | `--stage 2` produces filtered candidate pool |
| 3 | Combined Stage 4+5+6 prompt working against calibration set | `python scripts/run_calibration.py` reports agreement rate |
| 4 | Stage 7 edition assembly | `--stage 7` produces well-formed Edition object |
| 5 | Stage 8a + 8b LLM | `--stage 8` adds hooks + contextualisation to edition |
| 6 | Piper TTS integration | `python scripts/try_voice.py --piper` produces audio file |
| 7 | Web page renderer + RSS feed generator | Daily edition viewable on `file://` + podcast feed validates |
| 8 | End-to-end run | `python -m aarva.daily` produces a real edition |
| 9 | Calibration iteration on Stage 4 prompt | Agreement rate ≥ 85% |
| 10 | Polish, error handling, simple monitoring, README | Pipeline survives a week of daily runs without intervention |

## What the user does

- **Install Claude Code** if not already installed: https://docs.claude.com/claude-code
- **Install Piper** (instructions in v0.1 README) — single binary download
- **Set up a cron job or scheduled task** that runs `python -m aarva.daily` once a day
- **Host the output directory** somewhere with a public URL — for v0.1 this can be:
  - GitHub Pages (free, simple)
  - Cloudflare R2 or Pages (free tier, simple)
  - A small VPS the user owns
  - Even just a Dropbox / iCloud public folder for proof of concept (less reliable but free)
- **Subscribe to the podcast feed URL** in their podcast app

## Known v0.1 limitations / honest expectations

- Single edition for all listeners (no personalisation yet — that's v0.2).
- TTS quality is whatever Piper produces. Acceptable for proof, may want upgrade.
- Stage 1.5 consolidation is naïve (headline similarity only) — may keep too many similar pieces or collapse legitimately distinct framings. Tunable, but rough at first.
- Stage 4 calibration is ongoing — first runs may have wider disagreement with the user's labels. Iteration is built into the loop.
- No mobile app — uses podcast app for mobile.
- Paywalled-pubs free-crumb fishing not implemented yet.
- No filter-bubble protection yet (single shared edition means it's a non-issue at v0.1; reintroduced when personalisation comes in v0.2).
- No drift detection. We'll know prompts are drifting if the calibration agreement rate falls; v0.1 detection is "run the calibration script monthly."

## Next architectural decisions to make during build

These are small enough not to block, but will get raised as we hit them:

- Where to host the public output (the listener needs a stable URL to subscribe).
- Where to host the audio files (could be same place as web page, or a separate bucket).
- Whether to commit the SQLite DB to git or keep it local (probably local, since it'll grow).
- Whether to use uv, pip, or Poetry for Python dependencies (I'll default to uv unless you prefer otherwise).
- Whether to wrap daily.py in a simple Flask app for ad-hoc preview, or keep it strictly CLI for v0.1 (I'd say strictly CLI).

## Post-v0.1 changes (live)

This section is the running log of behaviour that's shipped since the
original v0.1 spec above was written. Read it as an addendum, not a
replacement.

### Crosscut episodes — a second daily episode type

Beyond the daily edition, Aarva now publishes a **Crosscut**: a daily
paired-listening episode that puts two rigorous articles on the same
topic but with different angles back-to-back, stitched together by
editorial intro / bridge / outro. Not a debate format — a "multiple
angles" format. The original spec deferred pairings to v0.2; the
pairing implementation we shipped is narrower than that (one pair per
day, no UI) but it is a real second pipeline.

Implementation summary:

- New module `aarva/stages/stage_crosscut.py` with three phases:
  pair detection, episode-script generation, and TTS composition.
- New persistence: `crosscut_pair_candidates` table (one row per
  detected pair, with topic_label, angle_a/b, connection_summary,
  connection_score, divergence_score, selected_at, edition_id,
  superseded_at).
- New entry-point flags in `daily.py`:
  - `--crosscut-detect [--require-fresh]` runs pair detection and
    persists a ~10-pair longlist for review.
  - `--crosscut-build` generates intro/bridges/outro from the
    user-selected pair and persists an edition with edition_type='crosscut'.
  - `--crosscut-tts` synthesises the three-voice episode (host
    voice + one voice per article).
- New review CLI: `python -m aarva.crosscut` shows the longlist and
  takes a selection. Re-running `--crosscut-detect` regenerates fresh
  pairs (no exact pair is re-shown; same articles may recur in new
  pairings unless `--require-fresh` is set).
- Editions table gained `edition_type` ('daily' | 'crosscut'),
  `topic_label`, `intro_text`, `outro_text` columns and a composite
  UNIQUE (edition_date, edition_type).
- `edition_pieces` gained `bridge_text` for the cross-piece bridge.
- Crosscut TTS uses **three distinct Gemini voices**: Sulafat (host /
  intro / bridges / outro), Charon (article A), Vindemiatrix (article B).
- Crosscut narration uses the **full article body**, not an extracted
  excerpt — listeners hear the whole piece.
- Publish path: Stage 10 audio conversion and RSS feed regeneration
  both pick up crosscut episodes automatically. The RSS feed
  interleaves daily and crosscut items by date; crosscut items render
  as one item per episode (not one-per-piece) titled
  "Crosscut: {topic}".

Daily and crosscut share article pool and the rigour filter; crosscut
applies a higher floor (`ranking_score ≥ 0.7`).

### New JTBD: `delight`

Added alongside the existing four (`keep_up_to_date`, `keep_ahead`,
`curiosity`, `smart_escape`). `delight` is for genuinely light,
playful, fun pieces — humour, oddities, surprising joys, wit, viral
curiosities. Distinct from `smart_escape` (which is restorative /
gentle / "settle in"). Stage 7 carries a `delight: 1` slot that
renders as "A Bit of Delight" in the web output and sits at the end of
the edition.

### Cross-edition publication-rotation cooldown

Stage 7 now applies a decaying penalty (-0.12, -0.08, -0.05, -0.03,
-0.02) to the ranking score of any publication that appeared in one
of the last 5 daily editions. This is the rotation force that
prevents the "same 4-5 publications every day" pattern when a few
pubs dominate the high-score tier of the candidate pool. Tunable via
`assembly.publication_cooldown_editions` in `pipeline.yaml` (set to 0
to disable).

### Stage 1.5 cluster persistence

The original spec stored Stage 1.5 cluster decisions only by marking
duplicates `status='filtered_out'`. It never populated
`event_clusters` or `article_clusters` — which meant Stage 7's
within-edition cluster cap (`max_per_cluster_per_edition`) was a
no-op. Stage 1.5 now persists multi-article clusters idempotently:
re-runs delete old memberships for the articles being re-clustered
and garbage-collect orphaned clusters before inserting fresh.

### Gemini-first LLM and TTS

The original spec named Claude Code (subprocess) as the default LLM
backend. Current default is **Gemini API** (`gemini-2.5-flash`) for
all non-coding LLM calls and for TTS. The `LLMClient` abstraction is
still used; `GeminiAPIClient` is now the production path. Claude Code
remains a swap-by-config option. TTS uses `GeminiTTSClient`.

The TTS `style_prompt` carries an explicit **"narrate only in
English"** instruction — Gemini TTS otherwise occasionally renders
foreign names or quoted phrases in their native language.

### Gemini token limits

`GeminiAPIClient.DEFAULT_MAX_TOKENS` is **16384** (up from 4096) and
the client now detects `finish_reason == MAX_TOKENS` and raises
immediately rather than silently retrying on a partial JSON response.

### Review workflow

Daily edition review CLI (`python -m aarva.review`) supports
`extra_slots` / `dropped_slots` / `slot_biases` overrides persisted on
the editions row. Aliases: `feature`, `future`, `humans`, `behind`,
`curiosity`, `escape`, `delight`. Plus filter aliases `+pub:NAME`
(case-insensitive substring match on publication name) and
`+topic:KEYWORD` (case-insensitive match against article title) for
ad-hoc constrained slots.

### Bonus episodes (user-picked ad-hoc publishes)

A user can publish any article from the pool as a standalone bonus
episode via `python -m aarva.publish_articles <id> [<id> ...]` or via
the search CLI's `--publish` flag. Bonus episodes:

- Live in `editions` with `edition_type='bonus'` and (for the web app)
  `user_id` set to the publisher.
- Get the full Aarva editorial treatment: Stage 8 generates hook +
  context + show_notes; Stage 9 narrates the hook + context + article
  body.
- Tag as `itunes:episodeType="bonus"` in the RSS feed so Apple /
  Spotify shelve them as side content alongside the daily series.
- Mark the source article `status='in_edition'` so tomorrow's daily
  selection won't double-pick it.

The `editions` table's UNIQUE constraint became partial as a result:
one `daily` + one `crosscut` per date are still enforced, but
`bonus` rows are unconstrained (multiple per date allowed).

### Search CLI

`python -m aarva.search` supports lexical (substring on title +
excerpt by default, also full-text with `--full-text`) and semantic
(embedding cosine similarity, `--semantic`) search modes, plus
structured filters (`--pub`, `--lens`, `--jtbd`, `--status`,
`--since`, `--limit`, `--json`). The `--publish` flag forwards the
top results into `aarva.publish_articles` after confirmation.

### Article re-tagging utility

`scripts/retag_jtbd.py` re-classifies the JTBD field on already-
scored articles using the current prompt. Useful after a prompt
update; sends each article's full text + fingerprint to Gemini and
updates `article_scores.jtbd_primary` + `jtbd_secondary` in place.
Has `--dry-run`, `--pub`, `--limit`, `--all` flags.

## App-layer foundation (Phase A)

The pipeline above operates without a notion of users. The web app
layers per-user state on top — see new tables and modules below.

### Schema additions

```sql
-- People consuming Aarva via the web app
users (id, email UNIQUE, name, settings_json, created_at,
       last_login_at, is_admin)

-- Persistent login tokens (cookie value)
user_sessions (token PK, user_id, created_at, expires_at,
               revoked_at, user_agent, ip)

-- Single-use, short-lived auth tokens (magic-link flow)
magic_link_tokens (token PK, email, created_at, expires_at,
                   consumed_at, ip)

-- Per-user interactions with articles. Drives the dismiss-from-feed
-- feature today; will drive per-user taste centroids in Phase B.
user_actions (id, user_id, article_id,
              action IN ('dismissed', 'liked', 'disliked',
                         'listened', 'completed', 'shared'),
              created_at, metadata_json)

-- Durable background-job queue
jobs (id, kind, payload_json,
      status IN ('pending', 'running', 'completed',
                 'failed', 'cancelled'),
      created_at, started_at, finished_at,
      result_json, error_message, user_id, progress)

-- editions gained a nullable user_id column:
--   NULL = global (daily, crosscut, shared bonus)
--   set  = private to that user (their own ad-hoc bonus picks)
```

### Service layer (`aarva/services/`)

The boundary between web routes and the rest of the system.
Pure-Python functions taking a Database + arguments; return plain
data; raise `aarva.exceptions.*` rather than printing or
exiting.

- **`services/users.py`** — magic-link request/verify, session
  minting, session lookup. 20-min TTL on links, 30-day on sessions.
- **`services/actions.py`** — record/list dismissals + likes/listens/
  completes/shares with optional metadata.
- **`services/feeds.py`** — `get_user_feed(user_id, since_days)`
  returns the personalised feed: shared daily (minus dismissals) +
  shared crosscut (minus crosscuts whose source articles are
  dismissed) + the user's own bonus picks.
- **`services/articles.py`** — `get_article`, `search_articles`.
- **`services/editions.py`** — `list_editions`,
  `publish_bonus_article` (enqueues a job, returns the Job row).
- **`services/jobs.py`** — durable queue: `enqueue`, `run_once`,
  `WorkerThread` (FastAPI starts this at boot). Handlers registered
  via `register_handler(kind, fn)`. Atomic claim prevents
  double-runs across workers.
- **`services/queries.py`** — shared SQL queries used by the RSS
  feed, web renderer, and feed service. Centralises the
  edition_pieces+articles+publications JOIN patterns that were
  previously duplicated.

### Auth model

Magic-link only (no passwords). Flow:
1. Anon user submits email → `request_magic_link(email)` stores a
   short-lived token; caller emails the link.
2. User clicks → `verify_magic_link(token)` consumes the token,
   creates/fetches the User, mints a session.
3. Browser stores the session token as an HttpOnly cookie.
   `get_user_for_session(token)` resolves it on each request.

### Background-job pattern

TTS takes minutes per piece. The synchronous pipeline path is fine
for CLI invocation but blocks a web request unacceptably. The
`jobs` table + `WorkerThread` model decouples request from work:

1. Route receives e.g. `POST /api/publish_article/123`.
2. Service `publish_bonus_article(user_id, article_id)` enqueues a
   `publish_bonus_article` job and returns the Job id (HTTP 202).
3. Frontend polls `GET /api/jobs/{id}` until status='completed' or
   'failed'. Result includes the new edition_id.

The worker thread is an in-process design (no Redis / Celery
dependency). When you move to cloud, swap the worker for a
Lambda + SQS trigger or a Celery worker — the `jobs` table stays.

## Cross-cutting infrastructure changes

### Exception hierarchy (`aarva.exceptions`)

```
AarvaError                       — base; catch this in web routes
├── ConfigError                  — config missing / invalid / env var
├── DatabaseError                — connection / query failure
├── ExternalServiceError         — generic upstream failure
│   ├── LLMError                 — Gemini / Claude API failure
│   ├── TTSError                 — Gemini TTS or silence-retry exhaustion
│   └── EmbeddingError           — model load / call failure
├── PipelineError                — a stage failed during its run
└── NotFoundError                — article / edition / user / job missing
```

Suggested HTTP mappings documented in the module's docstring.

### Env-var overlay on config

`load_pipeline_config()` reads `aarva/config/pipeline.yaml`, then
applies environment variables as overrides. Recognised vars:
`AARVA_DB_PATH`, `AARVA_AUDIO_DIR`, `AARVA_WEB_DIR`,
`AARVA_RSS_FEED_PATH`, `AARVA_PUBLIC_URL_BASE`, `AARVA_FEED_EMAIL`,
`AARVA_LLM_PROVIDER`, `AARVA_LLM_MODEL`, `AARVA_EMBEDDING_PROVIDER`,
`AARVA_EMBEDDING_MODEL`, `AARVA_LOG_LEVEL`. Secrets via
`AARVA_GEMINI_API_KEY`, `AARVA_OPENAI_API_KEY` (read directly by
the clients, never written to YAML).

### Explicit dependency injection for stages

Every public stage entry point now accepts the LLM/TTS/Embedding
clients as optional keyword arguments. CLI orchestrator (`daily.py`)
doesn't pass them — backwards compat. Web routes / job handlers
build clients once at app init and inject, preserving rate-limiter
state across requests:

```python
stage_4_5_6_score.score_all(config, db, llm=shared_llm)
stage_8_hook_context.generate_for_edition(config, db, llm=shared_llm)
stage_9_tts.generate_for_edition(config, db, tts=shared_tts, llm=shared_llm)
stage_1_5_consolidate.consolidate(config, db, embedding_client=shared_emb)
stage_crosscut.detect_pair_candidates(config, db, llm=shared_llm)
stage_crosscut.build_episode_script(config, db, llm=shared_llm)
stage_crosscut.synthesize_crosscut_episode(config, db, tts=shared_tts)
```

### Shared utility modules

- **`aarva/cli_utils.py`** — single home for ANSI color helpers
  (BOLD/DIM/RED/GREEN/YELLOW/BLUE/CYAN). Previously duplicated
  across 4-5 CLI modules.
- **`aarva/prompts.py`** — single `load_prompts()` + `render()`
  with LRU caching. Previously duplicated in two stages.

### Standing rules for AI coding agents

A new top-level `AGENTS.md` captures the principles for how Claude
or any other coding agent should operate on this repo: brevity,
pre-approval for material trade-offs, default to higher-signal
inputs (full text, not excerpts) when judging, web-search for
post-training reality (RSS URLs, API endpoints), no first-person
voice in editorial copy, no LLM-tell vocabulary, etc. Agents
re-read this file at the start of each session.

End of post-v0.1 changes.

End of architecture spec v1. Updates as we build.

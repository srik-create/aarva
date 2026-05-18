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
    slot TEXT,                      -- 'deep_feature' | 'lens_card_future' | 'lens_card_humans' | 'lens_card_behind' | 'curiosity' | 'smart_escape'
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
    smart_escape: 1
  length_distribution:
    short: 0.30
    medium: 0.50
    long: 0.20
  trending_cap: 0.50

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

End of architecture spec v1. Updates as we build.

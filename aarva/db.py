"""SQLite schema and thin data-access layer for Aarva.

Single-file module — every read and write to the DB goes through here so the
schema and the access patterns stay co-located. As the pipeline grows we may
split this, but for v0.1 a single module keeps things easy to reason about.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)


SCHEMA_SQL = """
-- One row per article ever ingested. Status tracks where it is in the pipeline.
CREATE TABLE IF NOT EXISTS articles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_url   TEXT    UNIQUE NOT NULL,
    title           TEXT    NOT NULL,
    byline          TEXT,
    publication_id  INTEGER REFERENCES publications(id),
    published_date  DATETIME,
    ingested_date   DATETIME DEFAULT CURRENT_TIMESTAMP,
    word_count      INTEGER,
    full_text       TEXT,
    excerpt         TEXT,
    status          TEXT NOT NULL DEFAULT 'ingested'
        CHECK (status IN ('ingested', 'filtered_out', 'scored', 'in_basket',
                          'in_edition', 'extraction_failed')),
    embedding       BLOB,      -- float32 numpy bytes, L2-normalised
    embedding_model TEXT,      -- name of the model used (for invalidation on swap)
    -- Author-provenance-based TTS accent (2026-07-16) — see
    -- docs/session_plan_author_provenance_accents.md and
    -- aarva/stages/stage_8c_author_provenance.py. NULL = not yet
    -- classified; 'unknown' = classified but no usable evidence
    -- (a terminal result, not a retry state) — these are deliberately
    -- distinct. Never inferred from the author's name alone.
    author_country_code TEXT
);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status);
CREATE INDEX IF NOT EXISTS idx_articles_publication ON articles(publication_id);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_date);


CREATE TABLE IF NOT EXISTS publications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    UNIQUE NOT NULL,
    rss_url         TEXT,
    homepage        TEXT,
    tier            TEXT,
    enabled         INTEGER DEFAULT 1,
    licence_status  TEXT,
    notes           TEXT,
    -- Operator search + ad-hoc URL ingest (2026-07-22) — see
    -- docs/session_plan_operator_search_and_url_ingest.md. Publications
    -- known via publications.yaml carry their country tag there (see
    -- aarva.config.Publication.country); this DB-level column exists
    -- ONLY for publications registered at ingest time via `python -m
    -- aarva.ingest_url` (option b), which never touch the YAML file.
    -- stage_9_tts.py::_build_publication_country_map() merges both
    -- sources so ad-hoc publications still get real accent steering.
    country         TEXT
);


-- Stage 1.5 clusters (will populate from Day 2 onward; schema lives here from
-- Day 1 so we don't have to migrate).
CREATE TABLE IF NOT EXISTS event_clusters (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    centroid_embedding  BLOB,
    created_date        DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS article_clusters (
    article_id              INTEGER REFERENCES articles(id) ON DELETE CASCADE,
    cluster_id              INTEGER REFERENCES event_clusters(id) ON DELETE CASCADE,
    is_best_version         INTEGER DEFAULT 0,
    similarity_to_centroid  REAL,
    PRIMARY KEY (article_id, cluster_id)
);


-- Stage 4+5+6 combined output (populated from Day 3 onward).
CREATE TABLE IF NOT EXISTS article_scores (
    article_id                  INTEGER PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
    rigour                      REAL,
    rigour_rationale            TEXT,
    posture                     REAL,
    posture_rationale           TEXT,
    self_implication            REAL,
    self_implication_rationale  TEXT,
    verdict                     TEXT CHECK (verdict IN ('PASS', 'FAIL')),
    ranking_score               REAL,
    lens                        TEXT,
    pillar                      TEXT,
    jtbd_primary                TEXT,
    jtbd_secondary              TEXT,
    topic_recency_sensitivity   REAL,
    fingerprint_json            TEXT,
    scored_date                 DATETIME DEFAULT CURRENT_TIMESTAMP,
    prompt_version              TEXT
);


-- Editions (Day 4 onward).
--
-- edition_date is UNIQUE per (edition_date, edition_type) because we now
-- support two episode types on the same day: a daily edition plus a
-- daily crosscut episode. The UNIQUE constraint is enforced by an index
-- below to allow both to coexist.
CREATE TABLE IF NOT EXISTS editions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    edition_date    DATE    NOT NULL,
    edition_type    TEXT    NOT NULL DEFAULT 'daily'
        CHECK (edition_type IN ('daily', 'crosscut', 'bonus')),
    published_date  DATETIME DEFAULT CURRENT_TIMESTAMP,
    web_url         TEXT,
    rss_episode_url TEXT,
    -- Review-CLI overrides for daily editions (unused for crosscut).
    extra_slots     TEXT DEFAULT '[]',
    dropped_slots   TEXT DEFAULT '[]',
    -- Review CLI polish, Fix 1 (2026-07-18) — see
    -- docs/session_plan_review_cli_polish.md. dropped_slots (above)
    -- only excludes the SLOT for refill; without this, the dropped
    -- ARTICLE remained eligible for a different slot in the same
    -- edition. JSON list of article_ids dropped from THIS edition
    -- only — still eligible for future editions. NULL/absent on
    -- legacy editions, treated as an empty list.
    dropped_article_ids TEXT DEFAULT '[]',
    slot_biases     TEXT DEFAULT '{}',
    -- Crosscut episode-level framing text (unused for daily editions).
    -- intro_text:    ~100 words framing the topic + the two angles
    -- outro_text:    ~80 words landing the takeaway / takeaway question
    -- topic_label:   short editorial label for the topic (e.g., "AI safety")
    --                used in episode title + on-screen badge
    intro_text      TEXT,
    outro_text      TEXT,
    topic_label     TEXT,
    -- Per-user bonus episodes (Phase A web app). NULL = global
    -- (daily, crosscut, shared bonus). Set = private to that user
    -- (their own ad-hoc picks via /api/v1/publish_article — currently
    -- dead code, see aarva/services/editions.py). No FK: `users`
    -- moved to the listener DB 2026-07-15 (see
    -- docs/session_plan_users_and_crosscut_upgrades.md) — integrity
    -- is application-level, not DB-level, same as jobs.user_id.
    user_id         INTEGER
);
-- NOTE: the composite UNIQUE index on (edition_date, edition_type) is
-- created in _init_schema AFTER the ALTER TABLE migrations have added
-- the edition_type column to legacy DBs. Putting it here would fail on
-- DBs that pre-date edition_type.

CREATE TABLE IF NOT EXISTS edition_pieces (
    edition_id          INTEGER REFERENCES editions(id) ON DELETE CASCADE,
    article_id          INTEGER REFERENCES articles(id),
    slot                TEXT NOT NULL,
    position            INTEGER,
    hook                TEXT,
    contextualisation   TEXT,
    show_notes          TEXT,   -- 2-3 sentence factual summary; surfaced
                                -- in RSS description and web renderer
    audio_url           TEXT,
    duration_seconds    INTEGER,
    narrator_voice      TEXT,
    -- Review status: 'proposed' is the state right after Stage 7 picks the
    -- piece; 'approved' means the user (or the auto-approve path when
    -- review.enabled=false) has signed off and the piece can proceed to
    -- Stage 8+. Rejected pieces are *deleted* from this table and a row is
    -- added to edition_rejections so re-runs avoid re-picking them.
    review_status       TEXT NOT NULL DEFAULT 'approved'
        CHECK (review_status IN ('proposed', 'approved')),
    -- Post-hoc flag-and-remove (Q6). When flagged_at is non-NULL, the
    -- piece is filtered out of the RSS feed and the web renderer.
    -- Soft-delete preserves audit history and supports unflag. Reason is
    -- optional free text for now; later we may move to a controlled
    -- vocabulary for pattern detection.
    flagged_at          DATETIME,
    flag_reason         TEXT,
    -- Crosscut piece-level connective commentary. For a daily edition's
    -- pieces this stays NULL. For a crosscut episode it holds the bridge
    -- text that PRECEDES this piece in the audio. The first piece in a
    -- crosscut has its bridge serve as the article-intro ("piece 1 angle");
    -- the second piece's bridge_text is the cross-piece bridge that
    -- connects 1 → 2. Stage 9 reads this column when composing crosscut
    -- audio.
    bridge_text         TEXT,
    PRIMARY KEY (edition_id, article_id)
);

-- Articles the user explicitly rejected for a specific edition during
-- the cold-start review step. Stage 7 reads this on every re-run and
-- excludes these from the candidate pool so the same piece never gets
-- proposed twice in the same review session.
CREATE TABLE IF NOT EXISTS edition_rejections (
    edition_id          INTEGER NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    article_id          INTEGER NOT NULL REFERENCES articles(id),
    slot_at_rejection   TEXT,   -- which slot the piece was filling when rejected
    rejected_at         DATETIME DEFAULT CURRENT_TIMESTAMP,
    -- Reviewer feedback learning loop, Phase 1 (2026-07-17) — see
    -- docs/session_plan_reviewer_learning_loop.md. reason: one of the
    -- codes in aarva/services/review_reasons.py, NULL for legacy rows
    -- (no retroactive backfill by design). reason_note: optional free
    -- text, populated when reason='other'.
    reason               TEXT,
    reason_note          TEXT,
    PRIMARY KEY (edition_id, article_id)
);


-- Crosscut pair candidates (longlist).
--
-- The pair-detection stage proposes up to ~10 candidate pairs per day.
-- The user reviews this longlist and picks ONE pair to build the
-- crosscut episode from. Picked rows get linked to an edition_id once
-- the episode is built; unpicked rows are kept for analytics / future
-- restart but don't influence anything downstream.
CREATE TABLE IF NOT EXISTS crosscut_pair_candidates (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_date       DATE NOT NULL,
    article_a_id         INTEGER NOT NULL REFERENCES articles(id),
    article_b_id         INTEGER NOT NULL REFERENCES articles(id),
    topic_label          TEXT,           -- LLM-generated short topic name
    angle_a_label        TEXT,           -- one-line angle of article A
    angle_b_label        TEXT,           -- one-line angle of article B
    connection_summary   TEXT,           -- LLM's one-sentence connection rationale
    connection_score     REAL,           -- 0-10 LLM-rated quality
    divergence_score     REAL,           -- structural divergence (axes that differ)
    selected_at          DATETIME,       -- non-NULL once user picks this pair
    edition_id           INTEGER REFERENCES editions(id),  -- linked once built
    created_at           DATETIME DEFAULT CURRENT_TIMESTAMP,
    -- Soft-supersede column so seen-articles history persists across
    -- intra-day re-runs of detect (the require-fresh filter depends
    -- on this; without it, _clear_today_candidates would wipe the
    -- signal every time it fires).
    superseded_at        DATETIME,
    -- Divergent-view tier (2026-07-15) — see
    -- docs/session_plan_users_and_crosscut_upgrades.md §2. 'OPPOSING_
    -- VIEWS' / 'DIFFERENT_ANGLES' from _classify_pair_stance, or NULL
    -- for rows persisted before this column existed. Surfaced to the
    -- reviewer CLI (aarva/crosscut.py) as a `[divergent]` tag.
    stance               TEXT
);
CREATE INDEX IF NOT EXISTS idx_crosscut_candidates_date
    ON crosscut_pair_candidates(candidate_date);
CREATE INDEX IF NOT EXISTS idx_crosscut_candidates_selected
    ON crosscut_pair_candidates(selected_at);


-- Crosscut embeddings for the search index. Each published crosscut
-- episode can have multiple embedding variants under different `source`
-- values, so the search layer (Phase 2) can blend or pick depending on
-- the query shape. Today we store two:
--   - 'pairing_summary': embed the editorial text (topic_label +
--                        intro_text + bridge_between + outro_text).
--                        Captures the curatorial layer — why these two
--                        pieces sit together — that a simple article
--                        mean would miss.
--   - 'article_mean'   : mean of the two source articles' BGE vectors,
--                        L2-renormalised. No extra inference cost;
--                        useful as a fallback when pairing text is
--                        empty and as a complementary signal.
-- UNIQUE(edition_id, source, embedding_model) lets a single crosscut
-- carry several variants without collisions, and lets us swap models
-- later without ALTER TABLE migrations.
CREATE TABLE IF NOT EXISTS crosscut_embeddings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    edition_id      INTEGER NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    source          TEXT    NOT NULL,
    embedding       BLOB    NOT NULL,    -- float32 numpy bytes, L2-normalised
    embedding_model TEXT    NOT NULL,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(edition_id, source, embedding_model)
);
CREATE INDEX IF NOT EXISTS idx_crosscut_embeddings_edition
    ON crosscut_embeddings(edition_id);
CREATE INDEX IF NOT EXISTS idx_crosscut_embeddings_model
    ON crosscut_embeddings(embedding_model);


-- Promoted listener-created crosscuts, surfaced on /today under an
-- "Also today" section (see docs/session_plan_promote_listener_
-- created_as_bonus.md). Lightweight mapping table — doesn't mutate
-- the promoted edition row at all, so un-promoting is a plain DELETE.
--
-- No FK on featured_edition_id: since the 2026-07-06 listener-DB
-- split, virtually every listener-created crosscut lives in the
-- separate listener DB (aarva/listener_db.py), not this one — the id
-- may not correspond to any row in THIS file's `editions` table, so a
-- REFERENCES clause would be meaningless (and, if foreign_keys were
-- ever turned on, would incorrectly reject valid promotions). Written
-- and read by the /admin/promote-bonus + /admin/unpromote-bonus
-- endpoints (aarva/server/routes/admin.py) — see that module's
-- docstring for why this is admin-endpoint-driven rather than a local
-- CLI flag.
CREATE TABLE IF NOT EXISTS daily_bonus_features (
    daily_date          TEXT NOT NULL,
    featured_edition_id INTEGER NOT NULL,
    position            INTEGER NOT NULL,
    added_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (daily_date, featured_edition_id)
);
CREATE INDEX IF NOT EXISTS idx_daily_bonus_features_date
    ON daily_bonus_features(daily_date, position);


-- Pipeline run log: one row per invocation.
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at                      DATETIME DEFAULT CURRENT_TIMESTAMP,
    finished_at                     DATETIME,
    status                          TEXT
        CHECK (status IN ('running', 'success', 'failed')),
    articles_ingested               INTEGER DEFAULT 0,
    articles_after_consolidation    INTEGER DEFAULT 0,
    articles_after_filter           INTEGER DEFAULT 0,
    articles_passed_tonal           INTEGER DEFAULT 0,
    edition_id                      INTEGER REFERENCES editions(id),
    error_message                   TEXT,
    stage_invoked                   TEXT
);


-- ── App-layer tables (web app + per-user state) ─────────────────────────────
--
-- The pipeline above runs unaware of users (it produces a global daily +
-- crosscut). The web app layers per-user state on top: each user has their
-- own dismissals, bonus picks, listening history. The shared daily edition
-- still exists; users see (shared minus their dismissals) plus (their own
-- bonus picks).

-- NOTE (2026-07-15): `users` and `user_sessions` used to live here.
-- Moved to aarva/listener_db.py's LISTENER_SCHEMA_SQL — this file is
-- atomic-replaced by every scripts/sync_db_to_render.sh run, which
-- was silently wiping Render-authored user rows (same bug class as
-- the jobs-table move earlier the same day). See
-- docs/session_plan_users_and_crosscut_upgrades.md Section 1.
-- `magic_link_tokens` below did NOT move (no FK to users, just an
-- email string) — but `aarva/services/users.py`'s magic-link auth
-- flow (currently dead code, no live caller) spans both files now;
-- see that module's docstring.

-- Magic-link tokens — single-use, short-lived. Consumed on first click,
-- which mints a row in user_sessions.
CREATE TABLE IF NOT EXISTS magic_link_tokens (
    token           TEXT PRIMARY KEY,
    email           TEXT NOT NULL COLLATE NOCASE,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    expires_at      DATETIME NOT NULL,
    consumed_at     DATETIME,
    ip              TEXT
);
CREATE INDEX IF NOT EXISTS idx_magic_links_email ON magic_link_tokens(email);
CREATE INDEX IF NOT EXISTS idx_magic_links_expires ON magic_link_tokens(expires_at);


-- User actions — every meaningful interaction a user has with an article.
-- Drives the dismiss-from-feed feature today, and per-user taste centroids
-- + collaborative signals in Phase B. Currently dead code — see
-- aarva/services/actions.py's docstring.
--
-- action values:
--   'dismissed'  — user removed this article from their feed (won't appear
--                  again; doesn't affect other users)
--   'liked'      — explicit positive signal
--   'disliked'   — explicit negative signal
--   'listened'   — started playback
--   'completed'  — finished playback (or >90% played)
--   'shared'     — shared the article to someone
CREATE TABLE IF NOT EXISTS user_actions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    -- No FK on user_id: `users` moved to the listener DB 2026-07-15
    -- (see docs/session_plan_users_and_crosscut_upgrades.md) —
    -- integrity is application-level, not DB-level.
    user_id         INTEGER NOT NULL,
    article_id      INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    action          TEXT NOT NULL
        CHECK (action IN ('dismissed', 'liked', 'disliked',
                          'listened', 'completed', 'shared')),
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    metadata_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_actions_user ON user_actions(user_id);
CREATE INDEX IF NOT EXISTS idx_user_actions_article ON user_actions(article_id);
CREATE INDEX IF NOT EXISTS idx_user_actions_user_action
    ON user_actions(user_id, action);


-- NOTE (2026-07-15): the build_crosscut `jobs` table used to live
-- here. Moved to aarva/listener_db.py's LISTENER_SCHEMA_SQL — this
-- file is atomic-replaced by every scripts/sync_db_to_render.sh run,
-- which was silently wiping Render-authored job rows (same bug class
-- as the 2026-07-06 listener-episode loss). See
-- docs/session_plan_jobs_to_listener_db.md. aarva/services/jobs.py
-- (a separate, unrelated, unwired job-queue module — 'publish_bonus_
-- article' / 'rerecord_crosscut' / 'pipeline_stage' kinds, no live
-- caller anywhere) also targeted a table of this name; it remains
-- dead code and would need its own migration if ever activated.
"""


class Database:
    """Thin wrapper around sqlite3 with the Aarva schema preloaded."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)
            # Column-add migrations for legacy DBs. All columns the
            # historical migrations added are now in the base CREATE
            # TABLE definitions above — these ALTERs only fire on
            # pre-existing DBs that predate each column's introduction.
            # Idempotent: SQLite raises OperationalError when the
            # column already exists; we swallow it.
            #
            # New additions go here, not in SCHEMA_SQL, so existing
            # production DBs upgrade cleanly. After a column has been
            # in production for a few weeks across all instances,
            # move the entry into SCHEMA_SQL and delete from this list.
            _LEGACY_COLUMN_ADDS = (
                "ALTER TABLE edition_pieces ADD COLUMN narrator_voice TEXT",
                "ALTER TABLE edition_pieces ADD COLUMN review_status TEXT "
                "NOT NULL DEFAULT 'approved'",
                "ALTER TABLE edition_pieces ADD COLUMN show_notes TEXT",
                "ALTER TABLE edition_pieces ADD COLUMN flagged_at DATETIME",
                "ALTER TABLE edition_pieces ADD COLUMN flag_reason TEXT",
                "ALTER TABLE edition_pieces ADD COLUMN bridge_text TEXT",
                "ALTER TABLE editions ADD COLUMN extra_slots TEXT",
                "ALTER TABLE editions ADD COLUMN dropped_slots TEXT",
                "ALTER TABLE editions ADD COLUMN slot_biases TEXT",
                "ALTER TABLE editions ADD COLUMN edition_type TEXT "
                "NOT NULL DEFAULT 'daily'",
                "ALTER TABLE editions ADD COLUMN intro_text TEXT",
                "ALTER TABLE editions ADD COLUMN outro_text TEXT",
                "ALTER TABLE editions ADD COLUMN topic_label TEXT",
                "ALTER TABLE crosscut_pair_candidates "
                "ADD COLUMN superseded_at DATETIME",
                # No FK (users moved to the listener DB 2026-07-15) —
                # kept here only for legacy DBs pre-dating that move;
                # SCHEMA_SQL's fresh-create path above already omits it.
                "ALTER TABLE editions ADD COLUMN user_id INTEGER",
                # Content-quality Section 2/3 (2026-07-11) — see
                # docs/session_plan_content_quality.md. subhead_hook:
                # listener-facing one-sentence sub-heading, replacing
                # the plain "title_a x title_b" byline on crosscut
                # cards. originating_prompt: the listener's /create
                # search string for on-demand crosscuts; NULL for
                # daily-pipeline crosscuts.
                "ALTER TABLE editions ADD COLUMN subhead_hook TEXT",
                "ALTER TABLE editions ADD COLUMN originating_prompt TEXT",
                # Divergent-view tier reviewer marker (2026-07-15) —
                # see docs/session_plan_users_and_crosscut_upgrades.md
                # §2 and the roadmap's "mark divergent-tier candidates
                # in the reviewer CLI" item.
                "ALTER TABLE crosscut_pair_candidates ADD COLUMN stance TEXT",
                # Author-provenance-based TTS accent (2026-07-16) — see
                # docs/session_plan_author_provenance_accents.md.
                "ALTER TABLE articles ADD COLUMN author_country_code TEXT",
                # Reviewer feedback learning loop, Phase 1 (2026-07-17) —
                # see docs/session_plan_reviewer_learning_loop.md.
                "ALTER TABLE edition_rejections ADD COLUMN reason TEXT",
                "ALTER TABLE edition_rejections ADD COLUMN reason_note TEXT",
                # Review CLI polish, Fix 1 (2026-07-18) — see
                # docs/session_plan_review_cli_polish.md.
                "ALTER TABLE editions ADD COLUMN dropped_article_ids TEXT",
                # Operator search + ad-hoc URL ingest (2026-07-22) — see
                # docs/session_plan_operator_search_and_url_ingest.md.
                "ALTER TABLE publications ADD COLUMN country TEXT",
            )
            for migration in _LEGACY_COLUMN_ADDS:
                try:
                    conn.execute(migration)
                except sqlite3.OperationalError:
                    pass

            # Compound migration: the original editions schema had
            # `edition_date UNIQUE` at the column level. That blocks
            # storing a daily edition and a crosscut episode for the
            # same date. We rebuild the table without the column-level
            # UNIQUE and add a composite UNIQUE index on
            # (edition_date, edition_type) instead.
            #
            # SQLite doesn't support DROP CONSTRAINT, so this is a
            # table-rebuild migration. We detect the old schema by
            # checking whether the composite index exists.
            self._migrate_editions_uniqueness(conn)

            # Idempotent composite index. Created here (after migrations)
            # rather than in SCHEMA_SQL so we don't try to reference the
            # edition_type column before ALTER TABLE has added it on
            # legacy DBs. On fresh installs this is a no-op.
            #
            # PARTIAL UNIQUE: enforce singleton-per-day only for
            # 'daily' + 'crosscut' edition types. 'bonus' editions
            # (user-picked ad-hoc articles) can have multiple rows
            # per day and are intentionally not constrained.
            try:
                # Drop the old non-partial index if it exists (migration
                # from a previous version that constrained all types).
                conn.execute(
                    "DROP INDEX IF EXISTS idx_editions_date_type"
                )
                # Drop the previous partial index that locked crosscut
                # to one-per-day too. As of 2026-06-29 listeners can
                # generate on-demand crosscut episodes via the web app,
                # which means a given date can carry MULTIPLE crosscut
                # rows: one pipeline-generated (user_id IS NULL) plus
                # any number of listener-generated (user_id SET). The
                # daily singleton constraint stays — the daily pipeline
                # must not double-create.
                conn.execute(
                    "DROP INDEX IF EXISTS idx_editions_date_type_singleton"
                )
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "idx_editions_date_daily_singleton "
                    "ON editions(edition_date) "
                    "WHERE edition_type = 'daily'"
                )
            except sqlite3.OperationalError as e:
                logger.warning("Could not create editions singleton index: %s", e)

    def _migrate_editions_uniqueness(self, conn: sqlite3.Connection) -> None:
        """Rebuild `editions` if it still has either:
        (a) the old single-column UNIQUE constraint on `edition_date`, or
        (b) a CHECK constraint that doesn't include 'bonus' as a valid
            edition_type (added when ad-hoc bonus episodes shipped).

        No-op once both are addressed."""
        # Check current state of the editions table.
        sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='editions'"
        ).fetchone()
        if not sql_row:
            return  # no table yet — fresh init will use SCHEMA_SQL
        table_sql = sql_row[0] or ""

        legacy_unique = ("edition_date    DATE    UNIQUE" in table_sql)
        check_missing_bonus = (
            "CHECK" in table_sql
            and "bonus" not in table_sql
        )

        # Index marker tells us whether the UNIQUE part of the migration
        # has already run. CHECK migration is gated separately.
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND "
            "name IN ('idx_editions_date_type', "
            "         'idx_editions_date_type_singleton')",
        ).fetchone()
        unique_already_migrated = bool(row)

        if unique_already_migrated and not check_missing_bonus:
            return    # already fully migrated
        if not legacy_unique and not check_missing_bonus:
            return    # nothing to do (fresh schema)

        # Rebuild the editions table. Either: the legacy single-column
        # UNIQUE constraint is present (first-time migration from
        # pre-crosscut schema, no edition_type column), or the CHECK
        # constraint lacks 'bonus' (added when ad-hoc bonus episodes
        # shipped — edition_type column exists in this case).
        #
        # Introspect actual columns so the SELECT only references
        # columns that exist in the old table.
        existing_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(editions)").fetchall()
        }
        has_edition_type = "edition_type" in existing_cols
        has_intro = "intro_text" in existing_cols
        has_outro = "outro_text" in existing_cols
        has_topic = "topic_label" in existing_cols
        edition_type_expr = (
            "COALESCE(edition_type, 'daily')" if has_edition_type else "'daily'"
        )
        intro_expr = "intro_text" if has_intro else "NULL"
        outro_expr = "outro_text" if has_outro else "NULL"
        topic_expr = "topic_label" if has_topic else "NULL"

        migration_sql = f"""
            CREATE TABLE editions_new (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                edition_date    DATE NOT NULL,
                edition_type    TEXT NOT NULL DEFAULT 'daily'
                    CHECK (edition_type IN ('daily', 'crosscut', 'bonus')),
                published_date  DATETIME DEFAULT CURRENT_TIMESTAMP,
                web_url         TEXT,
                rss_episode_url TEXT,
                extra_slots     TEXT DEFAULT '[]',
                dropped_slots   TEXT DEFAULT '[]',
                slot_biases     TEXT DEFAULT '{{}}',
                intro_text      TEXT,
                outro_text      TEXT,
                topic_label     TEXT
            );
            INSERT INTO editions_new
                (id, edition_date, edition_type, published_date,
                 web_url, rss_episode_url,
                 extra_slots, dropped_slots, slot_biases,
                 intro_text, outro_text, topic_label)
            SELECT id, edition_date,
                   {edition_type_expr},
                   published_date,
                   web_url, rss_episode_url,
                   COALESCE(extra_slots, '[]'),
                   COALESCE(dropped_slots, '[]'),
                   COALESCE(slot_biases, '{{}}'),
                   {intro_expr}, {outro_expr}, {topic_expr}
              FROM editions;
            DROP TABLE editions;
            ALTER TABLE editions_new RENAME TO editions;
            DROP INDEX IF EXISTS idx_editions_date_type;
            CREATE UNIQUE INDEX idx_editions_date_type_singleton
                ON editions(edition_date, edition_type)
                WHERE edition_type IN ('daily', 'crosscut');
        """

        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.executescript(migration_sql)
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection that commits on exit (or rolls back on exception)."""
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Publications
    # ------------------------------------------------------------------

    def upsert_publication(
        self,
        name: str,
        rss_url: Optional[str] = None,
        homepage: Optional[str] = None,
        tier: Optional[str] = None,
        enabled: bool = True,
        licence_status: Optional[str] = None,
        notes: Optional[str] = None,
        country: Optional[str] = None,
    ) -> int:
        """country: DB-level accent tag (2026-07-22, see docs/session_
        plan_operator_search_and_url_ingest.md) — ONLY for publications
        registered via `python -m aarva.ingest_url`'s "register now"
        option; YAML-known publications carry their country tag in
        publications.yaml instead (aarva.config.Publication.country).
        Omitting it (the default, used by every other existing caller)
        leaves an already-set DB value untouched — COALESCE(?, country)
        rather than a blind overwrite, so the daily RSS-driven sync
        that calls this for every YAML publication can't accidentally
        clobber an ad-hoc-registered publication's country tag."""
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM publications WHERE name = ?", (name,)
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE publications
                       SET rss_url = ?, homepage = ?, tier = ?, enabled = ?,
                           licence_status = ?, notes = ?,
                           country = COALESCE(?, country)
                     WHERE id = ?
                    """,
                    (rss_url, homepage, tier, int(enabled), licence_status, notes,
                     country, existing["id"]),
                )
                return int(existing["id"])
            cursor = conn.execute(
                """
                INSERT INTO publications
                    (name, rss_url, homepage, tier, enabled, licence_status,
                     notes, country)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (name, rss_url, homepage, tier, int(enabled), licence_status,
                 notes, country),
            )
            return int(cursor.lastrowid)

    def get_enabled_publications(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM publications WHERE enabled = 1 ORDER BY tier, name"
            ).fetchall()

    # ------------------------------------------------------------------
    # Articles
    # ------------------------------------------------------------------

    def article_exists(self, canonical_url: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM articles WHERE canonical_url = ?", (canonical_url,)
            ).fetchone()
            return row is not None

    def insert_article(
        self,
        canonical_url: str,
        title: str,
        byline: Optional[str],
        publication_id: int,
        published_date: Optional[datetime],
        word_count: Optional[int],
        full_text: Optional[str],
        excerpt: Optional[str],
        status: str = "ingested",
    ) -> Optional[int]:
        """Insert a new article. Returns id on success, None on uniqueness conflict."""
        with self.connect() as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO articles
                        (canonical_url, title, byline, publication_id,
                         published_date, word_count, full_text, excerpt, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (canonical_url, title, byline, publication_id,
                     published_date, word_count, full_text, excerpt, status),
                )
                return int(cursor.lastrowid)
            except sqlite3.IntegrityError:
                return None

    def count_articles_by_status(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM articles GROUP BY status"
            ).fetchall()
            return {row["status"]: int(row["n"]) for row in rows}

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------

    def set_article_embedding(
        self,
        article_id: int,
        embedding_bytes: bytes,
        embedding_model: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE articles SET embedding = ?, embedding_model = ? WHERE id = ?",
                (embedding_bytes, embedding_model, article_id),
            )

    def set_crosscut_embedding(
        self,
        edition_id: int,
        source: str,
        embedding_bytes: bytes,
        embedding_model: str,
    ) -> None:
        """Insert-or-update a crosscut embedding.

        Keys on the UNIQUE(edition_id, source, embedding_model)
        constraint so re-running the embed pass for an already-embedded
        episode just refreshes the vector (and the created_at stamp)
        rather than duplicating rows."""
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO crosscut_embeddings
                    (edition_id, source, embedding, embedding_model)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(edition_id, source, embedding_model) DO UPDATE
                   SET embedding  = excluded.embedding,
                       created_at = CURRENT_TIMESTAMP
                """,
                (edition_id, source, embedding_bytes, embedding_model),
            )

    def get_articles_needing_embedding(
        self,
        embedding_model: str,
        status_in: tuple[str, ...] = ("ingested",),
    ) -> list[sqlite3.Row]:
        """Articles that don't yet have an embedding from the configured model."""
        placeholders = ",".join("?" * len(status_in))
        with self.connect() as conn:
            return conn.execute(
                f"""
                SELECT id, title, COALESCE(excerpt, '') AS excerpt
                  FROM articles
                 WHERE status IN ({placeholders})
                   AND (embedding IS NULL OR embedding_model != ?)
                """,
                (*status_in, embedding_model),
            ).fetchall()

    # ------------------------------------------------------------------
    # Pipeline runs
    # ------------------------------------------------------------------

    def start_run(self, stage_invoked: str) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO pipeline_runs (status, stage_invoked) VALUES ('running', ?)",
                (stage_invoked,),
            )
            return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        status: str,
        articles_ingested: int = 0,
        error_message: Optional[str] = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE pipeline_runs
                   SET finished_at = CURRENT_TIMESTAMP,
                       status = ?,
                       articles_ingested = ?,
                       error_message = ?
                 WHERE id = ?
                """,
                (status, articles_ingested, error_message, run_id),
            )

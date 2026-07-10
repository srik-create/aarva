"""Separate SQLite file for listener-built episodes.

The `/create` build worker on Render used to write its editions
directly into the main DB (`aarva/db.py`). The daily-DB sync
(`scripts/sync_db_to_render.sh`) atomic-replaces the entire main DB
file from the laptop's snapshot, which silently wiped every listener
episode built on Render since the last sync — observed 2026-07-03,
3 of 4 recently-built episodes lost.

Fix: listener episodes live in this separate file instead, on the
same persistent disk. Sync only ever touches the main DB's path, so
it never touches this one. See
docs/session_plan_listener_db_split.md for the full design.

Deliberately narrow schema — just the three tables an on-demand
crosscut episode needs: `editions`, `edition_pieces`, and
`crosscut_embeddings`. No `articles` or `publications` tables here;
those stay in the main DB (articles never move). `edition_pieces`
carries three denormalized columns (article_title,
article_publication, article_byline) captured at build time so
`/listener-created` and `/crosscut/<id>` can render without a
cross-database join.
"""
from __future__ import annotations

import sqlite3

from aarva.db import Database

LISTENER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS editions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    edition_date    DATE    NOT NULL,
    edition_type    TEXT    NOT NULL DEFAULT 'crosscut'
        CHECK (edition_type IN ('daily', 'crosscut', 'bonus')),
    published_date  DATETIME DEFAULT CURRENT_TIMESTAMP,
    web_url         TEXT,
    rss_episode_url TEXT,
    intro_text      TEXT,
    outro_text      TEXT,
    topic_label     TEXT,
    -- Content-quality Section 2/3 (2026-07-11) — see
    -- docs/session_plan_content_quality.md. Mirrors the same two
    -- columns added to the main DB's editions table.
    subhead_hook       TEXT,
    originating_prompt TEXT,
    -- NULL only for the brief window between build_episode_script's
    -- INSERT and episode_worker's stamp of the requester's user_id
    -- right after — every row is stamped before the build completes.
    -- No FK: `users` lives in the main DB.
    user_id         INTEGER
);

CREATE TABLE IF NOT EXISTS edition_pieces (
    edition_id          INTEGER REFERENCES editions(id) ON DELETE CASCADE,
    -- References the main DB's articles.id. No FK: `articles` doesn't
    -- exist in this file.
    article_id          INTEGER NOT NULL,
    slot                TEXT NOT NULL,
    position            INTEGER,
    show_notes          TEXT,   -- read-aloud passage text (see
                                -- stage_crosscut.py's use of this
                                -- column for crosscut pieces)
    audio_url           TEXT,
    duration_seconds    INTEGER,
    narrator_voice      TEXT,
    review_status       TEXT NOT NULL DEFAULT 'approved'
        CHECK (review_status IN ('proposed', 'approved')),
    flagged_at          DATETIME,
    flag_reason         TEXT,
    bridge_text         TEXT,
    -- Denormalized from the main DB's articles/publications tables at
    -- build time so the pages that read this file need no cross-DB
    -- join. See stage_crosscut.py::_persist_episode.
    article_title       TEXT,
    article_publication TEXT,
    article_byline      TEXT,
    PRIMARY KEY (edition_id, article_id)
);

CREATE TABLE IF NOT EXISTS crosscut_embeddings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    edition_id      INTEGER NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    source          TEXT    NOT NULL,
    embedding       BLOB    NOT NULL,
    embedding_model TEXT    NOT NULL,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(edition_id, source, embedding_model)
);
CREATE INDEX IF NOT EXISTS idx_listener_crosscut_embeddings_edition
    ON crosscut_embeddings(edition_id);
CREATE INDEX IF NOT EXISTS idx_listener_crosscut_embeddings_model
    ON crosscut_embeddings(embedding_model);
"""


class ListenerDatabase(Database):
    """Same connection/query helpers as `Database` (subclassed for
    `connect()` and `set_crosscut_embedding()` reuse), but its own
    narrow schema instead of the full pipeline schema."""

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(LISTENER_SCHEMA_SQL)
            # Column-add migrations for a listener DB file that
            # already existed before a given column was added to
            # LISTENER_SCHEMA_SQL above — `CREATE TABLE IF NOT EXISTS`
            # only applies to brand-new files, so an already-
            # provisioned file (this now exists for real on Render's
            # persistent disk since the render.yaml fix) needs the
            # same idempotent ALTER-list pattern aarva/db.py uses.
            for migration in (
                "ALTER TABLE editions ADD COLUMN subhead_hook TEXT",
                "ALTER TABLE editions ADD COLUMN originating_prompt TEXT",
            ):
                try:
                    conn.execute(migration)
                except sqlite3.OperationalError:
                    pass
            # Seed AUTOINCREMENT for `editions` far above the main
            # DB's id range. Both files' counters start at 1
            # independently — without this, the very first few
            # listener episodes would collide with existing main-DB
            # edition ids, and /crosscut/<id> (which tries the main
            # DB first) would silently show the wrong episode for
            # that id. `WHERE NOT EXISTS` makes this a one-time seed:
            # harmless to re-run on an already-seeded file (schema
            # init runs on every server startup).
            conn.execute("""
                INSERT INTO sqlite_sequence (name, seq)
                SELECT 'editions', 1000000
                 WHERE NOT EXISTS (
                    SELECT 1 FROM sqlite_sequence WHERE name = 'editions'
                 )
            """)

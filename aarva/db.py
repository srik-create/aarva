"""SQLite schema and thin data-access layer for Aarva.

Single-file module — every read and write to the DB goes through here so the
schema and the access patterns stay co-located. As the pipeline grows we may
split this, but for v0.1 a single module keeps things easy to reason about.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional


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
    embedding_model TEXT       -- name of the model used (for invalidation on swap)
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
    notes           TEXT
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
CREATE TABLE IF NOT EXISTS editions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    edition_date    DATE    UNIQUE NOT NULL,
    published_date  DATETIME DEFAULT CURRENT_TIMESTAMP,
    web_url         TEXT,
    rss_episode_url TEXT
);

CREATE TABLE IF NOT EXISTS edition_pieces (
    edition_id          INTEGER REFERENCES editions(id) ON DELETE CASCADE,
    article_id          INTEGER REFERENCES articles(id),
    slot                TEXT NOT NULL,
    position            INTEGER,
    hook                TEXT,
    contextualisation   TEXT,
    audio_url           TEXT,
    duration_seconds    INTEGER,
    narrator_voice      TEXT,
    PRIMARY KEY (edition_id, article_id)
);


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
            # Idempotent column-add migrations for DBs created before a
            # column existed. SQLite raises OperationalError when the
            # column already exists; we swallow that.
            for migration in (
                "ALTER TABLE edition_pieces ADD COLUMN narrator_voice TEXT",
            ):
                try:
                    conn.execute(migration)
                except sqlite3.OperationalError:
                    pass

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
    ) -> int:
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM publications WHERE name = ?", (name,)
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE publications
                       SET rss_url = ?, homepage = ?, tier = ?, enabled = ?,
                           licence_status = ?, notes = ?
                     WHERE id = ?
                    """,
                    (rss_url, homepage, tier, int(enabled), licence_status, notes,
                     existing["id"]),
                )
                return int(existing["id"])
            cursor = conn.execute(
                """
                INSERT INTO publications
                    (name, rss_url, homepage, tier, enabled, licence_status, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (name, rss_url, homepage, tier, int(enabled), licence_status, notes),
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

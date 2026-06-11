"""Edition-level reads + the publish-bonus operation.

This is the bridge between the existing CLI publish_articles and
the future API endpoint. Routes call `publish_bonus_article(user_id,
article_id)` — which enqueues a job (because TTS takes minutes) and
returns the job id for polling.

The synchronous version (`_publish_bonus_synchronous`) is exposed
too; the worker handler imports and calls it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from aarva.config import PipelineConfig
from aarva.db import Database
from aarva.exceptions import NotFoundError, PipelineError
from aarva.services.jobs import enqueue, Job

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EditionSummary:
    id: int
    edition_date: str
    edition_type: str
    user_id: Optional[int]
    topic_label: Optional[str]
    n_pieces: int


def _row_to_summary(row: Any) -> EditionSummary:
    return EditionSummary(
        id=int(row["id"]),
        edition_date=str(row["edition_date"]),
        edition_type=row["edition_type"],
        user_id=(int(row["user_id"]) if row["user_id"] is not None else None),
        topic_label=row["topic_label"],
        n_pieces=int(row["n_pieces"]),
    )


def list_editions(
    db: Database,
    *,
    user_id: Optional[int] = None,
    edition_type: Optional[str] = None,
    limit: int = 50,
) -> list[EditionSummary]:
    """List editions visible to a user. If user_id is given, returns
    global editions + that user's private bonus editions. Without
    user_id, returns only globals (admin/operator view)."""
    where = []
    params: list[Any] = []
    if user_id is None:
        where.append("e.user_id IS NULL")
    else:
        where.append("(e.user_id IS NULL OR e.user_id = ?)")
        params.append(user_id)
    if edition_type:
        where.append("e.edition_type = ?")
        params.append(edition_type)
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    sql = f"""
        SELECT e.id, e.edition_date, e.edition_type, e.user_id,
               e.topic_label,
               (SELECT COUNT(*) FROM edition_pieces ep
                 WHERE ep.edition_id = e.id) AS n_pieces
          FROM editions e
        {where_sql}
         ORDER BY e.edition_date DESC, e.id DESC
         LIMIT ?
    """
    params.append(limit)
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_summary(r) for r in rows]


def publish_bonus_article(
    db: Database,
    user_id: int,
    article_id: int,
    *,
    force: bool = False,
) -> Job:
    """Enqueue a 'publish_bonus_article' job. Returns the Job row.
    The web frontend polls GET /api/jobs/{id} until status is
    'completed' or 'failed'.

    The actual work (Stage 8 + Stage 9 + audio conversion) happens
    in `_publish_bonus_synchronous` via the worker thread. We do
    minimal validation here so the API returns 404 / 400 quickly
    rather than discovering invalid input during background work.
    """
    # Light validation before queueing
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, status FROM articles WHERE id = ?", (article_id,),
        ).fetchone()
    if not row:
        raise NotFoundError(f"Article {article_id} not found.")
    if row["status"] == "in_edition" and not force:
        raise PipelineError(
            f"Article {article_id} is already in a past edition. "
            f"Pass force=true to re-publish (overwrites existing audio)."
        )

    return enqueue(
        db,
        kind="publish_bonus_article",
        payload={"article_id": article_id, "force": force},
        user_id=user_id,
    )


def _publish_bonus_synchronous(
    config: PipelineConfig,
    db: Database,
    article_id: int,
    user_id: int,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """The work the job handler invokes. Equivalent to running
    aarva.publish_articles.publish_one inline, except the bonus
    edition's user_id column is set to scope the episode to this
    user. The shared daily / crosscut path is unaffected."""
    # Import here so that this module loads cheaply for routes that
    # only do list operations.
    from aarva.publish_articles import publish_one

    edition_id = publish_one(
        config, db, article_id,
        today=date.today(), force=force,
    )
    if edition_id is None:
        raise PipelineError(
            f"publish_one returned no edition for article {article_id} "
            f"(validation likely failed). Check pipeline logs."
        )
    # Scope the resulting bonus edition to the user.
    with db.connect() as conn:
        conn.execute(
            "UPDATE editions SET user_id = ? WHERE id = ?",
            (user_id, edition_id),
        )
    return {
        "article_id": article_id,
        "edition_id": int(edition_id),
        "user_id": user_id,
    }

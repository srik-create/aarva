"""Durable background-job queue.

A `jobs` table holds work. A worker thread (started by the web app
at boot, or by a CLI process for the pipeline) polls for `pending`
jobs, claims them atomically, runs them, and updates status.

Kinds we support today:
  - 'publish_bonus_article'    → run stage_8 + stage_9 + audio convert
                                  for an article id; emit edition_id.
  - 'rerecord_crosscut'        → re-record an existing crosscut.
  - 'pipeline_stage'           → run a daily-pipeline stage (used by
                                  scheduled jobs in the future).

The job table is intentionally simple — a future Celery/SQS migration
is a swap of the worker loop, not a redesign of the schema.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from aarva.db import Database

logger = logging.getLogger(__name__)


# Registry of job-kind → handler callable. Handlers receive the
# payload dict + a Database, do their work, and return a result dict.
# Raise any exception to mark the job failed.
JobHandler = Callable[[dict[str, Any], Database], Optional[dict[str, Any]]]
_HANDLERS: dict[str, JobHandler] = {}


def register_handler(kind: str, handler: JobHandler) -> None:
    """Register a handler for a job kind. Idempotent (re-registering
    the same kind overwrites)."""
    _HANDLERS[kind] = handler


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    payload: dict[str, Any]
    status: str
    created_at: str
    started_at: Optional[str]
    finished_at: Optional[str]
    result: Optional[dict[str, Any]]
    error_message: Optional[str]
    user_id: Optional[int]
    progress: Optional[str]


def _row_to_job(row: Any) -> Job:
    return Job(
        id=int(row["id"]),
        kind=row["kind"],
        payload=json.loads(row["payload_json"] or "{}"),
        status=row["status"],
        created_at=str(row["created_at"]),
        started_at=str(row["started_at"]) if row["started_at"] else None,
        finished_at=str(row["finished_at"]) if row["finished_at"] else None,
        result=(json.loads(row["result_json"]) if row["result_json"] else None),
        error_message=row["error_message"],
        user_id=(int(row["user_id"]) if row["user_id"] is not None else None),
        progress=row["progress"],
    )


def enqueue(
    db: Database,
    kind: str,
    payload: dict[str, Any],
    *,
    user_id: Optional[int] = None,
) -> Job:
    """Enqueue a job. The worker thread picks it up on its next poll.

    The kind must have a registered handler at runtime, but we don't
    validate that here — enqueue is a write, handler registration
    happens at worker startup.
    """
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (kind, payload_json, user_id) VALUES (?, ?, ?)",
            (kind, json.dumps(payload), user_id),
        )
        jid = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    return _row_to_job(row)


def get_job(db: Database, job_id: int) -> Optional[Job]:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def list_jobs(
    db: Database,
    *,
    user_id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[Job]:
    where = []
    params: list[Any] = []
    if user_id is not None:
        where.append("user_id = ?")
        params.append(user_id)
    if status:
        where.append("status = ?")
        params.append(status)
    sql = "SELECT * FROM jobs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_job(r) for r in rows]


def _claim_next_pending(db: Database) -> Optional[Job]:
    """Atomically claim the next pending job. Sets status='running'
    and started_at, returns the claimed Job (or None if queue empty).
    """
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM jobs WHERE status = 'pending' "
            "ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        jid = int(row["id"])
        # Compare-and-set: only claim if still pending. Prevents two
        # workers from grabbing the same job.
        cur = conn.execute(
            "UPDATE jobs SET status = 'running', "
            "started_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND status = 'pending'",
            (jid,),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    return _row_to_job(row)


def _mark_completed(
    db: Database, job_id: int, result: Optional[dict[str, Any]],
) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'completed', "
            "finished_at = CURRENT_TIMESTAMP, result_json = ? "
            "WHERE id = ?",
            (json.dumps(result) if result is not None else None, job_id),
        )


def _mark_failed(db: Database, job_id: int, error: str) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'failed', "
            "finished_at = CURRENT_TIMESTAMP, error_message = ? "
            "WHERE id = ?",
            (error[:2000], job_id),
        )


def set_progress(db: Database, job_id: int, progress: str) -> None:
    """Handlers can call this to surface a progress string back to
    the UI (which polls GET /api/jobs/{id})."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE jobs SET progress = ? WHERE id = ?",
            (progress[:500], job_id),
        )


def run_once(db: Database) -> bool:
    """Pull one job off the queue and run it. Returns True if a job
    was processed, False if the queue was empty."""
    job = _claim_next_pending(db)
    if not job:
        return False
    handler = _HANDLERS.get(job.kind)
    if not handler:
        _mark_failed(db, job.id, f"No handler registered for kind '{job.kind}'")
        logger.warning("Job %d: no handler for kind '%s'", job.id, job.kind)
        return True
    logger.info("Job %d: running kind=%s", job.id, job.kind)
    try:
        result = handler(job.payload, db)
        _mark_completed(db, job.id, result)
        logger.info("Job %d: completed", job.id)
    except Exception as e:
        logger.exception("Job %d: failed", job.id)
        _mark_failed(db, job.id, f"{type(e).__name__}: {e}")
    return True


class WorkerThread(threading.Thread):
    """Background worker thread. Polls the queue every `poll_seconds`
    and runs jobs until `stop()` is called or the process exits.

    Designed for FastAPI: started in the app factory, registered to
    stop on shutdown via `app.on_event('shutdown')`.
    """

    def __init__(self, db: Database, *, poll_seconds: float = 2.0):
        super().__init__(daemon=True, name="aarva-jobs-worker")
        self.db = db
        self.poll_seconds = poll_seconds
        self._stop_event = threading.Event()

    def run(self) -> None:
        logger.info("Job worker started (poll=%.1fs)", self.poll_seconds)
        while not self._stop_event.is_set():
            try:
                processed = run_once(self.db)
            except Exception:
                logger.exception("Job worker iteration crashed; continuing")
                processed = False
            if not processed:
                # Queue empty — sleep before next poll. stop() will
                # unblock this early via the Event's wait timeout.
                self._stop_event.wait(timeout=self.poll_seconds)
        logger.info("Job worker stopped")

    def stop(self) -> None:
        self._stop_event.set()

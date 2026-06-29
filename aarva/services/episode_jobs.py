"""Background-job helpers for on-demand episode builds.

Uses the existing `jobs` table (see `aarva/db.py`). The `kind`
discriminator for episode-build jobs is the constant `JOB_KIND` below.

Lifecycle:
  pending  →  running  →  completed | failed
  pending  →  cancelled  (manual operator override)

Stuck-job recovery: a job that's been `running` longer than 30 min is
almost certainly orphaned by a process restart; `reset_stuck_jobs()`
flips it back to `pending` so a later worker iteration picks it up.

The payload JSON shape for a build_crosscut job:

    {
      "prompt":          "the science of belief",
      "article_a_id":    123,
      "article_b_id":    456,
      "topic_label":     "Trust in invisible systems",
      "why":             "Both pieces look at how we form belief …",
      "requester_email": "alice@example.com",
      "user_id":         42
    }

`user_id` is the editions.user_id we'll stamp on the built episode
(distinguishing listener-generated from pipeline-generated).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from aarva.db import Database

logger = logging.getLogger(__name__)


JOB_KIND = "build_crosscut"


# Per-email build cap. Each build costs ~$0.80 in Gemini TTS — a small
# cap keeps cost predictable while still letting a curious listener
# try a couple of pairings the same day. Tune higher (or move to a
# paid tier) when demand justifies it.
DEFAULT_BUILDS_PER_24H = 2


class BuildQuotaExceeded(Exception):
    """Raised by `enqueue_build_job` when the requester has hit their
    per-24-hour build cap. The route handler catches this and renders
    a friendly 'try again tomorrow' page instead of letting it
    propagate as a 500."""
    def __init__(self, email: str, count: int, limit: int):
        self.email = email
        self.count = count
        self.limit = limit
        super().__init__(
            f"{email} has {count} build(s) in the last 24h "
            f"(limit: {limit})"
        )


# ─── User upsert ──────────────────────────────────────────────────────────

def ensure_user_for_email(db: Database, email: str) -> int:
    """Get or create a users row for `email`; return the user_id.

    No auth yet — the email is the entire identity for v1. A row is
    created the first time a listener requests an episode; future
    requests from the same email reuse it. Magic-link login lands
    later (the schema is already there) and the same user_id will
    apply."""
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("ensure_user_for_email: empty email")
    with db.connect() as conn:
        # INSERT OR IGNORE relies on the UNIQUE(email COLLATE NOCASE)
        # constraint in the users table.
        conn.execute(
            "INSERT OR IGNORE INTO users (email) VALUES (?)",
            (email,),
        )
        row = conn.execute(
            "SELECT id FROM users WHERE email = ?",
            (email,),
        ).fetchone()
    if not row:
        # Shouldn't happen — INSERT OR IGNORE then SELECT is atomic
        # within the same connection — but guard anyway.
        raise RuntimeError(f"failed to upsert user for {email!r}")
    return int(row["id"])


# ─── Enqueue ──────────────────────────────────────────────────────────────

def _count_builds_24h(db: Database, user_id: int) -> int:
    """Count this user's build_crosscut jobs in the last 24 hours.

    Only `pending`, `running`, and `completed` count — `failed` and
    `cancelled` are excluded so a system error on Aarva's side doesn't
    burn a slot the listener owns."""
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n
              FROM jobs
             WHERE kind = ?
               AND user_id = ?
               AND status IN ('pending', 'running', 'completed')
               AND created_at >= datetime('now', '-24 hours')
            """,
            (JOB_KIND, int(user_id)),
        ).fetchone()
    return int(row["n"]) if row else 0


def enqueue_build_job(
    db: Database,
    *,
    prompt: str,
    article_a_id: int,
    article_b_id: int,
    topic_label: str,
    why: str,
    requester_email: str,
    builds_per_24h_limit: int = DEFAULT_BUILDS_PER_24H,
) -> int:
    """Queue a new on-demand build. Returns the job_id.

    Creates / upserts the requester's users row so the eventual
    `editions.user_id` can point at them. The job_id is the listener's
    status-page key — pass it back as part of the redirect.

    Raises BuildQuotaExceeded when the requester has hit the per-24h
    cap. No DB write happens in that case (the users row upsert is
    cheap and idempotent so still runs — harmless either way)."""
    user_id = ensure_user_for_email(db, requester_email)

    count_today = _count_builds_24h(db, user_id)
    if count_today >= builds_per_24h_limit:
        raise BuildQuotaExceeded(
            requester_email, count_today, builds_per_24h_limit,
        )

    payload = {
        "prompt": prompt,
        "article_a_id": int(article_a_id),
        "article_b_id": int(article_b_id),
        "topic_label": topic_label,
        "why": why,
        "requester_email": requester_email.strip().lower(),
        "user_id": user_id,
    }

    with db.connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO jobs (kind, payload_json, status, user_id)
            VALUES (?, ?, 'pending', ?)
            """,
            (JOB_KIND, json.dumps(payload), user_id),
        )
        job_id = int(cur.lastrowid)
    logger.info(
        "episode_jobs: enqueued job %d for user %d (a=%d b=%d topic=%r)",
        job_id, user_id, article_a_id, article_b_id, topic_label,
    )
    return job_id


# ─── Claim + state transitions ────────────────────────────────────────────

def claim_next_pending(db: Database) -> Optional[dict[str, Any]]:
    """Atomically claim the oldest pending build_crosscut job.

    Implementation: single UPDATE that flips status pending → running
    and stamps started_at, returning the affected row. SQLite doesn't
    support `RETURNING` on older versions consistently; we do
    SELECT + UPDATE + re-SELECT within a single connection so reads
    are consistent on the same isolation level.

    Returns the claimed job as a dict, or None if the queue is empty."""
    with db.connect() as conn:
        # IMMEDIATE so we hold the write lock for the whole claim.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT id, kind, payload_json, status, user_id
              FROM jobs
             WHERE kind = ? AND status = 'pending'
             ORDER BY id
             LIMIT 1
            """,
            (JOB_KIND,),
        ).fetchone()
        if not row:
            conn.execute("COMMIT")
            return None
        conn.execute(
            """
            UPDATE jobs
               SET status = 'running',
                   started_at = CURRENT_TIMESTAMP
             WHERE id = ? AND status = 'pending'
            """,
            (int(row["id"]),),
        )
        conn.execute("COMMIT")
        return {
            "id": int(row["id"]),
            "kind": row["kind"],
            "payload": json.loads(row["payload_json"]),
            "user_id": int(row["user_id"]) if row["user_id"] is not None else None,
        }


def mark_completed(
    db: Database,
    job_id: int,
    result: dict[str, Any],
) -> None:
    """Mark the job done, store its result. `result` should at least
    carry `edition_id` so the status page can link to /crosscut/<id>."""
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE jobs
               SET status = 'completed',
                   finished_at = CURRENT_TIMESTAMP,
                   result_json = ?
             WHERE id = ?
            """,
            (json.dumps(result), int(job_id)),
        )
    logger.info("episode_jobs: job %d completed (edition_id=%s)",
                job_id, result.get("edition_id"))


def mark_failed(
    db: Database,
    job_id: int,
    error_message: str,
) -> None:
    """Mark the job failed with a one-line operator-facing error."""
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE jobs
               SET status = 'failed',
                   finished_at = CURRENT_TIMESTAMP,
                   error_message = ?
             WHERE id = ?
            """,
            (str(error_message)[:2000], int(job_id)),
        )
    logger.warning("episode_jobs: job %d failed: %s", job_id, error_message)


def update_progress(
    db: Database,
    job_id: int,
    progress: str,
) -> None:
    """Set a short human-readable progress string. Polled by the
    status page so the listener sees 'writing intro', 'rendering
    audio', etc. instead of just a spinner."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE jobs SET progress = ? WHERE id = ?",
            (str(progress)[:200], int(job_id)),
        )


# ─── Lookup + recovery ────────────────────────────────────────────────────

def get_job(db: Database, job_id: int) -> Optional[dict[str, Any]]:
    """Return the job row + parsed payload, or None if not found."""
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT id, kind, payload_json, status, created_at, started_at,
                   finished_at, result_json, error_message, user_id, progress
              FROM jobs
             WHERE id = ?
            """,
            (int(job_id),),
        ).fetchone()
    if not row:
        return None
    out = dict(row)
    try:
        out["payload"] = json.loads(out.pop("payload_json"))
    except (ValueError, TypeError):
        out["payload"] = None
    if out.get("result_json"):
        try:
            out["result"] = json.loads(out["result_json"])
        except (ValueError, TypeError):
            out["result"] = None
    else:
        out["result"] = None
    return out


def reset_stuck_jobs(db: Database, *, older_than_minutes: int = 30) -> int:
    """Reset any `running` build_crosscut jobs older than the window
    back to `pending`. Called at worker startup so a crashed process
    doesn't leave jobs wedged forever. Returns the count reset."""
    with db.connect() as conn:
        cur = conn.execute(
            f"""
            UPDATE jobs
               SET status = 'pending',
                   started_at = NULL,
                   progress = 'recovered after stale running state'
             WHERE kind = ?
               AND status = 'running'
               AND started_at IS NOT NULL
               AND started_at < datetime('now', '-{int(older_than_minutes)} minutes')
            """,
            (JOB_KIND,),
        )
        count = cur.rowcount or 0
    if count:
        logger.warning(
            "episode_jobs: reset %d stuck running job(s) at startup",
            count,
        )
    return int(count)

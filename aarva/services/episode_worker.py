"""Background worker that builds on-demand crosscut episodes.

Daemon thread spawned from `aarva.server.app`'s lifespan() at startup.
Polls the existing `jobs` table for `kind='build_crosscut'` rows in
`pending` state, claims them one at a time, and runs the full episode-
build pipeline end-to-end:

  1. Insert a `crosscut_pair_candidates` row matching the listener's
     pick. The existing `build_episode_script` finds today's selected
     candidate via `_selected_candidate`, so we satisfy that contract
     by inserting a freshly-selected row here. Single-threaded worker
     means there's no race — the row we just inserted is the most
     recent for today, which is what `_selected_candidate` returns.
  2. Call `stage_crosscut.build_episode_script(config, db)` — same code
     the daily pipeline uses. Generates intro / bridge_a / bridge_between
     / outro via Gemini, persists the editions + edition_pieces rows,
     and (already wired) embeds the new episode into
     `crosscut_embeddings` so it's searchable by future prompts.
  3. Stamp `editions.user_id` with the requester's user_id so the
     episode lands on `/listener-created` instead of `/today` /
     `/crosscuts` (which filter to `user_id IS NULL`).
  4. Call `stage_crosscut.synthesize_crosscut_episode(config, db,
     edition_id=...)` — the existing TTS path that renders audio,
     loudness-normalises, uploads to R2, and writes `audio_url` onto
     the pieces.
  5. Send a "your episode is ready" email via `aarva.services.email`.
     In local dev (no RESEND_API_KEY) this just logs the would-send
     payload to stdout — the status page remains the listener's real
     notification surface in that mode.
  6. Mark the job `completed` with `result_json = {"edition_id": ...}`.

Operational properties:
  - Single concurrent build at v1. Trivially extendable to N workers
    later (claim_next_pending is atomic under an IMMEDIATE transaction).
  - FIFO order (oldest pending job first).
  - Worker crash on one job logs + marks the job failed; the loop
    keeps going. The next iteration is unaffected.
  - Process restart: any jobs left in `running` get reset back to
    `pending` at startup via `reset_stuck_jobs()`. Acceptable
    duplication-of-work risk: building twice produces two editions
    pointing at the same article pair, which clutters but doesn't
    corrupt. Tighten with a stale-running-but-not-too-stale window
    if it bites.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import date
from typing import Optional

from aarva.config import PipelineConfig
from aarva.db import Database

logger = logging.getLogger(__name__)


# Seconds between empty-queue polls. Short enough that listeners
# don't notice latency between submit and "build started"; long
# enough not to hammer SQLite.
_POLL_SECONDS = 5


@dataclass
class WorkerHandle:
    """Returned from `start_worker`. lifespan() holds onto this and
    sets `stop_event` at shutdown to ask the loop to exit cleanly."""
    thread: threading.Thread
    stop_event: threading.Event

    def stop(self, timeout: Optional[float] = 5.0) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout)


def start_worker(db: Database, config: PipelineConfig) -> WorkerHandle:
    """Spawn the daemon thread and reset any stuck jobs from a previous
    process. Idempotent if called multiple times only in the sense that
    each call spawns another thread — callers should hold one handle."""
    from aarva.services.episode_jobs import reset_stuck_jobs

    n_reset = reset_stuck_jobs(db)
    if n_reset:
        logger.info("episode_worker: reset %d stuck job(s) on startup", n_reset)

    stop_event = threading.Event()
    thread = threading.Thread(
        target=_worker_loop,
        args=(db, config, stop_event),
        daemon=True,
        name="aarva-episode-worker",
    )
    thread.start()
    logger.info("episode_worker: daemon thread started")
    return WorkerHandle(thread=thread, stop_event=stop_event)


def _worker_loop(
    db: Database,
    config: PipelineConfig,
    stop_event: threading.Event,
) -> None:
    """Polling loop. Exits when stop_event is set."""
    from aarva.services.episode_jobs import (
        claim_next_pending, mark_failed,
    )

    while not stop_event.is_set():
        try:
            job = claim_next_pending(db)
        except Exception as e:
            logger.exception("episode_worker: claim_next_pending crashed: %s", e)
            stop_event.wait(_POLL_SECONDS * 2)
            continue

        if job is None:
            # Queue empty — idle.
            stop_event.wait(_POLL_SECONDS)
            continue

        job_id = job["id"]
        try:
            _run_job(db, config, job)
        except Exception as e:
            logger.exception("episode_worker: job %d crashed: %s", job_id, e)
            try:
                mark_failed(db, job_id, f"{type(e).__name__}: {e}")
            except Exception as inner:
                logger.error("episode_worker: also failed to mark_failed: %s", inner)


def _run_job(
    db: Database,
    config: PipelineConfig,
    job: dict,
) -> None:
    """Run one build_crosscut job end-to-end. Any raise propagates to
    the loop which marks the job failed."""
    from aarva.services.email import send_email
    from aarva.services.episode_jobs import (
        mark_completed, stamp_edition_id, update_progress,
    )
    from aarva.stages.stage_crosscut import (
        build_episode_script, synthesize_crosscut_episode,
    )

    job_id = job["id"]
    payload = job["payload"] or {}
    requester_email = str(payload.get("requester_email") or "").strip()
    requester_user_id = payload.get("user_id")
    if requester_user_id is None:
        raise ValueError("payload missing user_id")
    requester_user_id = int(requester_user_id)

    logger.info(
        "episode_worker: starting job %d (a=%s b=%s topic=%r)",
        job_id, payload.get("article_a_id"), payload.get("article_b_id"),
        payload.get("topic_label"),
    )

    # Resumability checkpoint. If a prior attempt got as far as
    # creating the edition row, payload.edition_id was stamped after
    # step 2. On the retry, skip steps 1-3 (candidate insert + LLM
    # proposal + user_id stamp — none of which are idempotent) and go
    # straight into TTS/convert/upload, all of which ARE idempotent
    # per-piece and will pick up where the previous attempt left off.
    checkpoint_edition_id = payload.get("edition_id")
    if checkpoint_edition_id is not None:
        edition_id = int(checkpoint_edition_id)
        logger.info(
            "episode_worker: job %d resuming from checkpoint — "
            "edition %d already built, jumping to TTS",
            job_id, edition_id,
        )
        update_progress(db, job_id, "Resuming — audio still needed…")
    else:
        # 1. Insert the candidate row build_episode_script will find
        # via _selected_candidate(today). Worker is single-threaded
        # so this row WILL be the most-recently-selected when we call
        # into build_episode_script next.
        update_progress(db, job_id, "Setting up the build…")
        today = date.today()
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO crosscut_pair_candidates
                    (candidate_date, article_a_id, article_b_id,
                     topic_label, connection_summary, connection_score,
                     selected_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    today.isoformat(),
                    int(payload["article_a_id"]),
                    int(payload["article_b_id"]),
                    str(payload.get("topic_label") or ""),
                    str(payload.get("why") or ""),
                    0.0,    # connection_score — not used on the on-demand path
                ),
            )

        # 2. Build the episode script (intro / bridges / outro +
        # persist the editions row + embed into crosscut_embeddings).
        update_progress(db, job_id, "Writing the intro and bridges…")
        build_stats = build_episode_script(config, db)
        if build_stats.errors or not build_stats.edition_id:
            raise RuntimeError(
                f"build_episode_script reported errors: {build_stats!r}"
            )
        edition_id = int(build_stats.edition_id)
        logger.info("episode_worker: job %d built edition %d", job_id, edition_id)

        # 3. Stamp the requester's user_id so the episode shows up on
        # /listener-created (rather than /today / /crosscuts which
        # filter to user_id IS NULL).
        with db.connect() as conn:
            conn.execute(
                "UPDATE editions SET user_id = ? WHERE id = ?",
                (requester_user_id, edition_id),
            )

        # Stamp the checkpoint. From here on, a Render OOM restart
        # will resume from step 4 (TTS) rather than re-running the
        # LLM proposal + edition creation.
        stamp_edition_id(db, job_id, edition_id)

    # 4. Run TTS — this is the long-pole step (~15 min for a normal
    # crosscut at the configured chunk size). The TTS path produces a
    # WAV per piece and writes audio_url onto the edition_pieces rows.
    update_progress(db, job_id, "Rendering the audio (~15 min)…")
    tts_stats = synthesize_crosscut_episode(config, db, edition_id=edition_id)
    if tts_stats.errors:
        raise RuntimeError(
            f"synthesize_crosscut_episode reported errors: {tts_stats!r}"
        )

    # 4b. Convert WAV → MP3 (with loudness normalisation) and upload
    # to R2. The daily pipeline runs this via `scripts/publish.sh`
    # as Stage 10; for on-demand builds we do it inline here so the
    # episode is immediately listenable. Both calls are idempotent —
    # already-converted MP3s and already-uploaded R2 keys are skipped.
    update_progress(db, job_id, "Converting to MP3 and uploading…")
    from aarva.output import audio_converter, r2_uploader
    conv_stats = audio_converter.convert_all_for_publish(config, db)
    logger.info(
        "episode_worker: job %d conversion stats: %r",
        job_id, conv_stats,
    )
    upload_stats = r2_uploader.upload_all_pending(config, db)
    logger.info(
        "episode_worker: job %d R2 upload stats: %r",
        job_id, upload_stats,
    )

    # 5. Notify the listener (stub in dev — logs only).
    update_progress(db, job_id, "Sending the notification…")
    public_url = (
        os.environ.get("AARVA_SERVER_PUBLIC_URL", "").rstrip("/")
        or "http://localhost:8000"
    )
    listen_link = f"{public_url}/crosscut/{edition_id}"
    listener_index = f"{public_url}/listener-created"

    subject = "Your Aarva episode is ready"
    text = (
        f"Your episode is ready to listen.\n\n"
        f"  {listen_link}\n\n"
        f"It's also browseable at {listener_index}. "
        f"Thanks for shaping the catalog.\n"
    )
    html = (
        f'<p>Your episode is ready to listen.</p>'
        f'<p><a href="{listen_link}">Open it on Aarva</a></p>'
        f'<p style="color:#7E715E;font-size:13px">'
        f'It\'s also browseable at '
        f'<a href="{listener_index}">aarva.app/listener-created</a>. '
        f'Thanks for shaping the catalog.</p>'
    )
    if requester_email:
        send_email(to=requester_email, subject=subject, html=html, text=text)

    # 6. Done.
    mark_completed(db, job_id, {
        "edition_id": edition_id,
        "episode_path": f"/crosscut/{edition_id}",
    })
    logger.info(
        "episode_worker: job %d completed → edition %d (audio_url path set)",
        job_id, edition_id,
    )

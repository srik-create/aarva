"""Episode-creation routes.

Four endpoints make up the listener-facing creation flow:

  GET  /create?q=<prompt>      Render the candidate page. Calls
                               aarva.services.episode_candidates
                               to get up to 3 candidates for the
                               prompt. Empty q → redirect to /.

  POST /create/build           Form submit from the candidate page.
                               Validates the picked article pair +
                               requester email, queues a
                               build_crosscut job in the existing
                               jobs table, redirects to the status
                               page.

  GET  /build/<job_id>         Status page for a queued / running /
                               completed build. Polls itself every
                               few seconds via a tiny JS fragment so
                               the listener sees progress.

  GET  /listener-created       List of episodes generated via this
                               flow (editions.user_id IS NOT NULL),
                               same card layout as /crosscuts.

Auth: none. The /build/<job_id> URL is the only thing tying a
listener to "their" episode, and there's no PII on the status page
itself (just progress text + the listen link once ready).
"""
from __future__ import annotations

import logging

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from aarva.server.app import app
from aarva.server.templates import templates
from aarva.services.episode_candidates import propose_candidates
from aarva.services.episode_jobs import enqueue_build_job, get_job
from aarva.services.queries import load_crosscut_episodes

logger = logging.getLogger(__name__)


# ─── Candidate page ──────────────────────────────────────────────────────

@app.get("/create", response_class=HTMLResponse)
async def create_candidates(request: Request) -> HTMLResponse:
    """Show up to 3 candidate episodes for the listener's prompt."""
    q = (request.query_params.get("q") or "").strip()
    if not q:
        return RedirectResponse(url="/", status_code=303)

    db = request.app.state.db
    embedding_client = request.app.state.embedding_client
    llm_client = request.app.state.llm_client

    try:
        candidates = propose_candidates(
            db=db,
            embedding_client=embedding_client,
            llm_client=llm_client,
            prompt=q,
            n=3,
        )
    except Exception as e:
        logger.exception("create_candidates: propose_candidates crashed: %s", e)
        candidates = []

    return templates.TemplateResponse(
        request, "create.html",
        {
            "prompt": q,
            "candidates": candidates,
        },
    )


# ─── Build trigger ───────────────────────────────────────────────────────

@app.post("/create/build")
async def create_build(request: Request):
    """Queue a build job for the picked candidate. Form fields:
      prompt           — original listener prompt (echoed back)
      article_a_id     — int
      article_b_id     — int
      topic_label      — str
      why              — str
      email            — listener's email"""
    form = await request.form()

    def _str(name: str) -> str:
        return str(form.get(name) or "").strip()

    prompt = _str("prompt")
    email = _str("email")
    topic_label = _str("topic_label")
    why = _str("why")
    try:
        article_a_id = int(form.get("article_a_id"))
        article_b_id = int(form.get("article_b_id"))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="article_a_id and article_b_id must be integers",
        )

    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="A valid email is required.")
    if not topic_label:
        raise HTTPException(status_code=400, detail="topic_label is required.")

    db = request.app.state.db
    job_id = enqueue_build_job(
        db,
        prompt=prompt,
        article_a_id=article_a_id,
        article_b_id=article_b_id,
        topic_label=topic_label,
        why=why,
        requester_email=email,
    )
    return RedirectResponse(url=f"/build/{job_id}", status_code=303)


# ─── Status page ─────────────────────────────────────────────────────────

@app.get("/build/{job_id}", response_class=HTMLResponse)
async def build_status(request: Request, job_id: int) -> HTMLResponse:
    """Status page for a queued build. Polls itself via a small JS
    refresh so the listener sees progress + the final listen link."""
    db = request.app.state.db
    job = get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Build not found")

    # Pull a compact view of the picked pair for the page header.
    payload = job.get("payload") or {}
    return templates.TemplateResponse(
        request, "build_status.html",
        {
            "job": job,
            "payload": payload,
        },
    )


# ─── Listener-created catalog ────────────────────────────────────────────

@app.get("/listener-created", response_class=HTMLResponse)
async def listener_created(request: Request) -> HTMLResponse:
    """List of all listener-generated crosscut episodes, newest first.
    Filters editions to user_id IS NOT NULL via
    `load_crosscut_episodes(user_generated_only=True)`."""
    db = request.app.state.db
    crosscuts = load_crosscut_episodes(
        db,
        include_user_id_null=False,
        user_generated_only=True,
    )
    return templates.TemplateResponse(
        request, "listener_created.html",
        {"crosscuts": crosscuts},
    )

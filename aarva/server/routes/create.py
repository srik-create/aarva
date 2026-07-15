"""Episode-creation routes.

Five endpoints make up the listener-facing creation flow:

  GET  /create?q=<prompt>      Render the candidate-page SHELL —
                               prompt header, explainer, loading
                               animation. Cheap; no DB / LLM work.
                               The candidates load async via the
                               fetch below so the listener sees a
                               spinner instead of a frozen tab while
                               Gemini composes pairings. Empty q →
                               redirect to /.

  GET  /api/candidates?q=...   HTML fragment of the candidate cards.
                               Called by create.html's inline JS.
                               Returns just the inner-HTML, not a
                               full page (no <html>/<head>/<body>).
                               Heavy: embeds the prompt, queries
                               crosscut_embeddings for existing
                               matches, calls Gemini for new pairings.

  POST /create/build           Form submit from a candidate card.
                               Validates the picked article pair +
                               requester email, queues a
                               build_crosscut job in the listener
                               DB's jobs table (see
                               aarva/listener_db.py), redirects to
                               the status page.

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
from starlette.concurrency import run_in_threadpool

from aarva.server.app import app
from aarva.server.templates import templates
from aarva.services.episode_candidates import propose_candidates
from aarva.services.episode_jobs import (
    BuildQuotaExceeded, enqueue_build_job, get_job,
)
from aarva.services.queries import load_crosscut_episodes, load_listener_episodes

logger = logging.getLogger(__name__)


def ensure_user_for_email(listener_db, email: str) -> int:
    """Get or create a users row for `email`; return the user_id.

    Lives here (not episode_jobs.py) since 2026-07-15's jobs-table
    move to the listener DB — episode_jobs.py is purely listener-DB-
    facing. This is the only caller that needs a user_id before
    enqueuing a build job.

    Takes `listener_db`, not `db`: `users` moved to the listener DB
    the same day as jobs, same bug class (see
    docs/session_plan_users_and_crosscut_upgrades.md Section 1) — a
    laptop→Render sync was silently wiping any users row created on
    Render since the previous sync.

    No auth yet — the email is the entire identity for v1. A row is
    created the first time a listener requests an episode; future
    requests from the same email reuse it. Magic-link login lands
    later (the schema is already there) and the same user_id will
    apply."""
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("ensure_user_for_email: empty email")
    with listener_db.connect() as conn:
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


# ─── Recovered episodes ───────────────────────────────────────────────────
#
# Two listener-built episodes finished successfully (audio rendered +
# uploaded) between the 2026-07-06 listener-DB split and the
# 2026-07-11 render.yaml persistent-disk fix — during that window the
# listener DB lived on Render's ephemeral disk, so a later redeploy
# wiped their editions/edition_pieces rows before they could be
# synced anywhere. The raw audio survived in R2; the topic, article
# pairing, and script did not. Manually curated (there's no DB row to
# drive this — nothing to query), per user decision 2026-07-11 not to
# just let the audio sit unreferenced. See docs/roadmap.md.
#
# If this list ever grows past a couple of entries, move it to a
# small data file — a Python constant is fine for two.
RECOVERED_EPISODES = [
    {
        "edition_date": "2026-07-06",
        "topic_label": "Recovered episode — built 2026-07-06",
        "note": (
            "This episode's audio survived a deployment bug that "
            "wiped its title, article pairing, and script before a "
            "fix landed on 2026-07-11 — only the finished audio "
            "could be recovered."
        ),
        "audio_url": "output/audio/2026-07-06/crosscut_1000001.mp3",
    },
    {
        "edition_date": "2026-07-07",
        "topic_label": "Recovered episode — built 2026-07-07",
        "note": (
            "Same deployment bug as the 2026-07-06 recovery above — "
            "only the finished audio could be recovered."
        ),
        "audio_url": "output/audio/2026-07-07/crosscut_1000002.mp3",
    },
]


# ─── Candidate page ──────────────────────────────────────────────────────

@app.get("/create", response_class=HTMLResponse)
async def create_candidates(request: Request) -> HTMLResponse:
    """Render the candidate-page shell. No LLM / DB work here — the
    candidates load asynchronously via /api/candidates."""
    q = (request.query_params.get("q") or "").strip()
    if not q:
        return RedirectResponse(url="/", status_code=303)

    return templates.TemplateResponse(
        request, "create.html",
        {"prompt": q},
    )


@app.get("/api/candidates", response_class=HTMLResponse)
async def api_candidates(request: Request) -> HTMLResponse:
    """Returns the candidate cards as an HTML fragment (no <html>/
    <head>/<body>). Called by create.html's inline JS. This is where
    the actual embedding + Gemini work happens — kept off /create so
    the page-shell render stays instant."""
    q = (request.query_params.get("q") or "").strip()
    if not q:
        return HTMLResponse("", status_code=400)

    db = request.app.state.db
    listener_db = request.app.state.listener_db
    embedding_client = request.app.state.embedding_client
    llm_client = request.app.state.llm_client
    max_age_days_news = int(
        request.app.state.pipeline_cfg.raw.get("search", {}).get(
            "max_age_days_news", 6,
        )
    )

    try:
        # propose_candidates does an embedding round-trip + a Gemini
        # LLM proposal call + DB scan; can easily take 10-30s in
        # aggregate. Running that inline in an async handler would
        # block the FastAPI event loop, starving /health polls and
        # letting Render's 5s health-check timeout kill the instance
        # mid-request. Bounce to the threadpool so the event loop
        # stays responsive.
        candidates = await run_in_threadpool(
            propose_candidates,
            db=db,
            listener_db=listener_db,
            embedding_client=embedding_client,
            llm_client=llm_client,
            prompt=q,
            n=3,
            max_age_days_news=max_age_days_news,
        )
    except Exception as e:
        logger.exception("api_candidates: propose_candidates crashed: %s", e)
        candidates = []

    return templates.TemplateResponse(
        request, "_candidates_fragment.html",
        {"prompt": q, "candidates": candidates},
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

    listener_db = request.app.state.listener_db
    try:
        user_id = ensure_user_for_email(listener_db, email)
        job_id = enqueue_build_job(
            listener_db,
            prompt=prompt,
            article_a_id=article_a_id,
            article_b_id=article_b_id,
            topic_label=topic_label,
            why=why,
            requester_email=email,
            user_id=user_id,
        )
    except BuildQuotaExceeded as e:
        # Listener has already used their slots for the last 24 hours.
        # Render a friendly page rather than letting this become a 500.
        return templates.TemplateResponse(
            request, "create_quota_exceeded.html",
            {
                "prompt": prompt,
                "email": email,
                "count": e.count,
                "limit": e.limit,
            },
            status_code=429,
        )
    return RedirectResponse(url=f"/build/{job_id}", status_code=303)


# ─── Status page ─────────────────────────────────────────────────────────

@app.get("/build/{job_id}", response_class=HTMLResponse)
async def build_status(request: Request, job_id: int) -> HTMLResponse:
    """Status page for a queued build. Polls itself via a small JS
    refresh so the listener sees progress + the final listen link."""
    listener_db = request.app.state.listener_db
    job = get_job(listener_db, job_id)
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

    Merges two sources: episodes built before the 2026-07-06
    listener-DB split (still sitting in the main DB, user_id IS NOT
    NULL) and everything built since (in the listener DB — see
    aarva/listener_db.py). New builds only ever land in the listener
    DB going forward; the main-DB query exists so pre-split episodes
    that survived don't just disappear from this page."""
    db = request.app.state.db
    listener_db = request.app.state.listener_db
    legacy = load_crosscut_episodes(
        db,
        include_user_id_null=False,
        user_generated_only=True,
    )
    current = load_listener_episodes(listener_db)
    crosscuts = sorted(
        legacy + current,
        key=lambda c: (c["edition_date"], c["edition_id"]),
        reverse=True,
    )
    return templates.TemplateResponse(
        request, "listener_created.html",
        {"crosscuts": crosscuts, "recovered_episodes": RECOVERED_EPISODES},
    )

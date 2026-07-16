"""Crosscut browse routes.

  /crosscuts             — list of every past crosscut episode,
                           newest first. Each row is a peach card
                           with topic label, the two source titles,
                           date, and a compact player.
  /crosscut/<edition_id> — per-episode detail (title, big play button,
                           intro / bridge / outro text, links to the
                           two source articles).
"""
from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from aarva.server.app import app
from aarva.server.templates import templates
from aarva.services.queries import load_crosscut_episodes, load_listener_episodes
from aarva.services.share_analytics import log_referrer_visit


@app.get("/crosscuts", response_class=HTMLResponse)
async def crosscuts_list(request: Request) -> HTMLResponse:
    """List of all past crosscut episodes, newest first."""
    db = request.app.state.db
    crosscuts = load_crosscut_episodes(db)
    return templates.TemplateResponse(
        request, "crosscuts_list.html",
        {"crosscuts": crosscuts},
    )


@app.get("/crosscut/{edition_id}", response_class=HTMLResponse)
async def crosscut_detail(request: Request, edition_id: int) -> HTMLResponse:
    """One crosscut episode page. 404 if the edition isn't a published
    crosscut with audio.

    Tries the main DB first (the editorial catalog + any pre-split
    listener episodes), then the listener DB (everything built since
    the 2026-07-06 split — see aarva/listener_db.py). Edition ids are
    independent per-DB sequences, so this is a real ambiguity in
    principle, but in practice ids collide only when both DBs happen
    to have a row at that id — main-DB editorial content is checked
    first and always wins that case."""
    db = request.app.state.db
    listener_db = request.app.state.listener_db
    rows = load_crosscut_episodes(db, edition_id=edition_id)
    if not rows:
        rows = load_listener_episodes(listener_db, edition_id=edition_id)
    if not rows:
        raise HTTPException(status_code=404, detail="Crosscut not found")

    # Share analytics (2026-07-16) — see aarva/services/share_analytics.py.
    log_referrer_visit(
        listener_db, "crosscut", int(edition_id),
        request.headers.get("referer", ""), request.url.hostname or "",
    )

    return templates.TemplateResponse(
        request, "crosscut.html",
        {
            "crosscut": rows[0],
            "card_color": "peach",
        },
    )

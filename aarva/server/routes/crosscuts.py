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
from aarva.services.queries import load_crosscut_episodes


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
    crosscut with audio."""
    db = request.app.state.db
    rows = load_crosscut_episodes(db, edition_id=edition_id)
    if not rows:
        raise HTTPException(status_code=404, detail="Crosscut not found")
    return templates.TemplateResponse(
        request, "crosscut.html",
        {
            "crosscut": rows[0],
            "card_color": "peach",
        },
    )

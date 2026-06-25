"""Per-crosscut detail page.

  /crosscut/<edition_id> — full crosscut episode view: title, both
                           source articles, big play button, intro /
                           bridge / outro text + show notes, links out
                           to the two original articles.

Wraps the content in a peach card to match the home-page crosscut tile.
"""
from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from aarva.server.app import app
from aarva.server.templates import templates
from aarva.services.queries import load_crosscut_episodes


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

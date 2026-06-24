"""Landing page at `/` — explains Aarva to first-time visitors.

For listeners who already know what Aarva is, /today is the natural
entry point. This page is for the first arrival: tagline, what it is,
why it exists, how to start. Heavy on type, light on chrome.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse

from aarva.server.app import app
from aarva.server.templates import templates


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request) -> HTMLResponse:
    """Marketing landing. Shows the tagline + an explanation + a CTA
    to /today. Counts of pieces / publications give weight to the
    pitch without being a number-fest."""
    db = request.app.state.db
    with db.connect() as conn:
        # Quick stats to anchor the pitch — pieces published + publications
        # we draw from. Both counts are cheap (existing indices).
        n_pieces = conn.execute("""
            SELECT COUNT(*) AS n
              FROM edition_pieces
             WHERE audio_url IS NOT NULL AND audio_url != ''
        """).fetchone()["n"]
        n_pubs = conn.execute("""
            SELECT COUNT(DISTINCT p.id) AS n
              FROM publications p
              JOIN articles a ON a.publication_id = p.id
              JOIN edition_pieces ep ON ep.article_id = a.id
             WHERE ep.audio_url IS NOT NULL AND ep.audio_url != ''
        """).fetchone()["n"]
    return templates.TemplateResponse(
        request, "landing.html",
        {
            "n_pieces": int(n_pieces or 0),
            "n_pubs": int(n_pubs or 0),
        },
    )

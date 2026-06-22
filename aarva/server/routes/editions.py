"""Past-editions browsing.

  /editions       — list page of recent daily editions (date + topic)
  /edition/<date> — a specific daily edition's pieces, JTBD-grouped

Reuses the JTBD grouper from home.py so both views render the same
section structure.
"""
from __future__ import annotations

from datetime import date

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from aarva.server.app import app
from aarva.server.routes.home import _group_pieces_by_jtbd
from aarva.server.templates import templates
from aarva.services.queries import (
    load_crosscut_episodes,
    load_daily_pieces_with_audio,
)


@app.get("/editions", response_class=HTMLResponse)
async def list_editions(request: Request) -> HTMLResponse:
    """List recent daily editions so listeners can browse the back
    catalogue. Newest first; clicking a row links to /edition/<date>."""
    db = request.app.state.db
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT id, edition_date, published_date
              FROM editions
             WHERE edition_type = 'daily'
             ORDER BY edition_date DESC, id DESC
             LIMIT 60
        """).fetchall()
    return templates.TemplateResponse(
        request, "editions_list.html",
        {
            "editions": [dict(r) for r in rows],
        },
    )


@app.get("/edition/{edition_date}", response_class=HTMLResponse)
async def edition_detail(request: Request, edition_date: str) -> HTMLResponse:
    """One day's daily edition + crosscut. URL-friendly date format
    (YYYY-MM-DD). 404 if no daily exists for that date."""
    try:
        ed_date = date.fromisoformat(edition_date)
    except ValueError:
        raise HTTPException(status_code=404, detail="Bad edition date")

    db = request.app.state.db
    with db.connect() as conn:
        row = conn.execute("""
            SELECT id FROM editions
             WHERE edition_type = 'daily'
               AND edition_date = ?
             ORDER BY id DESC
             LIMIT 1
        """, (ed_date.isoformat(),)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No edition for that date")

    pieces = load_daily_pieces_with_audio(db, edition_id=int(row["id"]))
    grouped = _group_pieces_by_jtbd(pieces)

    crosscuts = load_crosscut_episodes(db)
    crosscut = next(
        (cc for cc in crosscuts if str(cc["edition_date"]) == ed_date.isoformat()),
        None,
    )

    return templates.TemplateResponse(
        request, "home.html",
        {
            "edition_date": ed_date.isoformat(),
            "grouped_pieces": grouped,
            "crosscut": crosscut,
        },
    )

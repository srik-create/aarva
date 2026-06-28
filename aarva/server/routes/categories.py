"""Browse-by-category routes.

  /categories         — list page of the editorial JTBDs (Future-gazing,
                        Behind the news, For the curious, Small delights,
                        Smart escape) with their descriptions and counts.
  /category/<slug>    — every published piece tagged with that JTBD,
                        newest first, capped at 100 for the v1 view.

Slug values match the slug field in JTBD_INFO (e.g. 'future-gazing',
'behind-the-news'). Underscored database keys are NOT used in URLs.
"""
from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from aarva.server.app import app
from aarva.server.jtbd_meta import JTBD_BY_SLUG, JTBD_INFO
from aarva.server.templates import templates


@app.get("/categories", response_class=HTMLResponse)
async def categories_list(request: Request) -> HTMLResponse:
    """List page: one card per JTBD with description + piece count,
    plus a 'Crosscuts' tile that points at the crosscut browse page."""
    db = request.app.state.db
    with db.connect() as conn:
        # Count pieces per JTBD (using primary OR secondary). One query
        # avoids N round trips.
        rows = conn.execute("""
            SELECT s.jtbd_primary AS k, COUNT(*) AS n
              FROM article_scores s
              JOIN edition_pieces ep ON ep.article_id = s.article_id
             WHERE ep.audio_url IS NOT NULL AND ep.audio_url != ''
             GROUP BY s.jtbd_primary
        """).fetchall()
        # Count distinct crosscut episodes too — surfaced in the
        # Crosscuts tile below the JTBD cards.
        crosscut_row = conn.execute("""
            SELECT COUNT(*) AS n
              FROM editions e
              JOIN edition_pieces ep ON ep.edition_id = e.id
             WHERE e.edition_type = 'crosscut'
               AND ep.audio_url IS NOT NULL AND ep.audio_url != ''
               AND ep.position = 0
               AND ep.flagged_at IS NULL
        """).fetchone()
        crosscut_count = int(crosscut_row["n"]) if crosscut_row else 0
    counts: dict[str, int] = {r["k"]: int(r["n"]) for r in rows if r["k"]}

    categories = [
        {
            "label": j["label"],
            "slug": j["slug"],
            "card_color": j["card_color"],
            "description": j["description"],
            "count": counts.get(j["key"], 0),
        }
        for j in JTBD_INFO
    ]
    return templates.TemplateResponse(
        request, "categories_list.html",
        {
            "categories": categories,
            "crosscut_count": crosscut_count,
        },
    )


@app.get("/category/{slug}", response_class=HTMLResponse)
async def category_detail(request: Request, slug: str) -> HTMLResponse:
    """All pieces under a JTBD, newest first."""
    info = JTBD_BY_SLUG.get(slug)
    if not info:
        raise HTTPException(status_code=404, detail="Category not found")

    db = request.app.state.db
    with db.connect() as conn:
        # All pieces with audio, filtered by jtbd_primary OR jtbd_secondary
        # equal to this category key, newest first.
        rows = conn.execute("""
            SELECT ep.article_id, ep.hook,
                   ep.audio_url, ep.duration_seconds, ep.narrator_voice,
                   a.title, a.byline,
                   p.name AS publication_name,
                   e.edition_date,
                   s.jtbd_primary
              FROM edition_pieces ep
              JOIN editions e   ON e.id = ep.edition_id
              JOIN articles a   ON a.id = ep.article_id
              JOIN publications p ON p.id = a.publication_id
              JOIN article_scores s ON s.article_id = a.id
             WHERE ep.audio_url IS NOT NULL AND ep.audio_url != ''
               AND (s.jtbd_primary = ? OR s.jtbd_secondary = ?)
               AND ep.flagged_at IS NULL
             ORDER BY e.edition_date DESC, ep.position
             LIMIT 100
        """, (info["key"], info["key"])).fetchall()

    pieces = [dict(r) for r in rows]
    return templates.TemplateResponse(
        request, "category_detail.html",
        {
            "info": info,
            "pieces": pieces,
        },
    )

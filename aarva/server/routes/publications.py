"""Browse-by-publication routes.

  /publications              — list of publications that have at least one
                               published piece, alphabetised, with counts.
  /publication/<slug>        — every published piece from that publication,
                               newest first, capped at 100.

Slugs are derived from publication.name via _publication_slug() (lower-
case, ASCII, hyphens). Resolution is by full-table scan over the small
publications list (~70 rows) — no slug column in the DB, no migration.
"""
from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from aarva.server.app import app
from aarva.server.templates import _publication_slug, templates


def _resolve_publication_by_slug(db, slug: str) -> dict | None:
    """Find the publication whose slugify(name) matches `slug`.
    Linear scan over publications — fine at v0.1 scale (~70 rows)."""
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT id, name, homepage
              FROM publications
        """).fetchall()
    for r in rows:
        if _publication_slug(r["name"]) == slug:
            return dict(r)
    return None


@app.get("/publications", response_class=HTMLResponse)
async def publications_list(request: Request) -> HTMLResponse:
    """List of publications with at least one published piece (with audio)."""
    db = request.app.state.db
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT p.id, p.name, p.homepage, COUNT(*) AS n_pieces
              FROM publications p
              JOIN articles a ON a.publication_id = p.id
              JOIN edition_pieces ep ON ep.article_id = a.id
             WHERE ep.audio_url IS NOT NULL AND ep.audio_url != ''
               AND ep.flagged_at IS NULL
             GROUP BY p.id
             ORDER BY p.name COLLATE NOCASE
        """).fetchall()
    publications = [
        {
            "name": r["name"],
            "slug": _publication_slug(r["name"]),
            "homepage": r["homepage"],
            "n_pieces": int(r["n_pieces"]),
        }
        for r in rows
    ]
    return templates.TemplateResponse(
        request, "publications_list.html",
        {"publications": publications},
    )


@app.get("/publication/{slug}", response_class=HTMLResponse)
async def publication_detail(request: Request, slug: str) -> HTMLResponse:
    """All pieces from one publication, newest first."""
    db = request.app.state.db
    pub = _resolve_publication_by_slug(db, slug)
    if not pub:
        raise HTTPException(status_code=404, detail="Publication not found")

    with db.connect() as conn:
        rows = conn.execute("""
            SELECT ep.article_id, ep.hook,
                   ep.audio_url, ep.duration_seconds, ep.narrator_voice,
                   a.title, a.byline,
                   e.edition_date,
                   s.jtbd_primary
              FROM edition_pieces ep
              JOIN editions e   ON e.id = ep.edition_id
              JOIN articles a   ON a.id = ep.article_id
              JOIN article_scores s ON s.article_id = a.id
             WHERE a.publication_id = ?
               AND ep.audio_url IS NOT NULL AND ep.audio_url != ''
               AND ep.flagged_at IS NULL
             ORDER BY e.edition_date DESC, ep.position
             LIMIT 100
        """, (int(pub["id"]),)).fetchall()
    pieces = [dict(r) for r in rows]

    return templates.TemplateResponse(
        request, "publication_detail.html",
        {
            "publication": pub,
            "pieces": pieces,
        },
    )

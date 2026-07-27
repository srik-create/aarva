"""Per-article detail page.

  /article/<id> — full article view: title, byline, hook, contextualisation,
                  show notes, audio player, link out to the publication.

This is the page listeners land on when they click an item in
today's edition (or in search results, Phase 2). Each article lives
inside one or more editions — we surface which edition(s) it appeared
in to give it temporal context.
"""
from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from aarva.server.app import app
from aarva.server.jtbd_meta import card_color_for_jtbd
from aarva.server.templates import templates
from aarva.services.share_analytics import log_referrer_visit


@app.get("/article/{article_id}", response_class=HTMLResponse)
async def article_detail(request: Request, article_id: int) -> HTMLResponse:
    """One article's full audio+text page. 404 if the article doesn't
    have audio published (or doesn't exist)."""
    db = request.app.state.db

    with db.connect() as conn:
        # Article + publication + most-recent edition_piece (carries
        # the audio URL, hook, contextualisation, narrator voice).
        row = conn.execute("""
            SELECT a.id, a.title, a.byline, a.canonical_url,
                   a.word_count, a.published_date,
                   p.name AS publication_name,
                   ep.hook, ep.contextualisation, ep.show_notes,
                   ep.audio_url, ep.duration_seconds, ep.narrator_voice,
                   e.edition_date, e.edition_type,
                   s.jtbd_primary, s.jtbd_secondary, s.lens, s.pillar
              FROM articles a
              JOIN publications p ON p.id = a.publication_id
              JOIN edition_pieces ep ON ep.article_id = a.id
              JOIN editions e ON e.id = ep.edition_id
              LEFT JOIN article_scores s ON s.article_id = a.id
             WHERE a.id = ?
               AND ep.audio_url IS NOT NULL AND ep.audio_url != ''
             ORDER BY e.edition_date DESC, e.id DESC
             LIMIT 1
        """, (article_id,)).fetchone()

    if not row:
        # Not in the main DB's edition_pieces — this happens for
        # articles that have only ever been used in a listener-built
        # episode (common: on-demand builds often pull articles that
        # never made the daily cut). Those editions/edition_pieces
        # live in the listener DB instead (see aarva/listener_db.py),
        # which denormalizes title/publication/byline since it has no
        # `articles` table to join. hook/contextualisation/
        # canonical_url/word_count/published_date/jtbd-* aren't
        # available there — article.html already renders fine without
        # them (all guarded with {% if %}).
        listener_db = request.app.state.listener_db
        with listener_db.connect() as conn:
            row = conn.execute("""
                SELECT ep.article_id AS id, ep.article_title AS title,
                       ep.article_byline AS byline,
                       ep.article_publication AS publication_name,
                       ep.show_notes, ep.audio_url, ep.duration_seconds,
                       ep.narrator_voice, e.edition_date, e.edition_type
                  FROM edition_pieces ep
                  JOIN editions e ON e.id = ep.edition_id
                 WHERE ep.article_id = ?
                   AND ep.audio_url IS NOT NULL AND ep.audio_url != ''
                 ORDER BY e.edition_date DESC, e.id DESC
                 LIMIT 1
            """, (article_id,)).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Article not found")

    piece = dict(row)

    # Share analytics (2026-07-16) — rough proxy for "where this got
    # shared to" since Web Share doesn't expose the destination
    # platform. See aarva/services/share_analytics.py.
    log_referrer_visit(
        request.app.state.listener_db, "article", int(piece["id"]),
        request.headers.get("referer", ""), request.url.hostname or "",
    )

    # Wrap the article in a card whose colour matches the JTBD tile
    # that brought the listener here (falls back to 'paper' for
    # listener-created articles, which have no jtbd_primary).
    card_color = card_color_for_jtbd(piece.get("jtbd_primary"))

    return templates.TemplateResponse(
        request, "article.html",
        {
            "piece": piece,
            "card_color": card_color,
        },
    )

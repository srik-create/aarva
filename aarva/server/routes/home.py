"""Home page (`/`) — today's daily edition, JTBD-grouped.

Shows the most recent daily edition's pieces, organised by JTBD
(deep_feature / lens cards / curiosity / smart_escape / delight).
If today hasn't published yet, falls back to the latest daily.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import date
from typing import Optional

from fastapi import Request
from fastapi.responses import HTMLResponse

from aarva.server.app import app
from aarva.server.templates import templates
from aarva.services.queries import (
    load_crosscut_episodes,
    load_daily_pieces_with_audio,
)


# Display order for JTBD sections on the home page. Each entry:
#   (jtbd_key, display_label, card_color, header_text_color)
# - card_color: Tailwind colour token used for the article card backgrounds
#   in that section. Each JTBD gets its own pastel.
# - header_text_color: lighter tint for the section label text on the dark
#   page background.
_JTBD_DISPLAY_ORDER = [
    ("keep_ahead",       "Future-gazing",   "sky",      "sky"),
    ("keep_up_to_date",  "Behind the news", "lavender", "lavender"),
    ("curiosity",        "For the curious", "lemon",    "lemon"),
    ("delight",          "Small delights",  "blush",    "blush"),
    ("smart_escape",     "Smart escape",    "mint",     "mint"),
    ("other",            "Other reads",     "paper",    "cream-light"),
]


def _group_pieces_by_jtbd(pieces: list[dict]) -> list[dict]:
    """Bucket pieces into JTBD groups in display order. Returns a list
    of dicts {label, card_color, header_color, pieces} ready for Jinja
    iteration. Empty groups are omitted so the template doesn't render
    empty sections."""
    buckets: dict[str, list[dict]] = {
        key: [] for key, _, _, _ in _JTBD_DISPLAY_ORDER
    }
    for p in pieces:
        jtbd = p.get("jtbd_primary") or "other"
        if jtbd not in buckets:
            jtbd = "other"
        buckets[jtbd].append(p)
    grouped: list[dict] = []
    for key, label, card_color, header_color in _JTBD_DISPLAY_ORDER:
        if buckets[key]:
            grouped.append({
                "label": label,
                "card_color": card_color,
                "header_color": header_color,
                "pieces": buckets[key],
            })
    return grouped


def _latest_daily_edition_id(db) -> Optional[int]:
    """Return the most recent daily edition's id, or None if no daily
    has published yet."""
    with db.connect() as conn:
        row = conn.execute("""
            SELECT id FROM editions
             WHERE edition_type = 'daily'
             ORDER BY edition_date DESC, id DESC
             LIMIT 1
        """).fetchone()
    return int(row["id"]) if row else None


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    """Today's daily edition, JTBD-grouped, with the day's crosscut
    (if one was published) appearing as a featured card above."""
    db = request.app.state.db
    pipeline_cfg = request.app.state.pipeline_cfg

    edition_id = _latest_daily_edition_id(db)
    if edition_id is None:
        # Pre-launch state — DB has articles but no edition yet.
        return templates.TemplateResponse(
            request, "home_empty.html", {},
        )

    pieces = load_daily_pieces_with_audio(db, edition_id=edition_id)
    grouped = _group_pieces_by_jtbd(pieces)
    edition_date_str = (
        pieces[0]["edition_date"] if pieces else date.today().isoformat()
    )

    # Today's crosscut, if any. Match on the same edition_date as the
    # daily so we don't surface yesterday's crosscut alongside today's
    # daily during the brief window between daily publish + crosscut
    # publish.
    crosscuts = load_crosscut_episodes(db) if pieces else []
    todays_crosscut = next(
        (cc for cc in crosscuts if str(cc["edition_date"]) == edition_date_str),
        None,
    )

    return templates.TemplateResponse(
        request, "home.html",
        {
            "edition_date": edition_date_str,
            "grouped_pieces": grouped,
            "crosscut": todays_crosscut,
        },
    )

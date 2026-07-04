"""Today's daily edition view (`/today`).

Shows the most recent daily edition's pieces, organised by JTBD
(deep_feature / lens cards / curiosity / smart_escape / delight).
If today hasn't published yet, falls back to the latest daily.

Routes:
  /today  — today's daily edition (JTBD-grouped, with today's crosscut
            surfaced above if one is published).
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import Request
from fastapi.responses import HTMLResponse

from aarva.server.app import app
from aarva.server.jtbd_meta import JTBD_INFO
from aarva.server.templates import templates
from aarva.services.queries import (
    load_bonus_pieces_with_audio,
    load_crosscut_episodes,
    load_daily_pieces_with_audio,
)


# Display order for "Other reads" — pieces with a JTBD outside the
# main editorial taxonomy, or unknown JTBDs. Rendered at the bottom of
# the daily.
_OTHER_GROUP = {
    "label": "Other reads",
    "card_color": "paper",
    "header_color": "cream-light",
}


def _group_pieces_by_jtbd(pieces: list[dict]) -> list[dict]:
    """Bucket pieces into JTBD groups in display order. Returns a list
    of dicts {label, card_color, header_color, slug, pieces} ready for
    Jinja iteration. Empty groups are omitted so the template doesn't
    render empty sections."""
    buckets: dict[str, list[dict]] = {j["key"]: [] for j in JTBD_INFO}
    other_bucket: list[dict] = []
    known_keys = set(buckets.keys())

    for p in pieces:
        jtbd = p.get("jtbd_primary")
        if jtbd in known_keys:
            buckets[jtbd].append(p)
        else:
            other_bucket.append(p)

    grouped: list[dict] = []
    for j in JTBD_INFO:
        if buckets[j["key"]]:
            grouped.append({
                "label": j["label"],
                "card_color": j["card_color"],
                "header_color": j["header_color"],
                "slug": j["slug"],
                "pieces": buckets[j["key"]],
            })
    if other_bucket:
        grouped.append({**_OTHER_GROUP, "slug": None, "pieces": other_bucket})
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


@app.get("/today", response_class=HTMLResponse)
async def today(request: Request) -> HTMLResponse:
    """Today's daily edition, JTBD-grouped, with the day's crosscut
    (if one was published) appearing as a featured card above."""
    db = request.app.state.db

    edition_id = _latest_daily_edition_id(db)
    if edition_id is None:
        return templates.TemplateResponse(
            request, "home_empty.html", {},
        )

    pieces = load_daily_pieces_with_audio(db, edition_id=edition_id)
    grouped = _group_pieces_by_jtbd(pieces)
    edition_date_str = (
        pieces[0]["edition_date"] if pieces else date.today().isoformat()
    )

    crosscuts = load_crosscut_episodes(db) if pieces else []
    todays_crosscut = next(
        (cc for cc in crosscuts if str(cc["edition_date"]) == edition_date_str),
        None,
    )

    # Bonus episodes whose edition_date is today's daily-edition date.
    # Rendered as a compact section between the crosscut card (above)
    # and the JTBD-grouped daily pieces (below). Empty list -> the
    # template skips the whole section, so days without a bonus look
    # exactly as before this change.
    try:
        edition_dt = date.fromisoformat(edition_date_str)
    except ValueError:
        edition_dt = date.today()
    todays_bonuses = load_bonus_pieces_with_audio(db, since_date=edition_dt)
    # since_date is a floor; if the operator published bonuses for a
    # later date they'd sneak in — filter to strict equality on the
    # daily edition's date so /today matches the day being shown.
    todays_bonuses = [
        b for b in todays_bonuses if str(b["edition_date"]) == edition_date_str
    ]

    return templates.TemplateResponse(
        request, "home.html",
        {
            "edition_date": edition_date_str,
            "grouped_pieces": grouped,
            "crosscut": todays_crosscut,
            "bonus_pieces": todays_bonuses,
        },
    )

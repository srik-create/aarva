"""JTBD metadata — single source of truth for category labels, card
colours, and editorial descriptions used across the web app.

Used by:
  - routes/home.py        (sections on the daily edition page)
  - routes/articles.py    (card colour for the per-article detail page)
  - routes/crosscuts.py   (crosscut card colour; always 'peach')
  - routes/categories.py  (the browse-by-category list and per-JTBD page)

Adding a new JTBD: add an entry here in editorial display order, then
make sure prompts.yaml's Stage 4-5-6 prompt knows about it (or it
won't appear in scored articles).
"""
from __future__ import annotations

from typing import TypedDict


class JTBDInfo(TypedDict):
    key: str            # database value (jtbd_primary / jtbd_secondary)
    label: str          # display label
    slug: str           # URL slug for /category/<slug>
    card_color: str     # Tailwind token for the article-tile background
    header_color: str   # Tailwind token for section labels on dark bg
    description: str    # one-line editorial description (shown on
                        # /categories list + per-category page)


JTBD_INFO: list[JTBDInfo] = [
    {
        "key": "keep_ahead",
        "label": "Future-gazing",
        "slug": "future-gazing",
        "card_color": "sky",
        "header_color": "sky",
        "description":
            "Pieces about where the world is heading — emerging technologies, "
            "shifting institutions, the contours of what's coming next.",
    },
    {
        "key": "keep_up_to_date",
        "label": "Behind the news",
        "slug": "behind-the-news",
        "card_color": "lavender",
        "header_color": "lavender",
        "description":
            "Context, analysis and reporting that goes deeper than the "
            "headlines on stories already in the air.",
    },
    {
        "key": "curiosity",
        "label": "For the curious",
        "slug": "for-the-curious",
        "card_color": "lemon",
        "header_color": "lemon",
        "description":
            "Pieces that scratch an itch you didn't know you had — "
            "science, history, ideas, the shape of overlooked things.",
    },
    {
        "key": "delight",
        "label": "Small delights",
        "slug": "small-delights",
        "card_color": "blush",
        "header_color": "blush",
        "description":
            "Genuinely fun, playful, sometimes odd writing. Humour, wit, "
            "objects of unexpected beauty. The send-the-listener-off-"
            "smiling slot.",
    },
    {
        "key": "smart_escape",
        "label": "Smart escape",
        "slug": "smart-escape",
        "card_color": "mint",
        "header_color": "mint",
        "description":
            "Restorative reading. Long-form essays and narrative journalism "
            "that pull you out of the day without skimping on thought.",
    },
]


# Lookups — built once at import time from JTBD_INFO. Use these in
# route handlers rather than scanning the list each request.
JTBD_BY_KEY: dict[str, JTBDInfo] = {j["key"]: j for j in JTBD_INFO}
JTBD_BY_SLUG: dict[str, JTBDInfo] = {j["slug"]: j for j in JTBD_INFO}


def card_color_for_jtbd(jtbd_primary: str | None) -> str:
    """Return the card-colour token for a JTBD key. Falls back to
    'paper' for unknown / missing JTBDs."""
    if not jtbd_primary:
        return "paper"
    info = JTBD_BY_KEY.get(jtbd_primary)
    return info["card_color"] if info else "paper"

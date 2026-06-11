"""Per-user feed composition.

The hybrid model:
  - The shared global daily edition + crosscut are produced by the
    pipeline (no user_id).
  - Each user gets a personalised view: (shared items - their
    dismissals) + (their bonus picks).
  - The same JSON shape is returned for the web UI (HTMX) and the
    podcast RSS endpoint.

Routes call `get_user_feed(user_id, since=date)` and serialise the
result. Pure read function — no DB writes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Optional

from aarva.db import Database
from aarva.services.actions import get_dismissed_articles_for_user
from aarva.services.queries import (
    load_daily_pieces_with_audio,
    load_bonus_pieces_with_audio,
    load_crosscut_episodes,
)


@dataclass(frozen=True)
class FeedItem:
    """One playable item in a user's personalised feed."""
    kind: str                       # 'daily' | 'crosscut' | 'bonus'
    edition_id: int
    edition_date: str
    article_id: Optional[int]       # None for crosscut (it's the pair, not a single piece)
    title: str
    publication: Optional[str]
    byline: Optional[str]
    canonical_url: Optional[str]
    hook: Optional[str]
    context: Optional[str]
    show_notes: Optional[str]
    audio_url: Optional[str]
    duration_seconds: Optional[int]
    narrator_voice: Optional[str]
    is_dismissable: bool            # bonus eps not dismissable (user picked them)
    # Extra fields for crosscut items
    topic_label: Optional[str] = None
    intro_text: Optional[str] = None
    outro_text: Optional[str] = None
    bridge_between: Optional[str] = None
    article_a: Optional[dict[str, Any]] = None
    article_b: Optional[dict[str, Any]] = None


def _row_to_daily_item(row: Any, dismissable: bool = True) -> FeedItem:
    return FeedItem(
        kind="daily",
        edition_id=int(row["edition_id"]),
        edition_date=str(row["edition_date"]),
        article_id=int(row["article_id"]),
        title=row["title"] or "",
        publication=row["publication_name"],
        byline=row["byline"],
        canonical_url=row["canonical_url"],
        hook=row["hook"],
        context=row["contextualisation"],
        show_notes=row["show_notes"],
        audio_url=row["audio_url"],
        duration_seconds=(int(row["duration_seconds"])
                          if row["duration_seconds"] else None),
        narrator_voice=row["narrator_voice"],
        is_dismissable=dismissable,
    )


def _row_to_crosscut_item(row: Any) -> FeedItem:
    return FeedItem(
        kind="crosscut",
        edition_id=int(row["edition_id"]),
        edition_date=str(row["edition_date"]),
        article_id=None,
        title=f"Crosscut: {row['topic_label']}" if row["topic_label"] else "Crosscut",
        publication=None,
        byline=None,
        canonical_url=None,
        hook=None,
        context=None,
        show_notes=None,
        audio_url=row["audio_url"],
        duration_seconds=(int(row["duration_seconds"])
                          if row["duration_seconds"] else None),
        narrator_voice=row["narrator_voice"],
        is_dismissable=True,
        topic_label=row["topic_label"],
        intro_text=row["intro_text"],
        outro_text=row["outro_text"],
        bridge_between=row["bridge_between"],
        article_a={
            "title": row["title_a"], "byline": row["byline_a"],
            "publication": row["pub_a"], "canonical_url": row["url_a"],
        },
        article_b={
            "title": row["title_b"], "byline": row["byline_b"],
            "publication": row["pub_b"], "canonical_url": row["url_b"],
        },
    )


def get_user_feed(
    db: Database,
    user_id: int,
    *,
    since_days: int = 30,
) -> list[FeedItem]:
    """Compose the personalised feed for a user.

    Combines:
      - shared daily editions (minus articles the user has dismissed),
      - shared crosscut episodes (minus dismissed),
      - the user's own bonus picks.

    Ordered most-recent first, with intra-day ordering: bonus picks
    surface above daily pieces above crosscut (since bonus is the
    most personal signal — the user picked it).
    """
    since = date.today() - timedelta(days=since_days)
    dismissed = get_dismissed_articles_for_user(db, user_id)

    daily_rows = load_daily_pieces_with_audio(db, since_date=since)
    crosscut_rows = load_crosscut_episodes(db, since_date=since)
    bonus_rows = load_bonus_pieces_with_audio(db, user_id=user_id, since_date=since)

    items: list[FeedItem] = []

    # Bonus first within their date — user-curated.
    for r in bonus_rows:
        item = _row_to_daily_item(r, dismissable=False)
        items.append(FeedItem(**{**item.__dict__, "kind": "bonus"}))

    # Then daily, filtered by dismissals.
    for r in daily_rows:
        if int(r["article_id"]) in dismissed:
            continue
        items.append(_row_to_daily_item(r, dismissable=True))

    # Then crosscut. Dismissals for crosscut are keyed off either
    # article in the pair — if the user has dismissed EITHER source,
    # skip the crosscut.
    for r in crosscut_rows:
        a_id = int(r["article_a_id"]) if r["article_a_id"] else None
        b_id = int(r["article_b_id"]) if r["article_b_id"] else None
        if (a_id and a_id in dismissed) or (b_id and b_id in dismissed):
            continue
        items.append(_row_to_crosscut_item(r))

    # Final sort: by edition_date desc; within a date keep insertion
    # order (bonus → daily → crosscut), which reads as personal →
    # global news → contextual paired listening.
    items.sort(key=lambda it: it.edition_date, reverse=True)
    return items

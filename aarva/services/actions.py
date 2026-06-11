"""User actions — dismiss / like / listened / completed signals.

Stored as rows in user_actions. The feed service consumes these to
filter / personalise what each user sees. Phase B will use the same
table for taste-centroid updates.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from aarva.db import Database


VALID_ACTIONS = {"dismissed", "liked", "disliked",
                 "listened", "completed", "shared"}


@dataclass(frozen=True)
class UserAction:
    id: int
    user_id: int
    article_id: int
    action: str
    created_at: str
    metadata: dict[str, Any]


def _row_to_action(row: Any) -> UserAction:
    meta = {}
    if row["metadata_json"]:
        try:
            meta = json.loads(row["metadata_json"])
        except (json.JSONDecodeError, TypeError):
            meta = {}
    return UserAction(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        article_id=int(row["article_id"]),
        action=row["action"],
        created_at=str(row["created_at"]),
        metadata=meta,
    )


def record_action(
    db: Database,
    user_id: int,
    article_id: int,
    action: str,
    *,
    metadata: Optional[dict[str, Any]] = None,
) -> UserAction:
    """Record a user action. Same (user, article, action) can repeat
    over time — e.g., 'listened' fires every play. We don't dedupe at
    the table level; downstream queries handle aggregation."""
    if action not in VALID_ACTIONS:
        raise ValueError(
            f"Unknown action '{action}'. Must be one of {sorted(VALID_ACTIONS)}"
        )
    meta_json = json.dumps(metadata) if metadata else None
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO user_actions "
            "(user_id, article_id, action, metadata_json) "
            "VALUES (?, ?, ?, ?)",
            (user_id, article_id, action, meta_json),
        )
        action_id = int(cur.lastrowid)
        row = conn.execute(
            "SELECT * FROM user_actions WHERE id = ?", (action_id,),
        ).fetchone()
    return _row_to_action(row)


def get_dismissed_articles_for_user(
    db: Database, user_id: int,
) -> set[int]:
    """Set of article IDs the user has dismissed. Used by the feed
    service to filter shared content out of their personalised feed.
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT article_id FROM user_actions "
            "WHERE user_id = ? AND action = 'dismissed'",
            (user_id,),
        ).fetchall()
    return {int(r["article_id"]) for r in rows}


def undismiss(db: Database, user_id: int, article_id: int) -> int:
    """Remove all 'dismissed' rows for this (user, article). The
    article will reappear in their feed on next render. Returns
    number of rows removed."""
    with db.connect() as conn:
        cur = conn.execute(
            "DELETE FROM user_actions "
            "WHERE user_id = ? AND article_id = ? AND action = 'dismissed'",
            (user_id, article_id),
        )
        return cur.rowcount


def get_recent_actions(
    db: Database,
    user_id: int,
    *,
    actions: Optional[list[str]] = None,
    limit: int = 100,
) -> list[UserAction]:
    """Recent activity for a user. Drives the "history" view and (in
    Phase B) the per-user taste centroid update batch."""
    if actions:
        unknown = set(actions) - VALID_ACTIONS
        if unknown:
            raise ValueError(f"Unknown actions: {sorted(unknown)}")
        placeholders = ",".join("?" for _ in actions)
        sql = (
            f"SELECT * FROM user_actions "
            f"WHERE user_id = ? AND action IN ({placeholders}) "
            f"ORDER BY created_at DESC LIMIT ?"
        )
        params: tuple = (user_id, *actions, limit)
    else:
        sql = (
            "SELECT * FROM user_actions WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT ?"
        )
        params = (user_id, limit)
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_action(r) for r in rows]

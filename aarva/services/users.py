"""User management — create / lookup / sessions.

Auth model: magic-link only (no passwords). Caller flow:
  1. Anonymous user submits email → `request_magic_link(email)`.
     A short-lived token row is created; the caller emails the link.
  2. User clicks link → `verify_magic_link(token)` consumes the token,
     creates (or fetches) the User, returns a fresh session token.
  3. Browser stores the session token as an HttpOnly cookie. Every
     subsequent request calls `get_user_for_session(token)` → User dict
     or None.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from aarva.db import Database
from aarva.exceptions import NotFoundError


# Token TTLs — kept generous for the laptop deployment, tighten for prod.
MAGIC_LINK_TTL = timedelta(minutes=20)
SESSION_TTL = timedelta(days=30)


@dataclass(frozen=True)
class User:
    id: int
    email: str
    name: Optional[str]
    is_admin: bool
    created_at: str
    last_login_at: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _row_to_user(row: Any) -> User:
    return User(
        id=int(row["id"]),
        email=row["email"],
        name=row["name"],
        is_admin=bool(row["is_admin"]),
        created_at=str(row["created_at"]),
        last_login_at=(str(row["last_login_at"])
                       if row["last_login_at"] else None),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ─── Lookups ─────────────────────────────────────────────────────────────

def get_user_by_id(db: Database, user_id: int) -> Optional[User]:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,),
        ).fetchone()
    return _row_to_user(row) if row else None


def get_user_by_email(db: Database, email: str) -> Optional[User]:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ? COLLATE NOCASE",
            (email.strip(),),
        ).fetchone()
    return _row_to_user(row) if row else None


def list_users(db: Database) -> list[User]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY created_at DESC"
        ).fetchall()
    return [_row_to_user(r) for r in rows]


# ─── Magic-link flow ─────────────────────────────────────────────────────

def request_magic_link(db: Database, email: str, ip: Optional[str] = None) -> str:
    """Mint a magic-link token for the given email. Returns the token
    so the caller can email it. The email is normalised to lowercase
    so a future signup with the same case-shifted email finds the
    same user."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("Invalid email address.")
    token = secrets.token_urlsafe(32)
    expires_at = _iso(_now() + MAGIC_LINK_TTL)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO magic_link_tokens (token, email, expires_at, ip) "
            "VALUES (?, ?, ?, ?)",
            (token, email, expires_at, ip),
        )
    return token


def verify_magic_link(
    db: Database,
    token: str,
    *,
    user_agent: Optional[str] = None,
    ip: Optional[str] = None,
) -> tuple[User, str]:
    """Consume a magic-link token. Creates the user if first time;
    mints a session token. Returns (user, session_token).

    Raises NotFoundError if the token is missing, expired, or already
    consumed. Caller should map this to HTTP 401 + a "request a new
    link" UI affordance.
    """
    now = _now()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT email, expires_at, consumed_at FROM magic_link_tokens "
            "WHERE token = ?", (token,),
        ).fetchone()
        if not row:
            raise NotFoundError("Magic link not found or already used.")
        if row["consumed_at"]:
            raise NotFoundError("Magic link already used.")
        if datetime.fromisoformat(row["expires_at"]) < now:
            raise NotFoundError("Magic link has expired. Request a new one.")
        email = row["email"]

        # Mark token consumed.
        conn.execute(
            "UPDATE magic_link_tokens SET consumed_at = ? WHERE token = ?",
            (_iso(now), token),
        )

        # Fetch-or-create user.
        user_row = conn.execute(
            "SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email,),
        ).fetchone()
        if not user_row:
            cur = conn.execute(
                "INSERT INTO users (email, last_login_at) VALUES (?, ?)",
                (email, _iso(now)),
            )
            user_id = int(cur.lastrowid)
            user_row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,),
            ).fetchone()
        else:
            conn.execute(
                "UPDATE users SET last_login_at = ? WHERE id = ?",
                (_iso(now), int(user_row["id"])),
            )

        # Mint a session.
        session_token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO user_sessions "
            "(token, user_id, expires_at, user_agent, ip) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_token, int(user_row["id"]),
             _iso(now + SESSION_TTL), user_agent, ip),
        )

    return _row_to_user(user_row), session_token


# ─── Session lookup (called on every authenticated request) ─────────────

def get_user_for_session(db: Database, token: str) -> Optional[User]:
    """Resolve a session token to a User. Returns None if the session
    is missing, revoked, or expired. Web middleware uses this to set
    the request's current_user."""
    if not token:
        return None
    now_iso = _iso(_now())
    with db.connect() as conn:
        row = conn.execute(
            "SELECT u.* "
            "  FROM user_sessions s "
            "  JOIN users u ON u.id = s.user_id "
            " WHERE s.token = ? "
            "   AND s.revoked_at IS NULL "
            "   AND s.expires_at > ?",
            (token, now_iso),
        ).fetchone()
    return _row_to_user(row) if row else None


def revoke_session(db: Database, token: str) -> None:
    """Mark a session revoked. Idempotent."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE user_sessions SET revoked_at = CURRENT_TIMESTAMP "
            "WHERE token = ? AND revoked_at IS NULL",
            (token,),
        )

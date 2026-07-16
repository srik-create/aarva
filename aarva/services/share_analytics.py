"""Share-button + referrer analytics.

Two lightweight signals, both logged to the listener DB — not the
main DB, since every write here happens from a live Render request,
and the main DB is atomic-replaced by every laptop→Render sync (same
bug class as jobs/users; see aarva/listener_db.py's docstring):

  'share_clicked'   The share button succeeded (Web Share resolved,
                    or copy-link succeeded). No destination-platform
                    info — the Web Share API deliberately never
                    exposes which app the listener picked.
  'referrer_visit'  A page view arrived with an external Referer
                    header. The only available proxy for "where this
                    got shared to", since there are no platform-
                    specific share buttons to attribute a click to
                    directly. Browser-based platforms (X, Facebook,
                    LinkedIn) preserve the referrer; messaging apps
                    (WhatsApp, iMessage) typically strip it entirely,
                    so those shares surface as ordinary direct
                    visits, not attributed to any platform.

Both functions are best-effort and never raise — an analytics write
failing must never break the page render or the share-event request
it's attached to.
"""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlparse

from aarva.listener_db import ListenerDatabase

logger = logging.getLogger(__name__)

VALID_CONTENT_TYPES = {"article", "crosscut"}

# Known referrer hosts -> friendly platform label. Not exhaustive —
# an unrecognised external host still gets logged, just under its raw
# hostname instead of a friendly name.
_KNOWN_REFERRER_HOSTS = {
    "t.co": "X (Twitter)",
    "twitter.com": "X (Twitter)",
    "x.com": "X (Twitter)",
    "facebook.com": "Facebook",
    "l.facebook.com": "Facebook",
    "lm.facebook.com": "Facebook",
    "m.facebook.com": "Facebook",
    "linkedin.com": "LinkedIn",
    "lnkd.in": "LinkedIn",
    "instagram.com": "Instagram",
    "l.instagram.com": "Instagram",
    "reddit.com": "Reddit",
    "out.reddit.com": "Reddit",
}


def log_share_click(
    listener_db: ListenerDatabase, content_type: str, content_id: int,
) -> None:
    """Record that the share button succeeded for this content."""
    if content_type not in VALID_CONTENT_TYPES:
        return
    try:
        with listener_db.connect() as conn:
            conn.execute(
                "INSERT INTO share_signals (content_type, content_id, signal) "
                "VALUES (?, ?, 'share_clicked')",
                (content_type, int(content_id)),
            )
    except Exception as e:
        logger.warning("share_analytics: failed to log share_clicked: %s", e)


def bucket_referrer(referrer: str, own_host: str) -> Optional[str]:
    """Parse a Referer header into a friendly platform bucket, or None
    if there's nothing worth logging (no referrer, or same-origin
    navigation — in-site nav sends a real Referer too, which isn't a
    "shared to" signal)."""
    if not referrer:
        return None
    try:
        host = (urlparse(referrer).hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    own_host = (own_host or "").lower()
    if own_host and (host == own_host or host.endswith("." + own_host)):
        return None
    for known_host, label in _KNOWN_REFERRER_HOSTS.items():
        if host == known_host or host.endswith("." + known_host):
            return label
    return host


def log_referrer_visit(
    listener_db: ListenerDatabase,
    content_type: str,
    content_id: int,
    referrer: str,
    own_host: str,
) -> None:
    """Record an inbound page view that arrived with an external
    referrer. No-op if there's nothing to log."""
    if content_type not in VALID_CONTENT_TYPES:
        return
    bucket = bucket_referrer(referrer, own_host)
    if not bucket:
        return
    try:
        with listener_db.connect() as conn:
            conn.execute(
                "INSERT INTO share_signals "
                "(content_type, content_id, signal, referrer_domain) "
                "VALUES (?, ?, 'referrer_visit', ?)",
                (content_type, int(content_id), bucket),
            )
    except Exception as e:
        logger.warning("share_analytics: failed to log referrer_visit: %s", e)

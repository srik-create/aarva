"""Shared SQL queries used by both presentation modules (RSS feed,
HTML renderer) and the personalised feed service.

The same JOIN pattern (edition_pieces + articles + publications, or
the two-piece crosscut shape) was repeated across three modules with
subtly different WHERE clauses. This module centralises the joins
and lets callers compose filters explicitly.

All functions return plain dicts (sqlite3.Row → dict). Add new
filters as keyword arguments rather than spawning new functions.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Optional

from aarva.db import Database


def load_daily_pieces_with_audio(
    db: Database,
    *,
    edition_id: Optional[int] = None,
    since_date: Optional[date] = None,
    user_id_filter: Optional[int] = None,
    include_user_id_null: bool = True,
    include_flagged: bool = False,
) -> list[dict[str, Any]]:
    """Pieces from daily editions whose audio has been generated.

    edition_id           — single-edition mode (used by the HTML
                           renderer's per-edition view).
    since_date           — cutoff for batch listings (RSS feed, feed
                           service).
    user_id_filter       — restrict to this user's editions. With
                           include_user_id_null=True, returns
                           (user's editions OR shared globals).
    include_user_id_null — whether to include global (user_id IS NULL)
                           editions. Default True.
    include_flagged      — include post-hoc flagged pieces. Default
                           False (the RSS feed and web renderer both
                           hide flagged content).
    """
    where: list[str] = ["e.edition_type = 'daily'"]
    params: list[Any] = []

    if edition_id is not None:
        where.append("e.id = ?")
        params.append(edition_id)
    if since_date is not None:
        where.append("e.edition_date >= ?")
        params.append(since_date.isoformat())
    if user_id_filter is not None:
        if include_user_id_null:
            where.append("(e.user_id = ? OR e.user_id IS NULL)")
            params.append(user_id_filter)
        else:
            where.append("e.user_id = ?")
            params.append(user_id_filter)
    elif include_user_id_null:
        where.append("e.user_id IS NULL")

    where.append("ep.audio_url IS NOT NULL AND ep.audio_url != ''")
    if not include_flagged:
        where.append("ep.flagged_at IS NULL")

    sql = f"""
        SELECT ep.edition_id, ep.article_id, ep.slot, ep.position,
               ep.hook, ep.contextualisation, ep.show_notes,
               ep.audio_url, ep.duration_seconds, ep.narrator_voice,
               a.title, a.byline, a.canonical_url,
               p.name AS publication_name,
               e.edition_date, e.published_date, e.edition_type,
               e.user_id,
               s.jtbd_primary, s.jtbd_secondary, s.lens, s.pillar
          FROM edition_pieces ep
          JOIN editions e ON e.id = ep.edition_id
          JOIN articles a ON a.id = ep.article_id
          JOIN publications p ON p.id = a.publication_id
          LEFT JOIN article_scores s ON s.article_id = a.id
         WHERE {' AND '.join(where)}
         ORDER BY e.edition_date DESC, ep.position
    """
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def load_bonus_pieces_with_audio(
    db: Database,
    *,
    user_id: Optional[int] = None,
    include_global: bool = True,
    since_date: Optional[date] = None,
    include_flagged: bool = False,
) -> list[dict[str, Any]]:
    """Bonus episodes (edition_type='bonus') with audio attached.

    user_id semantics:
      - None (default): return ALL bonus episodes regardless of
        user_id. Used by the public RSS feed (one feed.xml for
        everyone) and by admin views.
      - int: return that user's own bonus episodes. If
        include_global=True (default), also include bonus episodes
        with user_id IS NULL — the "shared" pool produced by the
        CLI publish_articles flow before per-user attribution
        existed, OR by an admin acting outside any user account.
    """
    where: list[str] = [
        "e.edition_type = 'bonus'",
        "ep.audio_url IS NOT NULL AND ep.audio_url != ''",
    ]
    params: list[Any] = []
    if user_id is not None:
        if include_global:
            where.append("(e.user_id = ? OR e.user_id IS NULL)")
            params.append(user_id)
        else:
            where.append("e.user_id = ?")
            params.append(user_id)
    # When user_id is None, no user filter — return everything.

    if since_date is not None:
        where.append("e.edition_date >= ?")
        params.append(since_date.isoformat())
    if not include_flagged:
        where.append("ep.flagged_at IS NULL")

    sql = f"""
        SELECT ep.edition_id, ep.article_id, ep.slot, ep.position,
               ep.hook, ep.contextualisation, ep.show_notes,
               ep.audio_url, ep.duration_seconds, ep.narrator_voice,
               a.title, a.byline, a.canonical_url,
               p.name AS publication_name,
               e.edition_date, e.published_date, e.edition_type,
               e.user_id
          FROM edition_pieces ep
          JOIN editions e ON e.id = ep.edition_id
          JOIN articles a ON a.id = ep.article_id
          JOIN publications p ON p.id = a.publication_id
         WHERE {' AND '.join(where)}
         ORDER BY e.edition_date DESC, ep.position
    """
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def load_crosscut_episodes(
    db: Database,
    *,
    edition_id: Optional[int] = None,
    since_date: Optional[date] = None,
    include_user_id_null: bool = True,
    user_generated_only: bool = False,
    include_flagged: bool = False,
) -> list[dict[str, Any]]:
    """Crosscut episodes flattened into one row per edition with both
    pieces' metadata joined in. Used by RSS feed, HTML renderer, and
    personalised feed service.

    user_id semantics:
      - Pipeline-generated crosscuts have user_id IS NULL — these are
        the editorial daily crosscut. `/today`, `/crosscuts`, and the
        RSS feed show these.
      - Listener-generated crosscuts (web-app on-demand episodes) have
        user_id SET to the requester's user row. `/listener-created`
        shows these.

    Filter behaviour:
      - When `edition_id` is supplied (single-row lookup, e.g. the
        `/crosscut/<id>` detail page) — the user_id filter is skipped
        entirely so the detail page works for BOTH kinds. The primary
        key already uniquely identifies the row.
      - Otherwise: `user_generated_only=True` → only user_id IS NOT
        NULL; else `include_user_id_null=True` (default) → only
        user_id IS NULL; else (both False) → no user_id filter."""
    where: list[str] = [
        "e.edition_type = 'crosscut'",
        "ep_a.audio_url IS NOT NULL AND ep_a.audio_url != ''",
    ]
    params: list[Any] = []
    if edition_id is not None:
        where.append("e.id = ?")
        params.append(edition_id)
    if since_date is not None:
        where.append("e.edition_date >= ?")
        params.append(since_date.isoformat())
    # user_id filter only applied for list-style queries (edition_id
    # not specified). Detail-page lookups by ID work for any user_id.
    if edition_id is None:
        if user_generated_only:
            where.append("e.user_id IS NOT NULL")
        elif include_user_id_null:
            where.append("e.user_id IS NULL")
    if not include_flagged:
        where.append("ep_a.flagged_at IS NULL")

    sql = f"""
        SELECT e.id AS edition_id, e.edition_date, e.published_date,
               e.topic_label, e.intro_text, e.outro_text,
               ep_a.audio_url, ep_a.duration_seconds,
               ep_a.narrator_voice,
               ep_a.article_id AS article_a_id,
               ep_a.bridge_text AS bridge_a,
               ep_b.article_id AS article_b_id,
               a_a.title AS title_a, a_a.byline AS byline_a,
               a_a.canonical_url AS url_a,
               p_a.name AS pub_a,
               ep_b.bridge_text AS bridge_between,
               a_b.title AS title_b, a_b.byline AS byline_b,
               a_b.canonical_url AS url_b,
               p_b.name AS pub_b
          FROM editions e
          JOIN edition_pieces ep_a
            ON ep_a.edition_id = e.id AND ep_a.position = 0
          JOIN articles a_a ON a_a.id = ep_a.article_id
          JOIN publications p_a ON p_a.id = a_a.publication_id
          JOIN edition_pieces ep_b
            ON ep_b.edition_id = e.id AND ep_b.position = 1
          JOIN articles a_b ON a_b.id = ep_b.article_id
          JOIN publications p_b ON p_b.id = a_b.publication_id
         WHERE {' AND '.join(where)}
         ORDER BY e.edition_date DESC, e.id DESC
    """
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def load_listener_episodes(
    db: Database,
    *,
    edition_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Crosscut episodes from the listener DB (see aarva/listener_db.py),
    flattened into the same row shape as `load_crosscut_episodes` so
    templates (`listener_created.html`, `crosscut.html`) don't need to
    care which DB an episode came from.

    Title/publication/byline come from edition_pieces' denormalized
    columns instead of a join — the listener DB has no `articles` or
    `publications` tables. `url_a`/`url_b` (the source article's own
    URL, used for the optional "Read on <publication>" link) aren't
    denormalized and are simply absent here; the templates already
    guard on them being present."""
    where: list[str] = ["ep_a.audio_url IS NOT NULL AND ep_a.audio_url != ''"]
    params: list[Any] = []
    if edition_id is not None:
        where.append("e.id = ?")
        params.append(edition_id)
    where.append("ep_a.flagged_at IS NULL")

    sql = f"""
        SELECT e.id AS edition_id, e.edition_date, e.published_date,
               e.topic_label, e.intro_text, e.outro_text, e.user_id,
               ep_a.audio_url, ep_a.duration_seconds,
               ep_a.narrator_voice,
               ep_a.article_id AS article_a_id,
               ep_a.bridge_text AS bridge_a,
               ep_a.article_title AS title_a,
               ep_a.article_byline AS byline_a,
               ep_a.article_publication AS pub_a,
               ep_b.article_id AS article_b_id,
               ep_b.bridge_text AS bridge_between,
               ep_b.article_title AS title_b,
               ep_b.article_byline AS byline_b,
               ep_b.article_publication AS pub_b
          FROM editions e
          JOIN edition_pieces ep_a
            ON ep_a.edition_id = e.id AND ep_a.position = 0
          JOIN edition_pieces ep_b
            ON ep_b.edition_id = e.id AND ep_b.position = 1
         WHERE {' AND '.join(where)}
         ORDER BY e.edition_date DESC, e.id DESC
    """
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

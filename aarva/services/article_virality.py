"""Reverse-lookup virality signal — see
docs/session_plan_trend_signal_v2.md concept B.

The forward trend layer (aarva.services.trend_matcher) answers "what's
the world talking about -> do we have coverage?" This answers the
opposite: "for an article Aarva already selected -> is anyone paying
attention to it right now?" Scans Aarva's own already-scored catalog
against external sources, surfacing hits in `python -m aarva.review`'s
"Trending Aarva articles" section as boost candidates.

v2 scope (locked 2026-08-20, see docs/roadmap.md's 2026-08-20 entry):
HN Algolia URL-search only. Reddit dropped entirely — confirmed dead,
not just unverified (OAuth closed November 2025, the unauthenticated
`.json` fallback the original spec relied on was shut down May 30,
2026). Bluesky's `searchPosts` also now requires authentication
(verified 2026-08-20) and is deferred pending the operator setting up
a dedicated bot account.

Guardrails (locked 2026-08-20, asymmetric vs. forward matching on
purpose): JTBD filter applies (same allowlist as forward matching) —
external virality doesn't override editorial voice on news-shaped
content. NO age constraints — an article being trending externally has
earned its way in regardless of age; Aarva's own editorial bar already
ran when the article was scored. The `published_date >= -N days` scan
window is a COST cap (limits how many articles get HTTP-queried), not
an editorial guardrail.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
import time

import httpx

from aarva.config import PipelineConfig
from aarva.db import Database

logger = logging.getLogger(__name__)

HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search"


@dataclass
class ArticleViralityStats:
    articles_scanned: int = 0
    hits_added: int = 0
    hits_already_seen: int = 0
    scan_errors: int = 0


def _load_scan_candidates(
    db: Database, allowed_jtbds: list[str], scan_window_days: int,
) -> list[dict]:
    """Guardrail: JTBD filter only, NO age minimum (locked decision —
    see module docstring). `scan_window_days` is a scan-cost cap, not
    an editorial guardrail — it just bounds how many articles get
    HTTP-queried per run."""
    if not allowed_jtbds:
        return []
    placeholders = ",".join("?" for _ in allowed_jtbds)
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT a.id, a.canonical_url, a.title
              FROM articles a
              JOIN article_scores s ON s.article_id = a.id
             WHERE a.status = 'scored'
               AND a.published_date >= datetime('now', ?)
               AND s.jtbd_primary IN ({placeholders})
            """,
            (f"-{scan_window_days} days", *allowed_jtbds),
        ).fetchall()
    return [dict(r) for r in rows]


def _already_scanned_urls(db: Database) -> set[str]:
    """Articles that already have an HN hit (any operator_action state,
    including still-unresolved) don't need re-querying — a persistent
    external post's existence doesn't change, only whether the
    operator has acted on it yet, which is tracked on the existing row."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT article_id FROM article_virality_hits "
            "WHERE source_name = 'hn'",
        ).fetchall()
    return {r["article_id"] for r in rows}


def _hn_url_search(
    canonical_url: str, points_threshold: int, lookback_days: int,
) -> list[dict]:
    """HN Algolia's `query` param does fuzzy text matching against the
    URL string, not exact equality (verified 2026-08-20 — a search for
    one URL also matched a same-domain-different-path variant). Filter
    client-side to the exact canonical_url to avoid misattributing a
    different article's buzz."""
    try:
        response = httpx.get(
            HN_SEARCH_URL,
            params={"query": canonical_url, "tags": "story", "hitsPerPage": 10},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        logger.warning("HN virality search failed for %r: %s", canonical_url, e)
        return []

    cutoff = time.time() - lookback_days * 86400
    hits = []
    for hit in data.get("hits", []):
        if hit.get("url") != canonical_url:
            continue
        if (hit.get("points") or 0) < points_threshold:
            continue
        if (hit.get("created_at_i") or 0) < cutoff:
            continue
        hits.append(hit)
    return hits


def scan_for_virality(
    config: PipelineConfig, db: Database,
) -> ArticleViralityStats:
    """HN-only reverse-lookup scan (v2 scope — see module docstring).
    Idempotent per (article_id, source_name, external_url) via
    article_virality_hits' unique index; articles that already have
    an HN hit are skipped entirely rather than re-queried."""
    trends_cfg = config.trends
    allowed_jtbds = list(trends_cfg.get(
        "reverse_lookup_allowed_jtbds",
        ["delight", "curiosity", "smart_escape", "keep_ahead"],
    ))
    scan_window_days = int(trends_cfg.get("reverse_lookup_scan_window_days", 90))
    points_threshold = int(trends_cfg.get("reverse_lookup_hn_points_threshold", 100))
    lookback_days = int(trends_cfg.get("reverse_lookup_lookback_days", 14))

    stats = ArticleViralityStats()
    candidates = _load_scan_candidates(db, allowed_jtbds, scan_window_days)
    already_scanned = _already_scanned_urls(db)
    candidates = [c for c in candidates if c["id"] not in already_scanned]
    stats.articles_scanned = len(candidates)

    for article in candidates:
        try:
            hits = _hn_url_search(
                article["canonical_url"], points_threshold, lookback_days,
            )
        except Exception as e:
            logger.warning(
                "Virality scan failed for article %d: %s", article["id"], e,
            )
            stats.scan_errors += 1
            continue

        with db.connect() as conn:
            for hit in hits:
                story_id = hit.get("objectID")
                external_url = f"https://news.ycombinator.com/item?id={story_id}"
                raw_metadata = {
                    "story_id": story_id,
                    "queried_url": article["canonical_url"],
                }
                cur = conn.execute(
                    "INSERT OR IGNORE INTO article_virality_hits "
                    "(article_id, source_name, external_url, score, "
                    " num_comments, external_created_at, raw_metadata_json) "
                    "VALUES (?, 'hn', ?, ?, ?, datetime(?, 'unixepoch'), ?)",
                    (article["id"], external_url, hit.get("points"),
                     hit.get("num_comments"), hit.get("created_at_i"),
                     json.dumps(raw_metadata)),
                )
                if cur.rowcount > 0:
                    stats.hits_added += 1
                else:
                    stats.hits_already_seen += 1

    logger.info(
        "Virality scan done — %d articles scanned, %d new hits, "
        "%d already known, %d errors",
        stats.articles_scanned, stats.hits_added,
        stats.hits_already_seen, stats.scan_errors,
    )
    return stats

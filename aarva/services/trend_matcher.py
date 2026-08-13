"""Matches unresolved trend_hits rows against Aarva's article catalog —
see docs/session_plan_trend_signal_for_delight.md.

Flow per unresolved trend: blacklist check → LLM query expansion →
semantic retrieval against articles.embedding (with the editorial
guardrail filter applied at SQL time) → LLM re-rank → threshold. No
match clears the threshold → GDELT DOC-API fallback search restricted
to aarva/config/publications.yaml's allowlist domains, surfacing
candidate URLs for the operator to pull in via aarva.ingest_url.

v1 scope note (2026-08-13): GDELT's DOC 2.0 API is purely search-
driven (verified against the official docs) — it has no "what's
trending" endpoint, so it's used here ONLY as the fallback search,
never as an independent crawled trend source (that's
aarva.sources.trend_crawler's job, Google Trends only in v1).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx
import numpy as np

from aarva.clients.embedding import EmbeddingClient, build_embedding_client
from aarva.clients.llm import LLMClient, build_llm_client
from aarva.config import PipelineConfig, load_publications
from aarva.db import Database

logger = logging.getLogger(__name__)


GDELT_DOC_API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

_QUERY_EXPANSION_PROMPT = (
    "Given the trending topic '{trend_phrase_en}', what would relevant "
    "articles look like? Give 3 alternative descriptive phrasings that "
    "capture the same underlying interest.\n\n"
    'Reply as JSON: {{"phrasings": ["...", "...", "..."]}}'
)

_RERANK_PROMPT = """A topic is currently trending: '{trend_phrase_en}'.

Below are candidate articles Aarva has already ingested. Score each one
1-5 for how well it fits the trending interest (5 = directly relevant
coverage or a strong thematic match; 1 = unrelated). This is a shape-
agnostic match — an essay, a reported piece, or a lighter piece can all
score well if the underlying interest genuinely connects.

Candidates:
{candidates_block}

Reply as JSON: {{"scores": {{"<id>": <1-5>, ...}}}}"""


@dataclass
class TrendMatchStats:
    trends_processed: int = 0
    blacklisted: int = 0
    matched: int = 0
    fallback_ran: int = 0
    fallback_found_urls: int = 0
    no_candidates: int = 0


def _load_unresolved_trends(db: Database) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, source_name, trend_phrase, trend_phrase_en, region "
            "FROM trend_hits "
            "WHERE operator_action IS NULL "
            "  AND matched_article_id IS NULL "
            "  AND fallback_urls_json IS NULL "
            "ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def _load_candidate_articles(
    db: Database,
    age_min_hours: int,
    allowed_jtbds: list[str],
    embedding_model: str,
    exclude_ids: set[int],
) -> list[dict]:
    """Guardrail #6: age >= age_min_hours, JTBD in allowed_jtbds,
    status='scored' (excludes anything already 'in_edition'), has a
    usable embedding. jtbd_primary lives on article_scores, not
    articles — a JOIN is required (the spec's illustrative SQL omitted
    this; verified via aarva/db.py's article_scores schema)."""
    if not allowed_jtbds:
        return []
    placeholders = ",".join("?" for _ in allowed_jtbds)
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT a.id, a.title, a.embedding
              FROM articles a
              JOIN article_scores s ON s.article_id = a.id
             WHERE a.status = 'scored'
               AND a.embedding IS NOT NULL
               AND a.embedding_model = ?
               AND a.published_date <= datetime('now', ?)
               AND s.jtbd_primary IN ({placeholders})
            """,
            (embedding_model, f"-{age_min_hours} hours", *allowed_jtbds),
        ).fetchall()
    return [
        {"id": r["id"], "title": r["title"],
         "embedding": np.frombuffer(r["embedding"], dtype=np.float32)}
        for r in rows if r["id"] not in exclude_ids
    ]


def _recently_surfaced_article_ids(db: Database, window_days: int = 7) -> set[int]:
    """Guardrail: same trend-article pair not re-surfaced within the
    window — implemented as "this article wasn't already trend-
    matched at all in the window", which is the stricter and simpler
    reading of the guardrail."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT matched_article_id FROM trend_hits "
            "WHERE matched_article_id IS NOT NULL "
            "  AND seen_at >= datetime('now', ?)",
            (f"-{window_days} days",),
        ).fetchall()
    return {r["matched_article_id"] for r in rows}


def _is_blacklisted(phrase_en: str, blacklist: list[str]) -> bool:
    lower = phrase_en.lower()
    return any(term in lower for term in blacklist if term)


def _expand_query(phrase_en: str, llm: LLMClient) -> list[str]:
    try:
        response = llm.complete(
            _QUERY_EXPANSION_PROMPT.format(trend_phrase_en=phrase_en),
            expect_json=True,
        )
        phrasings = response.get("phrasings") if isinstance(response, dict) else None
        if phrasings and isinstance(phrasings, list):
            return [str(p) for p in phrasings[:3]]
    except Exception as e:
        logger.warning("Query expansion failed for %r: %s", phrase_en, e)
    return [phrase_en]


def _semantic_candidates(
    phrasings: list[str],
    candidates: list[dict],
    embedding_client: EmbeddingClient,
    top_k: int = 10,
) -> list[dict]:
    if not candidates:
        return []
    candidate_matrix = np.stack([c["embedding"] for c in candidates])
    seen_ids: set[int] = set()
    union: list[dict] = []
    for phrasing in phrasings:
        try:
            query_vec = embedding_client.embed(
                [phrasing], task_type="RETRIEVAL_QUERY",
            )[0]
        except Exception as e:
            logger.warning("Embedding failed for phrasing %r: %s", phrasing, e)
            continue
        sims = candidate_matrix @ query_vec
        top_idx = np.argsort(sims)[::-1][:top_k]
        for i in top_idx:
            c = candidates[i]
            if c["id"] not in seen_ids:
                seen_ids.add(c["id"])
                union.append(c)
    return union


def _rerank(phrase_en: str, candidates: list[dict], llm: LLMClient) -> tuple[int | None, float]:
    if not candidates:
        return None, 0.0
    candidates_block = "\n".join(
        f"id={c['id']}: {c['title']}" for c in candidates
    )
    try:
        response = llm.complete(
            _RERANK_PROMPT.format(
                trend_phrase_en=phrase_en, candidates_block=candidates_block,
            ),
            expect_json=True,
        )
        scores = response.get("scores", {}) if isinstance(response, dict) else {}
        if not scores:
            return None, 0.0
        best_id_str = max(scores, key=lambda k: float(scores[k]))
        return int(best_id_str), float(scores[best_id_str])
    except Exception as e:
        logger.warning("Re-rank failed for %r: %s", phrase_en, e)
        return None, 0.0


def _allowlist_domains() -> list[str]:
    domains = []
    for pub in load_publications():
        if not pub.enabled or not pub.homepage:
            continue
        domain = (
            pub.homepage.replace("https://", "").replace("http://", "")
            .split("/")[0].removeprefix("www.")
        )
        if domain:
            domains.append(domain)
    return domains


def _gdelt_fallback_search(
    phrase_en: str, domains: list[str], max_records: int, timespan: str,
) -> list[dict]:
    """Free, no-auth GDELT DOC 2.0 API search restricted to Aarva's
    publication allowlist domains. Returns [] (with a logged warning)
    on any failure — rate limits, network errors, or an over-long
    domain-filter query are all treated as "no fallback candidates
    found," matching the codebase's existing "one external dependency
    failing doesn't break the run" posture."""
    if not domains:
        return []
    domain_clause = " OR ".join(f"domain:{d}" for d in domains)
    query = f"{phrase_en} ({domain_clause})"
    try:
        response = httpx.get(
            GDELT_DOC_API_URL,
            params={
                "query": query,
                "mode": "ArtList",
                "format": "json",
                "maxrecords": str(max_records),
                "timespan": timespan,
            },
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        articles = data.get("articles", [])
        return [
            {"url": a.get("url"), "title": a.get("title"), "domain": a.get("domain")}
            for a in articles if a.get("url")
        ]
    except Exception as e:
        logger.warning("GDELT fallback search failed for %r: %s", phrase_en, e)
        return []


def match_trends(
    config: PipelineConfig,
    db: Database,
    llm: LLMClient | None = None,
    embedding_client: EmbeddingClient | None = None,
) -> TrendMatchStats:
    trends_cfg = config.trends
    threshold = float(trends_cfg.get("vector_match_threshold", 3.5))
    age_min_hours = int(trends_cfg.get("article_age_min_hours", 48))
    allowed_jtbds = list(trends_cfg.get(
        "allowed_jtbds", ["delight", "curiosity", "smart_escape", "keep_ahead"],
    ))
    blacklist = [p.lower() for p in trends_cfg.get("blacklist_phrases", [])]
    gdelt_max_records = int(trends_cfg.get("gdelt_max_records", 25))
    gdelt_timespan = trends_cfg.get("gdelt_timespan", "14d")

    if llm is None:
        llm = build_llm_client(config.llm)
    if embedding_client is None:
        embedding_client = build_embedding_client(config.raw.get("embedding", {}))

    stats = TrendMatchStats()
    unresolved = _load_unresolved_trends(db)
    if not unresolved:
        return stats

    exclude_ids = _recently_surfaced_article_ids(db)
    candidates = _load_candidate_articles(
        db, age_min_hours, allowed_jtbds, embedding_client.name, exclude_ids,
    )
    domains = _allowlist_domains()

    for trend in unresolved:
        stats.trends_processed += 1
        phrase_en = trend["trend_phrase_en"] or trend["trend_phrase"]

        if _is_blacklisted(phrase_en, blacklist):
            stats.blacklisted += 1
            with db.connect() as conn:
                conn.execute(
                    "UPDATE trend_hits SET operator_action = 'dismissed', "
                    "resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (trend["id"],),
                )
            continue

        phrasings = _expand_query(phrase_en, llm)
        semantic_candidates = _semantic_candidates(phrasings, candidates, embedding_client)

        matched_id, score = (None, 0.0)
        if semantic_candidates:
            matched_id, score = _rerank(phrase_en, semantic_candidates, llm)
        else:
            stats.no_candidates += 1

        if matched_id is not None and score >= threshold:
            stats.matched += 1
            with db.connect() as conn:
                conn.execute(
                    "UPDATE trend_hits SET matched_article_id = ?, "
                    "match_score = ? WHERE id = ?",
                    (matched_id, score, trend["id"]),
                )
        else:
            stats.fallback_ran += 1
            fallback = _gdelt_fallback_search(
                phrase_en, domains, gdelt_max_records, gdelt_timespan,
            )
            if fallback:
                stats.fallback_found_urls += len(fallback)
            with db.connect() as conn:
                conn.execute(
                    "UPDATE trend_hits SET fallback_urls_json = ? WHERE id = ?",
                    (json.dumps(fallback), trend["id"]),
                )

    logger.info(
        "Trend matching done — %d processed, %d blacklisted, %d matched, "
        "%d fallback searches (%d URLs found), %d had no semantic candidates",
        stats.trends_processed, stats.blacklisted, stats.matched,
        stats.fallback_ran, stats.fallback_found_urls, stats.no_candidates,
    )
    return stats

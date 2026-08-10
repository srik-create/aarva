"""URL normalization + curation_hits lookup for the "not too niche"
signal — see docs/session_plan_curation_platform_signal.md and, for
the fuzzy topic-similarity path,
docs/session_plan_curation_topic_similarity.md.

Peer-curator platforms link through tracking-parameter-heavy URLs
(utm_source, ref, fbclid, ...); Aarva stores canonical_url without
those. normalize_url() puts both sides into the same canonical shape
so a match isn't missed over a query-string difference that has
nothing to do with which article it is.
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import numpy as np

from aarva.db import Database

# Topic-similarity matches count at a reduced weight relative to an
# exact URL hit, reflecting their lower confidence — see the "Locked
# decisions" section of docs/session_plan_curation_topic_similarity.md.
FUZZY_MATCH_WEIGHT_MULTIPLIER = 0.7

# Query params that vary per-share/per-click but don't identify a
# different article. Stripped before comparing two URLs.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "ref_src", "referer", "referrer", "source",
    "mc_cid", "mc_eid", "fbclid", "gclid",
}


def normalize_url(url: str) -> str:
    """Canonicalize a URL for cross-source matching:
    lower-case scheme+host, strip tracking query params, sort the
    remaining params (two systems can legitimately serialize the same
    params in different order), strip the fragment, strip a trailing
    slash. Does NOT follow redirects (the spec allows it optionally;
    skipped for v1 — see Non-goals in
    docs/session_plan_curation_platform_signal.md)."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/") or ""
    kept_query = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    )
    query = urlencode(kept_query)
    return urlunsplit((scheme, netloc, path, query, ""))


def curation_lookup(db: Database, canonical_url: str) -> list[dict]:
    """Return every curation_hits row matching this article's URL
    (normalized on both sides). Empty list = no curator picked it up
    yet, which is a neutral result, not evidence of niche-ness (see
    the spec's positive-only signal decision)."""
    if not canonical_url:
        return []
    normalized = normalize_url(canonical_url)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT source_name, url, url_normalized, title, seen_at "
            "FROM curation_hits WHERE url_normalized = ?",
            (normalized,),
        ).fetchall()
    return [dict(row) for row in rows]


def curation_score_for(
    db: Database,
    canonical_url: str,
    source_weights: dict[str, float],
    *,
    article_embedding: np.ndarray | None = None,
    hit_embeddings: list[tuple[str, np.ndarray]] | None = None,
    topic_similarity_floor: float = 0.80,
) -> float:
    """Sum of per-source weights for every source that picked up this
    article — by exact URL, or (if article_embedding/hit_embeddings
    are given) by topic similarity above topic_similarity_floor.

    Exact matches take priority: a source that exact-matched is never
    also counted via the fuzzy path (would double-count the same
    editorial signal). Among fuzzy-only matches, only the single
    highest-similarity hit per source counts — stops one prolific
    source from stacking multiple weak partial credits.

    `hit_embeddings` MUST be pre-loaded once per score_all() run (see
    aarva.stages.stage_4_5_6_score) and passed in here — this function
    is called once per article inside concurrent worker threads, so
    loading it from inside here would re-run the same query once per
    article instead of once per run.

    A source not present in source_weights (e.g. disabled since the
    hit was crawled) contributes 0, not an error — config can shrink
    the source list without orphaning old curation_hits rows."""
    exact_hits = curation_lookup(db, canonical_url)
    exact_by_source = {
        h["source_name"]: source_weights.get(h["source_name"], 0.0)
        for h in exact_hits
    }

    fuzzy_by_source: dict[str, float] = {}
    if article_embedding is not None and hit_embeddings:
        for source_name, hit_vec in hit_embeddings:
            if source_name in exact_by_source:
                continue  # exact match already at full weight
            sim = float(np.dot(article_embedding, hit_vec))
            if sim >= topic_similarity_floor:
                weight = source_weights.get(source_name, 0.0) * FUZZY_MATCH_WEIGHT_MULTIPLIER
                fuzzy_by_source[source_name] = max(
                    fuzzy_by_source.get(source_name, 0.0), weight
                )

    all_sources = set(exact_by_source) | set(fuzzy_by_source)
    return sum(
        max(exact_by_source.get(s, 0.0), fuzzy_by_source.get(s, 0.0))
        for s in all_sources
    )


def all_hit_embeddings(db: Database, embedding_model: str) -> list[tuple[str, np.ndarray]]:
    """Load (source_name, embedding) for every curation_hits row with
    a non-NULL embedding matching the configured model. Call once per
    score_all() run (mirroring how source_weights is already loaded
    once, not once per article) and pass the result into
    curation_score_for's hit_embeddings parameter."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT source_name, embedding FROM curation_hits "
            "WHERE embedding IS NOT NULL AND embedding_model = ?",
            (embedding_model,),
        ).fetchall()
    return [
        (r["source_name"], np.frombuffer(r["embedding"], dtype=np.float32))
        for r in rows
    ]

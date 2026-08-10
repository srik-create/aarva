"""URL normalization + curation_hits lookup for the "not too niche"
signal — see docs/session_plan_curation_platform_signal.md.

Peer-curator platforms link through tracking-parameter-heavy URLs
(utm_source, ref, fbclid, ...); Aarva stores canonical_url without
those. normalize_url() puts both sides into the same canonical shape
so a match isn't missed over a query-string difference that has
nothing to do with which article it is.
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from aarva.db import Database

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
    db: Database, canonical_url: str, source_weights: dict[str, float],
) -> float:
    """Sum of weights for every source that picked up this URL. A
    source not present in source_weights (e.g. disabled since the hit
    was crawled) contributes 0, not an error — config can shrink the
    source list without orphaning old curation_hits rows."""
    hits = curation_lookup(db, canonical_url)
    return sum(source_weights.get(hit["source_name"], 0.0) for hit in hits)

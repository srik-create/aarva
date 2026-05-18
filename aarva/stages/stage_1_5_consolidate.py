"""Stage 1.5 — Consolidation.

Cluster articles by event and pick the best version per cluster. Two
backends, switchable by config:

  - 'embeddings' (default for v0.1): persistent vector representations from
    a sentence-transformers model. Embeddings are stored on the article row
    in SQLite, so the vector space is "living" — new articles can be
    compared to historical ones across pipeline runs. Same vectors will
    later power topical-similarity personalisation, drift detection,
    cross-time relevance bridging (Q15 Mode B/C), and pairing (Q31).

  - 'tfidf' (fallback): re-fits TF-IDF per run. Cheap, no extra deps beyond
    scikit-learn, but representations don't persist across runs. Useful
    for environments without PyTorch.

Best-version selection within each cluster (heuristic stack):
  1. Publication tier preference (A/B preferred for substance).
  2. Length (longer typically = more substance).
  3. Recency, original-reporting signal — deferred to v0.2.

Diversity preservation: a cluster contains articles judged to be the same
event; meaningfully-different framings of the same event score below the
similarity threshold and survive as separate articles (becoming Q31 pairing
candidates downstream).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

import numpy as np

from aarva.clients.embedding import EmbeddingClient, build_embedding_client
from aarva.config import PipelineConfig
from aarva.db import Database

logger = logging.getLogger(__name__)


# Publication-tier preference: lower number = preferred best-version when
# length is similar. Editorial reasoning in kickoff §2.
TIER_PREFERENCE = {
    "A": 1, "B": 1,
    "E": 2, "C": 2,
    "D": 3,
    "F": 4,
    "G": 5, "H": 5,
}


@dataclass
class ConsolidationStats:
    candidates: int = 0
    embeddings_computed: int = 0
    clusters_formed: int = 0
    singletons: int = 0
    survivors: int = 0
    filtered_out: int = 0
    backend: str = ""


@dataclass
class _ArticleRow:
    id: int
    title: str
    excerpt: str
    word_count: int
    publication_id: int
    tier: str | None


def _load_candidates(db: Database) -> list[_ArticleRow]:
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT a.id, a.title, COALESCE(a.excerpt, '') AS excerpt,
                   COALESCE(a.word_count, 0) AS word_count,
                   a.publication_id, p.tier
              FROM articles a
              JOIN publications p ON p.id = a.publication_id
             WHERE a.status = 'ingested'
               AND a.full_text IS NOT NULL
               AND a.word_count IS NOT NULL
        """).fetchall()
    return [
        _ArticleRow(
            id=row["id"], title=row["title"], excerpt=row["excerpt"],
            word_count=int(row["word_count"]),
            publication_id=int(row["publication_id"]),
            tier=row["tier"],
        )
        for row in rows
    ]


def _ensure_embeddings(
    db: Database,
    client: EmbeddingClient,
    articles: list[_ArticleRow],
) -> dict[int, np.ndarray]:
    """Ensure every article has an up-to-date embedding for the configured model.

    Returns a map article_id → embedding vector.
    """
    # Load existing embeddings for the current model
    article_ids = [a.id for a in articles]
    if not article_ids:
        return {}
    placeholders = ",".join("?" * len(article_ids))
    existing: dict[int, np.ndarray] = {}
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, embedding, embedding_model
              FROM articles
             WHERE id IN ({placeholders})
            """,
            article_ids,
        ).fetchall()
    for row in rows:
        if row["embedding"] and row["embedding_model"] == client.name:
            existing[int(row["id"])] = np.frombuffer(
                row["embedding"], dtype=np.float32
            )

    # Identify articles needing fresh embedding
    needs_embed = [a for a in articles if a.id not in existing]
    if needs_embed:
        logger.info("Embedding %d new articles via %s", len(needs_embed), client.name)
        docs = [f"{a.title}. {a.excerpt[:1500]}" for a in needs_embed]
        vectors = client.embed(docs)
        for a, vec in zip(needs_embed, vectors):
            db.set_article_embedding(
                article_id=a.id,
                embedding_bytes=vec.tobytes(),
                embedding_model=client.name,
            )
            existing[a.id] = vec
    else:
        logger.info("All %d candidates already have %s embeddings", len(articles), client.name)

    return existing


def _cluster_via_embeddings(
    articles: list[_ArticleRow],
    embeddings: dict[int, np.ndarray],
    similarity_threshold: float,
) -> list[list[int]]:
    """Cluster via embedding cosine similarity.

    Single-linkage union-find. Returns list of clusters of article-indices.
    """
    n = len(articles)
    if n < 2:
        return [[i] for i in range(n)]

    matrix = np.array([embeddings[a.id] for a in articles])
    # Vectors are L2-normalised so cosine similarity == dot product.
    sim = matrix @ matrix.T

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            if sim[i, j] >= similarity_threshold:
                union(i, j)

    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        clusters[find(i)].append(i)
    return list(clusters.values())


def _cluster_via_tfidf(
    articles: list[_ArticleRow],
    similarity_threshold: float,
) -> list[list[int]]:
    """Fallback: TF-IDF + cosine. No dependency on the embedding stack."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    n = len(articles)
    if n < 2:
        return [[i] for i in range(n)]

    docs = [f"{a.title}. {a.excerpt[:500]}" for a in articles]
    vec = TfidfVectorizer(
        ngram_range=(1, 2), max_features=4000, stop_words="english", min_df=1,
    )
    matrix = vec.fit_transform(docs)
    sim = cosine_similarity(matrix)

    parent = list(range(n))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb: parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            if sim[i, j] >= similarity_threshold:
                union(i, j)

    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        clusters[find(i)].append(i)
    return list(clusters.values())


def _pick_best_version(cluster: list[_ArticleRow]) -> _ArticleRow:
    def sort_key(a: _ArticleRow) -> tuple:
        return (TIER_PREFERENCE.get(a.tier or "Z", 9), -a.word_count)
    return min(cluster, key=sort_key)


def consolidate(
    config: PipelineConfig,
    db: Database,
) -> ConsolidationStats:
    """Run Stage 1.5.

    Backend chosen by config.consolidation.method ('embeddings' or 'tfidf').
    """
    cons_cfg = config.consolidation
    method = cons_cfg.get("method", "embeddings")
    similarity_threshold = float(cons_cfg.get("similarity_threshold", 0.78))

    articles = _load_candidates(db)
    stats = ConsolidationStats(candidates=len(articles), backend=method)

    if not articles:
        logger.info("No candidate articles for consolidation.")
        return stats

    if method == "embeddings":
        emb_cfg = config.raw.get("embedding", {})
        client = build_embedding_client(emb_cfg)
        embeddings = _ensure_embeddings(db, client, articles)
        stats.embeddings_computed = len(embeddings)
        clusters = _cluster_via_embeddings(articles, embeddings, similarity_threshold)
    elif method == "tfidf":
        clusters = _cluster_via_tfidf(articles, similarity_threshold)
    else:
        raise ValueError(f"Unknown consolidation method: {method}")

    stats.clusters_formed = len(clusters)

    survivor_ids: set[int] = set()
    filtered_ids: list[int] = []

    for idx_list in clusters:
        cluster = [articles[i] for i in idx_list]
        if len(cluster) == 1:
            stats.singletons += 1
            survivor_ids.add(cluster[0].id)
            continue
        best = _pick_best_version(cluster)
        survivor_ids.add(best.id)
        for member in cluster:
            if member.id != best.id:
                filtered_ids.append(member.id)
        logger.info(
            "Cluster of %d → kept %s (id=%d, tier %s, %dw), dropped %d others",
            len(cluster), best.title[:50], best.id, best.tier, best.word_count,
            len(cluster) - 1,
        )

    stats.survivors = len(survivor_ids)
    stats.filtered_out = len(filtered_ids)

    with db.connect() as conn:
        for article_id in filtered_ids:
            conn.execute(
                "UPDATE articles SET status = 'filtered_out' WHERE id = ?",
                (article_id,),
            )

    return stats

"""Compare TF-IDF and embedding-based consolidation on the same article pool.

Runs both methods against articles currently sitting in the DB with
status='ingested', and prints a side-by-side report:
  - Which articles each method puts in the same cluster
  - The cosine similarity between every pair (under each method)
  - How many duplicates each method catches at its calibrated threshold

NB: this script does NOT modify article status. It's read-only — it loads
candidates, computes both clusterings in-memory, and prints results. Run it
to decide which method to commit to as the default, then update
config/pipeline.yaml accordingly.

Usage:
    python -m aarva.scripts.compare_consolidation

Requires the embedding backend to be installed if you want the embedding
comparison (otherwise it falls back to TF-IDF only with a clear message).
"""
from __future__ import annotations

import logging
import sys
from itertools import combinations
from pathlib import Path

import numpy as np

from aarva.clients.embedding import build_embedding_client
from aarva.config import load_pipeline_config
from aarva.db import Database
from aarva.stages.stage_1_5_consolidate import (
    _load_candidates, _cluster_via_embeddings, _cluster_via_tfidf,
    _ensure_embeddings,
)


def _print_pairwise_similarity_table(
    articles, sim_matrix: np.ndarray, label: str, top_n: int = 15,
) -> None:
    """Print the top-N most-similar article pairs."""
    pairs = []
    for i, j in combinations(range(len(articles)), 2):
        pairs.append((sim_matrix[i, j], i, j))
    pairs.sort(reverse=True)

    print(f"\nTop {min(top_n, len(pairs))} most-similar pairs ({label}):")
    print(f"  {'Sim':>6s}  {'A (id)':>40s}  {'B (id)':>40s}")
    for sim, i, j in pairs[:top_n]:
        a = articles[i]
        b = articles[j]
        print(f"  {sim:>6.3f}  {(a.title[:38] + ' (' + str(a.id) + ')')[:40]:>40s}"
              f"  {(b.title[:38] + ' (' + str(b.id) + ')')[:40]:>40s}")


def _print_clusters(clusters, articles, label: str) -> None:
    multi = [c for c in clusters if len(c) > 1]
    singletons = [c for c in clusters if len(c) == 1]
    print(f"\n{label}: {len(clusters)} clusters ({len(singletons)} singletons, "
          f"{len(multi)} multi-article)")
    for cluster in multi:
        print(f"  Cluster of {len(cluster)}:")
        for idx in cluster:
            a = articles[idx]
            print(f"    [{a.id:>3d}]  ({a.tier or '?'}, {a.word_count:>4d}w)  {a.title[:75]}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s  %(name)s  %(message)s",
    )

    config = load_pipeline_config()
    db = Database(config.db_path)
    articles = _load_candidates(db)

    if not articles:
        print("No 'ingested' articles to compare. Run --stage 1 first.")
        sys.exit(0)

    print(f"Comparing {len(articles)} ingested articles")
    print("=" * 80)

    # ───── TF-IDF run ─────
    tfidf_threshold = 0.20    # calibrated value for short-text TF-IDF
    tfidf_clusters = _cluster_via_tfidf(articles, tfidf_threshold)
    print(f"\n── TF-IDF (threshold={tfidf_threshold}) ──")

    # Also compute the full TF-IDF similarity matrix for the pairwise table.
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    docs = [f"{a.title}. {a.excerpt[:500]}" for a in articles]
    tv = TfidfVectorizer(ngram_range=(1, 2), max_features=4000,
                         stop_words="english", min_df=1)
    tm = tv.fit_transform(docs)
    tsim = cosine_similarity(tm)
    _print_pairwise_similarity_table(articles, tsim, "TF-IDF")
    _print_clusters(tfidf_clusters, articles, "TF-IDF clusters")

    # ───── Embedding run ─────
    print("\n" + "=" * 80)
    print("\n── Embeddings ──")
    try:
        emb_cfg = config.raw.get("embedding", {})
        client = build_embedding_client(emb_cfg)
        print(f"Using embedding backend: {client.name}")
        embeddings = _ensure_embeddings(db, client, articles)
        embedding_threshold = 0.78
        emb_clusters = _cluster_via_embeddings(articles, embeddings,
                                                embedding_threshold)
        matrix = np.array([embeddings[a.id] for a in articles])
        esim = matrix @ matrix.T
        _print_pairwise_similarity_table(articles, esim, "Embeddings")
        _print_clusters(emb_clusters, articles,
                        f"Embedding clusters (threshold={embedding_threshold})")
    except RuntimeError as e:
        print(f"\nEmbedding comparison skipped: {e}")
        print("\nInstall the embedding backend with:")
        print("  pip install sentence-transformers")
        sys.exit(0)

    # ───── Side-by-side summary ─────
    print("\n" + "=" * 80)
    print("\nSUMMARY")
    tfidf_multi = sum(1 for c in tfidf_clusters if len(c) > 1)
    emb_multi = sum(1 for c in emb_clusters if len(c) > 1)
    tfidf_filtered = sum(len(c) - 1 for c in tfidf_clusters if len(c) > 1)
    emb_filtered = sum(len(c) - 1 for c in emb_clusters if len(c) > 1)
    print(f"  TF-IDF      : {len(tfidf_clusters)} clusters, "
          f"{tfidf_multi} multi-article, {tfidf_filtered} duplicates filtered")
    print(f"  Embeddings  : {len(emb_clusters)} clusters, "
          f"{emb_multi} multi-article, {emb_filtered} duplicates filtered")


if __name__ == "__main__":
    main()

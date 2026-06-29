"""Compute and store embeddings for crosscut episodes.

The article store has lived in a single embedding space since Stage 1.5
day one — each `articles` row carries a BGE-base vector that powers
event clustering, taste centroids, and (now) Phase 2 search. Crosscut
episodes were the odd one out: they reuse two articles but had no
embedding of their own, so they were only discoverable via JOINs
through their underlying pieces.

This module brings crosscuts into the same space. Per crosscut episode
it computes two complementary embedding variants, both stored in the
`crosscut_embeddings` table (see `aarva/db.py` for schema):

  - 'pairing_summary': embed the editorial text that defines the
                       crosscut — topic_label + intro_text +
                       bridge_between + outro_text. Captures the
                       CURATORIAL layer: why these two pieces sit
                       together, the angle the bridge text draws out.
                       A query like "the science of belief" hits this
                       embedding cleanly even if neither source article
                       title contains the phrase.
  - 'article_mean'   : mean of the two source articles' BGE vectors,
                       L2-renormalised. Zero extra inference cost;
                       useful as a fallback when the pairing text is
                       empty (older episodes, sparse intros) and as a
                       complementary signal the search layer can blend
                       in.

Used by:
  - aarva/stages/stage_crosscut.py — auto-embeds each new episode
    after build_episode_script() persists it.
  - scripts/backfill_crosscut_embeddings.py — one-off / on-demand
    backfill for episodes that predate auto-embedding.

Idempotent throughout: re-running for an already-embedded episode just
refreshes the vector via the UNIQUE constraint on
(edition_id, source, embedding_model).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np

from aarva.clients.embedding import EmbeddingClient
from aarva.db import Database

logger = logging.getLogger(__name__)


# ─── Stats ────────────────────────────────────────────────────────────────

@dataclass
class EmbedStats:
    pairing_embedded: int = 0
    article_mean_embedded: int = 0
    skipped_no_text: int = 0
    skipped_missing_article_embeddings: int = 0
    errors: int = 0


# ─── Helpers ──────────────────────────────────────────────────────────────

def _build_pairing_summary(crosscut_row: dict) -> str:
    """Concatenate the editorial text fields into a single search-target
    string. Empty fields are skipped. Order roughly follows how a
    listener experiences the episode (topic → intro → bridge → outro)
    so the embedding weighs the topic + intro most (longest contribution
    at the front of the text)."""
    parts: list[str] = []
    for key in ("topic_label", "intro_text", "bridge_between", "outro_text"):
        val = (crosscut_row.get(key) or "").strip()
        if val:
            parts.append(val)
    return " ".join(parts)


def _load_article_embedding(
    db: Database, article_id: int, model_name: str,
) -> Optional[np.ndarray]:
    """Return the article's BGE vector (float32, L2-normalised) for the
    requested model, or None if no compatible embedding exists.

    We check `embedding_model` because old vectors from a previous
    model would mix incompatible spaces if averaged with current-model
    vectors — silent failure mode worth guarding against."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT embedding, embedding_model FROM articles WHERE id = ?",
            (article_id,),
        ).fetchone()
    if not row or not row["embedding"]:
        return None
    if row["embedding_model"] != model_name:
        return None
    return np.frombuffer(row["embedding"], dtype=np.float32)


# ─── Public API ───────────────────────────────────────────────────────────

def embed_crosscut_episode(
    db: Database,
    client: EmbeddingClient,
    edition_id: int,
) -> EmbedStats:
    """Compute and persist both embedding variants for one crosscut.

    Returns per-call stats. Errors are logged + counted, not raised —
    callers iterating many episodes shouldn't abort on a single
    bad row."""
    # Imported here (not at module top) to avoid a circular import:
    # queries.py -> db.py -> indirectly this module via stage_crosscut.
    from aarva.services.queries import load_crosscut_episodes

    stats = EmbedStats()

    rows = load_crosscut_episodes(db, edition_id=edition_id)
    if not rows:
        logger.warning(
            "embed_crosscut_episode: no crosscut found for edition_id=%d",
            edition_id,
        )
        stats.errors += 1
        return stats
    crosscut = rows[0]

    # ── Pairing-summary embedding ───────────────────────────────────
    pairing_text = _build_pairing_summary(crosscut)
    if pairing_text:
        try:
            vec = client.embed([pairing_text])[0]
            db.set_crosscut_embedding(
                edition_id=edition_id,
                source="pairing_summary",
                embedding_bytes=vec.tobytes(),
                embedding_model=client.name,
            )
            stats.pairing_embedded += 1
        except Exception as e:
            logger.warning(
                "pairing_summary embed failed for edition_id=%d: %s",
                edition_id, e,
            )
            stats.errors += 1
    else:
        logger.info(
            "edition_id=%d has no pairing summary text — skipping pairing_summary",
            edition_id,
        )
        stats.skipped_no_text += 1

    # ── Article-mean embedding ──────────────────────────────────────
    vec_a = _load_article_embedding(db, crosscut["article_a_id"], client.name)
    vec_b = _load_article_embedding(db, crosscut["article_b_id"], client.name)
    if vec_a is not None and vec_b is not None:
        mean = (vec_a + vec_b) / 2.0
        # Re-normalise so cosine similarity stays a dot product.
        norm = float(np.linalg.norm(mean))
        if norm > 0:
            mean = mean / norm
        else:
            # Antipodal articles: mean is the zero vector. Vanishingly
            # rare but worth handling — leave as zero (cosine 0 against
            # any query is harmless; the article_mean just won't rank).
            logger.warning(
                "zero-norm article_mean for edition_id=%d "
                "(source articles are antipodal in embedding space)",
                edition_id,
            )
        try:
            db.set_crosscut_embedding(
                edition_id=edition_id,
                source="article_mean",
                embedding_bytes=mean.astype(np.float32).tobytes(),
                embedding_model=client.name,
            )
            stats.article_mean_embedded += 1
        except Exception as e:
            logger.warning(
                "article_mean store failed for edition_id=%d: %s",
                edition_id, e,
            )
            stats.errors += 1
    else:
        missing = []
        if vec_a is None:
            missing.append(f"article_a={crosscut['article_a_id']}")
        if vec_b is None:
            missing.append(f"article_b={crosscut['article_b_id']}")
        logger.info(
            "edition_id=%d missing source-article embeddings (%s) — "
            "skipping article_mean",
            edition_id, ", ".join(missing),
        )
        stats.skipped_missing_article_embeddings += 1

    return stats

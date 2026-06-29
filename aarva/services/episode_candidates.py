"""Episode-creation candidates from a listener prompt.

The web app's primary entry point is the prompt input ("create an
episode on anything"). Submitting a prompt calls
`propose_candidates()` here, which returns up to 3 candidate episodes:

  1. Existing crosscut episodes whose stored embedding (either
     `pairing_summary` or `article_mean` in `crosscut_embeddings`)
     scores above a similarity threshold against the prompt. Shown to
     the listener as "Listen now" — no build required.

  2. New pairings proposed by Gemini from the top ~30 articles
     closest to the prompt in the vector space. Excludes articles
     already covered by step-1 matches to avoid showing the same
     pieces twice. Shown to the listener as "Create this episode" —
     picking one queues a build job (see episode_jobs.py).

Total candidates: capped at N (default 3). Existing matches fill
slots first; new pairings fill the rest. If both stages fail to
produce anything, an empty list is returned — caller renders an
"I couldn't find anything; try a different prompt" message.

The word "crosscut" never appears in listener-facing copy here —
that's a curator-internal term. Templates use the generic word
"episode".
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from aarva.clients.embedding import EmbeddingClient
from aarva.clients.llm import LLMClient
from aarva.db import Database

logger = logging.getLogger(__name__)


# ─── Tuning knobs ────────────────────────────────────────────────────────

# Cosine-similarity floor for an existing crosscut to be considered a
# match for the listener's prompt. 0.65 was chosen empirically against
# the current 19-episode catalog: lower lets unrelated topics surface,
# higher hides legitimate near-matches. Revisit when catalog grows.
DEFAULT_EXISTING_MATCH_FLOOR = 0.65

# Number of articles to feed Gemini as the candidate pool for new
# pairings. The LLM picks pairs from this pool. Too small → forced
# pairings; too large → token-cost blowup and dilution.
DEFAULT_ARTICLE_POOL_SIZE = 30

# Per-article excerpt length sent to Gemini. The model needs enough
# text to judge whether two pieces would actually pair, but full
# bodies × 30 articles would blow the context window.
DEFAULT_EXCERPT_CHARS = 600


# ─── Data shape ──────────────────────────────────────────────────────────

@dataclass
class Candidate:
    """One row in the candidate list returned to the listener.

    `kind` discriminates the action:
      - 'existing' → Listen now → /crosscut/<edition_id>
      - 'new'      → Create this episode → queue a build job

    All listener-facing copy is in the templates, not here. This struct
    is the data shape only."""
    kind: str                       # 'existing' | 'new'
    topic_label: str
    why: str                        # one-sentence connection rationale
    title_a: str
    title_b: str
    publication_a: str
    publication_b: str

    # Filled when kind == 'existing'
    edition_id: Optional[int] = None

    # Filled when kind == 'new'
    article_a_id: Optional[int] = None
    article_b_id: Optional[int] = None

    # For sort stability + presentation
    score: float = 0.0              # higher = better match for prompt


# ─── Existing-match lookup ───────────────────────────────────────────────

def _load_crosscut_vectors(
    db: Database, model_name: str,
) -> dict[int, dict[str, np.ndarray]]:
    """Return {edition_id: {source: vector}} for every crosscut embedded
    with the current model. Both 'pairing_summary' and 'article_mean'
    are loaded so the caller can score against whichever sits higher.

    Old-model rows are intentionally filtered out — they live in a
    different vector space and would produce garbage similarities."""
    out: dict[int, dict[str, np.ndarray]] = {}
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT edition_id, source, embedding
              FROM crosscut_embeddings
             WHERE embedding_model = ?
            """,
            (model_name,),
        ).fetchall()
    for r in rows:
        vec = np.frombuffer(r["embedding"], dtype=np.float32)
        out.setdefault(int(r["edition_id"]), {})[r["source"]] = vec
    return out


def _existing_matches(
    db: Database,
    prompt_vec: np.ndarray,
    model_name: str,
    *,
    floor: float,
    n_max: int,
) -> list[Candidate]:
    """Find existing crosscuts whose best vector beats `floor` against
    the prompt. Returns up to n_max candidates, highest-score first."""
    if n_max <= 0:
        return []

    by_edition = _load_crosscut_vectors(db, model_name)
    if not by_edition:
        return []

    # For each edition, take the max similarity across its sources
    # (pairing_summary, article_mean). Best-of avoids penalising
    # episodes whose pairing text was sparse — the article_mean still
    # carries signal there.
    scored: list[tuple[int, float]] = []
    for edition_id, sources in by_edition.items():
        best = max(float(prompt_vec @ v) for v in sources.values())
        if best >= floor:
            scored.append((edition_id, best))
    scored.sort(key=lambda t: t[1], reverse=True)
    scored = scored[:n_max]

    if not scored:
        return []

    # Hydrate metadata for the matched editions in one query.
    from aarva.services.queries import load_crosscut_episodes

    candidates: list[Candidate] = []
    for edition_id, score in scored:
        rows = load_crosscut_episodes(db, edition_id=edition_id)
        if not rows:
            continue
        cc = rows[0]
        # `why` for existing matches uses the editorial intro_text if
        # available (the actual hook the listener will hear in the
        # episode) — falls back to topic_label otherwise.
        why = (cc.get("intro_text") or "").strip().split("\n", 1)[0]
        if not why:
            why = cc.get("topic_label") or "Two pieces in conversation."
        # Trim to a single concise line.
        if len(why) > 240:
            why = why[:237].rsplit(" ", 1)[0] + "…"
        candidates.append(Candidate(
            kind="existing",
            topic_label=(cc.get("topic_label") or "Two angles").strip(),
            why=why,
            title_a=str(cc.get("title_a") or ""),
            title_b=str(cc.get("title_b") or ""),
            publication_a=str(cc.get("pub_a") or ""),
            publication_b=str(cc.get("pub_b") or ""),
            edition_id=edition_id,
            score=score,
        ))
    return candidates


# ─── New-pairing proposal via Gemini ─────────────────────────────────────

_PROPOSAL_PROMPT = """You compose paired-listening episode ideas for Aarva — a curated daily podcast that pairs two articles per episode, with bridges drawing out their non-obvious connection.

LISTENER PROMPT
{{ prompt }}

ARTICLE POOL (the 30 articles closest to the prompt in our vector space, format: ID — Publication — Title — short excerpt)
{{ pool }}

ALREADY-CHOSEN ARTICLE IDS (do not propose pairings involving any of these — they're already covered by existing episodes returned to the listener)
{{ exclude_ids }}

YOUR TASK
Propose UP TO {{ n_needed }} candidate pairings drawn from the pool. Each pairing is two articles that, when listened to back-to-back with editorial bridges, would meaningfully engage someone who typed the listener prompt above.

Good pairings:
- Share a question, theme, or domain — but bring different angles, stances, or scales of analysis. The connection should be non-obvious enough that hearing them together adds something neither piece would alone.
- Are NOT near-duplicates. If two articles tell the same story, that's not a pairing — that's redundancy.
- Are achievable: both articles in the pool, IDs valid.

Bad pairings:
- Two takes on the same news story.
- One profound piece + one filler piece chosen just to fill the slot.
- Pairings that only loosely relate to the listener's prompt — better to return FEWER candidates than to dilute.

OUTPUT
Return JSON ONLY — no prose, no markdown fences. A single object with one key:
{
  "pairings": [
    {
      "article_a_id": <int from pool>,
      "article_b_id": <int from pool, different from a>,
      "topic_label":  "<short editorial label — 4–8 words, no quotes>",
      "why":          "<one sentence, max ~30 words, explaining the connection between A and B in plain language. Avoid LLM-tell vocabulary (delve, navigate, tapestry, etc.) and first person (no 'we', 'us', 'our').>"
    }
  ]
}

If you can't propose ANY worthwhile pairing from the pool, return {"pairings": []}. Quality over quantity."""


def _load_article_pool(
    db: Database,
    prompt_vec: np.ndarray,
    model_name: str,
    *,
    n: int,
    exclude_ids: set[int],
) -> list[dict]:
    """Top-n articles closest to the prompt in vector space, excluding
    any IDs the caller passes (typically the source articles of
    existing-match crosscuts, to avoid duplicate proposals)."""
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.title, a.excerpt, a.embedding,
                   p.name AS publication
              FROM articles a
              JOIN publications p ON p.id = a.publication_id
             WHERE a.embedding IS NOT NULL
               AND a.embedding_model = ?
               AND a.status IN ('scored', 'in_basket', 'in_edition')
            """,
            (model_name,),
        ).fetchall()

    scored: list[tuple[float, dict]] = []
    for r in rows:
        if r["id"] in exclude_ids:
            continue
        v = np.frombuffer(r["embedding"], dtype=np.float32)
        sim = float(prompt_vec @ v)
        scored.append((sim, {
            "id": int(r["id"]),
            "title": r["title"],
            "excerpt": (r["excerpt"] or "")[:DEFAULT_EXCERPT_CHARS],
            "publication": r["publication"],
            "score": sim,
        }))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [row for _, row in scored[:n]]


def _format_pool_for_prompt(pool: list[dict]) -> str:
    """Render the article pool as readable lines for Gemini. ID prefix
    is so Gemini can echo back integer IDs we validate against."""
    parts = []
    for a in pool:
        excerpt = a["excerpt"].replace("\n", " ").strip()
        parts.append(
            f'{a["id"]} — {a["publication"]} — {a["title"]} — {excerpt}'
        )
    return "\n\n".join(parts)


def _propose_new_pairings(
    db: Database,
    llm: LLMClient,
    prompt: str,
    pool: list[dict],
    *,
    n_needed: int,
    exclude_ids: set[int],
) -> list[Candidate]:
    """Ask Gemini for up to n_needed new pairings from the pool.
    Validates the response shape and the article IDs; returns only
    candidates whose IDs are both in the pool."""
    if n_needed <= 0 or not pool:
        return []

    rendered = (
        _PROPOSAL_PROMPT
        .replace("{{ prompt }}", prompt)
        .replace("{{ pool }}", _format_pool_for_prompt(pool))
        .replace("{{ exclude_ids }}", ", ".join(str(i) for i in sorted(exclude_ids)) or "(none)")
        .replace("{{ n_needed }}", str(n_needed))
    )

    try:
        result = llm.complete(rendered, expect_json=True, temperature=0.6)
    except Exception as e:
        logger.warning("episode_candidates: LLM call failed: %s", e)
        return []

    if not isinstance(result, dict):
        logger.warning("episode_candidates: LLM returned non-dict: %r", result)
        return []
    pairings_raw = result.get("pairings") or []
    if not isinstance(pairings_raw, list):
        logger.warning("episode_candidates: 'pairings' is not a list")
        return []

    # Index pool by id for quick metadata lookup + valid-id validation.
    pool_by_id = {a["id"]: a for a in pool}

    out: list[Candidate] = []
    for p in pairings_raw:
        if not isinstance(p, dict):
            continue
        try:
            a_id = int(p.get("article_a_id"))
            b_id = int(p.get("article_b_id"))
        except (TypeError, ValueError):
            continue
        if a_id == b_id or a_id not in pool_by_id or b_id not in pool_by_id:
            logger.info("episode_candidates: dropping invalid pairing a=%s b=%s", a_id, b_id)
            continue
        a = pool_by_id[a_id]
        b = pool_by_id[b_id]
        out.append(Candidate(
            kind="new",
            topic_label=str(p.get("topic_label") or "").strip()[:80] or "Two angles",
            why=str(p.get("why") or "").strip()[:280] or "Two pieces in conversation.",
            title_a=a["title"],
            title_b=b["title"],
            publication_a=a["publication"],
            publication_b=b["publication"],
            article_a_id=a_id,
            article_b_id=b_id,
            # Average of the two articles' prompt-similarities — pure
            # presentation-order metric, not used by the build flow.
            score=(a["score"] + b["score"]) / 2,
        ))
        if len(out) >= n_needed:
            break
    return out


# ─── Public entry point ──────────────────────────────────────────────────

def propose_candidates(
    db: Database,
    embedding_client: EmbeddingClient,
    llm_client: LLMClient,
    prompt: str,
    *,
    n: int = 3,
    existing_floor: float = DEFAULT_EXISTING_MATCH_FLOOR,
    pool_size: int = DEFAULT_ARTICLE_POOL_SIZE,
) -> list[Candidate]:
    """Return up to `n` episode candidates for a listener prompt.

    Mix of existing crosscut matches (kind='existing') and Gemini-
    proposed new pairings (kind='new'). Existing matches fill slots
    first; remaining slots are filled by the LLM proposal pass."""
    prompt = (prompt or "").strip()
    if not prompt:
        return []

    prompt_vec = embedding_client.embed([prompt])[0]

    # 1) existing matches
    existing = _existing_matches(
        db, prompt_vec, embedding_client.name,
        floor=existing_floor, n_max=n,
    )
    logger.info("episode_candidates: %d existing match(es) above floor", len(existing))

    remaining = n - len(existing)
    if remaining <= 0:
        return existing

    # 2) new pairings — exclude articles already in the existing matches
    exclude_ids: set[int] = set()
    from aarva.services.queries import load_crosscut_episodes
    for cand in existing:
        assert cand.edition_id is not None
        rows = load_crosscut_episodes(db, edition_id=cand.edition_id)
        if rows:
            r = rows[0]
            if r.get("article_a_id"):
                exclude_ids.add(int(r["article_a_id"]))
            if r.get("article_b_id"):
                exclude_ids.add(int(r["article_b_id"]))

    pool = _load_article_pool(
        db, prompt_vec, embedding_client.name,
        n=pool_size, exclude_ids=exclude_ids,
    )
    logger.info("episode_candidates: article pool size=%d (after excludes)", len(pool))

    new_pairings = _propose_new_pairings(
        db, llm_client, prompt, pool,
        n_needed=remaining, exclude_ids=exclude_ids,
    )
    logger.info("episode_candidates: %d new pairing(s) proposed", len(new_pairings))

    return existing + new_pairings

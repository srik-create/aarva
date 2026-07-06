"""Episode-creation candidates from a listener prompt.

The web app's primary entry point is the prompt input ("create an
episode on anything"). Submitting a prompt calls
`propose_candidates()` here, which returns up to 3 candidate episodes:

  1. Existing crosscut episodes whose stored embedding (either
     `pairing_summary` or `article_mean` in `crosscut_embeddings`)
     scores above a similarity threshold against the prompt. Searched
     across both the main DB and the listener DB (aarva/listener_db.py)
     so one listener's on-demand build is discoverable by another's
     matching prompt. The prompt is also classified (see
     prompt_classifier.py) so a behind-the-news / future-gazing
     prompt only matches episodes built in the last
     `search.max_age_days_news` days — an old episode about a stale
     story is a bad match even on topic. Shown to the listener as
     "Listen now" — no build required.

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
from datetime import date, timedelta
from typing import Optional

import numpy as np

from aarva.clients.embedding import EmbeddingClient
from aarva.clients.llm import LLMClient
from aarva.db import Database
from aarva.services.prompt_classifier import classify_prompt

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
    why: str                        # 4–5 sentence description; for
                                    # existing matches this is the
                                    # actual episode intro_text, for
                                    # new pairings it's the Gemini-
                                    # generated rationale (includes
                                    # author names + expertise where
                                    # the model can confidently
                                    # identify them).
    title_a: str
    title_b: str
    publication_a: str
    publication_b: str
    byline_a: str = ""
    byline_b: str = ""

    # Filled when kind == 'existing'
    edition_id: Optional[int] = None

    # Filled when kind == 'new'
    article_a_id: Optional[int] = None
    article_b_id: Optional[int] = None

    # Approximate listening duration in seconds. For 'existing' this is
    # the actual rendered duration_seconds from the crosscut. For 'new'
    # this is estimated from the two source articles' word counts plus
    # intro/bridge/outro overhead at the 140 WPM pace target. Surfaced
    # to the listener so they can pick by length as well as by topic.
    duration_seconds_estimate: int = 0

    # For sort stability + presentation
    score: float = 0.0              # higher = better match for prompt


# ─── Existing-match lookup ───────────────────────────────────────────────

def _load_crosscut_vectors(
    db: Database, model_name: str, *, min_edition_date: Optional[str] = None,
) -> dict[int, dict[str, np.ndarray]]:
    """Return {edition_id: {source: vector}} for every crosscut embedded
    with the current model. Both 'pairing_summary' and 'article_mean'
    are loaded so the caller can score against whichever sits higher.

    Old-model rows are intentionally filtered out — they live in a
    different vector space and would produce garbage similarities.

    min_edition_date: news-y prompts (see prompt_classifier.py) only
    want recent episodes — filtering here (before scoring/truncation)
    rather than post-hoc on the top-n_max results, so a recent match
    ranked below n_max old ones still surfaces."""
    where = ["ce.embedding_model = ?"]
    params: list = [model_name]
    if min_edition_date is not None:
        where.append("e.edition_date >= ?")
        params.append(min_edition_date)

    out: dict[int, dict[str, np.ndarray]] = {}
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT ce.edition_id, ce.source, ce.embedding
              FROM crosscut_embeddings ce
              JOIN editions e ON e.id = ce.edition_id
             WHERE {' AND '.join(where)}
            """,
            params,
        ).fetchall()
    for r in rows:
        vec = np.frombuffer(r["embedding"], dtype=np.float32)
        out.setdefault(int(r["edition_id"]), {})[r["source"]] = vec
    return out


def _existing_matches(
    db: Database,
    listener_db: Database,
    prompt_vec: np.ndarray,
    model_name: str,
    *,
    floor: float,
    n_max: int,
    min_edition_date: Optional[str] = None,
) -> list[Candidate]:
    """Find existing crosscuts whose best vector beats `floor` against
    the prompt (across BOTH the main DB and the listener DB — see
    aarva/listener_db.py). Returns up to n_max candidates, highest-
    score first.

    Edition ids are independent AUTOINCREMENT sequences per DB file
    (the listener DB's is seeded to start at 1,000,000 specifically to
    avoid collisions, but nothing stops a match from either source) —
    every scored entry below carries its source DB alongside the id so
    hydration always queries the right one, rather than merging into a
    single {edition_id: ...} dict that could conflate two unrelated
    episodes that happen to share a number."""
    if n_max <= 0:
        return []

    scored: list[tuple[Database, int, float]] = []
    for source_db in (db, listener_db):
        by_edition = _load_crosscut_vectors(
            source_db, model_name, min_edition_date=min_edition_date,
        )
        # For each edition, take the max similarity across its sources
        # (pairing_summary, article_mean). Best-of avoids penalising
        # episodes whose pairing text was sparse — the article_mean
        # still carries signal there.
        for edition_id, sources in by_edition.items():
            best = max(float(prompt_vec @ v) for v in sources.values())
            if best >= floor:
                scored.append((source_db, edition_id, best))

    scored.sort(key=lambda t: t[2], reverse=True)
    scored = scored[:n_max]

    if not scored:
        return []

    # Hydrate metadata for the matched editions.
    from aarva.services.queries import load_crosscut_episodes, load_listener_episodes

    candidates: list[Candidate] = []
    for source_db, edition_id, score in scored:
        loader = load_crosscut_episodes if source_db is db else load_listener_episodes
        rows = loader(source_db, edition_id=edition_id)
        if not rows:
            continue
        cc = rows[0]
        # `why` for existing matches is the full editorial intro_text
        # (the actual hook the listener will hear when they play the
        # episode) — captures the connection in the curator's voice.
        # Falls back to topic_label only when intro_text is missing.
        why = (cc.get("intro_text") or "").strip()
        if not why:
            why = cc.get("topic_label") or "Two pieces in conversation."
        candidates.append(Candidate(
            kind="existing",
            topic_label=(cc.get("topic_label") or "Two angles").strip(),
            why=why,
            title_a=str(cc.get("title_a") or ""),
            title_b=str(cc.get("title_b") or ""),
            publication_a=str(cc.get("pub_a") or ""),
            publication_b=str(cc.get("pub_b") or ""),
            byline_a=str(cc.get("byline_a") or ""),
            byline_b=str(cc.get("byline_b") or ""),
            edition_id=edition_id,
            duration_seconds_estimate=int(cc.get("duration_seconds") or 0),
            score=score,
        ))
    return candidates


# ─── New-pairing proposal via Gemini ─────────────────────────────────────

_PROPOSAL_PROMPT = """You compose paired-listening episode ideas for Aarva — a curated daily podcast that pairs two articles per episode, with bridges drawing out their non-obvious connection.

LISTENER PROMPT
{{ prompt }}

ARTICLE POOL (the 30 articles closest to the prompt in our vector space, format: ID — Publication — Title — by Byline — short excerpt)
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
      "why":          "<a 4–5 sentence paragraph (~80–120 words) describing the proposed episode to the listener. Cover three things: (1) what each piece argues or describes; (2) how the two connect — the angle that makes pairing them worthwhile; (3) the authors by name (always — they're in the pool data) and one short phrase of relevant expertise IF you can confidently identify them from prior knowledge (otherwise leave the expertise claim out — do NOT invent credentials). Plain language. NO first person ('I/we/us/our'). NO LLM-tell vocabulary: delve, delves, navigate, tapestry, robust, fascinating, intricate, multifaceted, paramount, crucial, landscape (as metaphor), realm, embark, unpack, resonates with.>"
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
    existing-match crosscuts, to avoid duplicate proposals).

    Pulls byline + word_count too — byline so the LLM can mention
    authors by name in its rationale, word_count so we can estimate
    listening duration without inventing a separate query."""
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.title, a.excerpt, a.embedding,
                   a.byline, a.word_count,
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
            "byline": (r["byline"] or "").strip(),
            "excerpt": (r["excerpt"] or "")[:DEFAULT_EXCERPT_CHARS],
            "publication": r["publication"],
            "word_count": int(r["word_count"] or 0),
            "score": sim,
        }))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [row for _, row in scored[:n]]


def _format_pool_for_prompt(pool: list[dict]) -> str:
    """Render the article pool as readable lines for Gemini. ID prefix
    is so Gemini can echo back integer IDs we validate against. Byline
    is surfaced so the rationale can name authors."""
    parts = []
    for a in pool:
        excerpt = a["excerpt"].replace("\n", " ").strip()
        byline = a["byline"] or "unknown"
        parts.append(
            f'{a["id"]} — {a["publication"]} — {a["title"]} — by {byline} — {excerpt}'
        )
    return "\n\n".join(parts)


# Estimated speech rate (WPM) used to translate word counts → listening
# duration. Mirrors the TTS pace tag in pipeline.yaml — keep in sync.
_TTS_WPM = 140

# Word budget for the editorial overhead a crosscut adds on top of the
# two source articles: intro (~150) + bridge-A (~80) + bridge-between
# (~150) + outro (~100) = ~480 words. Adjust here if the prompts in
# stage_crosscut.py change.
_OVERHEAD_WORDS = 480


def _estimate_duration_seconds(word_count_a: int, word_count_b: int) -> int:
    """Estimate listening duration for a new pairing.

    Falls back to 1500 words/article when word_count is missing/zero
    (a reasonable median for the catalog). Adds the editorial overhead
    above before converting to seconds at the configured WPM."""
    wc_a = word_count_a if word_count_a and word_count_a > 0 else 1500
    wc_b = word_count_b if word_count_b and word_count_b > 0 else 1500
    total_words = wc_a + wc_b + _OVERHEAD_WORDS
    return int(total_words * 60 / _TTS_WPM)


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
        # Cap `why` generously now that the prompt asks for ~120 words
        # (~700 chars). The hard cap is defensive against a runaway
        # response, not a normal-case constraint.
        why_text = str(p.get("why") or "").strip()
        if len(why_text) > 1200:
            why_text = why_text[:1197].rsplit(" ", 1)[0] + "…"
        if not why_text:
            why_text = "Two pieces in conversation."
        out.append(Candidate(
            kind="new",
            topic_label=str(p.get("topic_label") or "").strip()[:80] or "Two angles",
            why=why_text,
            title_a=a["title"],
            title_b=b["title"],
            publication_a=a["publication"],
            publication_b=b["publication"],
            byline_a=a["byline"],
            byline_b=b["byline"],
            article_a_id=a_id,
            article_b_id=b_id,
            duration_seconds_estimate=_estimate_duration_seconds(
                a["word_count"], b["word_count"],
            ),
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
    listener_db: Database,
    embedding_client: EmbeddingClient,
    llm_client: LLMClient,
    prompt: str,
    *,
    n: int = 3,
    existing_floor: float = DEFAULT_EXISTING_MATCH_FLOOR,
    pool_size: int = DEFAULT_ARTICLE_POOL_SIZE,
    max_age_days_news: int = 6,
) -> list[Candidate]:
    """Return up to `n` episode candidates for a listener prompt.

    Mix of existing crosscut matches (kind='existing') and Gemini-
    proposed new pairings (kind='new'). Existing matches fill slots
    first; remaining slots are filled by the LLM proposal pass.

    listener_db: episodes built on-demand by other listeners are
    searchable too — see aarva/listener_db.py and _existing_matches.

    max_age_days_news: a prompt classified as behind_the_news or
    future_gazing (see prompt_classifier.py) only matches existing
    episodes built within this many days — an old episode about a
    stale news story is a bad match even if the topic is similar.
    Evergreen prompts get no date filter."""
    prompt = (prompt or "").strip()
    if not prompt:
        return []

    # task_type='RETRIEVAL_QUERY' is the asymmetric-retrieval pair to
    # the RETRIEVAL_DOCUMENT embedding that the article + crosscut
    # vectors were indexed under (see aarva/clients/embedding.py for
    # the task-type semantics). Local BGE / OpenAI clients ignore the
    # kwarg — only Vertex AI uses it. Per Google's docs, mixing
    # QUERY + DOCUMENT this way gives better top-K than embedding
    # both sides identically.
    prompt_vec = embedding_client.embed(
        [prompt], task_type="RETRIEVAL_QUERY",
    )[0]

    category = classify_prompt(prompt, llm_client)
    min_edition_date: Optional[str] = None
    if category in ("behind_the_news", "future_gazing"):
        min_edition_date = (
            date.today() - timedelta(days=max_age_days_news)
        ).isoformat()
    logger.info(
        "episode_candidates: prompt classified as %r (min_edition_date=%s)",
        category, min_edition_date,
    )

    # 1) existing matches
    existing = _existing_matches(
        db, listener_db, prompt_vec, embedding_client.name,
        floor=existing_floor, n_max=n, min_edition_date=min_edition_date,
    )
    logger.info("episode_candidates: %d existing match(es) above floor", len(existing))

    remaining = n - len(existing)
    if remaining <= 0:
        return existing

    # 2) new pairings — exclude articles already in the existing matches
    exclude_ids: set[int] = set()
    from aarva.services.queries import load_crosscut_episodes, load_listener_episodes
    for cand in existing:
        assert cand.edition_id is not None
        rows = load_crosscut_episodes(db, edition_id=cand.edition_id)
        if not rows:
            rows = load_listener_episodes(listener_db, edition_id=cand.edition_id)
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

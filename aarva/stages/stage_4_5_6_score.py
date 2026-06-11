"""Stage 4 + 5 + 6 — combined LLM scoring.

Single LLM call per article produces:
  - Stage 4: rigour / posture / self_implication scores + verdict + ranking
  - Stage 5: lens, pillar, JTBD primary/secondary, topic recency sensitivity
  - Stage 6: six-dimension narrative fingerprint (JSON blob)

Articles passing the hard gate (rigour >= 0.5 AND posture >= 0.5) move from
status='ingested' to status='scored'. Failures get status='filtered_out'
with the reason captured in article_scores.

Cost note: at v0.1 volumes (~200/day target, currently smaller while we
work with your real data), this is the single most expensive stage. Each
call is ~4000 input tokens + ~500 output tokens. We run it once per
article and cache the result; the same article never gets re-scored
unless the prompt version changes.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from aarva.clients.llm import LLMClient, build_llm_client
from aarva.config import PipelineConfig
from aarva.db import Database

logger = logging.getLogger(__name__)


@dataclass
class ScoringStats:
    candidates: int = 0
    scored: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0


PROMPTS_PATH = Path(__file__).parent.parent / "config" / "prompts.yaml"


from aarva.prompts import load_prompts as _load_prompts, render as _render_prompt


def _build_user_prompt(
    prompt_config: dict,
    article: dict,
) -> str:
    user_template = prompt_config["user"]
    return _render_prompt(
        user_template,
        publication=article.get("publication_name") or "Unknown",
        published_date=str(article.get("published_date") or "Unknown"),
        article_body=article.get("full_text") or "",
    )


def _persist_score(
    db: Database,
    article_id: int,
    response: dict,
    prompt_version: str,
) -> None:
    """Write the LLM response to article_scores. Keeps the full fingerprint as JSON."""
    fingerprint = {
        "structural_form": response.get("structural_form"),
        "method_of_inquiry": response.get("method_of_inquiry"),
        "voice_register": response.get("voice_register"),
        "temporal_lens": response.get("temporal_lens"),
        "cognitive_density": response.get("cognitive_density"),
        "emotional_register": response.get("emotional_register"),
    }
    with db.connect() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO article_scores (
                article_id,
                rigour, rigour_rationale,
                posture, posture_rationale,
                self_implication, self_implication_rationale,
                verdict, ranking_score,
                lens, pillar, jtbd_primary, jtbd_secondary,
                topic_recency_sensitivity, fingerprint_json, prompt_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                article_id,
                response.get("rigour"),
                response.get("rigour_rationale"),
                response.get("posture"),
                response.get("posture_rationale"),
                response.get("self_implication"),
                response.get("self_implication_rationale"),
                response.get("verdict"),
                response.get("ranking_score"),
                response.get("lens"),
                response.get("pillar"),
                response.get("jtbd_primary"),
                response.get("jtbd_secondary"),
                response.get("topic_recency_sensitivity"),
                json.dumps(fingerprint),
                prompt_version,
            )
        )


def score_all(
    config: PipelineConfig,
    db: Database,
    *,
    article_filter_ids: Optional[set[int]] = None,
    llm: Optional[LLMClient] = None,
) -> ScoringStats:
    """Run Stage 4+5+6 on all articles with status='ingested'.

    article_filter_ids: if provided, only score articles with these IDs.
    Useful for the calibration loop.

    llm: pass an existing client to avoid rebuilding (and to preserve
    rate-limiter state across calls). If None, builds one from
    config.llm — the CLI orchestrator path.
    """
    prompts = _load_prompts()
    prompt_version = config.scoring.get("prompt_version", "v1")
    prompt_config = prompts.get("stage_4_5_6", {}).get(prompt_version)
    if not prompt_config:
        raise RuntimeError(
            f"Stage 4+5+6 prompt version '{prompt_version}' not in prompts.yaml"
        )

    if llm is None:
        llm = build_llm_client(config.llm)
    logger.info("Stage 4+5+6 starting with LLM=%s, prompt=%s",
                llm.name, prompt_version)

    # Pull candidates
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT a.id, a.title, a.full_text, a.published_date,
                   p.name AS publication_name
              FROM articles a
              JOIN publications p ON p.id = a.publication_id
             WHERE a.status = 'ingested'
               AND a.full_text IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM article_scores s
                                WHERE s.article_id = a.id
                                  AND s.prompt_version = ?)
            ORDER BY a.id
        """, (prompt_version,)).fetchall()

    candidates = [dict(row) for row in rows]
    if article_filter_ids:
        candidates = [c for c in candidates if c["id"] in article_filter_ids]

    stats = ScoringStats(candidates=len(candidates))
    pass_min_rigour = float(config.scoring.get("rigour_min", 0.5))
    pass_min_posture = float(config.scoring.get("posture_min", 0.5))
    # Concurrency: LLM calls are network-bound (~5–10s each). Running
    # them sequentially means a 200-article run takes 20–30 minutes
    # wall-clock; parallelising drops that to a few minutes, capped by
    # the Gemini RPM limit (the client's internal _RateLimiter is
    # thread-safe and throttles globally). SQLite's single-writer lock
    # serialises the persists; that's milliseconds and not the
    # bottleneck.
    max_workers = int(config.scoring.get("concurrent_workers", 8))

    import concurrent.futures
    import threading
    stats_lock = threading.Lock()

    def _score_one(article: dict) -> None:
        """Worker: score one article, persist, update stats. Logs
        errors but does not raise — exceptions become stats.errors."""
        article_id = article["id"]
        try:
            prompt = _build_user_prompt(prompt_config, article)
            full_prompt = prompt_config.get("system", "") + "\n\n" + prompt
            response = llm.complete(full_prompt, expect_json=True)
            assert isinstance(response, dict)

            rigour = float(response.get("rigour") or 0)
            posture = float(response.get("posture") or 0)
            self_imp = float(response.get("self_implication") or 0)
            verdict = (
                "PASS"
                if rigour >= pass_min_rigour and posture >= pass_min_posture
                else "FAIL"
            )
            response["verdict"] = verdict
            response["ranking_score"] = round(
                0.45 * rigour + 0.45 * posture + 0.10 * self_imp, 4
            )

            _persist_score(db, article_id, response, prompt_version)
            new_status = "scored" if verdict == "PASS" else "filtered_out"
            with db.connect() as conn:
                conn.execute(
                    "UPDATE articles SET status = ? WHERE id = ?",
                    (new_status, article_id),
                )

            with stats_lock:
                stats.scored += 1
                if verdict == "PASS":
                    stats.passed += 1
                else:
                    stats.failed += 1

            logger.info(
                "Article %d: %s (rigour=%.2f, posture=%.2f, self=%.2f, rank=%.2f) — %s",
                article_id, verdict,
                rigour, posture, self_imp,
                response["ranking_score"],
                article.get("title", "")[:60],
            )

        except Exception as e:
            with stats_lock:
                stats.errors += 1
            logger.warning("Scoring failed for article %d: %s", article_id, e)

    if candidates:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="stage456",
        ) as ex:
            # Submit all and wait — we don't need the results here,
            # _score_one writes to the DB and stats directly.
            list(ex.map(_score_one, candidates))

    logger.info(
        "Stage 4+5+6 done — %d scored (%d PASS, %d FAIL), %d errors  "
        "(workers=%d)",
        stats.scored, stats.passed, stats.failed, stats.errors, max_workers,
    )
    return stats

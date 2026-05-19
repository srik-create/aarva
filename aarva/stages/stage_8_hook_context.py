"""Stage 8 — Editor's hook (8a) and why-now contextualisation (8b).

For each piece in today's edition, two LLM calls:
  8a — one-line italic question (Aarva voice, ~8-18 words)
  8b — 60-100 word "why this is worth your time right now" paragraph

These are the most editorially-visible LLM outputs in the system. If the
hook reads as generic AI prose, the editorial promise dies on first
contact. The prompts in `prompts.yaml` are heavy on negative examples
("don't write 'have you ever wondered...'") to push the model away from
clichés.

We split into two LLM calls rather than one combined call because:
  - The two outputs need different lengths and tones
  - The hook deserves the model's full attention (highest-stakes single
    piece of Aarva's voice)
  - Cost is trivial (~12 calls per daily edition)

Idempotency: pieces that already have a hook + contextualisation skip
re-generation. Run again to fill in pieces that errored on a previous
pass. To force re-generation, NULL out the columns or pass --rebuild
(deferred for now).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

import yaml

from aarva.clients.llm import LLMClient, build_llm_client
from aarva.config import PipelineConfig
from aarva.db import Database

logger = logging.getLogger(__name__)


PROMPTS_PATH = Path(__file__).parent.parent / "config" / "prompts.yaml"


@dataclass
class Stage8Stats:
    pieces_total: int = 0
    hooks_generated: int = 0
    contexts_generated: int = 0
    skipped_already_done: int = 0
    errors: int = 0


def _load_prompts() -> dict:
    with PROMPTS_PATH.open() as f:
        return yaml.safe_load(f)


def _render(template: str, **kwargs) -> str:
    """Lightweight {{ var }} substitution. Same as Stage 4+5+6's renderer."""
    out = template
    for k, v in kwargs.items():
        out = out.replace("{{ " + k + " }}", str(v))
        out = out.replace("{{" + k + "}}", str(v))
    return out


def _clean_hook(raw: str) -> str:
    """Normalise the LLM's hook output — single line, no quotes/markdown."""
    line = raw.strip()
    # Take only first line if the model returned more
    line = line.split("\n", 1)[0].strip()
    # Strip wrapping quotes / italics markers if the model added them
    for wrap in ('"', "'", "*", "_"):
        if line.startswith(wrap) and line.endswith(wrap) and len(line) > 2:
            line = line[1:-1].strip()
    # Ensure trailing question mark survives (common output: model returns
    # the question without quotes; rarely it appends a period).
    return line


def _clean_context(raw: str) -> str:
    """Normalise the why-now paragraph: strip wrapping quotes, collapse whitespace."""
    text = raw.strip()
    for wrap in ('"', "'"):
        if text.startswith(wrap) and text.endswith(wrap) and len(text) > 2:
            text = text[1:-1].strip()
    # Collapse runs of whitespace within the paragraph; preserve sentences.
    parts = [p.strip() for p in text.split("\n") if p.strip()]
    return " ".join(parts)


def _load_edition_pieces(
    db: Database,
    edition_id: int,
    include_complete: bool = False,
) -> list[dict]:
    """Pull all pieces in the edition, with article + score metadata.

    If include_complete=False (default), skip pieces that already have both
    hook and contextualisation populated.
    """
    where = "" if include_complete else (
        " AND (ep.hook IS NULL OR ep.hook = '' "
        "      OR ep.contextualisation IS NULL OR ep.contextualisation = '')"
    )
    with db.connect() as conn:
        rows = conn.execute(f"""
            SELECT ep.edition_id, ep.article_id, ep.slot,
                   ep.hook AS existing_hook,
                   ep.contextualisation AS existing_context,
                   a.title, a.full_text, a.published_date,
                   p.name AS publication_name,
                   s.topic_recency_sensitivity
              FROM edition_pieces ep
              JOIN articles a ON a.id = ep.article_id
              JOIN publications p ON p.id = a.publication_id
              LEFT JOIN article_scores s ON s.article_id = a.id
             WHERE ep.edition_id = ?
               {where}
             ORDER BY ep.position
        """, (edition_id,)).fetchall()
    return [dict(r) for r in rows]


def _generate_hook(
    llm: LLMClient,
    prompt_config: dict,
    piece: dict,
) -> str:
    rendered = _render(
        prompt_config["user"],
        publication=piece["publication_name"] or "Unknown",
        title=piece["title"] or "",
        article_body=piece["full_text"] or "",
    )
    full_prompt = prompt_config.get("system", "") + "\n\n" + rendered
    response = llm.complete(full_prompt, expect_json=False, temperature=0.7)
    return _clean_hook(str(response))


def _generate_context(
    llm: LLMClient,
    prompt_config: dict,
    piece: dict,
) -> str:
    rendered = _render(
        prompt_config["user"],
        publication=piece["publication_name"] or "Unknown",
        title=piece["title"] or "",
        article_body=piece["full_text"] or "",
        published_date=str(piece["published_date"] or "Unknown"),
        topic_recency_sensitivity=str(piece.get("topic_recency_sensitivity") or 0.5),
        today=date.today().isoformat(),
    )
    full_prompt = prompt_config.get("system", "") + "\n\n" + rendered
    response = llm.complete(full_prompt, expect_json=False, temperature=0.6)
    return _clean_context(str(response))


def _save(
    db: Database,
    edition_id: int,
    article_id: int,
    hook: Optional[str],
    context: Optional[str],
) -> None:
    """Update edition_pieces with whichever fields were generated."""
    with db.connect() as conn:
        if hook is not None and context is not None:
            conn.execute(
                "UPDATE edition_pieces SET hook = ?, contextualisation = ? "
                "WHERE edition_id = ? AND article_id = ?",
                (hook, context, edition_id, article_id),
            )
        elif hook is not None:
            conn.execute(
                "UPDATE edition_pieces SET hook = ? "
                "WHERE edition_id = ? AND article_id = ?",
                (hook, edition_id, article_id),
            )
        elif context is not None:
            conn.execute(
                "UPDATE edition_pieces SET contextualisation = ? "
                "WHERE edition_id = ? AND article_id = ?",
                (context, edition_id, article_id),
            )


def _get_latest_edition_id(db: Database) -> Optional[int]:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM editions ORDER BY edition_date DESC, id DESC LIMIT 1"
        ).fetchone()
    return int(row["id"]) if row else None


def generate_for_edition(
    config: PipelineConfig,
    db: Database,
    *,
    edition_id: Optional[int] = None,
    include_complete: bool = False,
) -> Stage8Stats:
    """Run Stage 8 (8a + 8b) for every piece in the specified edition.

    edition_id: defaults to the latest edition (by date) in the DB.
    include_complete: re-generate even for pieces that already have hook+context.
    """
    stats = Stage8Stats()

    if edition_id is None:
        edition_id = _get_latest_edition_id(db)
        if edition_id is None:
            logger.warning("Stage 8: no editions in DB.")
            return stats
        logger.info("Stage 8: using latest edition #%d", edition_id)

    prompts = _load_prompts()
    prompt_8a = prompts.get("stage_8a", {}).get("v1")
    prompt_8b = prompts.get("stage_8b", {}).get("v1")
    if not (prompt_8a and prompt_8b):
        raise RuntimeError("Stage 8: v1 prompts missing from prompts.yaml")

    llm = build_llm_client(config.llm)
    pieces = _load_edition_pieces(db, edition_id, include_complete=include_complete)
    stats.pieces_total = len(pieces)

    if not pieces:
        logger.info("Stage 8: no pieces in edition #%d need generation", edition_id)
        return stats

    logger.info("Stage 8: generating for %d pieces via LLM=%s",
                len(pieces), llm.name)

    for piece in pieces:
        article_id = piece["article_id"]
        slot = piece["slot"]
        title_preview = (piece["title"] or "")[:50]
        logger.info("  [%s] article %d — %s", slot, article_id, title_preview)

        new_hook: Optional[str] = None
        new_context: Optional[str] = None

        # 8a — Hook
        if include_complete or not piece.get("existing_hook"):
            try:
                new_hook = _generate_hook(llm, prompt_8a, piece)
                stats.hooks_generated += 1
                logger.info("      hook: %s", new_hook)
            except Exception as e:
                stats.errors += 1
                logger.warning("      hook generation failed: %s", e)

        # 8b — Why-now
        if include_complete or not piece.get("existing_context"):
            try:
                new_context = _generate_context(llm, prompt_8b, piece)
                stats.contexts_generated += 1
                # Log first 80 chars of context to keep log readable
                preview = new_context if len(new_context) <= 80 else new_context[:77] + "..."
                logger.info("      context: %s", preview)
            except Exception as e:
                stats.errors += 1
                logger.warning("      context generation failed: %s", e)

        if new_hook is None and new_context is None:
            stats.skipped_already_done += 1
            continue

        _save(db, edition_id, article_id, new_hook, new_context)

    logger.info(
        "Stage 8 done — %d pieces, %d hooks, %d contexts, %d skipped, %d errors",
        stats.pieces_total, stats.hooks_generated, stats.contexts_generated,
        stats.skipped_already_done, stats.errors,
    )
    return stats

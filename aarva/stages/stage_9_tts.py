"""Stage 9 — Audio synthesis.

For every piece in today's edition, pick a narrator voice (per the
configured selection rule), build the full narration text
(hook + contextualisation + article body), synthesize via the configured
TTSClient, save the WAV file, and update edition_pieces with the audio
URL, duration, and narrator voice.

Voice-selection rules (configured in pipeline.yaml under tts.voice_selection_rule):

  alternate_with_gender_match (default for v0.1):
    For each piece, a small LLM call detects whether the article is
    first-person AND the author's gender is identifiable. If yes,
    match the voice (Serena for female, Jamie for male). Otherwise
    alternate between voice_default and voice_alternate by slot
    position to vary narration across the edition.

  alternate:
    Pure alternation by position. No LLM call. Cheapest.

  single:
    Always use voice_default. No per-piece selection.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from aarva.clients.llm import LLMClient, build_llm_client
from aarva.clients.tts import TTSClient, build_tts_client
from aarva.config import PipelineConfig
from aarva.db import Database

logger = logging.getLogger(__name__)


@dataclass
class Stage9Stats:
    pieces_total: int = 0
    audio_generated: int = 0
    skipped_already_done: int = 0
    errors: int = 0
    total_audio_seconds: float = 0.0


def _compose_narration(piece: dict) -> str:
    """Combine hook + context + article body; blank lines render as pauses."""
    parts: list[str] = []
    if piece.get("hook"):
        parts.append(str(piece["hook"]).strip())
    if piece.get("contextualisation"):
        parts.append(str(piece["contextualisation"]).strip())
    if piece.get("full_text"):
        parts.append(str(piece["full_text"]).strip())
    return "\n\n".join(p for p in parts if p)


def _audio_path(audio_dir: Path, edition_date: date, article_id: int) -> Path:
    return audio_dir / edition_date.isoformat() / f"article_{article_id:04d}.wav"


def _load_edition_pieces(
    db: Database,
    edition_id: int,
    include_done: bool = False,
) -> tuple[date, list[dict]]:
    where = "" if include_done else " AND (ep.audio_url IS NULL OR ep.audio_url = '')"
    with db.connect() as conn:
        edition = conn.execute(
            "SELECT id, edition_date FROM editions WHERE id = ?", (edition_id,),
        ).fetchone()
        if not edition:
            raise RuntimeError(f"Edition {edition_id} not found.")
        edition_date = date.fromisoformat(str(edition["edition_date"]))

        rows = conn.execute(f"""
            SELECT ep.edition_id, ep.article_id, ep.slot, ep.position,
                   ep.hook, ep.contextualisation,
                   ep.audio_url AS existing_audio_url,
                   a.title, a.full_text, a.byline
              FROM edition_pieces ep
              JOIN articles a ON a.id = ep.article_id
             WHERE ep.edition_id = ?
               {where}
             ORDER BY ep.position
        """, (edition_id,)).fetchall()
    return edition_date, [dict(r) for r in rows]


def _save_audio(
    db: Database,
    edition_id: int,
    article_id: int,
    audio_url: str,
    duration_seconds: int,
    narrator_voice: str,
) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE edition_pieces "
            "SET audio_url = ?, duration_seconds = ?, narrator_voice = ? "
            "WHERE edition_id = ? AND article_id = ?",
            (audio_url, duration_seconds, narrator_voice, edition_id, article_id),
        )


def _get_latest_edition_id(db: Database) -> Optional[int]:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM editions ORDER BY edition_date DESC, id DESC LIMIT 1"
        ).fetchone()
    return int(row["id"]) if row else None


# ─────────────────────────────────────────────────────────────────────────────
# Voice selection
# ─────────────────────────────────────────────────────────────────────────────

_NARRATOR_PROMPT = """\
Read this article. Reply with EXACTLY ONE WORD:

- MALE: if the article is written in first-person AND the author is clearly male (the byline name is conventionally male or the text explicitly says so)
- FEMALE: if the article is written in first-person AND the author is clearly female (the byline name is conventionally female or the text explicitly says so)
- NEUTRAL: in all other cases (third-person reporting, ambiguous byline, multi-author, or no clear first-person voice)

When unsure, return NEUTRAL.

Byline: {byline}
Article (first 2500 chars):
{excerpt}

Reply with one word: MALE, FEMALE, or NEUTRAL."""


def _detect_narrator_gender(piece: dict, llm: LLMClient) -> str:
    """Returns 'male', 'female', or 'neutral'."""
    excerpt = (piece.get("full_text") or "")[:2500]
    byline = piece.get("byline") or "Unknown"
    prompt = _NARRATOR_PROMPT.format(excerpt=excerpt, byline=byline)
    try:
        response = llm.complete(prompt, expect_json=False, temperature=0.0)
        text = str(response).strip().upper()
        # The model sometimes returns "FEMALE" inside a longer sentence;
        # match on word boundaries and prefer the most specific token.
        if re.search(r"\bFEMALE\b", text):
            return "female"
        if re.search(r"\bMALE\b", text):
            return "male"
        return "neutral"
    except Exception as e:
        logger.warning("Narrator detection failed (%s); defaulting to neutral", e)
        return "neutral"


def _pick_voice(
    piece: dict,
    voice_default: str,
    voice_alternate: str,
    rule: str,
    llm: Optional[LLMClient],
) -> tuple[str, str]:
    """Returns (voice_id, reason) for logging."""
    position = int(piece.get("position") or 0)

    if rule == "single":
        return voice_default, "single-voice rule"

    if rule == "alternate":
        if position % 2 == 0:
            return voice_default, f"alternation (position {position}, even)"
        return voice_alternate, f"alternation (position {position}, odd)"

    # alternate_with_gender_match (default)
    if llm is None:
        # Fallback: just alternate
        return (
            (voice_default, f"alternation (position {position}, even); no LLM")
            if position % 2 == 0
            else (voice_alternate, f"alternation (position {position}, odd); no LLM")
        )

    hint = _detect_narrator_gender(piece, llm)
    if hint == "female":
        return voice_default, "first-person female narrator detected"
    if hint == "male":
        return voice_alternate, "first-person male narrator detected"
    # NEUTRAL → alternate by position
    if position % 2 == 0:
        return voice_default, f"third-person/neutral, alternation (position {position}, even)"
    return voice_alternate, f"third-person/neutral, alternation (position {position}, odd)"


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def generate_for_edition(
    config: PipelineConfig,
    db: Database,
    *,
    edition_id: Optional[int] = None,
    include_done: bool = False,
) -> Stage9Stats:
    stats = Stage9Stats()

    if edition_id is None:
        edition_id = _get_latest_edition_id(db)
        if edition_id is None:
            logger.warning("Stage 9: no editions in DB.")
            return stats
        logger.info("Stage 9: using latest edition #%d", edition_id)

    edition_date, pieces = _load_edition_pieces(
        db, edition_id, include_done=include_done
    )
    stats.pieces_total = len(pieces)
    if not pieces:
        logger.info("Stage 9: no pieces in edition #%d need audio", edition_id)
        return stats

    tts = build_tts_client(config.tts)
    voice_default = config.tts.get("voice_default") or tts.default_voice_id
    voice_alternate = config.tts.get("voice_alternate") or voice_default
    rule = config.tts.get("voice_selection_rule", "alternate_with_gender_match")

    llm: Optional[LLMClient] = None
    if rule == "alternate_with_gender_match":
        llm = build_llm_client(config.llm)

    audio_dir = config.audio_dir
    logger.info(
        "Stage 9: synthesizing %d pieces  |  voice rule=%s  |  default=%s  alternate=%s",
        len(pieces), rule, voice_default, voice_alternate,
    )

    for piece in pieces:
        article_id = piece["article_id"]
        slot = piece["slot"]
        title_preview = (piece["title"] or "")[:50]

        narration = _compose_narration(piece)
        if not narration:
            logger.warning("  [%s] article %d — no narratable text; skipping",
                           slot, article_id)
            stats.errors += 1
            continue

        voice_id, reason = _pick_voice(
            piece, voice_default, voice_alternate, rule, llm
        )

        out_path = _audio_path(audio_dir, edition_date, article_id)
        char_count = len(narration)
        approx_minutes = char_count / 1000.0

        logger.info(
            "  [%s] article %d (%d chars, est ~%.1f min) — %s",
            slot, article_id, char_count, approx_minutes, title_preview,
        )
        logger.info("      voice: %s  (%s)", voice_id, reason)

        try:
            result = tts.synthesize(narration, out_path, voice_id=voice_id)
        except Exception as e:
            stats.errors += 1
            logger.warning("      synthesis failed: %s", e)
            continue

        try:
            rel_path = result.output_path.relative_to(audio_dir.parent.parent)
        except ValueError:
            rel_path = result.output_path
        audio_url = str(rel_path)

        _save_audio(
            db, edition_id, article_id,
            audio_url=audio_url,
            duration_seconds=int(round(result.duration_seconds)),
            narrator_voice=result.voice_id,
        )

        stats.audio_generated += 1
        stats.total_audio_seconds += result.duration_seconds

        mins, secs = divmod(int(result.duration_seconds), 60)
        logger.info("      audio: %s  (%dm %ds, %d Hz)",
                    audio_url, mins, secs, result.sample_rate)

    if stats.audio_generated:
        total_min = stats.total_audio_seconds / 60.0
        logger.info(
            "Stage 9 done — %d pieces, %d audio generated (%.1f total min), "
            "%d skipped, %d errors",
            stats.pieces_total, stats.audio_generated, total_min,
            stats.skipped_already_done, stats.errors,
        )
    else:
        logger.info(
            "Stage 9 done — %d pieces, no audio generated (%d errors)",
            stats.pieces_total, stats.errors,
        )

    return stats

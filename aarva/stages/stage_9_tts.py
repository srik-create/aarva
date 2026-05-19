"""Stage 9 — Audio synthesis.

For every piece in today's edition, build the full narration text
(hook + contextualisation + article body), synthesize via the configured
TTSClient, save the WAV file, and update edition_pieces with the audio
URL and duration.

Idempotent: pieces that already have audio_url skip re-synthesis.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

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
    """Combine the editorial wrapper + article body into one narration string.

    Layout (blank lines render as natural pauses in Piper):

        {hook}                  ← the editor's italic question

        {contextualisation}     ← the 60-100 word why-now paragraph

        {article body}          ← the article itself

    For pieces missing a hook or context (e.g., Stage 8 hadn't run on them
    yet), those segments are simply omitted — the article body still gets
    narrated.
    """
    parts: list[str] = []
    if piece.get("hook"):
        parts.append(str(piece["hook"]).strip())
    if piece.get("contextualisation"):
        parts.append(str(piece["contextualisation"]).strip())
    if piece.get("full_text"):
        parts.append(str(piece["full_text"]).strip())
    return "\n\n".join(p for p in parts if p)


def _audio_filename(article_id: int) -> str:
    return f"article_{article_id:04d}.wav"


def _audio_path(audio_dir: Path, edition_date: date, article_id: int) -> Path:
    return audio_dir / edition_date.isoformat() / _audio_filename(article_id)


def _load_edition_pieces(
    db: Database,
    edition_id: int,
    include_done: bool = False,
) -> tuple[date, list[dict]]:
    """Return (edition_date, pieces). Pieces with audio_url filled are
    skipped unless include_done=True.
    """
    where = "" if include_done else " AND (ep.audio_url IS NULL OR ep.audio_url = '')"
    with db.connect() as conn:
        edition = conn.execute(
            "SELECT id, edition_date FROM editions WHERE id = ?",
            (edition_id,),
        ).fetchone()
        if not edition:
            raise RuntimeError(f"Edition {edition_id} not found.")
        edition_date = date.fromisoformat(str(edition["edition_date"]))

        rows = conn.execute(f"""
            SELECT ep.edition_id, ep.article_id, ep.slot,
                   ep.hook, ep.contextualisation,
                   ep.audio_url AS existing_audio_url,
                   a.title, a.full_text
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
) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE edition_pieces "
            "SET audio_url = ?, duration_seconds = ? "
            "WHERE edition_id = ? AND article_id = ?",
            (audio_url, duration_seconds, edition_id, article_id),
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
    include_done: bool = False,
) -> Stage9Stats:
    """Run Stage 9 against the configured edition (default: latest)."""
    stats = Stage9Stats()

    if edition_id is None:
        edition_id = _get_latest_edition_id(db)
        if edition_id is None:
            logger.warning("Stage 9: no editions in DB.")
            return stats
        logger.info("Stage 9: using latest edition #%d", edition_id)

    edition_date, pieces = _load_edition_pieces(db, edition_id,
                                                 include_done=include_done)
    stats.pieces_total = len(pieces)
    if not pieces:
        logger.info("Stage 9: no pieces in edition #%d need audio", edition_id)
        return stats

    tts = build_tts_client(config.tts)
    audio_dir = config.audio_dir
    logger.info(
        "Stage 9: synthesizing %d pieces with voice=%s",
        len(pieces), tts.voice_id,
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

        out_path = _audio_path(audio_dir, edition_date, article_id)
        char_count = len(narration)
        approx_minutes = char_count / 1000.0   # ~1000 chars per spoken minute, very rough

        logger.info(
            "  [%s] article %d (%d chars, est ~%.1f min) — %s",
            slot, article_id, char_count, approx_minutes, title_preview,
        )

        try:
            result = tts.synthesize(narration, out_path)
        except Exception as e:
            stats.errors += 1
            logger.warning("      synthesis failed: %s", e)
            continue

        # Store the audio path relative to project root for portability.
        # The web/RSS renderers will join with a public URL base later.
        try:
            rel_path = result.output_path.relative_to(audio_dir.parent.parent)
        except ValueError:
            rel_path = result.output_path
        audio_url = str(rel_path)

        _save_audio(
            db, edition_id, article_id,
            audio_url=audio_url,
            duration_seconds=int(round(result.duration_seconds)),
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

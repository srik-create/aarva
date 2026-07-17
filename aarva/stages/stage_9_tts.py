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


def _attribution_line(piece: dict) -> str:
    """Build the spoken handoff between our intro and the article body.

    Format: 'Written by <author> in <publication>, narrated for Aarva.'

    Switched from 'read for Aarva' to 'narrated for Aarva' because
    Kokoro's phonemizer can't reliably disambiguate the heteronym 'read'
    (past 'red' vs present 'reed'). The default it chose was 'reed',
    which is grammatically present-tense and felt wrong here. 'Narrated'
    is unambiguous, has the same editorial register, and sidesteps the
    pronunciation problem entirely.

    Falls back gracefully when the byline is missing:
      - byline absent     → 'Written in <publication>, narrated for Aarva.'
      - publication absent → 'Narrated for Aarva.' (defensive; shouldn't
        happen because edition_pieces always join through to publications,
        but we don't want a crash if the column ever comes back NULL).

    Tonal note: the leading capital 'W'/'N' helps Kokoro raise pitch
    slightly at the start of the sentence, which makes the handoff feel
    like a distinct beat from the contextualisation. The trailing period
    gives a natural pause before the article body begins.
    """
    byline = (piece.get("byline") or "").strip()
    publication = (piece.get("publication_name") or "").strip()

    if byline and publication:
        return f"Written by {byline} in {publication}, narrated for Aarva."
    if publication:
        return f"Written in {publication}, narrated for Aarva."
    return "Narrated for Aarva."


# ─── Text normalisation for TTS ──────────────────────────────────────────────
#
# Article bodies come through trafilatura's `txt` extractor which is *mostly*
# plain text, but some publishers' HTML still produces output with stray
# markdown / pseudo-markdown formatting markers that Kokoro reads literally.
# The most common case observed in production: `*emphasis*` being read as
# "asterisk emphasis asterisk" instead of just emphasising the word.
#
# Kokoro has no SSML support, so we can't *add* emphasis. Best we can do is
# strip the markers and let the prosody fall where it falls.
#
# This is conservative — it strips markers when they look like inline
# formatting but leaves literal asterisks alone in mathematical contexts
# (e.g., "2*3=6" stays as-is because the asterisks aren't wrapping a
# word). Same with hyphens, em-dashes, etc.

_MD_EMPHASIS    = re.compile(r"(?<![*\w])\*([^*\s][^*]*?[^*\s]|[^*\s])\*(?![*\w])")
_MD_BOLD        = re.compile(r"(?<!\*)\*\*([^*]+?)\*\*(?!\*)")
_MD_ITAL_UNDER  = re.compile(r"(?<![_\w])_([^_\s][^_]*?[^_\s]|[^_\s])_(?![_\w])")
_MD_BOLD_UNDER  = re.compile(r"(?<!_)__([^_]+?)__(?!_)")
_MD_CODE_INLINE = re.compile(r"`([^`]+)`")
_MD_LINK        = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_HEADER      = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_BLOCKQUOTE  = re.compile(r"^>\s?", re.MULTILINE)
_MD_HRULE       = re.compile(r"^[-*_]{3,}\s*$", re.MULTILINE)
_MD_LIST_BULLET = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)


def _normalize_for_tts(text: str) -> str:
    """Strip inline formatting markers that Kokoro would otherwise pronounce.

    Order matters: bold patterns must run BEFORE single-asterisk emphasis
    (otherwise the inner pair eats the outer markers and we leave a stray
    asterisk behind). Headers and block-level markers run last because they
    consume leading whitespace.
    """
    if not text:
        return text

    # Block-level markdown first.
    text = _MD_HRULE.sub("", text)
    text = _MD_HEADER.sub("", text)
    text = _MD_BLOCKQUOTE.sub("", text)
    text = _MD_LIST_BULLET.sub("", text)

    # Links → keep the visible text, drop the URL.
    text = _MD_LINK.sub(r"\1", text)

    # Inline emphasis / bold. Bold before italic so doubled markers go first.
    text = _MD_BOLD.sub(r"\1", text)
    text = _MD_BOLD_UNDER.sub(r"\1", text)
    text = _MD_EMPHASIS.sub(r"\1", text)
    text = _MD_ITAL_UNDER.sub(r"\1", text)

    # Inline code → keep the word(s), drop the backticks.
    text = _MD_CODE_INLINE.sub(r"\1", text)

    return text


def _compose_narration(piece: dict) -> str:
    """Combine hook + context + attribution + article body.

    Blank lines render as pauses in Kokoro. The attribution line sits as
    its own paragraph between our intro and the article body so the
    transition is sonically clear. All text is run through
    _normalize_for_tts so that markdown emphasis markers aren't read
    literally by Kokoro.
    """
    parts: list[str] = []
    if piece.get("hook"):
        parts.append(_normalize_for_tts(str(piece["hook"]).strip()))
    if piece.get("contextualisation"):
        parts.append(_normalize_for_tts(str(piece["contextualisation"]).strip()))
    if piece.get("full_text"):
        # Attribution sits between the intro material and the body, so the
        # listener gets a clear handoff before the article proper starts.
        # Attribution is our own copy and doesn't need normalisation, but
        # we run it through anyway for consistency.
        parts.append(_normalize_for_tts(_attribution_line(piece)))
        parts.append(_normalize_for_tts(str(piece["full_text"]).strip()))
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
                   a.title, a.full_text, a.byline, a.author_country_code,
                   p.name AS publication_name
              FROM edition_pieces ep
              JOIN articles a ON a.id = ep.article_id
              JOIN publications p ON p.id = a.publication_id
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
    """Latest DAILY edition. Excludes crosscut episodes (which have
    their own TTS path) so Stage 9 doesn't accidentally grab a crosscut
    that was built after the daily edition row."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM editions "
            " WHERE edition_type = 'daily' "
            " ORDER BY edition_date DESC, id DESC LIMIT 1"
        ).fetchone()
    return int(row["id"]) if row else None


# ─────────────────────────────────────────────────────────────────────────────
# Accent steering (per-publication country → TTS style prompt)
# ─────────────────────────────────────────────────────────────────────────────
#
# Each publication in publications.yaml can carry a `country: us|uk|india`
# tag. When set, we prepend a per-piece style instruction to the TTS call
# so the model leans into that regional English flavour. Without a tag,
# the voice's baseline accent (broadly transatlantic/American) is used.
#
# Per Google's docs: accents on Gemini TTS are prompt-driven, not voice-
# name-driven. The same voice produces different accents based on this
# steer. More-specific phrasing yields more-pronounced accents.

_COUNTRY_TO_ACCENT_PROMPT: dict[str, str] = {
    "us":    "Spoken with a neutral American English accent.",
    "uk":    "Spoken with a British English accent in the style of BBC Radio 4.",
    "india": "Spoken with an educated Indian English accent "
             "(urban, English-medium-educated).",
}


def _build_publication_country_map() -> dict[str, str]:
    """Return {publication_name: country_code} from publications.yaml.

    Publications without a country tag are absent from the map. Loaded
    once per Stage 9 invocation; the YAML file is small (<10 KB)."""
    try:
        from aarva.config import load_publications
        return {
            p.name: p.country for p in load_publications()
            if p.country
        }
    except Exception as e:
        logger.warning("Could not load publications.yaml for accent map: %s", e)
        return {}


def _accent_prompt_for(piece: dict, country_map: dict[str, str]) -> str | None:
    """Look up the per-piece accent steer for this piece.

    Precedence (2026-07-16 — docs/session_plan_author_provenance_
    accents.md): author provenance strictly overrides the publication
    tag. Publication-based steering under-covers publications with
    unaffiliated global authors (The Diplomat) and over-covers pan-
    regional ones (Himal Southasian) — the real signal is where the
    AUTHOR currently lives/works, classified by Stage 8c and cached on
    articles.author_country_code. Falls through to the publication tag
    when provenance is NULL (not yet classified) or 'unknown'
    (classified, no usable evidence) — both cases mean "we don't know
    the author", so the publication tag is the best remaining signal.
    None (no accent steer) when neither is available."""
    author_cc = (piece.get("author_country_code") or "").strip().lower()
    if author_cc in _COUNTRY_TO_ACCENT_PROMPT:
        return _COUNTRY_TO_ACCENT_PROMPT[author_cc]

    pub_name = (piece.get("publication_name") or "").strip()
    if not pub_name:
        return None
    country = country_map.get(pub_name)
    if not country:
        return None
    return _COUNTRY_TO_ACCENT_PROMPT.get(country)


# ─────────────────────────────────────────────────────────────────────────────
# Voice selection
# ─────────────────────────────────────────────────────────────────────────────

_NARRATOR_PROMPT = """\
You are matching a voice to a narrator for an audio podcast. Decide
the narrator's gender from the byline + article body. Reply with
EXACTLY ONE WORD: MALE, FEMALE, or NEUTRAL.

Two-step reasoning:

STEP 1 — Is the article written in the first person?
Look for "I", "me", "my", "mine" used as the author's own voice
(NOT inside quoted speech from sources). The personal "we" / "us"
(e.g., "we drove to the coast") also counts; the editorial "we"
(e.g., "we believe accountability matters") does NOT.

If not first-person → respond NEUTRAL. Stop.

STEP 2 — If first-person, what is the author's gender?
Use your knowledge of names from ALL cultures, not just Anglo:
  - Male names exist across cultures: John, Giri, Hiroshi, Ahmed,
    Tunde, Diego, Jian, Karthik, Olu, Rajiv, Yusuf, Andrei, Pieter,
    Cheikh, Pranav, Adwait, Sunil, Vikram, Arjun, Rohan, Aravind.
  - Female names exist across cultures: Sarah, Priya, Yuki, Fatima,
    Aisha, Mei, Lakshmi, Adaeze, Sofia, Anika, Sneha, Kavita,
    Meera, Anushka, Shreya, Devika, Nandini, Saraswati.

Also look at the body for gender cues — "my husband", "my wife",
"as a father of two", "growing up as a girl in", etc.

If the byline name's gender is clear AND the article is first-person
→ MALE or FEMALE accordingly.

If the byline is truly genderless (initials only, pseudonym, multi-
author, or a name you genuinely cannot place across any culture)
AND there are no body cues → NEUTRAL.

Do NOT default to NEUTRAL just because the byline isn't a common
Anglo name. Indian, Chinese, Arabic, African, Latin American, and
European names all carry recognisable gender — use that knowledge.

Byline: {byline}
Article (first 2500 chars):
{excerpt}

Reply with one word: MALE, FEMALE, or NEUTRAL."""


_FIRST_PERSON_RE = re.compile(r"\b(I|my|me|mine)\b")
# Capture text inside ASCII double quotes AND smart curly quotes (longform
# pieces vary). We strip these before the first-person scan so quoted
# interviewee speech doesn't trigger false positives.
_QUOTED_SPEECH_RES = [
    re.compile(r'"[^"]*"'),
    re.compile(r'“[^”]*”'),    # "curly"
]


def _has_first_person_markers(text: str) -> bool:
    """True iff the article body contains 'I'/'me'/'my'/'mine' as the
    author's own voice (excluding text inside quoted speech)."""
    if not text:
        return False
    stripped = text
    for rx in _QUOTED_SPEECH_RES:
        stripped = rx.sub("", stripped)
    return bool(_FIRST_PERSON_RE.search(stripped))


# Larger excerpt so that articles with first-person passages deeper in
# the body (common for longform — many magazine pieces open with subject
# coverage and the author's voice surfaces only after 600-1500 words)
# are scored correctly. 8000 chars ≈ 1500 words ≈ 2000 tokens, trivial
# for Gemini 3 Flash.
_NARRATOR_EXCERPT_CHARS = 8000


def _detect_narrator_gender(piece: dict, llm: LLMClient) -> str:
    """Returns 'male', 'female', or 'neutral'.

    Two-stage detection:
      1. Python regex pre-scan over the WHOLE body (with quoted speech
         stripped) for first-person markers. If none → NEUTRAL,
         no LLM call (saves a Gemini round-trip for the ~60-70% of
         pieces that are third-person reporting).
      2. If first-person markers exist, an LLM call determines gender
         from byline + a generous excerpt window. The prompt is told
         that first-person markers ARE present, so it focuses on
         gender attribution rather than re-deciding the first-person
         question (which it can get wrong when those markers live
         outside its excerpt window).
    """
    full_text = piece.get("full_text") or ""
    byline = piece.get("byline") or "Unknown"

    if not _has_first_person_markers(full_text):
        return "neutral"

    excerpt = full_text[:_NARRATOR_EXCERPT_CHARS]
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


def _normalise_voice_pool(value) -> list[str]:
    """voice_map values can be a single string or a list. Always return a list."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def _gender_of_voice(voice_name: str,
                     female_pool: list[str],
                     male_pool: list[str]) -> str:
    if voice_name in female_pool:
        return "female"
    if voice_name in male_pool:
        return "male"
    return "unknown"


def _plan_voices_for_edition(
    pieces: list[dict],
    female_pool: list[str],
    male_pool: list[str],
    rule: str,
    llm: Optional[LLMClient],
) -> dict[int, tuple[str, str]]:
    """Pre-compute voice assignments for every piece in the edition.

    Returns {article_id: (voice_name, reason)}.

    Constraints, in priority order:

      1. Each voice used at most once per edition (anti-duplication).
      2. First-person pieces with detected gender MUST get a voice from
         the matching gender pool (gender-match override). If the
         matching pool is exhausted, we soft-relax and pick from the
         other pool, logging a warning — this happens only when an
         edition has more first-person pieces of one gender than we
         have matching voices.
      3. Neutral pieces alternate gender for natural variation (start
         with female if no prior pieces; else flip from previous piece).
      4. Within a gender pool, pick voices in the pool's listed order
         (so the first time a gender appears, it uses the first voice
         in the config — making the editorial intent visible).

    The two-pass design (first pass assigns gendered pieces, second pass
    fills neutral pieces) means gendered pieces never get a voice that
    would have been better used for a later neutral piece.

    Rules:
      - "rotate_with_gender_match" (default for the new 6-voice setup):
        full pre-planning with gender check + rotation.
      - "alternate_with_gender_match" (legacy): falls into this same
        planner — works identically when pools have only 1 voice each.
      - "alternate": skips the LLM gender call, alternates F/M by position.
      - "single": uses only the first voice in female_pool (or male_pool
        if female empty), every piece.
    """
    plan: dict[int, tuple[str, str]] = {}
    used: set[str] = set()

    # ── single ────────────────────────────────────────────────────────────
    if rule == "single":
        only = (female_pool + male_pool)[:1]
        if not only:
            return {}
        for piece in pieces:
            plan[int(piece["article_id"])] = (only[0], "single-voice rule")
        return plan

    # ── alternate (no gender detection) ───────────────────────────────────
    if rule == "alternate":
        for i, piece in enumerate(pieces):
            pool = female_pool if i % 2 == 0 else male_pool
            # Within pool, pick first unused; relax to any unused if exhausted
            chosen = next((v for v in pool if v not in used), None)
            if chosen is None:
                chosen = next(
                    (v for v in (female_pool + male_pool) if v not in used),
                    (female_pool + male_pool)[0] if (female_pool + male_pool) else "",
                )
            plan[int(piece["article_id"])] = (
                chosen, f"alternation (position {i})"
            )
            used.add(chosen)
        return plan

    # ── alternate_with_gender_match / rotate_with_gender_match ────────────
    # Two passes: gendered pieces first, then neutral fill.
    gender_hints: dict[int, str] = {}
    for piece in pieces:
        if llm is None:
            gender_hints[int(piece["article_id"])] = "neutral"
        else:
            gender_hints[int(piece["article_id"])] = _detect_narrator_gender(
                piece, llm,
            )

    # Pass 1: pieces with detected gender.
    for piece in pieces:
        aid = int(piece["article_id"])
        hint = gender_hints[aid]
        if hint not in ("female", "male"):
            continue
        primary = female_pool if hint == "female" else male_pool
        secondary = male_pool if hint == "female" else female_pool
        chosen = next((v for v in primary if v not in used), None)
        if chosen is not None:
            plan[aid] = (chosen, f"first-person {hint} → {chosen}")
            used.add(chosen)
        else:
            # Gender pool exhausted; soft-relax to the other pool.
            chosen = next((v for v in secondary if v not in used), None)
            if chosen is not None:
                plan[aid] = (chosen, f"first-person {hint}, but {hint} "
                                     f"pool exhausted → {chosen}")
                used.add(chosen)
            else:
                # All voices used — last resort, reuse the primary's first.
                chosen = primary[0] if primary else (secondary[0] if secondary else "")
                plan[aid] = (chosen, f"first-person {hint}, all voices used → "
                                     f"{chosen} (reuse)")

    # Pass 2: neutral pieces, alternating gender preference based on the
    # gender of the previously-placed voice (so the edition has natural
    # voice variation across slots).
    def _prev_gender_at(i: int) -> str:
        """Gender of voice at position i-1, if assigned, else 'female' (start with F)."""
        if i == 0:
            return "male"   # so the first neutral piece prefers female
        prev_piece = pieces[i - 1]
        prev_aid = int(prev_piece["article_id"])
        if prev_aid in plan:
            return _gender_of_voice(plan[prev_aid][0], female_pool, male_pool)
        return "male"

    for i, piece in enumerate(pieces):
        aid = int(piece["article_id"])
        if aid in plan:
            continue   # gendered piece already placed
        prev = _prev_gender_at(i)
        # Flip from previous gender for variety.
        primary = female_pool if prev == "male" else male_pool
        secondary = male_pool if prev == "male" else female_pool
        chosen = next((v for v in primary if v not in used), None)
        if chosen is None:
            chosen = next((v for v in secondary if v not in used), None)
        if chosen is None:
            # Everything used. With 6 voices and 6 slots this only happens
            # if pools are very small. Reuse the first voice as a fallback.
            all_voices = female_pool + male_pool
            chosen = all_voices[0] if all_voices else ""
            plan[aid] = (chosen, f"position {i}, all voices used → {chosen} (reuse)")
        else:
            plan[aid] = (chosen, f"position {i}, neutral → {chosen}")
            used.add(chosen)

    return plan


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def generate_for_edition(
    config: PipelineConfig,
    db: Database,
    *,
    edition_id: Optional[int] = None,
    include_done: bool = False,
    tts: Optional[TTSClient] = None,
    llm: Optional[LLMClient] = None,
) -> Stage9Stats:
    """Generate audio for pieces in an edition.

    tts: pass an existing TTS client to avoid rebuilding (DI).
    llm: pass an existing LLM client. Only used when voice_selection_rule
         is gender-aware; ignored otherwise.
    """
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

    if tts is None:
        tts = build_tts_client(config.tts)
    rule = config.tts.get("voice_selection_rule", "rotate_with_gender_match")

    # Build the per-gender voice pools from voice_map. Each entry can be a
    # single name (legacy) or a list (rotation mode). _normalise_voice_pool
    # handles both.
    voice_map_cfg = config.tts.get("voice_map") or {}
    female_pool = _normalise_voice_pool(voice_map_cfg.get("female"))
    male_pool = _normalise_voice_pool(voice_map_cfg.get("male"))

    # Gender-aware rules need the LLM to detect first-person speaker gender.
    # Build one lazily if the caller didn't supply one.
    if rule in ("rotate_with_gender_match", "alternate_with_gender_match"):
        if llm is None:
            llm = build_llm_client(config.llm)

    audio_dir = config.audio_dir
    # Pre-load the publication-name → country lookup for accent steering.
    # Built once per Stage 9 run; reused across every piece's TTS call.
    country_map = _build_publication_country_map()
    logger.info(
        "Stage 9: synthesizing %d pieces  |  rule=%s  |  "
        "female pool=%s  |  male pool=%s  |  "
        "accent-tagged publications: %d",
        len(pieces), rule, female_pool, male_pool, len(country_map),
    )

    # Plan voice assignments for the entire edition up front. This lets
    # the rotation logic see all pieces at once and avoid voice
    # duplication / gender-pool exhaustion surprises mid-loop.
    voice_plan = _plan_voices_for_edition(
        pieces, female_pool, male_pool, rule, llm,
    )

    # Pretty-print the plan so it's auditable in the log.
    logger.info("Stage 9 voice plan:")
    for piece in pieces:
        aid = int(piece["article_id"])
        if aid in voice_plan:
            v, reason = voice_plan[aid]
            logger.info("  pos=%d [%s] article %d → %s  (%s)",
                        piece.get("position", 0), piece["slot"], aid, v, reason)

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

        if int(article_id) not in voice_plan:
            logger.warning("  [%s] article %d — no voice planned; skipping",
                           slot, article_id)
            stats.errors += 1
            continue

        voice_id, reason = voice_plan[int(article_id)]

        out_path = _audio_path(audio_dir, edition_date, article_id)
        char_count = len(narration)
        approx_minutes = char_count / 1000.0

        logger.info(
            "  [%s] article %d (%d chars, est ~%.1f min) — %s",
            slot, article_id, char_count, approx_minutes, title_preview,
        )
        logger.info("      voice: %s  (%s)", voice_id, reason)

        # Per-piece accent steer, if the publication has a country tag.
        accent_prompt = _accent_prompt_for(piece, country_map)
        if accent_prompt:
            logger.info("      accent: %s", accent_prompt)

        try:
            result = tts.synthesize(
                narration, out_path,
                voice_id=voice_id,
                extra_style=accent_prompt,
            )
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

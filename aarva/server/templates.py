"""Shared Jinja2 templates instance.

All route modules import `templates` from here to render responses,
so we have one configured Environment with the right path + filters
instead of constructing it per-module.
"""
from __future__ import annotations

import html
from pathlib import Path

from fastapi.templating import Jinja2Templates

from aarva.services.prompt_suggestions import PROMPT_SUGGESTIONS


_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# Custom filters — small editorial niceties for the templates.

def _format_duration(seconds: float | None) -> str:
    """Format a duration in seconds as human-readable 'M:SS' / 'H:MM:SS'.

    Used in audio player / piece headers ('14:32' / '1:02:17').
    None → empty string."""
    if not seconds:
        return ""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _format_audio_url(audio_url: str | None, audio_base: str = "") -> str:
    """Convert the relative audio_url from edition_pieces into a
    full URL. Uses tts.r2.public_url_base when set in pipeline.yaml
    (so audio streams from Cloudflare R2); falls back to no-prefix
    relative URL otherwise (caller can resolve relative to host)."""
    if not audio_url:
        return ""
    base = (audio_base or "").rstrip("/")
    rel = audio_url.lstrip("/")
    return f"{base}/{rel}" if base else f"/{rel}"


_TITLE_CASE_SMALL_WORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "nor", "for", "yet", "so",
    "as", "at", "by", "in", "of", "on", "to", "up", "via", "with",
    "if", "is",   # common borderline; included to match magazine/editorial style
})


def _title_case(s: str | None) -> str:
    """Title-case a string for headings: significant words capitalised,
    small connectives lowercase, but the first/last word and any word
    after sentence-ending punctuation always capitalised.

    Handles three tricky cases that Python's stdlib .title() gets wrong:
      - 'isn't' → 'Isn't' (not 'Isn'T')
      - 'ChatGPT' → 'ChatGPT' (not 'Chatgpt' — mixed-case preserved)
      - 'But what if we're wrong?' → 'But What if We're Wrong?'
        (small word 'if' stays lowercase mid-sentence; final 'Wrong'
        always capitalised)

    Per AGENTS.md rule 9b — applied uniformly to all listener-facing
    titles (article titles, crosscut topic_label) so casing is
    consistent regardless of how the source publication / LLM
    capitalised it.
    """
    if not s:
        return s or ""
    # Decode HTML entities (e.g. Vox titles arrive as
    # "Shouldn&#8217;t Keep..." from the source RSS). Jinja's auto-
    # escape would otherwise turn the leading & into &amp; and the
    # browser would render "&#8217;" verbatim. Unescape ONCE here so
    # 'shouldn&#8217;t' becomes 'shouldn’t' before we case-fold
    # it; Jinja then escapes the Unicode apostrophe correctly (or
    # leaves it alone; either way it renders as ').
    s = html.unescape(s)
    words = s.split()
    if not words:
        return s

    out: list[str] = []
    prev_ended_sentence = True   # treat start of string as a boundary
    last_idx = len(words) - 1

    for i, w in enumerate(words):
        # Preserve mixed-case (acronyms / brand names): ChatGPT, iPhone, AI, IT.
        # If any char beyond the first is uppercase, leave the word as-is.
        if len(w) > 1 and any(c.isupper() for c in w[1:]):
            out.append(w)
            prev_ended_sentence = w.rstrip(",;:").endswith((".", "?", "!"))
            continue

        is_boundary = (i == 0 or i == last_idx or prev_ended_sentence)

        # Mid-sentence small words go lowercase
        if not is_boundary:
            stripped = w.lower().rstrip(".,;:!?")
            if stripped in _TITLE_CASE_SMALL_WORDS:
                lowered = w.lower()
                out.append(lowered)
                prev_ended_sentence = lowered.rstrip(",;:").endswith((".", "?", "!"))
                continue

        # Otherwise: cap first letter, lowercase the rest
        cap = w[0].upper() + (w[1:].lower() if len(w) > 1 else "")
        out.append(cap)
        prev_ended_sentence = cap.rstrip(",;:").endswith((".", "?", "!"))

    return " ".join(out)


_SLUG_NON_ALNUM = None  # lazy-compiled in _publication_slug


def _publication_slug(name: str | None) -> str:
    """URL-safe slug from a publication name.
    'ProPublica'         → 'propublica'
    'The Marshall Project' → 'the-marshall-project'
    'Le Monde diplomatique (English)' → 'le-monde-diplomatique-english'

    Used in /publication/<slug>. Round-trip: server compares slugify(pub.name)
    against the route arg, so the mapping is implicit (no slug table)."""
    import re
    global _SLUG_NON_ALNUM
    if _SLUG_NON_ALNUM is None:
        _SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")
    if not name:
        return ""
    return _SLUG_NON_ALNUM.sub("-", name.lower()).strip("-")


def _duration_minutes(seconds: float | int | None) -> str:
    """Render seconds as a coarse 'X min' string for listener-facing
    cards where 'M:SS' precision isn't useful. Returns empty string
    for None / 0 so templates can skip the line cleanly."""
    if not seconds:
        return ""
    minutes = round(int(seconds) / 60)
    if minutes <= 0:
        return ""
    return f"{minutes} min"


def _jtbd_label(jtbd_primary: str | None) -> str:
    """Human-readable label for a JTBD key. Falls back to empty so
    templates can `{% if piece.jtbd_primary | jtbd_label %}` cleanly."""
    from aarva.server.jtbd_meta import JTBD_BY_KEY
    if not jtbd_primary:
        return ""
    info = JTBD_BY_KEY.get(jtbd_primary)
    return info["label"] if info else ""


templates.env.filters["duration"] = _format_duration
templates.env.filters["audio_url"] = _format_audio_url
templates.env.filters["title_case"] = _title_case
templates.env.filters["publication_slug"] = _publication_slug
templates.env.filters["jtbd_label"] = _jtbd_label
templates.env.filters["duration_minutes"] = _duration_minutes

# Globals — available in every template without each route needing to
# pass them explicitly. PROMPT_SUGGESTIONS is used by the header
# dropdown (every page, via base.html) and the /create no-results
# fallback — see aarva/services/prompt_suggestions.py.
templates.env.globals["PROMPT_SUGGESTIONS"] = PROMPT_SUGGESTIONS

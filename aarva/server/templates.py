"""Shared Jinja2 templates instance.

All route modules import `templates` from here to render responses,
so we have one configured Environment with the right path + filters
instead of constructing it per-module.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates


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


templates.env.filters["duration"] = _format_duration
templates.env.filters["audio_url"] = _format_audio_url

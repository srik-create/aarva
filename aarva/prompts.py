"""Shared prompt loader + lightweight templating.

Previously duplicated in stage_4_5_6_score, stage_8_hook_context.
The crosscut module has its own small _render in-line because its
prompts live as Python string constants, not in prompts.yaml.

Caches the parsed YAML so reload-per-stage cost is gone. Tests that
need a fresh load can call invalidate_cache().
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


PROMPTS_PATH = Path(__file__).parent / "config" / "prompts.yaml"


@lru_cache(maxsize=1)
def load_prompts() -> dict[str, Any]:
    """Parse aarva/config/prompts.yaml. Cached after first call —
    invalidate_cache() to force a fresh read."""
    with PROMPTS_PATH.open() as f:
        return yaml.safe_load(f) or {}


def invalidate_cache() -> None:
    """Drop the cached prompts. Useful for hot-reload during dev /
    when running prompt-iteration scripts."""
    load_prompts.cache_clear()


def render(template: str, **kwargs: Any) -> str:
    """Lightweight `{{ var }}` substitution. Tolerates both `{{ var }}`
    (with spaces) and `{{var}}` (no spaces). Values are stringified."""
    out = template
    for k, v in kwargs.items():
        out = out.replace("{{ " + k + " }}", str(v))
        out = out.replace("{{" + k + "}}", str(v))
    return out

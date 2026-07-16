"""Shared example-prompt list — docs/session_plan_search_suggestions.md.

Single source of truth for two features that both show the same six
examples: the header dropdown shown on focus into an empty prompt
input (Feature A, rendered via the `PROMPT_SUGGESTIONS` Jinja global
registered in aarva/server/templates.py), and the `/create` no-results
fallback (Feature B).

Deliberately spans different registers a listener might type in:
topic, feeling, juxtaposition, question, opinion, vibe — so the list
signals "you can ask in any of these ways," not just one. Static,
locked list — per spec, do NOT rotate or personalise.
"""
from __future__ import annotations

PROMPT_SUGGESTIONS: list[str] = [
    "new perspectives on the iran war",
    "i'm feeling down — give me something to cheer me up",
    "jazz and ai",
    "how belief forms",
    "opposing views on carbon capture",
    "quietly thoughtful nature writing",
]

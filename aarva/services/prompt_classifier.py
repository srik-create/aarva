"""Classify a listener's /create prompt by time-sensitivity.

Existing-match search (episode_candidates.py::_existing_matches) can
surface episodes built weeks ago. That's fine for evergreen prompts
("how belief forms") but wrong for news-shaped ones ("what's happening
with the election") — an old episode about a since-resolved story
would be a bad match. This module classifies the prompt so the search
gate (search.max_age_days_news in pipeline.yaml) can apply an age
filter only where it's warranted.

Uses the existing aarva.clients.llm.LLMClient interface — same Gemini
call pattern as episode_candidates.py's pairing proposal, no new
provider integration.
"""
from __future__ import annotations

import logging

from aarva.clients.llm import LLMClient

logger = logging.getLogger(__name__)


VALID_CATEGORIES = ("behind_the_news", "future_gazing", "evergreen")

_CLASSIFY_PROMPT = """Classify the listener prompt below into exactly ONE category.

behind_the_news — asks about a current or recent news event, or the meaning behind a story from roughly the last one to two weeks (elections, wars, breaking scientific announcements, court rulings, etc.).
future_gazing    — asks about coming changes, speculation, or forward-looking analysis (e.g. "what's next for AI regulation", "where crypto is heading").
evergreen        — a timeless question, pattern, or idea with no dependency on current events (e.g. "how belief forms", "why we love myth", "the psychology of habit").

If genuinely unsure, prefer evergreen — that's the safer default (it just means no date filter is applied to search matches).

Prompt: {{ prompt }}

Return JSON ONLY — no prose, no markdown fences. A single object:
{"category": "<one of: behind_the_news, future_gazing, evergreen>"}"""


def classify_prompt(prompt: str, llm: LLMClient) -> str:
    """Return one of VALID_CATEGORIES for `prompt`. Falls back to
    'evergreen' (no age filter — the safer default) on any LLM
    failure or unparseable/invalid response, so a classifier hiccup
    degrades to "search everything" rather than silently hiding valid
    matches."""
    rendered = _CLASSIFY_PROMPT.replace("{{ prompt }}", prompt)
    try:
        result = llm.complete(rendered, expect_json=True, temperature=0.0)
    except Exception as e:
        logger.warning("prompt_classifier: LLM call failed: %s", e)
        return "evergreen"

    if not isinstance(result, dict):
        logger.warning("prompt_classifier: LLM returned non-dict: %r", result)
        return "evergreen"

    category = str(result.get("category") or "").strip()
    if category not in VALID_CATEGORIES:
        logger.warning("prompt_classifier: invalid category %r", category)
        return "evergreen"
    return category

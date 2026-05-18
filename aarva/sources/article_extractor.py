"""Full-text extraction from article URLs.

Uses trafilatura, which handles most modern publisher HTML well. Failures
(paywalls, JS-rendered pages, dead links) return None — Stage 2 will then
filter those out for not meeting the word floor.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx
import trafilatura

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExtractedArticle:
    full_text: str
    excerpt: str           # first ~300 words for consolidation / display
    word_count: int


def _make_excerpt(full_text: str, max_words: int = 300) -> str:
    words = full_text.split()
    if len(words) <= max_words:
        return full_text
    return " ".join(words[:max_words])


def extract_article(
    url: str,
    *,
    timeout: int = 30,
    user_agent: str = "Aarva/0.1",
) -> Optional[ExtractedArticle]:
    """Fetch a URL and extract its main article text. Returns None on failure."""
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True,
                          headers={"User-Agent": user_agent}) as client:
            response = client.get(url)
            response.raise_for_status()
            html = response.text
    except (httpx.HTTPError, httpx.RequestError) as e:
        logger.info("Fetch failed for %s: %s", url, e)
        return None

    extracted = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=False,
        favor_recall=False,
    )
    if not extracted:
        logger.info("Trafilatura extracted no text from %s", url)
        return None

    text = extracted.strip()
    word_count = len(text.split())
    if word_count == 0:
        return None

    return ExtractedArticle(
        full_text=text,
        excerpt=_make_excerpt(text),
        word_count=word_count,
    )

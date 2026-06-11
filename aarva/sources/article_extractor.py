"""Full-text extraction from article URLs.

Strategy is a three-stage cascade — each fallback is slightly more permissive,
which lets us catch sites (like Aeon) where the default trafilatura settings
under-extract:

  1. trafilatura precision mode (favor_recall=False)   ← best signal-to-noise
  2. trafilatura recall mode    (favor_recall=True)    ← catches more, riskier
  3. trafilatura "baseline" with minimal heuristics    ← last resort

Each step is only invoked if the prior step returned suspiciously little text
(measured against a configurable minimum word count). This means we don't pay
the cost of fallbacks on the common case where the default mode works fine,
but we do recover gracefully on sites where it doesn't.

Failures (paywalls, JS-rendered pages, dead links, video pages) return None.
Stage 2 then filters those out via the word floor.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx
import trafilatura

logger = logging.getLogger(__name__)


# If the first pass extracts fewer than this many words, we treat it as a
# probable under-extraction and try the recall-mode fallback. 200 is below
# our Stage 2 floor (600) so even a "good" short essay triggers a retry —
# which is fine; the retry is cheap (no new HTTP fetch).
UNDER_EXTRACTION_THRESHOLD = 200


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


def _fetch_html(url: str, timeout: int, user_agent: str) -> Optional[str]:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True,
                          headers={"User-Agent": user_agent}) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.text
    except (httpx.HTTPError, httpx.RequestError) as e:
        logger.info("Fetch failed for %s: %s", url, e)
        return None


def _try_extract(html: str, *, favor_recall: bool, no_fallback: bool = False) -> Optional[str]:
    """Wrap trafilatura.extract with a few tuned knobs.

    Returns the extracted text (stripped) or None if nothing came back.
    """
    try:
        text = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=False,
            favor_recall=favor_recall,
            no_fallback=no_fallback,
            output_format="txt",
        )
    except Exception as e:
        logger.warning("trafilatura raised %s: %s", type(e).__name__, e)
        return None
    if not text:
        return None
    text = text.strip()
    return text or None


def _word_count(text: Optional[str]) -> int:
    return len(text.split()) if text else 0


def extract_article(
    url: str,
    *,
    timeout: int = 30,
    user_agent: str = "Aarva/0.1",
) -> Optional[ExtractedArticle]:
    """Fetch a URL and extract its main article text. Returns None on failure."""
    html = _fetch_html(url, timeout, user_agent)
    if not html:
        return None

    # Pass 1 — default precision mode.
    text = _try_extract(html, favor_recall=False)
    n = _word_count(text)
    if n >= UNDER_EXTRACTION_THRESHOLD:
        return _build(text)

    # Pass 2 — recall mode. Catches sites where precision rejects valid body
    # blocks (Aeon, certain Substack themes, some long-form magazine layouts).
    text2 = _try_extract(html, favor_recall=True)
    n2 = _word_count(text2)
    if n2 > n:
        logger.info("Recall fallback for %s: %d → %d words", url, n, n2)
        text, n = text2, n2

    # Pass 3 — bypass trafilatura's algorithm fallback chain entirely and let
    # its baseline extractor try. Rarely beats pass 2 but occasionally rescues
    # very-unusual page layouts.
    text3 = _try_extract(html, favor_recall=True, no_fallback=True)
    n3 = _word_count(text3)
    if n3 > n:
        logger.info("Baseline fallback for %s: %d → %d words", url, n, n3)
        text, n = text3, n3

    if not text or n == 0:
        logger.info("Trafilatura extracted no text from %s", url)
        return None

    if n < UNDER_EXTRACTION_THRESHOLD:
        # We pulled *something* but it's under the threshold. Let it through —
        # Stage 2's word floor will catch it if it's genuinely too short, but
        # the extraction itself didn't "fail" so we record it as ingested.
        logger.info("Short extraction (%d words) for %s", n, url)

    return _build(text)


def _build(text: str) -> ExtractedArticle:
    word_count = len(text.split())
    return ExtractedArticle(
        full_text=text,
        excerpt=_make_excerpt(text),
        word_count=word_count,
    )

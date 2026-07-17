"""Stage 8c — Author-provenance classification for TTS accent steering.

See docs/session_plan_author_provenance_accents.md for the full design.
Today's accent steering (aarva/stages/stage_9_tts.py::_accent_prompt_for)
is publication-based, which under-covers publications with unaffiliated
global authors (The Diplomat) and over-covers pan-regional ones (Himal
Southasian). The real signal is where the AUTHOR currently lives/works,
not what publication they write for.

This stage classifies each article's author's CURRENT provenance —
explicitly NOT name-based inference, which the user ruled out: diaspora
authors are common (a UK-based author with an Indian name must get a
UK accent, not an Indian one). Only real evidence in the byline, author
bio, or article body counts. Absent or ambiguous evidence → 'unknown',
not a guess — a wrong accent is worse than the existing publication-tag
fallback.

Runs once per article, cached on articles.author_country_code:
  NULL     — not yet classified (pre-migration / newly ingested)
  'us' / 'uk' / 'india' — classified, real evidence found
  'unknown' — classified, no usable evidence (a terminal result, not
              a "try again" state)

Wired into the daily pipeline as Stage 8.5 (between Stage 8 hook/
context and Stage 9 TTS — see aarva/daily.py) so newly-ingested
articles get classified before the day's TTS run needs the value.
scripts/backfill_author_country.py runs the same function once over
the whole existing catalog.
"""
from __future__ import annotations

import concurrent.futures
import logging
import re
import threading
from dataclasses import dataclass
from typing import Optional

from aarva.clients.llm import LLMClient, build_llm_client
from aarva.config import PipelineConfig
from aarva.db import Database

logger = logging.getLogger(__name__)


VALID_PROVENANCE_CODES = {"us", "uk", "india"}

# Excerpt shape: first 2000 + last 1000 chars. Datelines and "the
# author lives in..." leads tend to open a piece; author bios tend to
# sit in a footer. The middle of a long article is usually pure
# reporting body, not provenance-relevant.
_HEAD_CHARS = 2000
_TAIL_CHARS = 1000


_PROVENANCE_PROMPT = """\
You classify the CURRENT PROVENANCE of a journalism article's author,
based ONLY on evidence in the byline, author bio, and article body.
This is used to select a TTS accent for the audio version.

Return ONE label:

- us       — Author currently lives, works, or grew up primarily in
             the United States. Evidence must be explicit: "based in
             New York", "American commentator", "grew up in Ohio",
             university/employer in the US, or clear first-person
             markers ("here in California").
- uk       — Same standard, for the United Kingdom.
- india    — Same standard, for India specifically. Not "South Asia"
             — for regional writers we still need explicit India
             signal.
- unknown  — Evidence is absent, ambiguous, or the author is clearly
             from elsewhere (France, China, Nigeria, Australia,
             diaspora writer whose provenance isn't one of us/uk/
             india, etc.).

CRITICAL RULES:
- Do NOT infer from the author's name. Names indicate heritage, not
  current provenance. Neel Mukherjee → NOT india unless his bio says
  he lives in India. Akhilesh Pillalamarri → NOT india unless his
  bio confirms India (he lives in the US → us if evidence supports,
  else unknown).
- Do NOT infer from the publication. This is about the AUTHOR.
- Do NOT infer from the article's topic or dateline. An article
  about U.S. domestic policy, published by a U.S. newsroom (e.g.
  ProPublica, AP, a local U.S. paper), is NOT by itself evidence the
  author lives in the U.S. — many wire-service and syndicated
  bylines are written by non-U.S.-based reporters, and staff writers
  at U.S. nonprofits are not all U.S. residents. You need a
  statement ABOUT THE AUTHOR specifically (bio line, first-person
  residence claim), not an inference from subject matter.
- Prefer 'unknown' when in doubt. A wrong-accent voice is worse than
  a neutral default.

Article byline: {byline}
Article body (first {head_chars} + last {tail_chars} chars, to
capture the opening and any author-bio footer):
{body_excerpt}

Reply with exactly one word: us, uk, india, or unknown."""


def _build_body_excerpt(full_text: str) -> str:
    text = full_text or ""
    if len(text) <= _HEAD_CHARS + _TAIL_CHARS:
        return text
    return text[:_HEAD_CHARS] + "\n...\n" + text[-_TAIL_CHARS:]


def classify_author_provenance(article: dict, llm: LLMClient) -> str:
    """Classify one article's author's CURRENT provenance from byline +
    body evidence only — never from the name alone (see module
    docstring). Returns 'us', 'uk', 'india', or 'unknown'. Defaults to
    'unknown' on any parse error or unexpected response, matching the
    prompt's own "prefer unknown when in doubt" instruction."""
    byline = (article.get("byline") or "").strip() or "Unknown"
    body_excerpt = _build_body_excerpt(article.get("full_text") or "")
    prompt = _PROVENANCE_PROMPT.format(
        byline=byline, body_excerpt=body_excerpt,
        head_chars=_HEAD_CHARS, tail_chars=_TAIL_CHARS,
    )
    try:
        response = llm.complete(prompt, expect_json=False, temperature=0.0)
        text = str(response).strip().lower()
        for code in ("us", "uk", "india"):
            if re.search(rf"\b{code}\b", text):
                return code
        return "unknown"
    except Exception as e:
        logger.warning("Author-provenance classification failed: %s", e)
        return "unknown"


@dataclass
class Stage8cStats:
    candidates: int = 0
    classified: int = 0
    us: int = 0
    uk: int = 0
    india: int = 0
    unknown: int = 0
    errors: int = 0


def classify_pending_articles(
    config: PipelineConfig,
    db: Database,
    *,
    llm: Optional[LLMClient] = None,
    limit: Optional[int] = None,
) -> Stage8cStats:
    """Classify author provenance for every article that hasn't been
    classified yet (author_country_code IS NULL). Idempotent —
    'unknown' is a terminal classification (evidence was absent or
    ambiguous), not a retry state; only NULL rows are re-considered.

    llm: pass an existing client to avoid rebuilding (and to preserve
    rate-limiter state across calls) — the CLI orchestrator path
    builds its own from config.llm when not provided.

    limit: cap the batch size (used by the backfill script to process
    in manageable chunks); None processes everything pending."""
    if llm is None:
        llm = build_llm_client(config.llm)
    logger.info("Stage 8c starting with LLM=%s", llm.name)

    with db.connect() as conn:
        query = """
            SELECT id, byline, full_text FROM articles
             WHERE author_country_code IS NULL
               AND full_text IS NOT NULL
             ORDER BY id
        """
        if limit:
            query += f" LIMIT {int(limit)}"
        rows = conn.execute(query).fetchall()

    candidates = [dict(r) for r in rows]
    stats = Stage8cStats(candidates=len(candidates))
    if not candidates:
        logger.info("Stage 8c: no unclassified articles pending")
        return stats

    # Concurrency mirrors Stage 4/5/6's pattern — network-bound LLM
    # calls, thread-safe stats, rate-limited globally by the LLM
    # client's own internal _RateLimiter (see aarva/clients/llm.py),
    # not by anything this stage manages itself.
    max_workers = 8
    stats_lock = threading.Lock()

    def _classify_one(article: dict) -> None:
        article_id = article["id"]
        try:
            code = classify_author_provenance(article, llm)
            with db.connect() as conn:
                conn.execute(
                    "UPDATE articles SET author_country_code = ? WHERE id = ?",
                    (code, article_id),
                )
            with stats_lock:
                stats.classified += 1
                if code == "us":
                    stats.us += 1
                elif code == "uk":
                    stats.uk += 1
                elif code == "india":
                    stats.india += 1
                else:
                    stats.unknown += 1
                if stats.classified % 100 == 0:
                    logger.info(
                        "Stage 8c: %d/%d classified",
                        stats.classified, stats.candidates,
                    )
        except Exception as e:
            with stats_lock:
                stats.errors += 1
            logger.warning(
                "Author-provenance classification failed for article %d: %s",
                article_id, e,
            )

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers, thread_name_prefix="stage8c",
    ) as ex:
        list(ex.map(_classify_one, candidates))

    logger.info(
        "Stage 8c done — %d classified (us=%d uk=%d india=%d unknown=%d), "
        "%d errors",
        stats.classified, stats.us, stats.uk, stats.india, stats.unknown,
        stats.errors,
    )
    return stats

"""Strip terminal boilerplate from article full_text before it's persisted.

See docs/session_plan_tts_boilerplate_strip.md. Publication boilerplate
appended to article tails — production credits, crisis-line footers,
author bios, subscription CTAs — is useless in audio (a listener can't
dial a hotline while listening) and, worse, deterministically trips
Gemini TTS's safety filter (returns HTTP 200 with candidates=None,
which the TTS client previously mishandled as a generic retryable
failure — see aarva/clients/tts.py's _NonRetryableTTSError).

Explicit user decision (2026-07-22): don't preserve stripped text
anywhere in Aarva. Listeners who want crisis-line info or production
credits click through to the source article.

Detection walks backward from the LAST paragraph and stops at the
first paragraph that doesn't match a boilerplate pattern — this
protects mid-article editorial prose that happens to mention, say,
"988" in a topical (not boilerplate-shaped) way from ever being
touched, since stripping only ever eats from the tail inward.
"""
from __future__ import annotations

import re

# Corrections/editor's notes carry real editorial information — never
# strip these even though they sometimes sit at the tail too.
_NEVER_STRIP_RE = re.compile(
    r"^\s*(correction:|editor.?s note:|updated on\b)", re.IGNORECASE,
)

# Paragraphs starting with a production-credit line.
_PRODUCTION_CREDIT_RE = re.compile(
    r"^\s*("
    r"design(?: and development)? by|"
    r"illustrations? by|"
    r"photography by|photos? by|"
    r"videos? by|"
    r"visual editing by|"
    r"additional reporting by|"
    r"copy edit(?:ed|ing) by|"
    r"fact-checked by|"
    r"edited by\s+[A-Z]"
    r")",
    re.IGNORECASE,
)

# Crisis-line / helpline footers. Matched as specific multi-word phrases
# (not a bare "988") so an article whose TOPIC is the 988 hotline itself
# doesn't get its editorial prose mistaken for the standard footer shape.
_CRISIS_LINE_RE = re.compile(
    r"("
    r"988 suicide(?: ?& ?crisis lifeline)?|"
    r"if you or someone you know|"
    r"national suicide prevention lifeline|"
    r"crisis text line|"
    r"samaritans\b.{0,40}\d{3}[\s.-]?\d{3,4}|"
    r"\brainn\b"
    r")",
    re.IGNORECASE,
)

# Author bios: "<Name>[, titles/credentials,] is a/an/the author of ..."
# or "<Name> writes about ... for <publication>". Credentials segments
# vary too much in punctuation (parens, degree abbreviations with their
# own periods, etc.) to parse precisely, so this is deliberately loose:
# paragraph starts with a capitalized name-shaped token AND the bio verb
# phrase appears within the first 120 chars. Regex-only per the spec —
# known to be fuzzy (e.g. "<Name> is a journalist who..." describing a
# PROFILE SUBJECT, not the author, could false-positive if it's also the
# terminal paragraph). Add an LLM-assist fallback if that proves to
# matter in practice.
_BIO_NAME_START_RE = re.compile(r"^\s*[A-Z][\w.'-]+(?:\s+[A-Z][\w.'-]+)?")
_BIO_VERB_RE = re.compile(
    r"\bis (?:a|an|the author of)\b|\bwrites about\b", re.IGNORECASE,
)


def _looks_like_bio(text: str) -> bool:
    if not _BIO_NAME_START_RE.match(text):
        return False
    return bool(_BIO_VERB_RE.search(text[:120]))


# Subscription / newsletter CTAs — typically their own short paragraph.
_SUBSCRIPTION_CTA_RE = re.compile(
    r"^\s*("
    r"sign up for our newsletter|"
    r"subscribe to\b|"
    r"support our journalism|"
    r"read more of our coverage|"
    r"join our commenting forum|"
    r"join thought-provoking conversations|"
    r"join the conversation\b"
    r")",
    re.IGNORECASE,
)

_STRIPPABLE_PATTERNS = (
    ("credits", _PRODUCTION_CREDIT_RE),
    ("crisis-line", _CRISIS_LINE_RE),
    ("cta", _SUBSCRIPTION_CTA_RE),
)


def _classify_paragraph(paragraph: str) -> str | None:
    """Return a short label if `paragraph` matches a boilerplate shape,
    None if it doesn't — None means "stop, this is real content"."""
    text = paragraph.strip()
    if not text:
        return None
    if _NEVER_STRIP_RE.search(text):
        return None
    for label, pattern in _STRIPPABLE_PATTERNS:
        if pattern.search(text):
            return label
    if _looks_like_bio(text):
        return "bio"
    return None


def strip_terminal_boilerplate(full_text: str) -> tuple[str, list[tuple[str, str]]]:
    """Strip boilerplate paragraphs from the END of `full_text`.

    Paragraphs are `full_text.split("\\n")` (trafilatura's txt output
    delimiter — confirmed empirically, no blank-line paragraphs in
    practice). Walks backward from the last paragraph; a blank tail
    paragraph is skipped without counting as a stop signal, but the
    first genuinely non-matching paragraph stops the walk — nothing
    above it is ever touched.

    Returns (cleaned_text, stripped) where `stripped` is
    [(label, paragraph_preview), ...] in original top-to-bottom order,
    for the Stage 1 INFO-level audit log. (cleaned_text, []) if nothing
    was stripped — including when `full_text` is empty/None.
    """
    if not full_text:
        return full_text, []

    paragraphs = full_text.split("\n")
    stripped: list[tuple[str, str]] = []

    end = len(paragraphs)
    while end > 0:
        candidate = paragraphs[end - 1]
        if not candidate.strip():
            end -= 1
            continue
        label = _classify_paragraph(candidate)
        if label is None:
            break
        stripped.append((label, candidate.strip()[:80]))
        end -= 1

    if not stripped:
        return full_text, []

    cleaned = "\n".join(paragraphs[:end]).rstrip()
    return cleaned, list(reversed(stripped))

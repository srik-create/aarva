"""Rejection reason codes for the reviewer feedback learning loop.

See docs/session_plan_reviewer_learning_loop.md Phase 1. Reason list
is data, not code — add a new (code, label) tuple here plus a CLI
menu update to add a reason; no DB enum, edition_rejections.reason is
a plain TEXT column with app-level validation.
"""
from __future__ import annotations

REJECTION_REASONS: list[tuple[str, str]] = [
    ("too_long", "Too long"),
    ("too_short", "Too short"),
    ("wrong_tone", "Wrong tone"),
    ("transcript", "Transcript of an audio/video interview"),
    ("video_dependent", "Meaning depends on embedded video we can't narrate"),
    ("listicle", "Listicle — numbered list, no essayistic argument"),
    ("other", "Other (free text)"),
]

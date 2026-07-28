"""Tests for aarva.services.terminal_boilerplate.

First test file in aarva/tests/ — see docs/session_plan_independent_
commenting_forum_strip.md for the incident (The Independent's "Join
our commenting forum" CTA tripped Gemini TTS's PROHIBITED_CONTENT
filter) that prompted both the new _SUBSCRIPTION_CTA_RE alternatives
and this test file.
"""
from __future__ import annotations

from aarva.services.terminal_boilerplate import (
    _classify_paragraph,
    strip_terminal_boilerplate,
)

# Exact CTA text from the 2026-07-28 incident (The Independent, article
# 10317) — see aarva/clients/tts.py's safety-block log format
# ("Chunk starts with: {text[:120]!r}") for how this was surfaced.
INDEPENDENT_CTA = (
    "Join our commenting forum\n"
    "Join thought-provoking conversations, follow other Independent "
    "readers and see their replies"
)


class TestClassifyParagraph:
    """Unit-level: _classify_paragraph on individual paragraphs."""

    def test_independent_cta_classified_as_cta(self):
        assert _classify_paragraph(INDEPENDENT_CTA) == "cta"

    def test_independent_cta_second_sentence_alone(self):
        # If a paragraph split lands mid-way through the CTA block, the
        # second sentence alone must still classify as cta.
        assert _classify_paragraph(
            "Join thought-provoking conversations, follow other "
            "Independent readers and see their replies"
        ) == "cta"

    def test_join_our_commenting_forum_mid_sentence_not_stripped(self):
        # The ^\s* anchor means the phrase only triggers as an opener,
        # not when embedded mid-sentence.
        assert _classify_paragraph(
            "For more discussion, join our commenting forum today."
        ) is None

    def test_join_the_conversation_mid_body_editorial_prose_not_stripped(self):
        assert _classify_paragraph(
            "Historians have long debated how ordinary citizens join "
            "the conversation about political reform."
        ) is None

    def test_join_the_conversation_as_terminal_cta_stripped(self):
        assert _classify_paragraph("Join the conversation: what do you think?") == "cta"

    def test_join_the_conversation_word_boundary_respected(self):
        # "join the conversational" shouldn't match \b after "conversation".
        assert _classify_paragraph("Join the conversational shift in tone.") is None

    def test_corrections_paragraph_never_stripped(self):
        assert _classify_paragraph(
            "Correction: an earlier version of this article misstated "
            "the year the policy took effect."
        ) is None

    def test_editors_note_never_stripped(self):
        assert _classify_paragraph(
            "Editor's note: this piece has been updated to reflect new information."
        ) is None

    # Regression — the four pre-existing CTA alternatives.
    def test_regression_newsletter_signup(self):
        assert _classify_paragraph("Sign up for our newsletter today.") == "cta"

    def test_regression_subscribe_to(self):
        assert _classify_paragraph("Subscribe to our daily briefing.") == "cta"

    def test_regression_support_our_journalism(self):
        assert _classify_paragraph("Support our journalism by becoming a member.") == "cta"

    def test_regression_read_more_of_our_coverage(self):
        assert _classify_paragraph("Read more of our coverage on this topic.") == "cta"


class TestStripTerminalBoilerplate:
    """Integration-level: strip_terminal_boilerplate on full multi-
    paragraph article text, exercising the terminal-only backward walk."""

    def test_independent_cta_stripped_from_article_tail(self):
        full_text = (
            "The government announced new measures today.\n"
            "Officials say the changes will take effect next month.\n"
            + INDEPENDENT_CTA
        )
        cleaned, stripped = strip_terminal_boilerplate(full_text)
        assert "Join our commenting forum" not in cleaned
        assert "join our commenting forum" not in cleaned.lower()
        assert any(label == "cta" for label, _ in stripped)
        # Editorial content above the CTA must survive untouched.
        assert "The government announced new measures today." in cleaned
        assert "Officials say the changes will take effect next month." in cleaned

    def test_join_the_conversation_mid_body_survives_because_not_terminal(self):
        # Editorial prose using this phrase mid-article (not at the
        # tail) must never be touched — the walk only ever eats from
        # the end inward, and even if it were at the tail, "join the
        # conversation" appearing mid-sentence doesn't match the ^\s*
        # anchor either.
        full_text = (
            "Historians have long debated how ordinary citizens join "
            "the conversation about political reform.\n"
            "This piece explores three case studies in depth."
        )
        cleaned, stripped = strip_terminal_boilerplate(full_text)
        assert cleaned == full_text
        assert stripped == []

    def test_regression_existing_four_ctas_still_strip(self):
        for cta_text in (
            "Sign up for our newsletter today.",
            "Subscribe to our daily briefing.",
            "Support our journalism by becoming a member.",
            "Read more of our coverage on this topic.",
        ):
            full_text = "Real editorial content goes here.\n" + cta_text
            cleaned, stripped = strip_terminal_boilerplate(full_text)
            assert cleaned == "Real editorial content goes here."
            assert stripped == [("cta", cta_text)]

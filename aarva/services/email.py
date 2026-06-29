"""Thin email-sending wrapper.

Two backends today:

  - STUB (default in local dev / when RESEND_API_KEY is unset): logs
    the would-send email to stdout. Lets us exercise the full episode-
    creation flow on a laptop without paying for / configuring email.
    The status page is the listener's real notification surface in
    this mode.

  - Resend (set RESEND_API_KEY env var to enable): production path.
    Single API call, single env var, free tier up to ~3k emails/month.

Backend choice is driven by env, not config, so a Render deploy with
the env var set "flips" automatically — no code change.

Listener-facing copy lives at the call site (so the same wrapper can
serve future emails like "your daily edition is ready" without
duplicating Aarva's voice rules here). This file is plumbing only.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class EmailResult:
    """Return value from `send_email`. `provider` records which backend
    handled the call so logs are unambiguous; `sent` is the truthy
    flag callers test."""
    sent: bool
    provider: str          # 'stub' | 'resend'
    detail: str = ""       # provider message-id or stub note


def _default_from() -> str:
    """The From address. Override via AARVA_EMAIL_FROM; otherwise we
    use the listener-friendly default rooted at the apex domain."""
    return os.environ.get("AARVA_EMAIL_FROM", "Aarva <hello@aarva.app>")


def send_email(
    *,
    to: str,
    subject: str,
    html: str,
    text: str,
    from_addr: Optional[str] = None,
) -> EmailResult:
    """Send a transactional email. Picks the backend by env.

    `to`       — recipient (single string for now; expand to list later).
    `subject`  — short subject line.
    `html`     — HTML body. Should also contain a plain-text fallback
                 (`text`) for clients that strip HTML.
    `text`     — plain-text body. Required.
    `from_addr` — defaults to AARVA_EMAIL_FROM env var or Aarva default."""
    sender = from_addr or _default_from()
    api_key = os.environ.get("RESEND_API_KEY", "").strip()

    if not api_key:
        # Stub backend: print a clear, grep-friendly block so the
        # operator can see exactly what would have been sent. Useful
        # in dev / local testing of the full flow.
        logger.warning(
            "[email-stub] RESEND_API_KEY unset — not actually sending.\n"
            "    From:    %s\n"
            "    To:      %s\n"
            "    Subject: %s\n"
            "    --- text body ---\n%s\n    --- end ---",
            sender, to, subject, text,
        )
        return EmailResult(sent=False, provider="stub",
                           detail="RESEND_API_KEY unset")

    # Resend backend — kept guarded inside an import so installations
    # without the `resend` package don't pay for it.
    try:
        import resend  # type: ignore
    except ImportError as e:
        logger.error(
            "send_email: RESEND_API_KEY is set but `resend` package "
            "isn't installed (%s). Add `resend` to requirements.txt.",
            e,
        )
        return EmailResult(sent=False, provider="resend",
                           detail="resend package missing")

    resend.api_key = api_key
    try:
        response = resend.Emails.send({
            "from": sender,
            "to": [to],
            "subject": subject,
            "html": html,
            "text": text,
        })
        msg_id = (response or {}).get("id", "")
        logger.info("send_email: Resend ok to=%s id=%s", to, msg_id)
        return EmailResult(sent=True, provider="resend", detail=msg_id)
    except Exception as e:
        logger.exception("send_email: Resend call failed: %s", e)
        return EmailResult(sent=False, provider="resend", detail=str(e)[:200])

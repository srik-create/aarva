"""LLM-client abstraction.

Two production paths, both available via the same interface:

  ClaudeCodeClient — uses the user's Claude subscription via the
    `claude -p "<prompt>"` CLI subprocess. No API tokens billed; runs
    against the subscription tier. Higher latency, lower parallelism, but
    a fixed monthly cost.

  AnthropicAPIClient — direct API calls. Pay per token. Higher
    parallelism, lower latency. Used when subscription tier hits limits or
    when running calibration / backfills.

Stage prompts are in aarva/config/prompts.yaml. Each call:
  1. Looks up prompt by version
  2. Substitutes the article body (and any other context)
  3. Sends to the configured backend
  4. Parses JSON response (with retry on parse failure)
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class LLMResponseParseError(Exception):
    """Raised when the LLM's response isn't valid JSON (after one retry)."""


class LLMClient(ABC):
    @abstractmethod
    def complete(
        self,
        prompt: str,
        *,
        expect_json: bool = True,
        temperature: float | None = None,
        timeout: int = 120,
    ) -> str | dict:
        """Send prompt to the LLM. If expect_json=True, returns a parsed dict.
        Otherwise returns raw text.
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        ...


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def extract_json(text: str) -> dict:
    """Best-effort JSON extraction from an LLM response.

    Tries, in order:
      1. Parse the whole response as JSON.
      2. Pull out the first ```json ... ``` fenced block.
      3. Pull out the largest {...} block.

    Raises LLMResponseParseError if all three fail.
    """
    # 1. Whole-response parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Fenced block
    match = _JSON_FENCE.search(text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # 3. Largest object
    match = _JSON_OBJECT.search(text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    raise LLMResponseParseError(
        f"Could not parse JSON from LLM response. First 400 chars: {text[:400]!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Claude Code (subprocess) backend
# ─────────────────────────────────────────────────────────────────────────────

class ClaudeCodeClient(LLMClient):
    """Invokes `claude -p` as a subprocess. Uses the user's subscription tier.

    Claude Code uses the same authentication as the desktop CLI — runs the
    `claude` command, captures stdout. No API key needed in the environment;
    the user authenticates Claude Code once via `claude login`.
    """

    def __init__(self, model: str | None = None, retries: int = 1):
        self._model = model    # if set, passed as --model; otherwise default
        self._retries = retries

    def complete(
        self,
        prompt: str,
        *,
        expect_json: bool = True,
        temperature: float | None = None,
        timeout: int = 120,
    ) -> str | dict:
        cmd = ["claude", "-p", prompt]
        if self._model:
            cmd.extend(["--model", self._model])

        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                logger.debug("Running %s (attempt %d/%d)", cmd[:2],
                             attempt + 1, self._retries + 1)
                proc = subprocess.run(
                    cmd,
                    capture_output=True, text=True,
                    timeout=timeout,
                    check=True,
                )
                raw = proc.stdout.strip()
                if not expect_json:
                    return raw
                return extract_json(raw)
            except subprocess.TimeoutExpired as e:
                last_error = e
                logger.warning("Claude Code timed out after %ds (attempt %d)",
                               timeout, attempt + 1)
            except subprocess.CalledProcessError as e:
                last_error = e
                logger.warning("Claude Code returned non-zero (attempt %d): %s",
                               attempt + 1, e.stderr[:300] if e.stderr else "")
            except LLMResponseParseError as e:
                last_error = e
                logger.warning("Claude Code response wasn't JSON (attempt %d)",
                               attempt + 1)

        raise RuntimeError(f"Claude Code failed after retries: {last_error}")

    @property
    def name(self) -> str:
        return f"claude_code:{self._model or 'default'}"


# ─────────────────────────────────────────────────────────────────────────────
# Anthropic API backend
# ─────────────────────────────────────────────────────────────────────────────

class AnthropicAPIClient(LLMClient):
    """Direct Anthropic API. Pay-per-token. Requires ANTHROPIC_API_KEY env var."""

    DEFAULT_MODEL = "claude-sonnet-4-6"
    DEFAULT_MAX_TOKENS = 4096

    def __init__(self, model: str | None = None, retries: int = 1):
        self._model = model or self.DEFAULT_MODEL
        self._retries = retries
        self._client = None

    def _load(self) -> None:
        if self._client is not None:
            return
        try:
            from anthropic import Anthropic
        except ImportError as e:
            raise RuntimeError(
                "AnthropicAPIClient requires anthropic. "
                "Install with: pip install anthropic"
            ) from e
        self._client = Anthropic()    # picks up ANTHROPIC_API_KEY

    def complete(
        self,
        prompt: str,
        *,
        expect_json: bool = True,
        temperature: float | None = None,
        timeout: int = 120,
    ) -> str | dict:
        self._load()
        last_error: Exception | None = None

        for attempt in range(self._retries + 1):
            try:
                message = self._client.messages.create(
                    model=self._model,
                    max_tokens=self.DEFAULT_MAX_TOKENS,
                    temperature=temperature if temperature is not None else 0.2,
                    messages=[{"role": "user", "content": prompt}],
                    timeout=timeout,
                )
                raw = "".join(
                    block.text for block in message.content
                    if hasattr(block, "text")
                ).strip()
                if not expect_json:
                    return raw
                return extract_json(raw)
            except LLMResponseParseError as e:
                last_error = e
                logger.warning("Anthropic API response wasn't JSON (attempt %d)",
                               attempt + 1)
            except Exception as e:
                last_error = e
                logger.warning("Anthropic API call failed (attempt %d): %s",
                               attempt + 1, e)

        raise RuntimeError(f"Anthropic API failed after retries: {last_error}")

    @property
    def name(self) -> str:
        return f"anthropic_api:{self._model}"


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

def build_llm_client(config: dict) -> LLMClient:
    """Build an LLM client from the relevant slice of pipeline.yaml.

    Expected config shape:
        llm:
          provider: claude_code | anthropic_api
          model:    optional model override
          retries:  optional retry count
    """
    provider = (config or {}).get("provider", "claude_code")
    model = (config or {}).get("model")
    retries = int((config or {}).get("retries", 1))

    if provider == "claude_code":
        return ClaudeCodeClient(model=model, retries=retries)
    if provider == "anthropic_api":
        return AnthropicAPIClient(model=model, retries=retries)
    raise ValueError(f"Unknown LLM provider: {provider}")

"""LLM-client abstraction.

Three production paths, all available via the same interface:

  ClaudeCodeClient — uses the user's Claude subscription via the
    `claude -p "<prompt>"` CLI subprocess. No API tokens billed; runs
    against the subscription tier. Higher latency, lower parallelism, but
    a fixed monthly cost.

  AnthropicAPIClient — direct Anthropic API calls. Pay per token. Higher
    parallelism, lower latency. Used when subscription tier hits limits or
    when running calibration / backfills.

  GeminiAPIClient — direct Google AI Studio API calls. Free tier (500-1500
    requests/day on Gemini 2.5 Flash, May 2026) covers Aarva's volume
    comfortably. Requires GEMINI_API_KEY or GOOGLE_API_KEY env var. Get a
    key at https://aistudio.google.com/apikey.

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
import random
import re
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)


from aarva.exceptions import ConfigError, LLMError


class LLMResponseParseError(LLMError):
    """Raised when the LLM's response isn't valid JSON (after one retry).
    Part of the AarvaError hierarchy via LLMError so web routes can
    catch it as a 502 Bad Gateway."""


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
# Gemini API backend
# ─────────────────────────────────────────────────────────────────────────────

class _RateLimiter:
    """Sliding-window rate limiter: at most `max_calls` per `window_seconds`.

    Thread-safe; the lock is needed because the existing pipeline is single-
    threaded today, but Stage 4+5+6 may parallelise later and the limiter
    should still work then.

    The implementation tracks the last `max_calls` timestamps; when a new
    call comes in and that window is full, it sleeps until the oldest
    timestamp ages out.
    """

    def __init__(self, max_calls: int, window_seconds: float):
        self._max_calls = max_calls
        self._window = window_seconds
        self._timestamps: deque[float] = deque(maxlen=max_calls)
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            # Drop timestamps that are outside the window.
            while self._timestamps and (now - self._timestamps[0]) >= self._window:
                self._timestamps.popleft()
            if len(self._timestamps) >= self._max_calls:
                sleep_for = self._window - (now - self._timestamps[0]) + 0.05
                if sleep_for > 0:
                    logger.debug("RateLimiter sleeping %.2fs to stay under "
                                 "%d/%.0fs", sleep_for, self._max_calls, self._window)
                    time.sleep(sleep_for)
                # After sleeping, oldest entry is now out of window.
                now = time.monotonic()
                while self._timestamps and (now - self._timestamps[0]) >= self._window:
                    self._timestamps.popleft()
            self._timestamps.append(time.monotonic())


# Pre-parsed retry delays for known transient Gemini errors. Tweak in one place.
_GEMINI_503_BACKOFF_SECONDS = (5, 15, 30, 60, 120)


def _parse_gemini_retry_delay(exc: Exception) -> float | None:
    """Pull the retryDelay (e.g. '35.131s') out of a Gemini 429 RESOURCE_EXHAUSTED.

    Returns the number of seconds to wait, or None if the field isn't present.
    """
    msg = str(exc)
    m = re.search(r"['\"]retryDelay['\"]\s*:\s*['\"]([\d.]+)s['\"]", msg)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _is_503_or_overload(exc: Exception) -> bool:
    s = str(exc)
    return "503" in s or "UNAVAILABLE" in s or "overloaded" in s.lower()


def _is_429_or_quota(exc: Exception) -> bool:
    s = str(exc)
    return "429" in s or "RESOURCE_EXHAUSTED" in s or "exceeded your current quota" in s


def _is_permanent_client_error(exc: Exception) -> bool:
    """Errors that won't recover by retrying inside this run.

    Today we catch:
      - 404 NOT_FOUND — bad model name (burned 6 retries on every call
        when pipeline.yaml pointed at `gemini-3-flash` before we
        discovered the preview SKU is `gemini-3-flash-preview`).
      - 429 RESOURCE_EXHAUSTED with "monthly spending cap" / "billing
        account" in the body — this is the paid-tier monthly spend
        cutoff, not a per-minute rate limit. It won't recover until
        the cap is raised at https://ai.studio/billing or the billing
        cycle resets, so retrying with a 30s sleep is pure waste
        (6 × 30s = 3 min burned per call).
      - 400 INVALID_ARGUMENT could be added here if it shows up.

    Note: regular 429 rate-limit responses (with a retryDelay field)
    are NOT permanent and continue to flow through the 429 sleep
    branch below.
    """
    s = str(exc)
    if "404" in s or "NOT_FOUND" in s:
        return True
    # Spending-cap exhaustion: account-level, not per-minute.
    if "spending cap" in s.lower() or "billing account" in s.lower():
        return True
    return False


class GeminiAPIClient(LLMClient):
    """Google Gemini via google-genai SDK.

    Supports two authentication modes:

      auth_mode='api_key' (legacy / default):
        Uses the AI Studio Gemini API endpoint
        (generativelanguage.googleapis.com). Requires GEMINI_API_KEY or
        GOOGLE_API_KEY (or AARVA_GEMINI_API_KEY) env var. Keys are issued
        from https://aistudio.google.com/apikey.

      auth_mode='adc' (Vertex AI):
        Uses Vertex AI endpoint (aiplatform.googleapis.com) with
        Application Default Credentials picked up from the gcloud setup at
        ~/.config/gcloud/application_default_credentials.json. Requires
        gcp_project (e.g., gen-lang-client-XXXXXXXXX) and gcp_location
        (e.g., 'global', 'us-central1', 'europe-west2'). Model
        availability varies by region — see Vertex AI docs.

    NOTE on subscriptions: a Gemini Advanced consumer subscription ($20/mo)
    does NOT include API access. The API-key path needs a separate AI
    Studio key; the ADC path needs a Google Cloud project with billing
    and the Vertex AI API enabled.

    Throttling: we cap at `rpm` requests per 60 seconds (default 4, one
    below the free tier's 5 RPM ceiling for safety). For paid tier with a
    higher quota, bump this in pipeline.yaml via `llm.rpm: 60` or whatever.
    """

    DEFAULT_MODEL = "gemini-2.5-flash"
    # 16384 gives ample headroom for our largest JSON responses (the
    # Stage 4+5+6 combined score response with all eight rationales can
    # exceed 4096 tokens; truncation there returns partial JSON that
    # silently fails parsing and burns six retries).
    DEFAULT_MAX_TOKENS = 16384
    DEFAULT_RPM = 4              # Free-tier safe default. Bump for paid tier.
    DEFAULT_RETRIES = 5          # 503s come in streaks; 1 retry isn't enough.
    DEFAULT_AUTH_MODE = "api_key"

    def __init__(
        self,
        model: str | None = None,
        retries: int | None = None,
        rpm: int | None = None,
        *,
        auth_mode: str | None = None,
        gcp_project: str | None = None,
        gcp_location: str | None = None,
    ):
        self._model = model or self.DEFAULT_MODEL
        self._retries = retries if retries is not None else self.DEFAULT_RETRIES
        self._client = None
        rpm_effective = rpm or self.DEFAULT_RPM
        self._limiter = _RateLimiter(max_calls=rpm_effective, window_seconds=60.0)
        self._rpm = rpm_effective
        self._auth_mode = (auth_mode or self.DEFAULT_AUTH_MODE).lower()
        self._gcp_project = gcp_project
        self._gcp_location = gcp_location

        if self._auth_mode not in ("api_key", "adc"):
            raise ConfigError(
                f"Unknown llm.auth_mode '{self._auth_mode}'. "
                f"Expected 'api_key' or 'adc'."
            )
        if self._auth_mode == "adc":
            if not self._gcp_project or not self._gcp_location:
                raise ConfigError(
                    "auth_mode='adc' requires both gcp_project and "
                    "gcp_location. Set llm.gcp_project (e.g. "
                    "'gen-lang-client-0889802137') and llm.gcp_location "
                    "(e.g. 'global') in pipeline.yaml."
                )

    def _load(self) -> None:
        if self._client is not None:
            return
        try:
            from google import genai  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "GeminiAPIClient requires google-genai. "
                "Install with: pip install google-genai"
            ) from e

        if self._auth_mode == "adc":
            # ADC path — credentials picked up from
            # ~/.config/gcloud/application_default_credentials.json (set up
            # via `gcloud auth application-default login` or the
            # setup_adc.sh bootstrap script). Quotas and billing are at the
            # Google Cloud project level, not per-key.
            self._client = genai.Client(
                vertexai=True,
                project=self._gcp_project,
                location=self._gcp_location,
            )
            logger.info(
                "Gemini client ready (ADC/Vertex) — model=%s, project=%s, "
                "location=%s, rpm=%d, retries=%d",
                self._model, self._gcp_project, self._gcp_location,
                self._rpm, self._retries,
            )
            return

        # api_key path. Check the Aarva-namespaced env var first, then
        # fall back to the Google-standard names. Cloud deployments
        # typically set AARVA_GEMINI_API_KEY through their secret manager.
        api_key = (
            os.environ.get("AARVA_GEMINI_API_KEY")
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
        )
        if not api_key:
            raise ConfigError(
                "No Gemini API key found. Set AARVA_GEMINI_API_KEY "
                "(or GEMINI_API_KEY / GOOGLE_API_KEY) in the environment. "
                "Get a key from https://aistudio.google.com/apikey, OR "
                "switch to auth_mode='adc' in pipeline.yaml to use "
                "Application Default Credentials."
            )
        self._client = genai.Client(api_key=api_key)
        logger.info("Gemini client ready — model=%s, rpm=%d, retries=%d",
                    self._model, self._rpm, self._retries)

    def complete(
        self,
        prompt: str,
        *,
        expect_json: bool = True,
        temperature: float | None = None,
        timeout: int = 120,
    ) -> str | dict:
        self._load()
        from google.genai import types as genai_types  # type: ignore

        gen_config_kwargs: dict[str, Any] = {
            "max_output_tokens": self.DEFAULT_MAX_TOKENS,
            "temperature": temperature if temperature is not None else 0.2,
        }
        if expect_json:
            # Server-side JSON enforcement. Much more reliable than
            # asking nicely in the prompt.
            gen_config_kwargs["response_mime_type"] = "application/json"
        gen_config = genai_types.GenerateContentConfig(**gen_config_kwargs)

        last_error: Exception | None = None

        for attempt in range(self._retries + 1):
            # Throttle BEFORE every attempt — that way retries also obey
            # the per-minute cap.
            self._limiter.acquire()

            try:
                response = self._client.models.generate_content(
                    model=self._model,
                    contents=prompt,
                    config=gen_config,
                )
                raw = (response.text or "").strip()
                # Detect truncation up-front. When Gemini hits the output
                # token cap it returns partial text with finish_reason
                # MAX_TOKENS — the parse will fail and the retry loop
                # will waste five more calls producing the same partial.
                # Surface this clearly so we can bump DEFAULT_MAX_TOKENS.
                truncated = False
                try:
                    cand = (response.candidates or [None])[0]
                    if cand is not None:
                        fr = getattr(cand, "finish_reason", None)
                        if fr is not None and str(fr).endswith("MAX_TOKENS"):
                            truncated = True
                except Exception:
                    pass
                if truncated:
                    raise LLMResponseParseError(
                        f"Gemini response hit MAX_TOKENS "
                        f"(max_output_tokens={self.DEFAULT_MAX_TOKENS}). "
                        f"Bump DEFAULT_MAX_TOKENS in aarva/clients/llm.py. "
                        f"First 200 chars: {raw[:200]!r}"
                    )
                if not expect_json:
                    return raw
                return extract_json(raw)

            except LLMResponseParseError as e:
                last_error = e
                # MAX_TOKENS won't recover by retrying with the same
                # config — bail immediately so the operator sees the
                # actionable error.
                if "MAX_TOKENS" in str(e):
                    logger.error("Gemini: %s", e)
                    raise
                logger.warning("Gemini response wasn't JSON (attempt %d/%d)",
                               attempt + 1, self._retries + 1)
                continue

            except Exception as e:
                last_error = e
                # Permanent errors (bad model name, malformed request)
                # won't recover by retrying — bail immediately so the
                # operator sees the actionable error without burning the
                # retry budget.
                if _is_permanent_client_error(e):
                    logger.error("Gemini permanent error — not retrying: %s", e)
                    raise
                # Decide how long to wait based on error type.
                if _is_429_or_quota(e):
                    delay = _parse_gemini_retry_delay(e) or 30.0
                    # Add ~10% jitter so concurrent clients (if any) don't
                    # all wake at exactly the same time.
                    delay = delay * (1.0 + random.uniform(0, 0.1))
                    logger.warning(
                        "Gemini 429 quota — sleeping %.1fs (attempt %d/%d)",
                        delay, attempt + 1, self._retries + 1,
                    )
                    time.sleep(delay)
                elif _is_503_or_overload(e):
                    # Exponential backoff for server overload.
                    idx = min(attempt, len(_GEMINI_503_BACKOFF_SECONDS) - 1)
                    base = _GEMINI_503_BACKOFF_SECONDS[idx]
                    delay = base * (1.0 + random.uniform(0, 0.3))
                    logger.warning(
                        "Gemini 503 UNAVAILABLE — sleeping %.1fs (attempt %d/%d)",
                        delay, attempt + 1, self._retries + 1,
                    )
                    time.sleep(delay)
                else:
                    # Unknown error class — short fixed backoff, then retry.
                    logger.warning(
                        "Gemini API call failed (attempt %d/%d): %s",
                        attempt + 1, self._retries + 1, e,
                    )
                    time.sleep(2.0)

        raise RuntimeError(f"Gemini API failed after {self._retries + 1} attempts: {last_error}")

    @property
    def name(self) -> str:
        if self._auth_mode == "adc":
            return f"gemini_api:{self._model}@vertex:{self._gcp_location}"
        return f"gemini_api:{self._model}"


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

def build_llm_client(config: dict) -> LLMClient:
    """Build an LLM client from the relevant slice of pipeline.yaml.

    Project policy: gemini_api is the production default. claude_code and
    anthropic_api remain available as debug fallbacks but emit a warning
    when used so it's hard to leave the pipeline pointing at the wrong
    backend by accident.

    Expected config shape:
        llm:
          provider: gemini_api | claude_code | anthropic_api
          model:    optional model override
          retries: optional retry count (overrides provider default)
          rpm:     optional requests-per-minute cap (gemini_api only;
                   default 4 for free-tier safety)
          # ─ Vertex AI / ADC auth (gemini_api only) ─
          auth_mode:    'api_key' (default) | 'adc'
          gcp_project:  required when auth_mode='adc'
          gcp_location: required when auth_mode='adc' (e.g. 'global')
    """
    cfg = config or {}
    # Default changed from claude_code → gemini_api per project policy
    # (Gemini for all pipeline LLM work, Claude for coding only).
    provider = cfg.get("provider", "gemini_api")
    model = cfg.get("model")
    retries_raw = cfg.get("retries")
    retries = int(retries_raw) if retries_raw is not None else None
    rpm_raw = cfg.get("rpm")
    rpm = int(rpm_raw) if rpm_raw is not None else None
    auth_mode = cfg.get("auth_mode")
    gcp_project = cfg.get("gcp_project")
    gcp_location = cfg.get("gcp_location")

    if provider == "gemini_api":
        return GeminiAPIClient(
            model=model,
            retries=retries,
            rpm=rpm,
            auth_mode=auth_mode,
            gcp_project=gcp_project,
            gcp_location=gcp_location,
        )
    if provider == "claude_code":
        logger.warning(
            "LLM provider is set to claude_code. Project policy is "
            "gemini_api for all pipeline work — only use claude_code for "
            "short-lived debugging. To return to production, set "
            "llm.provider: gemini_api in aarva/config/pipeline.yaml."
        )
        return ClaudeCodeClient(model=model, retries=retries if retries is not None else 1)
    if provider == "anthropic_api":
        logger.warning(
            "LLM provider is set to anthropic_api. Project policy is "
            "gemini_api for all pipeline work — only use anthropic_api "
            "for short-lived debugging. To return to production, set "
            "llm.provider: gemini_api in aarva/config/pipeline.yaml."
        )
        return AnthropicAPIClient(model=model, retries=retries if retries is not None else 1)
    raise ValueError(f"Unknown LLM provider: {provider}")

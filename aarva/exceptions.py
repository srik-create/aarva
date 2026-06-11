"""Aarva exception hierarchy.

Web routes can catch `AarvaError` and map subclasses to HTTP status
codes. Stages and services SHOULD raise these (or wrap low-level
exceptions in them) at their public boundaries — never let a raw
`sqlite3.Error`, `httpx.HTTPError`, or `google.genai`-specific error
escape into a route handler.

Hierarchy:

    AarvaError                       — base; catch this in routes
    ├── ConfigError                  — config file missing, invalid, or required
    │                                  env var unset
    ├── DatabaseError                — connection / query failure
    ├── ExternalServiceError         — generic external-service failure
    │   ├── LLMError                 — Gemini / Claude API failure or parse failure
    │   ├── TTSError                 — Gemini TTS API failure or silence retry
    │   │                              exhaustion
    │   └── EmbeddingError           — sentence-transformers or OpenAI embed
    │                                  failure
    ├── PipelineError                — a stage failed during its run
    └── NotFoundError                — requested article / edition / user
                                       doesn't exist

Suggested HTTP mappings (for the future web routes):

    ConfigError                      500   (operator error, not user fault)
    DatabaseError                    503   (transient — retry suggested)
    LLMError / TTSError / Embedding  502   (upstream failure)
    PipelineError                    500   (server fault; surface log id)
    NotFoundError                    404
    other AarvaError                 500
"""
from __future__ import annotations


class AarvaError(Exception):
    """Base class for all Aarva-raised exceptions."""


class ConfigError(AarvaError):
    """Configuration is missing, invalid, or a required env var is unset."""


class DatabaseError(AarvaError):
    """Database connection or query failure."""


class ExternalServiceError(AarvaError):
    """Generic upstream-service failure. Subclassed by LLM/TTS/Embedding."""


class LLMError(ExternalServiceError):
    """LLM call failed (network, quota, parse, etc.)."""


class TTSError(ExternalServiceError):
    """TTS call failed or audio quality check rejected the output."""


class EmbeddingError(ExternalServiceError):
    """Embedding model load or call failed."""


class PipelineError(AarvaError):
    """A pipeline stage failed during its run. Wraps the underlying
    stage exception. The web app can surface a log id to the user."""


class NotFoundError(AarvaError):
    """A requested resource (article, edition, user) doesn't exist."""

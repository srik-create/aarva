"""Tests for GeminiTTSClient's request timeout.

Real production incident (2026-09-26): a chunk synthesis call hung
indefinitely — no timeout was ever set on the underlying genai.Client,
so a stalled connection (server already closed its side, per a
CLOSE_WAIT socket observed on the live process) blocked forever
instead of raising and letting the existing retry-with-backoff logic
in _synthesize_chunk actually run. Covers: the config default flows
through build_tts_client, and the timeout (in milliseconds, per
google-genai's HttpOptions contract) is actually passed to
genai.Client — no real network calls, genai.Client itself is mocked.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from aarva.clients.tts import GeminiTTSClient, build_tts_client


class TestRequestTimeoutConfig:
    def test_default_timeout_is_120_seconds(self):
        client = GeminiTTSClient(voice_map={"female": "Sulafat"})
        assert client.request_timeout_seconds == 120

    def test_build_tts_client_reads_config_override(self):
        client = build_tts_client({
            "provider": "gemini",
            "voice_map": {"female": "Sulafat"},
            "request_timeout_seconds": 45,
        })
        assert client.request_timeout_seconds == 45

    def test_build_tts_client_default_when_unset(self):
        client = build_tts_client({
            "provider": "gemini",
            "voice_map": {"female": "Sulafat"},
        })
        assert client.request_timeout_seconds == 120


class TestRequestTimeoutWiredToClient:
    def test_adc_path_passes_http_options_timeout_in_milliseconds(self):
        client = GeminiTTSClient(
            voice_map={"female": "Sulafat"},
            auth_mode="adc",
            gcp_project="test-project",
            gcp_location="global",
            request_timeout_seconds=45,
        )
        with patch("google.genai.Client") as mock_client_cls:
            mock_client_cls.return_value = MagicMock()
            client._load()

        assert mock_client_cls.call_count == 1
        kwargs = mock_client_cls.call_args.kwargs
        assert kwargs["vertexai"] is True
        http_options = kwargs["http_options"]
        assert http_options.timeout == 45_000

    def test_api_key_path_passes_http_options_timeout_in_milliseconds(
        self, monkeypatch,
    ):
        monkeypatch.setenv("AARVA_GEMINI_API_KEY", "test-key")
        client = GeminiTTSClient(
            voice_map={"female": "Sulafat"},
            auth_mode="api_key",
            request_timeout_seconds=45,
        )
        with patch("google.genai.Client") as mock_client_cls:
            mock_client_cls.return_value = MagicMock()
            client._load()

        assert mock_client_cls.call_count == 1
        kwargs = mock_client_cls.call_args.kwargs
        assert kwargs["api_key"] == "test-key"
        http_options = kwargs["http_options"]
        assert http_options.timeout == 45_000

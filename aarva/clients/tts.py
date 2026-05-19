"""TTS-client abstraction.

Provider-agnostic interface. v0.1 ships PiperClient (local, free,
sentence-transformers-class quality). ElevenLabs / OpenAI / F5-TTS
implementations can be added later behind the same TTSClient interface
without touching stage code.
"""
from __future__ import annotations

import logging
import subprocess
import wave
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SynthesisResult:
    output_path: Path
    duration_seconds: float
    voice_id: str
    sample_rate: int


class TTSClient(ABC):
    @abstractmethod
    def synthesize(self, text: str, output_path: Path) -> SynthesisResult:
        ...

    @property
    @abstractmethod
    def voice_id(self) -> str:
        ...


def _wav_duration(path: Path) -> tuple[float, int]:
    """Read a WAV file's header and return (duration_seconds, sample_rate)."""
    with wave.open(str(path), "rb") as w:
        frames = w.getnframes()
        rate = w.getframerate()
        return (frames / float(rate), rate)


# ─────────────────────────────────────────────────────────────────────────────
# Piper backend
# ─────────────────────────────────────────────────────────────────────────────

class PiperClient(TTSClient):
    """Local Piper TTS. Spawns the `piper` binary as a subprocess.

    The binary reads text from stdin and writes a WAV file to --output_file.
    Long articles are sent as one chunk; Piper handles paragraph and
    sentence boundaries internally. If a synthesis run exceeds the timeout,
    we surface the error rather than truncating silently.
    """

    DEFAULT_BINARY = "piper"

    def __init__(
        self,
        model_path: str,
        binary: Optional[str] = None,
        speed: float = 1.0,
        timeout_seconds: int = 600,
    ):
        self.binary = binary or self.DEFAULT_BINARY
        self.model_path = Path(model_path).expanduser()
        self.speed = float(speed)
        self.timeout_seconds = int(timeout_seconds)
        if not self.model_path.exists():
            raise RuntimeError(
                f"Piper voice model not found: {self.model_path}. "
                f"Set tts.model_path in pipeline.yaml or download the model."
            )

    def synthesize(self, text: str, output_path: Path) -> SynthesisResult:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            self.binary,
            "--model", str(self.model_path),
            "--output_file", str(output_path),
        ]
        # Piper's --length_scale is inverse of speed: 0.9 = 1.11× speed.
        if self.speed != 1.0:
            cmd.extend(["--length_scale", str(round(1.0 / self.speed, 3))])

        try:
            subprocess.run(
                cmd,
                input=text, text=True,
                capture_output=True,
                check=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"Piper exceeded {self.timeout_seconds}s timeout for {output_path.name}"
            ) from e
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"Piper failed (returncode={e.returncode}): "
                f"{(e.stderr or '')[:500]}"
            ) from e

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError(f"Piper produced no output for {output_path}")

        duration, sample_rate = _wav_duration(output_path)
        return SynthesisResult(
            output_path=output_path,
            duration_seconds=duration,
            voice_id=self.voice_id,
            sample_rate=sample_rate,
        )

    @property
    def voice_id(self) -> str:
        # Strip the .onnx and produce "en_GB-northern_english_male-medium"
        return self.model_path.stem


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

def build_tts_client(config: dict) -> TTSClient:
    """Build a TTSClient from pipeline.yaml's tts: section.

    Expected shape:
        tts:
          provider: piper
          model_path: ~/.piper-voices/en_GB-northern_english_male-medium.onnx
          binary: piper             # optional override
          speed: 1.0                # optional, 1.0 = default
    """
    provider = (config or {}).get("provider", "piper")
    if provider == "piper":
        model_path = (config or {}).get(
            "model_path",
            "~/.piper-voices/en_GB-northern_english_male-medium.onnx",
        )
        binary = (config or {}).get("binary")
        speed = (config or {}).get("speed", 1.0)
        return PiperClient(model_path=model_path, binary=binary, speed=speed)
    raise ValueError(f"Unknown TTS provider: {provider}")

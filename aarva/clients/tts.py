"""TTS-client abstraction.

Provider-agnostic interface. v0.1 ships:
  - KokoroClient (Aarva's v0.1 default after extensive TTS shopping; local,
    ONNX-based, designed for long-form narration, ~5-10 min per Aarva piece
    on Apple Silicon, no PyTorch dependency required)
  - ChatterboxClient (Resemble AI, local, voice-cloning — higher quality but
    proved to hang on MacBook Air's MPS; kept as a swap-back option for
    beefier hardware)
  - MacSayClient (Apple's `say` command — no-install fallback)
  - PiperClient (kept for testing on non-Mac hosts)

ElevenLabs / OpenAI / F5-TTS implementations slot in behind the same
TTSClient interface without touching stage code.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
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
    def synthesize(
        self,
        text: str,
        output_path: Path,
        voice_id: Optional[str] = None,
    ) -> SynthesisResult:
        """Synthesize text to WAV. Voice_id overrides the client default if set."""
        ...

    @property
    @abstractmethod
    def default_voice_id(self) -> str:
        ...


def _wav_duration(path: Path) -> tuple[float, int]:
    """Read a WAV file's header and return (duration_seconds, sample_rate)."""
    with wave.open(str(path), "rb") as w:
        frames = w.getnframes()
        rate = w.getframerate()
        return (frames / float(rate), rate)


# ─────────────────────────────────────────────────────────────────────────────
# Kokoro backend (lightweight ONNX TTS; v0.1 default after extensive shopping)
# ─────────────────────────────────────────────────────────────────────────────

class KokoroClient(TTSClient):
    """Kokoro TTS via kokoro-onnx.

    Kokoro uses preset voices (not reference-clip cloning). The voice_map
    constructor argument maps Aarva-level voice IDs ('female', 'male') to
    Kokoro voice names ('af_bella', 'bm_daniel', etc.), so Stage 9's voice
    selection logic stays the same as for other providers.

    Kokoro is fast (~5-10 min per ~25K-char article on Apple Silicon) and
    light (~80MB onnxruntime + ~330MB model files). Long articles are still
    chunked at paragraph/sentence boundaries for stable inference and to
    keep memory pressure manageable.

    The Kokoro model is loaded lazily on the first synthesize() call.
    """

    DEFAULT_MAX_CHUNK_CHARS = 1500
    INTER_CHUNK_PAUSE_MS = 250
    DEFAULT_LANG = "en-us"

    def __init__(
        self,
        model_path: str,
        voices_path: str,
        voice_map: dict[str, str],
        default_voice: str = "female",
        speed: float = 1.0,
        lang: str = DEFAULT_LANG,
        max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    ):
        self.model_path = Path(model_path).expanduser()
        self.voices_path = Path(voices_path).expanduser()
        self.voice_map = dict(voice_map)
        self._default = default_voice
        self.speed = float(speed)
        self.lang = lang
        self.max_chunk_chars = int(max_chunk_chars)
        self._model = None

        if not self.model_path.exists():
            raise RuntimeError(
                f"Kokoro model not found at {self.model_path}. "
                f"Download with: curl -L -O https://github.com/thewh1teagle/"
                f"kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx"
            )
        if not self.voices_path.exists():
            raise RuntimeError(
                f"Kokoro voices file not found at {self.voices_path}. "
                f"Download with: curl -L -O https://github.com/thewh1teagle/"
                f"kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"
            )
        if not self.voice_map:
            raise RuntimeError("KokoroClient requires at least one voice mapping.")
        if self._default not in self.voice_map:
            raise RuntimeError(
                f"default_voice '{self._default}' not in voice_map "
                f"({list(self.voice_map)})"
            )

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            from kokoro_onnx import Kokoro
        except ImportError as e:
            raise RuntimeError(
                "KokoroClient requires kokoro-onnx. Install with: "
                "pip install kokoro-onnx soundfile"
            ) from e
        logger.info("Loading Kokoro model from %s (first call only)", self.model_path)
        self._model = Kokoro(str(self.model_path), str(self.voices_path))

    def _chunk_text(self, text: str) -> list[str]:
        """Split text into chunks ≤ max_chunk_chars, on paragraph then sentence boundaries."""
        import re

        chunks: list[str] = []
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        sentence_split = re.compile(r"(?<=[.!?])\s+")

        for para in paragraphs:
            if len(para) <= self.max_chunk_chars:
                chunks.append(para)
                continue
            sentences = sentence_split.split(para)
            current = ""
            for sent in sentences:
                if not current:
                    current = sent
                elif len(current) + 1 + len(sent) <= self.max_chunk_chars:
                    current = f"{current} {sent}"
                else:
                    chunks.append(current)
                    current = sent
            if current:
                if len(current) > self.max_chunk_chars:
                    for i in range(0, len(current), self.max_chunk_chars):
                        chunks.append(current[i:i + self.max_chunk_chars])
                else:
                    chunks.append(current)
        return chunks

    def synthesize(
        self,
        text: str,
        output_path: Path,
        voice_id: Optional[str] = None,
    ) -> SynthesisResult:
        self._load()
        import numpy as np
        import soundfile as sf

        voice_alias = voice_id or self._default
        if voice_alias not in self.voice_map:
            raise ValueError(
                f"Unknown voice_id '{voice_alias}'. Available: {list(self.voice_map)}"
            )
        kokoro_voice = self.voice_map[voice_alias]
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        chunks = self._chunk_text(text)
        if not chunks:
            raise RuntimeError("No text to synthesize after chunking")

        logger.info(
            "Kokoro: synthesizing %d chunks via voice='%s' (kokoro_voice=%s)",
            len(chunks), voice_alias, kokoro_voice,
        )

        segments = []
        sample_rate: int | None = None
        for i, chunk in enumerate(chunks):
            try:
                samples, sr = self._model.create(
                    chunk, voice=kokoro_voice, speed=self.speed, lang=self.lang,
                )
            except Exception as e:
                raise RuntimeError(
                    f"Kokoro synthesis failed on chunk {i+1}/{len(chunks)}: {e}"
                ) from e
            if sample_rate is None:
                sample_rate = int(sr)
            segments.append(samples)
            if i < len(chunks) - 1:
                # Inter-chunk silence for natural pauses.
                pause_samples = int(sample_rate * self.INTER_CHUNK_PAUSE_MS / 1000)
                segments.append(np.zeros(pause_samples, dtype=samples.dtype))

        combined = np.concatenate(segments)
        sf.write(str(output_path), combined, sample_rate)

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError(f"Kokoro produced no output for {output_path}")

        duration, sr_out = _wav_duration(output_path)
        return SynthesisResult(
            output_path=output_path,
            duration_seconds=duration,
            voice_id=voice_alias,
            sample_rate=sr_out,
        )

    @property
    def default_voice_id(self) -> str:
        return self._default


# ─────────────────────────────────────────────────────────────────────────────
# Chatterbox backend (Resemble AI; voice cloning via reference WAVs)
# ─────────────────────────────────────────────────────────────────────────────

class ChatterboxClient(TTSClient):
    """Chatterbox TTS with voice cloning from short reference clips.

    Voice selection: caller passes voice_id like 'female' or 'male'; the
    client looks up that voice_id in voice_references (configured at
    construction time) to find the reference WAV path, then calls
    model.generate(text, audio_prompt_path=<ref>) to synthesize.

    The Chatterbox model is heavy (~1.5GB) and is loaded lazily on the first
    synthesize() call; subsequent calls reuse the loaded model.

    Long articles are chunked at paragraph/sentence boundaries (Chatterbox
    has a soft text-length limit per generation, and chunking also keeps
    memory pressure manageable). Per-chunk waveforms are concatenated with
    short silences between to produce a single output WAV.
    """

    DEFAULT_DEVICE = "mps"   # Apple Silicon; falls back to CPU automatically
    DEFAULT_MAX_CHUNK_CHARS = 600
    INTER_CHUNK_PAUSE_MS = 250

    def __init__(
        self,
        voice_references: dict[str, Path | str],
        default_voice: str = "female",
        device: str = DEFAULT_DEVICE,
        max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
        exaggeration: float = 0.5,
        cfg_weight: float = 0.5,
        timeout_seconds: int = 600,
    ):
        self.voice_references: dict[str, Path] = {
            voice_id: Path(p).expanduser() for voice_id, p in voice_references.items()
        }
        self._default = default_voice
        self.device = device
        self.max_chunk_chars = int(max_chunk_chars)
        self.exaggeration = float(exaggeration)
        self.cfg_weight = float(cfg_weight)
        self.timeout_seconds = int(timeout_seconds)
        self._model = None

        if not self.voice_references:
            raise RuntimeError(
                "ChatterboxClient requires at least one voice reference."
            )
        for voice_id, path in self.voice_references.items():
            if not path.exists():
                raise RuntimeError(
                    f"Chatterbox voice reference '{voice_id}' missing: {path}"
                )
        if self._default not in self.voice_references:
            raise RuntimeError(
                f"default_voice '{self._default}' not in voice_references "
                f"({list(self.voice_references)})"
            )

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            from chatterbox.tts import ChatterboxTTS
            import torch
        except ImportError as e:
            raise RuntimeError(
                "ChatterboxClient requires chatterbox-tts. Install with: "
                "pip install chatterbox-tts"
            ) from e

        device = self.device
        if device == "mps" and not torch.backends.mps.is_available():
            logger.warning("MPS unavailable; ChatterboxClient falling back to CPU")
            device = "cpu"

        logger.info("Loading Chatterbox model on %s (first call only)", device)
        self._model = ChatterboxTTS.from_pretrained(device=device)
        self.device = device

    def _chunk_text(self, text: str) -> list[str]:
        """Split text into chunks <= max_chunk_chars, on paragraph then sentence boundaries."""
        import re

        chunks: list[str] = []
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        sentence_split = re.compile(r"(?<=[.!?])\s+")

        for para in paragraphs:
            if len(para) <= self.max_chunk_chars:
                chunks.append(para)
                continue
            sentences = sentence_split.split(para)
            current = ""
            for sent in sentences:
                if not current:
                    current = sent
                elif len(current) + 1 + len(sent) <= self.max_chunk_chars:
                    current = f"{current} {sent}"
                else:
                    chunks.append(current)
                    current = sent
            if current:
                if len(current) > self.max_chunk_chars:
                    # Single sentence longer than the limit — hard-split
                    for i in range(0, len(current), self.max_chunk_chars):
                        chunks.append(current[i:i + self.max_chunk_chars])
                else:
                    chunks.append(current)
        return chunks

    def synthesize(
        self,
        text: str,
        output_path: Path,
        voice_id: Optional[str] = None,
    ) -> SynthesisResult:
        self._load()
        import torch
        import torchaudio

        voice = voice_id or self._default
        if voice not in self.voice_references:
            raise ValueError(
                f"Unknown voice_id '{voice}'. Available: {list(self.voice_references)}"
            )
        ref_path = self.voice_references[voice]
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        chunks = self._chunk_text(text)
        if not chunks:
            raise RuntimeError("No text to synthesize after chunking")

        logger.info(
            "Chatterbox: synthesizing %d chunks via voice='%s' (ref=%s)",
            len(chunks), voice, ref_path.name,
        )

        segments: list = []
        pause_samples = int(self._model.sr * self.INTER_CHUNK_PAUSE_MS / 1000)
        pause = torch.zeros((1, pause_samples))

        for i, chunk in enumerate(chunks):
            try:
                wav = self._model.generate(
                    chunk,
                    audio_prompt_path=str(ref_path),
                    exaggeration=self.exaggeration,
                    cfg_weight=self.cfg_weight,
                )
            except Exception as e:
                raise RuntimeError(
                    f"Chatterbox generate failed on chunk {i+1}/{len(chunks)}: {e}"
                ) from e
            segments.append(wav)
            if i < len(chunks) - 1:
                segments.append(pause)

        combined = torch.cat(segments, dim=-1)
        torchaudio.save(str(output_path), combined, self._model.sr)

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError(f"Chatterbox produced no output for {output_path}")

        duration, sample_rate = _wav_duration(output_path)
        return SynthesisResult(
            output_path=output_path,
            duration_seconds=duration,
            voice_id=voice,
            sample_rate=sample_rate,
        )

    @property
    def default_voice_id(self) -> str:
        return self._default


# ─────────────────────────────────────────────────────────────────────────────
# macOS `say` command backend
# ─────────────────────────────────────────────────────────────────────────────

class MacSayClient(TTSClient):
    """Apple's built-in `say` command. Free, local, no API key.

    Voice quality depends entirely on which voices are installed. Premium
    voices (downloadable via System Settings → Accessibility → Spoken
    Content → Manage Voices) are meaningfully better than the bundled
    defaults — and meaningfully better than Piper-medium too.

    Aarva v0.1 default: Serena (Premium) as the primary, Jamie (Premium) as
    the alternate. Voice selection per piece happens in Stage 9.
    """

    DEFAULT_VOICE = "Serena (Premium)"
    DEFAULT_RATE_WPM = 175

    def __init__(
        self,
        default_voice: str = DEFAULT_VOICE,
        speed: float = 1.0,
        timeout_seconds: int = 600,
    ):
        self._default = default_voice
        self.speed = float(speed)
        self.timeout_seconds = int(timeout_seconds)

    def synthesize(
        self,
        text: str,
        output_path: Path,
        voice_id: Optional[str] = None,
    ) -> SynthesisResult:
        voice = voice_id or self._default
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Write text to a temp file so we don't have to worry about shell
        # quoting or stdin buffer limits on long articles.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write(text)
            text_file = f.name

        try:
            cmd = [
                "say",
                "-v", voice,
                "-f", text_file,
                "-o", str(output_path),
                "--file-format=WAVE",
                "--data-format=LEI16@22050",
            ]
            if self.speed != 1.0:
                # `say -r` is words per minute (default ~175).
                cmd.extend(["-r", str(int(self.DEFAULT_RATE_WPM * self.speed))])

            try:
                subprocess.run(
                    cmd,
                    capture_output=True, text=True,
                    check=True,
                    timeout=self.timeout_seconds,
                )
            except subprocess.TimeoutExpired as e:
                raise RuntimeError(
                    f"`say` exceeded {self.timeout_seconds}s timeout"
                ) from e
            except subprocess.CalledProcessError as e:
                raise RuntimeError(
                    f"`say` failed (returncode={e.returncode}): "
                    f"{(e.stderr or '')[:500]}"
                ) from e
        finally:
            try:
                os.unlink(text_file)
            except OSError:
                pass

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError(f"`say` produced no output for {output_path}")

        duration, sample_rate = _wav_duration(output_path)
        return SynthesisResult(
            output_path=output_path,
            duration_seconds=duration,
            voice_id=voice,
            sample_rate=sample_rate,
        )

    @property
    def default_voice_id(self) -> str:
        return self._default


# ─────────────────────────────────────────────────────────────────────────────
# Piper backend (kept for swap-back and for testing on non-Mac hosts)
# ─────────────────────────────────────────────────────────────────────────────

class PiperClient(TTSClient):
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

    def synthesize(
        self,
        text: str,
        output_path: Path,
        voice_id: Optional[str] = None,
    ) -> SynthesisResult:
        # Piper's voice is determined by the model file passed at construction
        # time; voice_id is therefore ignored. We accept the parameter for
        # interface compatibility with MacSayClient.
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            self.binary,
            "--model", str(self.model_path),
            "--output_file", str(output_path),
        ]
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
            voice_id=self.default_voice_id,
            sample_rate=sample_rate,
        )

    @property
    def default_voice_id(self) -> str:
        return self.model_path.stem


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

def build_tts_client(config: dict) -> TTSClient:
    """Build a TTSClient from pipeline.yaml's tts: section.

    Expected shapes:

        tts:                                # Chatterbox (v0.1 default)
          provider: chatterbox
          device: mps
          voices:
            female: aarva/voices/female_ref.wav
            male:   aarva/voices/male_ref.wav
          voice_default: female
          voice_alternate: male
          exaggeration: 0.5
          cfg_weight: 0.5

        tts:                                # macOS `say` fallback
          provider: macos_say
          voice_default: Serena (Premium)

        tts:                                # Piper fallback
          provider: piper
          model_path: ~/.piper-voices/en_GB-northern_english_male-medium.onnx
    """
    provider = (config or {}).get("provider", "kokoro")
    speed = float((config or {}).get("speed", 1.0))

    if provider == "kokoro":
        model_path = (config or {}).get(
            "model_path", "aarva/models/kokoro-v1.0.onnx",
        )
        voices_path = (config or {}).get(
            "voices_path", "aarva/models/voices-v1.0.bin",
        )
        voice_map = (config or {}).get("voice_map") or {}
        default = (config or {}).get("voice_default") or "female"
        lang = (config or {}).get("lang", "en-us")
        max_chunk = int((config or {}).get(
            "max_chunk_chars", KokoroClient.DEFAULT_MAX_CHUNK_CHARS,
        ))
        return KokoroClient(
            model_path=model_path,
            voices_path=voices_path,
            voice_map=voice_map,
            default_voice=default,
            speed=speed,
            lang=lang,
            max_chunk_chars=max_chunk,
        )

    if provider == "chatterbox":
        voices_cfg = (config or {}).get("voices") or {}
        # Accept either {"female": "path"} or {"female": {"reference": "path", ...}}
        voice_references: dict[str, str] = {}
        for voice_id, v in voices_cfg.items():
            if isinstance(v, dict):
                voice_references[voice_id] = v.get("reference") or v.get("path")
            else:
                voice_references[voice_id] = v
        default = (config or {}).get("voice_default") or "female"
        device = (config or {}).get("device") or ChatterboxClient.DEFAULT_DEVICE
        max_chunk = int((config or {}).get("max_chunk_chars",
                                            ChatterboxClient.DEFAULT_MAX_CHUNK_CHARS))
        exaggeration = float((config or {}).get("exaggeration", 0.5))
        cfg_weight = float((config or {}).get("cfg_weight", 0.5))
        return ChatterboxClient(
            voice_references=voice_references,
            default_voice=default,
            device=device,
            max_chunk_chars=max_chunk,
            exaggeration=exaggeration,
            cfg_weight=cfg_weight,
        )

    if provider == "macos_say":
        default_voice = (config or {}).get("voice_default") or MacSayClient.DEFAULT_VOICE
        return MacSayClient(default_voice=default_voice, speed=speed)

    if provider == "piper":
        model_path = (config or {}).get(
            "model_path",
            "~/.piper-voices/en_GB-northern_english_male-medium.onnx",
        )
        binary = (config or {}).get("binary")
        return PiperClient(model_path=model_path, binary=binary, speed=speed)

    raise ValueError(f"Unknown TTS provider: {provider}")

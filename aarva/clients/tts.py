"""TTS-client abstraction.

Provider-agnostic interface. Current production path:
  - GeminiTTSClient (★ default; same voices as NotebookLM Audio Overviews,
    via Google's google-genai SDK + GEMINI_API_KEY)

Fallback / legacy backends kept available behind the same interface:
  - ChatterboxClient (Resemble AI local voice-cloning; works on beefier
    hardware but hangs on the MacBook Air MPS path)
  - MacSayClient (Apple's `say` command — no-install fallback)
  - PiperClient (alternative local TTS for non-Mac hosts)

Kokoro was the v0.1 default but was removed once Gemini TTS landed —
quality was no longer competitive. The class lives on in git history if
ever needed.

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
        extra_style: Optional[str] = None,
    ) -> SynthesisResult:
        """Synthesize text to WAV. Voice_id overrides the client default if set.

        extra_style is an optional per-call style direction (e.g. an accent
        steer like "Spoken with an Indian English accent.") that backends
        MAY honour by appending it to their internal style prompt.
        Backends that don't support styling silently ignore it."""
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


def _has_long_silence(
    pcm: bytes,
    sample_rate: int,
    sample_width: int,
    threshold_amplitude: int,
    max_silence_seconds: float,
) -> bool:
    """Scan PCM for any continuous run of near-zero samples longer than
    `max_silence_seconds`. Used as a defensive sanity check after each
    Gemini TTS chunk — Gemini occasionally produces stretches of
    unprompted silence on longer outputs (Google's docs warn about
    quality drift past a few minutes).

    Implementation: we unpack the PCM in 64KB blocks and walk samples,
    tracking the current run of "silent" (|sample| < threshold) samples.
    If the run ever exceeds the per-second sample count × seconds
    threshold, return True immediately.

    Performance: O(n) over the PCM bytes; ~10ms for a 3-minute chunk on
    Apple Silicon. Negligible vs the API call latency.
    """
    import struct

    if not pcm:
        return False
    fmt_char = "h" if sample_width == 2 else "b"
    max_silent_samples = int(sample_rate * max_silence_seconds)
    bytes_per_block = 65536
    # Ensure block size is a multiple of sample_width.
    bytes_per_block -= bytes_per_block % sample_width
    run = 0
    for start in range(0, len(pcm), bytes_per_block):
        block = pcm[start:start + bytes_per_block]
        # Trim trailing partial sample if any (shouldn't happen with our
        # aligned blocks, but safeguard against truncated PCM).
        block = block[:len(block) - (len(block) % sample_width)]
        n_samples = len(block) // sample_width
        if n_samples == 0:
            continue
        samples = struct.unpack(f"<{n_samples}{fmt_char}", block)
        for s in samples:
            if abs(s) < threshold_amplitude:
                run += 1
                if run >= max_silent_samples:
                    return True
            else:
                run = 0
    return False



# ─────────────────────────────────────────────────────────────────────────────
# Gemini TTS backend (Google native audio — same voices as NotebookLM)
# ─────────────────────────────────────────────────────────────────────────────

class GeminiTTSClient(TTSClient):
    """Gemini TTS via the google-genai SDK.

    Same underlying voice models that power NotebookLM's Audio Overviews,
    exposed through the Gemini API. The 30 prebuilt voices each carry a
    named character (Sulafat=Warm, Charon=Informative, etc.); we map our
    abstract Aarva-level voice IDs ('female', 'male', etc.) to Gemini's
    voice names so Stage 9's selection logic is unchanged.

    Cost (as of May 2026): ~$0.50/M input text tokens + $10/M output audio
    tokens on Gemini 2.5/3.1 Flash TTS. Roughly $0.50-1/day for Aarva's
    ~60 min/day volume.

    Caveats from Google's own docs:
      - Quality drift on outputs longer than a few minutes. We chunk per
        the kickoff's standard pattern (paragraph/sentence boundaries,
        ≤max_chunk_chars per chunk) to keep each request short.
      - Occasional 500 errors that need retry. We do up to N retries with
        exponential backoff.
      - Models are currently in Preview status — may change. Project policy
        is to track changes via Google's docs and update model name as
        needed.

    The Gemini API returns raw 24kHz 16-bit mono PCM in the response. We
    stitch chunks with a short silence between them and write a single WAV
    file via the standard `wave` module — no extra audio library required.
    """

    # Gemini's audio output spec is fixed.
    GEMINI_SAMPLE_RATE = 24000        # 24 kHz mono PCM (per Google docs)
    GEMINI_SAMPLE_WIDTH = 2           # 16-bit
    GEMINI_CHANNELS = 1               # mono

    # Switched 2026-06-13 from gemini-2.5-flash-preview-tts →
    # gemini-3.1-flash-tts-preview as part of the Vertex AI / ADC
    # migration: the 2.5 TTS models persistently return 500 INTERNAL on
    # the Vertex 'global' endpoint, while 3.1 TTS works cleanly and
    # supports all six voices in Aarva's voice_map (Sulafat, Gacrux,
    # Vindemiatrix, Charon, Algieba, Rasalgethi — verified empirically
    # via probe_vertex_tts_voices.py).
    DEFAULT_MODEL = "gemini-3.1-flash-tts-preview"
    DEFAULT_AUTH_MODE = "api_key"
    # Bumped from 1500 → 2500 (~1.5 min audio per chunk) after observing
    # noticeable voice variation between chunks. Fewer chunks → fewer
    # transitions where the listener notices Gemini's per-request tone
    # drift. The chunker now *never* splits mid-sentence, so an
    # individual long sentence may exceed this cap.
    DEFAULT_MAX_CHUNK_CHARS = 2500
    INTER_CHUNK_PAUSE_MS = 250
    DEFAULT_RETRIES = 4               # 500s do happen; Google's docs warn

    # Defensive silence detection. Google's docs warn that Gemini TTS can
    # drift on longer outputs — in practice we've observed chunks where
    # the model goes unprompted-silent for multi-second stretches mid-
    # narration. After each chunk synthesis we scan the PCM for any
    # continuous run of near-zero samples longer than this threshold;
    # if found, the chunk is treated as a failed synthesis and retried.
    # 4 seconds catches the failure mode without false-positiving on
    # natural pauses (paragraph breaks, sentence ends, em-dash beats).
    MAX_SILENCE_SECONDS = 4.0
    SILENCE_AMPLITUDE_THRESHOLD = 200    # 16-bit samples; absolute value below = silent

    # Curated voice catalog from the 30 prebuilt voices, selected for
    # Aarva's editorial register. The full list is at
    # https://ai.google.dev/gemini-api/docs/speech-generation
    # Audition at https://aistudio.google.com/generate-speech.
    KNOWN_GEMINI_VOICES = {
        # Warm / steady — good for default narrator
        "Sulafat":       "warm",
        "Algieba":       "smooth",
        "Despina":       "smooth",
        "Vindemiatrix":  "gentle",
        # Editorial / informative
        "Charon":        "informative",
        "Rasalgethi":    "informative",
        "Gacrux":        "mature",
        # Bright / forward
        "Kore":          "firm",
        "Zephyr":        "bright",
        "Autonoe":       "bright",
        # Lighter / playful (for smart-escape pieces)
        "Leda":          "youthful",
        "Aoede":         "breezy",
        "Achird":        "friendly",
    }

    def __init__(
        self,
        voice_map: dict[str, str],
        default_voice: str = "female",
        model: str | None = None,
        max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
        retries: int = DEFAULT_RETRIES,
        style_prompt: str | None = None,
        *,
        auth_mode: str | None = None,
        gcp_project: str | None = None,
        gcp_location: str | None = None,
    ):
        self.voice_map = dict(voice_map)
        self._default = default_voice
        self.model = model or self.DEFAULT_MODEL
        self.max_chunk_chars = int(max_chunk_chars)
        self.retries = int(retries)
        # Optional style direction prepended to every chunk. Useful for
        # consistent editorial tone — e.g., "Read in a calm, editorial
        # register, like a thoughtful longform podcast host."
        self.style_prompt = style_prompt
        self._client = None

        # Auth: api_key (legacy) or adc (Vertex AI). Both supported via
        # the same google-genai SDK; only the client constructor differs.
        # Mirrors the dual-mode pattern in GeminiAPIClient.
        self._auth_mode = (auth_mode or self.DEFAULT_AUTH_MODE).lower()
        self._gcp_project = gcp_project
        self._gcp_location = gcp_location

        if self._auth_mode not in ("api_key", "adc"):
            raise RuntimeError(
                f"Unknown tts.auth_mode '{self._auth_mode}'. "
                f"Expected 'api_key' or 'adc'."
            )
        if self._auth_mode == "adc":
            if not self._gcp_project or not self._gcp_location:
                raise RuntimeError(
                    "tts.auth_mode='adc' requires both tts.gcp_project "
                    "and tts.gcp_location in pipeline.yaml."
                )

        if not self.voice_map:
            raise RuntimeError("GeminiTTSClient requires at least one voice mapping.")
        if self._default not in self.voice_map:
            raise RuntimeError(
                f"default_voice '{self._default}' not in voice_map "
                f"({list(self.voice_map)})"
            )
        # voice_map values may be either a single string or a list of strings
        # (a "pool" used by Stage 9's rotation logic). Verify all entries.
        for voice_id, mapped in self.voice_map.items():
            names = mapped if isinstance(mapped, list) else [mapped]
            for gemini_name in names:
                if gemini_name not in self.KNOWN_GEMINI_VOICES:
                    logger.warning(
                        "GeminiTTS: voice '%s' (mapped to '%s') is not in "
                        "the curated catalog. Letting it through — Gemini "
                        "may accept it, but verify at "
                        "https://aistudio.google.com/generate-speech.",
                        voice_id, gemini_name,
                    )

    def _load(self) -> None:
        if self._client is not None:
            return
        try:
            from google import genai  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "GeminiTTSClient requires google-genai. "
                "Install with: pip install google-genai"
            ) from e

        if self._auth_mode == "adc":
            # Vertex AI path — credentials picked up from
            # ~/.config/gcloud/application_default_credentials.json.
            self._client = genai.Client(
                vertexai=True,
                project=self._gcp_project,
                location=self._gcp_location,
            )
            logger.info(
                "GeminiTTS client ready (ADC/Vertex) — model=%s, "
                "project=%s, location=%s, voices=%s",
                self.model, self._gcp_project, self._gcp_location,
                list(self.voice_map.items()),
            )
            return

        # api_key path. Aarva-namespaced env var takes precedence.
        api_key = (
            os.environ.get("AARVA_GEMINI_API_KEY")
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
        )
        if not api_key:
            raise RuntimeError(
                "No Gemini API key found. Set AARVA_GEMINI_API_KEY "
                "(or GEMINI_API_KEY / GOOGLE_API_KEY) — or switch to "
                "tts.auth_mode='adc' in pipeline.yaml to use Vertex AI "
                "credentials."
            )
        self._client = genai.Client(api_key=api_key)
        logger.info("GeminiTTS client ready — model=%s, voices=%s",
                    self.model, list(self.voice_map.items()))

    def _chunk_text(self, text: str) -> list[str]:
        """Split text into chunks targeting ≤ max_chunk_chars each.

        Strict rule: NEVER split mid-sentence. A single sentence longer
        than max_chunk_chars becomes its own oversize chunk rather than
        being chopped at a character offset — mid-sentence breaks produce
        the most noticeable tone discontinuities in Gemini's output.

        The algorithm:
          1. Split text into paragraphs on blank lines.
          2. Within each paragraph, greedily pack whole sentences into
             a chunk until adding the next sentence would exceed the cap.
          3. Emit the chunk; start the next chunk with the sentence that
             didn't fit.
          4. If a single sentence is longer than the cap on its own, it
             gets its own chunk (and the cap is briefly exceeded).
        """
        import re

        chunks: list[str] = []
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        # Match sentence boundaries — period/!?/etc. followed by whitespace.
        # Lookbehind preserves the punctuation with the preceding sentence.
        sentence_split = re.compile(r"(?<=[.!?])\s+")

        # Walk paragraph-by-paragraph, building chunks as we go. We pack
        # whole paragraphs together when they fit, fall back to packing
        # sentences when they don't.
        current = ""
        for para in paragraphs:
            # Case A: the whole paragraph fits into the current chunk
            # alongside whatever's already there.
            joined = (f"{current}\n\n{para}" if current else para)
            if len(joined) <= self.max_chunk_chars:
                current = joined
                continue
            # Flush current before splitting the paragraph at sentence
            # boundaries.
            if current:
                chunks.append(current)
                current = ""
            # Case B: paragraph fits as its own chunk.
            if len(para) <= self.max_chunk_chars:
                current = para
                continue
            # Case C: paragraph is too big; split at sentence boundaries.
            sentences = sentence_split.split(para)
            for sent in sentences:
                if not current:
                    current = sent
                elif len(current) + 1 + len(sent) <= self.max_chunk_chars:
                    current = f"{current} {sent}"
                else:
                    chunks.append(current)
                    current = sent
            # Note: if a single sentence is longer than max_chunk_chars
            # it becomes its own oversize chunk — preferred over an
            # arbitrary mid-sentence break that causes audible tone shifts.

        if current:
            chunks.append(current)
        return chunks

    def _synthesize_chunk(
        self,
        text: str,
        gemini_voice: str,
        extra_style: str | None = None,
    ) -> bytes:
        """One LLM call for one chunk → raw PCM bytes.

        Implements retry-with-backoff for the 500 errors Google's docs warn
        are an expected (low-rate) failure mode. Returns the raw PCM that
        gets written into the chunk-concat buffer.

        extra_style: optional per-piece style direction appended to the
        global style_prompt — used by Stage 9 to add an accent steer
        (e.g. "Spoken with an educated Indian English accent.") for
        pieces from publications that have a country: tag in
        publications.yaml. Applied to every chunk of that piece.
        """
        import random
        import time

        from google.genai import types as genai_types  # type: ignore

        # Compose the leading style instructions. style_prompt is the
        # global "calm editorial register + quality tags" prompt from
        # pipeline.yaml. extra_style is per-piece (accent steer, etc.).
        # Whichever ones are set get joined with blank lines.
        style_parts = []
        if self.style_prompt:
            style_parts.append(self.style_prompt)
        if extra_style:
            style_parts.append(extra_style)
        style = "\n\n".join(style_parts)
        prompt = f"{style}\n\n{text}" if style else text

        config = genai_types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=genai_types.SpeechConfig(
                voice_config=genai_types.VoiceConfig(
                    prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                        voice_name=gemini_voice,
                    )
                )
            ),
        )

        last_error: Exception | None = None
        last_pcm: bytes | None = None    # kept so the last attempt can ship
                                          # what it had even if silence was
                                          # detected — better than nothing.
        for attempt in range(self.retries + 1):
            try:
                response = self._client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=config,
                )
                # The audio data lives at:
                #   response.candidates[0].content.parts[0].inline_data.data
                # Both the docs and the cookbook show this exact path.
                pcm = None
                parts = response.candidates[0].content.parts
                for part in parts:
                    if getattr(part, "inline_data", None) and part.inline_data.data:
                        pcm = part.inline_data.data
                        break

                if pcm is None:
                    # If we got here, the response was structured oddly —
                    # text instead of audio is the documented "rare" failure
                    # mode. Treat as a retryable error.
                    raise RuntimeError(
                        "Gemini TTS returned no audio data "
                        "(possibly the rare text-instead-of-audio failure mode)."
                    )

                # Defensive silence detection — Gemini occasionally drifts
                # into long stretches of unprompted silence inside a
                # chunk. Catch that here and treat as a retryable error.
                if _has_long_silence(
                    pcm,
                    sample_rate=self.GEMINI_SAMPLE_RATE,
                    sample_width=self.GEMINI_SAMPLE_WIDTH,
                    threshold_amplitude=self.SILENCE_AMPLITUDE_THRESHOLD,
                    max_silence_seconds=self.MAX_SILENCE_SECONDS,
                ):
                    last_pcm = pcm
                    if attempt < self.retries:
                        logger.warning(
                            "GeminiTTS chunk contains >%.1fs of unprompted "
                            "silence — discarding and retrying "
                            "(attempt %d/%d)",
                            self.MAX_SILENCE_SECONDS,
                            attempt + 1, self.retries + 1,
                        )
                        # Skip the normal retry-with-backoff (it's a server-
                        # quality issue, not a network one); brief pause and
                        # immediately re-request.
                        time.sleep(1.0)
                        continue
                    # Out of retries — ship the last attempt anyway, so the
                    # rest of the article isn't lost. Better partial than
                    # nothing.
                    logger.error(
                        "GeminiTTS chunk still contained long silence on "
                        "final attempt — shipping it anyway; the listener "
                        "will hear a quiet stretch."
                    )
                    return pcm

                return pcm
            except Exception as e:
                last_error = e
                # Exponential backoff with jitter: 2, 5, 10, 20s.
                backoffs = (2, 5, 10, 20, 40)
                if attempt < self.retries:
                    base = backoffs[min(attempt, len(backoffs) - 1)]
                    delay = base * (1.0 + random.uniform(0, 0.3))
                    logger.warning(
                        "GeminiTTS chunk synth failed (attempt %d/%d): %s — "
                        "sleeping %.1fs",
                        attempt + 1, self.retries + 1, e, delay,
                    )
                    time.sleep(delay)

        raise RuntimeError(
            f"GeminiTTS failed after {self.retries + 1} attempts: {last_error}"
        )

    def synthesize(
        self,
        text: str,
        output_path: Path,
        voice_id: Optional[str] = None,
        extra_style: str | None = None,
    ) -> SynthesisResult:
        """Synthesize text to a single WAV file.

        `voice_id` accepts two forms:
          - An Aarva-level abstract ID ('female' / 'male') that's looked up
            in `voice_map`. If the map value is a list, the first item is
            used (rotation should happen at the caller level).
          - A literal Gemini voice name ('Sulafat', 'Charon', etc.). Used
            when Stage 9 has already planned a specific voice for this
            piece via its own rotation logic.

        `extra_style` (optional) is a per-piece style direction appended
        to the global style_prompt — Stage 9 uses this to attach an
        accent steer per publication country. None means no extra
        steering on top of style_prompt.
        """
        self._load()

        voice_alias = voice_id or self._default

        # Resolve to a concrete Gemini voice name.
        if voice_alias in self.voice_map:
            mapped = self.voice_map[voice_alias]
            gemini_voice = mapped[0] if isinstance(mapped, list) else mapped
        elif voice_alias in self.KNOWN_GEMINI_VOICES:
            # Caller passed a literal voice name (Stage 9 rotation mode).
            gemini_voice = voice_alias
        else:
            # Last-ditch: assume the caller knows the Gemini voice name
            # exists even if we don't have it in our curated catalog
            # (Google may have added more voices since we last updated).
            logger.warning(
                "GeminiTTS voice_id '%s' not in voice_map or curated "
                "catalog. Passing through to Gemini — verify at "
                "https://aistudio.google.com/generate-speech.",
                voice_alias,
            )
            gemini_voice = voice_alias

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        chunks = self._chunk_text(text)
        if not chunks:
            raise RuntimeError("No text to synthesize after chunking")

        logger.info(
            "GeminiTTS: synthesizing %d chunks via voice='%s' "
            "(gemini_voice=%s, model=%s)",
            len(chunks), voice_alias, gemini_voice, self.model,
        )

        # Generate inter-chunk silence PCM bytes once. 24kHz × 16-bit ×
        # mono × pause_ms/1000.
        silence_samples = int(
            self.GEMINI_SAMPLE_RATE * self.INTER_CHUNK_PAUSE_MS / 1000
        )
        silence_bytes = b"\x00\x00" * silence_samples  # 16-bit zeros

        pcm_segments: list[bytes] = []
        for i, chunk in enumerate(chunks):
            try:
                pcm = self._synthesize_chunk(
                    chunk, gemini_voice, extra_style=extra_style,
                )
            except Exception as e:
                raise RuntimeError(
                    f"GeminiTTS failed on chunk {i+1}/{len(chunks)}: {e}"
                ) from e
            pcm_segments.append(pcm)
            if i < len(chunks) - 1:
                pcm_segments.append(silence_bytes)
            logger.info(
                "  GeminiTTS chunk %d/%d done (%d bytes)",
                i + 1, len(chunks), len(pcm),
            )

        combined = b"".join(pcm_segments)
        with wave.open(str(output_path), "wb") as wf:
            wf.setnchannels(self.GEMINI_CHANNELS)
            wf.setsampwidth(self.GEMINI_SAMPLE_WIDTH)
            wf.setframerate(self.GEMINI_SAMPLE_RATE)
            wf.writeframes(combined)

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError(f"GeminiTTS produced no output for {output_path}")

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
        extra_style: Optional[str] = None,    # accepted for interface parity; ignored
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
        extra_style: Optional[str] = None,    # accepted for interface parity; ignored
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
        extra_style: Optional[str] = None,    # accepted for interface parity; ignored
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
    provider = (config or {}).get("provider", "gemini")
    speed = float((config or {}).get("speed", 1.0))

    if provider == "gemini":
        voice_map = (config or {}).get("voice_map") or {}
        default = (config or {}).get("voice_default") or "female"
        model = (config or {}).get("model")
        max_chunk = int((config or {}).get(
            "max_chunk_chars", GeminiTTSClient.DEFAULT_MAX_CHUNK_CHARS,
        ))
        retries = int((config or {}).get(
            "retries", GeminiTTSClient.DEFAULT_RETRIES,
        ))
        style_prompt = (config or {}).get("style_prompt")
        # ADC / Vertex AI fields (optional — default is api_key mode).
        auth_mode = (config or {}).get("auth_mode")
        gcp_project = (config or {}).get("gcp_project")
        gcp_location = (config or {}).get("gcp_location")
        return GeminiTTSClient(
            voice_map=voice_map,
            default_voice=default,
            model=model,
            max_chunk_chars=max_chunk,
            retries=retries,
            style_prompt=style_prompt,
            auth_mode=auth_mode,
            gcp_project=gcp_project,
            gcp_location=gcp_location,
        )

    if provider == "kokoro":
        raise ValueError(
            "TTS provider 'kokoro' was removed when Gemini TTS became "
            "production. Use provider: gemini in pipeline.yaml. If you "
            "really need to restore Kokoro, the KokoroClient class is in "
            "git history — search for the commit that introduced "
            "GeminiTTSClient and revert the deletion."
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

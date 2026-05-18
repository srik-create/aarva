"""Embedding-client abstraction.

A persistent embedding space is the foundation of:
  - Stage 1.5 event clustering (the "living vector space")
  - The topical-similarity axis of the 4-axis personalisation model (Q4/Q26)
  - "More like this" / cross-time relevance (Q15 Mode B/C contextualisation)
  - Drift detection (Q7)
  - Pairing detection (Q31)

Default: local sentence-transformers (free, no API key, ~30-80ms per article
on Apple Silicon). Alternative: OpenAI embeddings API (cheap, no PyTorch
dependency, ~$0.50/month at Aarva's volume).

Importing this module does NOT load PyTorch or the OpenAI client; concrete
implementations defer their imports so users only pay for the backend they
actually use.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Sequence

import numpy as np

logger = logging.getLogger(__name__)


class EmbeddingClient(ABC):
    """Produces dense vector embeddings for text.

    All implementations return numpy float32 arrays of shape (N, D) for N
    input texts. Vectors are L2-normalised by default so cosine similarity
    can be computed via simple dot product.
    """

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """Return embeddings as a (len(texts), dim) float32 array, L2-normalised."""
        ...

    @property
    @abstractmethod
    def dim(self) -> int:
        """Embedding dimensionality."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier for this embedding backend (e.g. 'bge-base-en-v1.5')."""
        ...


# ─────────────────────────────────────────────────────────────────────────────
# Local backend (sentence-transformers)
# ─────────────────────────────────────────────────────────────────────────────

class LocalEmbeddingClient(EmbeddingClient):
    """sentence-transformers running locally on user's machine.

    First-run downloads the model (~110MB for bge-base-en-v1.5; ~1.3GB for
    bge-large-en-v1.5). Subsequent runs use the cached model.

    Requires the [embeddings] optional dependency group:
        pip install sentence-transformers
    """

    DEFAULT_MODEL = "BAAI/bge-base-en-v1.5"

    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or self.DEFAULT_MODEL
        self._model = None
        self._dim: int | None = None

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RuntimeError(
                "LocalEmbeddingClient requires sentence-transformers. "
                "Install with:  pip install sentence-transformers"
            ) from e

        logger.info("Loading embedding model %s (first run downloads ~100MB-1GB)",
                    self.model_name)
        self._model = SentenceTransformer(self.model_name)
        self._dim = int(self._model.get_sentence_embedding_dimension())
        logger.info("Embedding model loaded — dim=%d", self._dim)

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        self._load()
        # sentence-transformers normalises automatically with normalize_embeddings=True
        vectors = self._model.encode(
            list(texts),
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return vectors.astype(np.float32)

    @property
    def dim(self) -> int:
        self._load()
        assert self._dim is not None
        return self._dim

    @property
    def name(self) -> str:
        # e.g. "BAAI/bge-base-en-v1.5" → "bge-base-en-v1.5"
        return self.model_name.split("/")[-1]


# ─────────────────────────────────────────────────────────────────────────────
# OpenAI backend (alternative)
# ─────────────────────────────────────────────────────────────────────────────

class OpenAIEmbeddingClient(EmbeddingClient):
    """OpenAI embeddings via API.

    Cheap (~$0.50/month at Aarva's volume) and doesn't need PyTorch. Requires
    OPENAI_API_KEY environment variable.
    """

    DEFAULT_MODEL = "text-embedding-3-small"
    DIMS = {
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
    }

    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or self.DEFAULT_MODEL
        if self.model_name not in self.DIMS:
            raise ValueError(f"Unknown OpenAI embedding model: {self.model_name}")
        self._client = None

    def _load(self) -> None:
        if self._client is not None:
            return
        try:
            from openai import OpenAI
        except ImportError as e:
            raise RuntimeError(
                "OpenAIEmbeddingClient requires openai. "
                "Install with:  pip install openai"
            ) from e
        self._client = OpenAI()    # picks up OPENAI_API_KEY from env

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        self._load()
        response = self._client.embeddings.create(
            model=self.model_name,
            input=list(texts),
        )
        vectors = np.array(
            [d.embedding for d in response.data],
            dtype=np.float32,
        )
        # OpenAI returns unit-norm vectors; normalise defensively.
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vectors / norms

    @property
    def dim(self) -> int:
        return self.DIMS[self.model_name]

    @property
    def name(self) -> str:
        return self.model_name


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

def build_embedding_client(config: dict) -> EmbeddingClient:
    """Build an embedding client from the relevant slice of pipeline.yaml.

    Expected config shape:
        embedding:
          provider: local | openai
          model:    <optional model override>
    """
    provider = (config or {}).get("provider", "local")
    model = (config or {}).get("model")

    if provider == "local":
        return LocalEmbeddingClient(model_name=model)
    if provider == "openai":
        return OpenAIEmbeddingClient(model_name=model)
    raise ValueError(f"Unknown embedding provider: {provider}")

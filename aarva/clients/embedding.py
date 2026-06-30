"""Embedding-client abstraction.

A persistent embedding space is the foundation of:
  - Stage 1.5 event clustering (the "living vector space")
  - Crosscut episode embeddings (search-time discovery)
  - The topical-similarity axis of the 4-axis personalisation model (Q4/Q26)
  - "More like this" / cross-time relevance (Q15 Mode B/C contextualisation)
  - Drift detection (Q7)
  - Pairing detection (Q31)

Production default: **Vertex AI Gemini Embedding** (`gemini-embedding-001`),
matching the rest of Aarva's Gemini stack (LLM + TTS already live there).
API-based — no PyTorch on the server. The previous default was a local
sentence-transformers BGE-base model; that ran fine on the operator's
laptop but OOM'd Render Starter (512 MB) once the model + PyTorch
runtime loaded into memory, so the production deploy moved to Vertex AI
on 2026-06-30.

`LocalEmbeddingClient` (sentence-transformers / BGE) and
`OpenAIEmbeddingClient` remain available as alternatives — useful for
offline development or if Vertex AI is ever unreachable. Switching
backend is a YAML edit in `pipeline.yaml`'s `embedding:` block.

Importing this module does NOT load any vendor SDK; concrete
implementations defer their imports so users only pay for the backend
they actually use.

Task-type semantics
-------------------
The Vertex AI Gemini Embedding model is asymmetric: it produces
DIFFERENT vectors for the same text depending on the `task_type`
parameter. Per Google's documentation:
  - Embed indexed content (articles, episode pairing summaries) with
    `task_type='RETRIEVAL_DOCUMENT'`.
  - Embed the listener's open-ended query with
    `task_type='RETRIEVAL_QUERY'`.
The two halves of the dot-product live in the same metric space, so
mixing them this way gives better top-K retrieval than embedding both
sides identically.

Other backends (Local BGE, OpenAI) ignore `task_type` — they're
symmetric and produce the same vector regardless. The kwarg is plumbed
through every client so callers can pass it unconditionally without
branching on the active provider.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional, Sequence

import numpy as np

logger = logging.getLogger(__name__)


class EmbeddingClient(ABC):
    """Produces dense vector embeddings for text.

    All implementations return numpy float32 arrays of shape (N, D) for N
    input texts. Vectors are L2-normalised by default so cosine similarity
    can be computed via simple dot product.
    """

    @abstractmethod
    def embed(
        self,
        texts: Sequence[str],
        *,
        task_type: Optional[str] = None,
    ) -> np.ndarray:
        """Return embeddings as a (len(texts), dim) float32 array,
        L2-normalised.

        `task_type` is used only by backends that distinguish query-side
        vs document-side embeddings (currently Vertex AI). Pass
        'RETRIEVAL_QUERY' when embedding a listener's prompt and
        'RETRIEVAL_DOCUMENT' when embedding indexed text. Backends that
        don't care (Local BGE, OpenAI) ignore the parameter."""
        ...

    @property
    @abstractmethod
    def dim(self) -> int:
        """Embedding dimensionality."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier for this embedding backend (e.g. 'bge-base-en-v1.5',
        'gemini-embedding-001-768')."""
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

    def embed(
        self,
        texts: Sequence[str],
        *,
        task_type: Optional[str] = None,    # ignored — BGE is symmetric
    ) -> np.ndarray:
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

    def embed(
        self,
        texts: Sequence[str],
        *,
        task_type: Optional[str] = None,    # ignored — OpenAI is symmetric
    ) -> np.ndarray:
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
# Vertex AI backend (production default — see module docstring)
# ─────────────────────────────────────────────────────────────────────────────

class VertexAIEmbeddingClient(EmbeddingClient):
    """Vertex AI Gemini Embedding via the `google-genai` Python SDK.

    Auth is ADC (Application Default Credentials) — same path the
    pipeline's LLM client already uses. No API key required as long as
    `gcloud auth application-default login` has been run locally or the
    container runs in a GCP environment with a service account
    attached.

    Model: `gemini-embedding-001` (text). The model emits a 3072-dim
    vector natively and supports Matryoshka truncation — `output_
    dimensionality` of 768 / 1536 / 3072 are listed as recommended.
    We default to **768** to match the storage shape of the previous
    BGE-base vectors so the DB blob layout is unchanged.

    Task-type semantics: the model is asymmetric. `RETRIEVAL_DOCUMENT`
    and `RETRIEVAL_QUERY` produce different vectors for the same text;
    Google's recommendation for top-K search is to embed indexed
    content as DOCUMENT and queries as QUERY. If `task_type` is None
    (caller didn't specify), we default to `RETRIEVAL_DOCUMENT` — the
    safer choice for backfill / indexing code paths that don't know
    about the asymmetry.

    Truncation note: when `output_dimensionality < 3072`, Google's docs
    recommend re-L2-normalising client-side. We do that here.
    """

    DEFAULT_MODEL = "gemini-embedding-001"
    DEFAULT_DIM = 768
    DEFAULT_LOCATION = "us-central1"
    DEFAULT_TASK_TYPE = "RETRIEVAL_DOCUMENT"

    def __init__(
        self,
        *,
        project: Optional[str] = None,
        location: Optional[str] = None,
        model_name: Optional[str] = None,
        output_dimensionality: Optional[int] = None,
    ):
        self.model_name = model_name or self.DEFAULT_MODEL
        self.location = location or self.DEFAULT_LOCATION
        self.output_dim = int(output_dimensionality or self.DEFAULT_DIM)
        # Project may be None and inferred by the SDK from the
        # environment (GOOGLE_CLOUD_PROJECT or gcloud config). We
        # store it explicitly when supplied so the error message
        # if auth fails points at the right spot.
        self.project = project
        self._client = None

    def _load(self) -> None:
        if self._client is not None:
            return
        try:
            from google import genai      # type: ignore[import-not-found]
        except ImportError as e:
            raise RuntimeError(
                "VertexAIEmbeddingClient requires google-genai. "
                "Install with:  pip install google-genai"
            ) from e

        # vertexai=True picks ADC + the named project/location instead
        # of the public AI Studio API-key path. The SDK reads
        # GOOGLE_CLOUD_PROJECT if `project` isn't passed.
        if self.project:
            self._client = genai.Client(
                vertexai=True,
                project=self.project,
                location=self.location,
            )
        else:
            self._client = genai.Client(
                vertexai=True,
                location=self.location,
            )
        logger.info(
            "VertexAIEmbeddingClient ready — model=%s dim=%d location=%s",
            self.model_name, self.output_dim, self.location,
        )

    def embed(
        self,
        texts: Sequence[str],
        *,
        task_type: Optional[str] = None,
    ) -> np.ndarray:
        self._load()
        try:
            from google.genai import types   # type: ignore[import-not-found]
        except ImportError as e:
            raise RuntimeError("google-genai types module unavailable") from e

        effective_task = task_type or self.DEFAULT_TASK_TYPE
        text_list = list(texts)
        if not text_list:
            return np.zeros((0, self.output_dim), dtype=np.float32)

        # The SDK accepts either a single string or a list under
        # `contents`. We always pass a list so the return shape is
        # uniform across single / batch calls.
        result = self._client.models.embed_content(
            model=self.model_name,
            contents=text_list,
            config=types.EmbedContentConfig(
                task_type=effective_task,
                output_dimensionality=self.output_dim,
            ),
        )
        vectors = np.array(
            [e.values for e in result.embeddings],
            dtype=np.float32,
        )

        # Matryoshka truncation requires client-side L2-renormalisation
        # so cosine similarity remains a dot product. Defensive even at
        # full 3072 (some return paths are already unit-norm).
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vectors / norms

    @property
    def dim(self) -> int:
        return self.output_dim

    @property
    def name(self) -> str:
        # Encode the dimension so 768 / 1536 / 3072 variants don't
        # collide in `embedding_model` columns. e.g.
        # 'gemini-embedding-001-768'.
        return f"{self.model_name}-{self.output_dim}"


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

def build_embedding_client(config: dict) -> EmbeddingClient:
    """Build an embedding client from the relevant slice of pipeline.yaml.

    Expected config shape:
        embedding:
          provider:              local | openai | vertex_ai
          model:                 <optional model override>
          # Vertex AI only:
          location:              us-central1 (default)
          project:               <optional; defaults to GOOGLE_CLOUD_PROJECT>
          output_dimensionality: 768 (default) | 1536 | 3072
    """
    provider = (config or {}).get("provider", "local")
    model = (config or {}).get("model")

    if provider == "local":
        return LocalEmbeddingClient(model_name=model)
    if provider == "openai":
        return OpenAIEmbeddingClient(model_name=model)
    if provider == "vertex_ai":
        # Accepts both gcp_project/gcp_location (matching the llm
        # block's convention) and the simpler project/location names
        # for forward-compat. gcp_project wins when both are set.
        cfg = config or {}
        project = cfg.get("gcp_project") or cfg.get("project")
        location = cfg.get("gcp_location") or cfg.get("location")
        return VertexAIEmbeddingClient(
            project=project,
            location=location,
            model_name=model,
            output_dimensionality=cfg.get("output_dimensionality"),
        )
    raise ValueError(f"Unknown embedding provider: {provider}")

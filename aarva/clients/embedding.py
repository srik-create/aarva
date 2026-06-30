"""Embedding-client abstraction.

A persistent embedding space is the foundation of:
  - Stage 1.5 event clustering (the "living vector space")
  - Crosscut episode embeddings (search-time discovery)
  - The topical-similarity axis of the 4-axis personalisation model (Q4/Q26)
  - "More like this" / cross-time relevance (Q15 Mode B/C contextualisation)
  - Drift detection (Q7)
  - Pairing detection (Q31)

Production default: **Gemini Embedding** (`gemini-embedding-001`), the
same model used by the rest of Aarva's Gemini stack (LLM + TTS).
API-based — no PyTorch on the server. The previous default was a local
sentence-transformers BGE-base model; that ran fine on the operator's
laptop but OOM'd Render Starter (512 MB) once the model + PyTorch
runtime loaded into memory, so the production deploy moved to a Gemini
API embedding client on 2026-06-30.

Auth — two modes, same model
----------------------------
`GeminiEmbeddingClient` supports both auth paths the wider Gemini stack
uses, mirroring `GeminiAPIClient` in `aarva/clients/llm.py`:

  - `auth_mode='api_key'` — calls AI Studio's
    generativelanguage.googleapis.com endpoint with an API key from
    AARVA_GEMINI_API_KEY / GEMINI_API_KEY / GOOGLE_API_KEY. This is the
    production default; the paid-tier AI Studio terms (no training on
    prompts) satisfy gibran.ai's data-governance constraints.
  - `auth_mode='adc'` — calls Vertex AI via
    us-central1-aiplatform.googleapis.com with Application Default
    Credentials. Useful on laptops with `gcloud auth
    application-default login` and on GCP-hosted compute with attached
    service accounts. Requires `gcp_project` and `gcp_location` in the
    config block.

The model name, task-type parameters, and `output_dimensionality`
behaviour are identical across the two paths, so vectors produced under
one auth mode are interchangeable with vectors produced under the other
— no re-embed is needed when flipping the mode.

`LocalEmbeddingClient` (sentence-transformers / BGE) and
`OpenAIEmbeddingClient` remain available as alternatives — useful for
offline development or if Gemini is ever unreachable. Switching backend
is a YAML edit in `pipeline.yaml`'s `embedding:` block.

Importing this module does NOT load any vendor SDK; concrete
implementations defer their imports so users only pay for the backend
they actually use.

Task-type semantics
-------------------
The Gemini Embedding model is asymmetric: it produces DIFFERENT vectors
for the same text depending on the `task_type` parameter. Per Google's
documentation:
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
# Gemini backend (production default — see module docstring)
# ─────────────────────────────────────────────────────────────────────────────

class GeminiEmbeddingClient(EmbeddingClient):
    """Gemini Embedding via the `google-genai` Python SDK, with two
    interchangeable auth paths.

    Auth modes (see module docstring for the production rationale):
      - 'api_key' (production default): AI Studio endpoint. Reads
        AARVA_GEMINI_API_KEY / GEMINI_API_KEY / GOOGLE_API_KEY from the
        environment. Works on any compute — no GCP service account or
        ADC bootstrap needed. The paid-tier no-train terms cover
        Aarva's data-governance constraints.
      - 'adc': Vertex AI endpoint with Application Default Credentials.
        Requires `gcp_project` + `gcp_location`. Useful on a laptop with
        `gcloud auth application-default login` or on GCP-hosted
        compute with an attached service account.

    Both modes hit the same underlying model and respect the same
    `task_type` + `output_dimensionality` knobs, so vectors are
    interchangeable across modes (no re-embed when flipping).

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
    DEFAULT_AUTH_MODE = "api_key"

    def __init__(
        self,
        *,
        auth_mode: Optional[str] = None,
        project: Optional[str] = None,
        location: Optional[str] = None,
        model_name: Optional[str] = None,
        output_dimensionality: Optional[int] = None,
    ):
        self.model_name = model_name or self.DEFAULT_MODEL
        self.location = location or self.DEFAULT_LOCATION
        self.output_dim = int(output_dimensionality or self.DEFAULT_DIM)
        # Project may be None for api_key mode. For adc mode the SDK
        # would otherwise infer it from the environment, but we require
        # explicit config so the error message points at the right spot
        # if auth fails.
        self.project = project
        self.auth_mode = (auth_mode or self.DEFAULT_AUTH_MODE).lower()
        if self.auth_mode not in ("api_key", "adc"):
            raise ValueError(
                f"Unknown embedding auth_mode '{self.auth_mode}'. "
                f"Expected 'api_key' or 'adc'."
            )
        if self.auth_mode == "adc" and not self.project:
            raise ValueError(
                "auth_mode='adc' requires gcp_project in pipeline.yaml's "
                "embedding block (e.g. 'gen-lang-client-0889802137')."
            )
        self._client = None

    def _load(self) -> None:
        if self._client is not None:
            return
        try:
            from google import genai      # type: ignore[import-not-found]
        except ImportError as e:
            raise RuntimeError(
                "GeminiEmbeddingClient requires google-genai. "
                "Install with:  pip install google-genai"
            ) from e

        if self.auth_mode == "adc":
            # vertexai=True picks ADC + the named project/location
            # instead of the public AI Studio API-key path.
            self._client = genai.Client(
                vertexai=True,
                project=self.project,
                location=self.location,
            )
            logger.info(
                "GeminiEmbeddingClient ready (adc/Vertex) — model=%s dim=%d "
                "project=%s location=%s",
                self.model_name, self.output_dim, self.project, self.location,
            )
            return

        # api_key path. Check the Aarva-namespaced env var first, then
        # fall back to the Google-standard names. Cloud deployments
        # typically set AARVA_GEMINI_API_KEY through their secret manager.
        import os as _os
        api_key = (
            _os.environ.get("AARVA_GEMINI_API_KEY")
            or _os.environ.get("GEMINI_API_KEY")
            or _os.environ.get("GOOGLE_API_KEY")
        )
        if not api_key:
            raise RuntimeError(
                "No Gemini API key found. Set AARVA_GEMINI_API_KEY "
                "(or GEMINI_API_KEY / GOOGLE_API_KEY) in the environment. "
                "Get a key from https://aistudio.google.com/apikey, OR "
                "switch to auth_mode='adc' in pipeline.yaml to use "
                "Application Default Credentials instead."
            )
        self._client = genai.Client(api_key=api_key)
        logger.info(
            "GeminiEmbeddingClient ready (api_key/AI Studio) — model=%s dim=%d",
            self.model_name, self.output_dim,
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
        # 'gemini-embedding-001-768'. Note: the same name is used
        # regardless of auth_mode, since vectors are interchangeable.
        return f"{self.model_name}-{self.output_dim}"


# Backwards-compatible alias. The class used to be called
# VertexAIEmbeddingClient when ADC was the only supported path. Kept
# importable so any in-flight branches / scripts that reference the old
# name still work. Prefer the new name in new code.
VertexAIEmbeddingClient = GeminiEmbeddingClient


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

def build_embedding_client(config: dict) -> EmbeddingClient:
    """Build an embedding client from the relevant slice of pipeline.yaml.

    Expected config shape:
        embedding:
          provider:              local | openai | gemini
          model:                 <optional model override>
          # Gemini only:
          auth_mode:             api_key (default) | adc
          # auth_mode='adc' also requires:
          gcp_project:           <e.g. 'gen-lang-client-0889802137'>
          gcp_location:          us-central1 (default)
          output_dimensionality: 768 (default) | 1536 | 3072

    The legacy provider name `vertex_ai` is accepted as an alias for
    `gemini` with `auth_mode='adc'` defaulted, so existing config files
    keep working through the transition.
    """
    cfg = config or {}
    provider = cfg.get("provider", "local")
    model = cfg.get("model")

    if provider == "local":
        return LocalEmbeddingClient(model_name=model)
    if provider == "openai":
        return OpenAIEmbeddingClient(model_name=model)
    if provider in ("gemini", "vertex_ai"):
        # Accepts both gcp_project/gcp_location (matching the llm
        # block's convention) and the simpler project/location names
        # for forward-compat. gcp_project wins when both are set.
        project = cfg.get("gcp_project") or cfg.get("project")
        location = cfg.get("gcp_location") or cfg.get("location")
        # `vertex_ai` is a legacy alias that defaults to ADC; `gemini`
        # defaults to whatever GeminiEmbeddingClient.DEFAULT_AUTH_MODE
        # is (currently api_key). Explicit auth_mode in the config
        # always wins over either default.
        default_auth = "adc" if provider == "vertex_ai" else None
        return GeminiEmbeddingClient(
            auth_mode=cfg.get("auth_mode") or default_auth,
            project=project,
            location=location,
            model_name=model,
            output_dimensionality=cfg.get("output_dimensionality"),
        )
    raise ValueError(f"Unknown embedding provider: {provider}")

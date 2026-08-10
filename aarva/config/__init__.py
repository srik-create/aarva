"""Config loaders for Aarva.

YAML files in this directory are the default source of truth for all
tunable parameters. Environment variables override individual YAML
keys — useful for cloud deployment (12-factor app pattern) and for
keeping secrets out of the config repo.

Precedence: ENV > YAML > built-in defaults.

Recognised env vars (all optional):

  AARVA_DB_PATH                — overrides paths.db
  AARVA_AUDIO_DIR              — overrides paths.audio_dir
  AARVA_WEB_DIR                — overrides paths.web_dir
  AARVA_RSS_FEED_PATH          — overrides paths.rss_feed
  AARVA_PUBLIC_URL_BASE        — overrides output.public_url_base
  AARVA_FEED_EMAIL             — overrides output.feed_email
  AARVA_LLM_PROVIDER           — overrides llm.provider
  AARVA_LLM_MODEL              — overrides llm.model
  AARVA_GEMINI_API_KEY         — read by aarva.clients.llm (secret;
                                  never put this in YAML)
  AARVA_EMBEDDING_PROVIDER     — overrides embedding.provider
  AARVA_EMBEDDING_MODEL        — overrides embedding.model
  AARVA_OPENAI_API_KEY         — for OpenAI-backed embeddings
  AARVA_LOG_LEVEL              — overrides logging.level
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from aarva.exceptions import ConfigError


CONFIG_DIR = Path(__file__).parent


@dataclass(frozen=True)
class Publication:
    name: str
    rss_url: str | None
    homepage: str | None
    tier: str | None
    enabled: bool
    licence_status: str | None
    notes: str | None
    # ISO-style country code used to steer TTS accent (e.g. 'us', 'uk',
    # 'india'). Optional — None means use the voice's baseline accent.
    # See stage_9_tts._COUNTRY_TO_ACCENT_PROMPT for the mapping.
    country: str | None = None


@dataclass(frozen=True)
class CurationSource:
    """A peer-curator RSS/Atom feed crawled for the "not too niche"
    signal — see docs/session_plan_curation_platform_signal.md."""
    name: str
    homepage: str | None
    feed_url: str
    weight: float
    enabled: bool
    notes: str | None


@dataclass(frozen=True)
class IngestionConfig:
    max_entries_per_feed: int = 30
    lookback_days: int = 7
    http_timeout_seconds: int = 30
    user_agent: str = "Aarva/0.1"


@dataclass(frozen=True)
class FiltersConfig:
    word_floor: int = 600
    listicle_keywords: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PipelineConfig:
    db_path: Path
    audio_dir: Path
    web_dir: Path
    rss_feed_path: Path
    ingestion: IngestionConfig
    filters: FiltersConfig
    raw: dict[str, Any]   # full parsed config for stages that want anything else

    @property
    def assembly(self) -> dict[str, Any]:
        return self.raw.get("assembly", {})

    @property
    def scoring(self) -> dict[str, Any]:
        return self.raw.get("scoring", {})

    @property
    def llm(self) -> dict[str, Any]:
        return self.raw.get("llm", {})

    @property
    def tts(self) -> dict[str, Any]:
        return self.raw.get("tts", {})

    @property
    def consolidation(self) -> dict[str, Any]:
        return self.raw.get("consolidation", {})

    @property
    def curation(self) -> dict[str, Any]:
        return self.raw.get("curation", {})


def _resolve_path(rel_or_abs: str, project_root: Path) -> Path:
    p = Path(rel_or_abs)
    return p if p.is_absolute() else (project_root / p).resolve()


# ─── Env-var overlay ─────────────────────────────────────────────────────

# Maps env-var name → (dotted YAML key path). When the env var is set
# (non-empty), its value overrides the corresponding YAML key.
_ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "AARVA_DB_PATH":           ("paths", "db"),
    "AARVA_AUDIO_DIR":         ("paths", "audio_dir"),
    "AARVA_WEB_DIR":           ("paths", "web_dir"),
    "AARVA_RSS_FEED_PATH":     ("paths", "rss_feed"),
    "AARVA_PUBLIC_URL_BASE":   ("output", "public_url_base"),
    "AARVA_FEED_EMAIL":        ("output", "feed_email"),
    "AARVA_LLM_PROVIDER":      ("llm", "provider"),
    "AARVA_LLM_MODEL":         ("llm", "model"),
    "AARVA_EMBEDDING_PROVIDER": ("embedding", "provider"),
    "AARVA_EMBEDDING_MODEL":    ("embedding", "model"),
    "AARVA_LOG_LEVEL":         ("logging", "level"),
}


def _apply_env_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    """Mutate `raw` in place, applying env-var overrides where set.
    Returns the same dict for chainability."""
    for env_name, path in _ENV_OVERRIDES.items():
        value = os.environ.get(env_name)
        if value is None or value == "":
            continue
        # Walk into the dict, creating intermediate dicts if missing.
        cursor: Any = raw
        for key in path[:-1]:
            if key not in cursor or not isinstance(cursor[key], dict):
                cursor[key] = {}
            cursor = cursor[key]
        cursor[path[-1]] = value
    return raw


def load_pipeline_config(project_root: Path | None = None) -> PipelineConfig:
    """Load aarva/config/pipeline.yaml, then apply env-var overrides.

    Paths are resolved relative to the project root. Raises
    ConfigError if the YAML file is missing or unparseable.
    """
    if project_root is None:
        # Default: two parents up from this file (aarva/config/__init__.py → project root).
        project_root = CONFIG_DIR.parent.parent
    yaml_path = CONFIG_DIR / "pipeline.yaml"
    try:
        with yaml_path.open() as f:
            raw = yaml.safe_load(f) or {}
    except FileNotFoundError as e:
        raise ConfigError(
            f"pipeline.yaml not found at {yaml_path}. Either restore the "
            f"file or set AARVA_CONFIG_PATH to point at an alternative."
        ) from e
    except yaml.YAMLError as e:
        raise ConfigError(f"pipeline.yaml is not valid YAML: {e}") from e

    _apply_env_overrides(raw)

    paths = raw.get("paths", {}) or {}
    ingestion = raw.get("ingestion", {}) or {}
    filters = raw.get("filters", {}) or {}

    return PipelineConfig(
        db_path=_resolve_path(paths.get("db", "aarva/data/aarva.db"), project_root),
        audio_dir=_resolve_path(paths.get("audio_dir", "aarva/output/audio"), project_root),
        web_dir=_resolve_path(paths.get("web_dir", "aarva/output/web"), project_root),
        rss_feed_path=_resolve_path(paths.get("rss_feed", "aarva/output/feed.xml"), project_root),
        ingestion=IngestionConfig(
            max_entries_per_feed=ingestion.get("max_entries_per_feed", 30),
            lookback_days=ingestion.get("lookback_days", 7),
            http_timeout_seconds=ingestion.get("http_timeout_seconds", 30),
            user_agent=ingestion.get("user_agent", "Aarva/0.1"),
        ),
        filters=FiltersConfig(
            word_floor=filters.get("word_floor", 600),
            listicle_keywords=filters.get("listicle_keywords", []),
        ),
        raw=raw,
    )


def load_publications() -> list[Publication]:
    """Load aarva/config/publications.yaml."""
    with (CONFIG_DIR / "publications.yaml").open() as f:
        raw = yaml.safe_load(f)
    return [
        Publication(
            name=p["name"],
            rss_url=p.get("rss_url"),
            homepage=p.get("homepage"),
            tier=p.get("tier"),
            enabled=p.get("enabled", True),
            licence_status=p.get("licence_status"),
            notes=p.get("notes"),
            country=p.get("country"),
        )
        for p in raw.get("publications", [])
    ]


def load_curation_sources() -> list[CurationSource]:
    """Load aarva/config/curation_sources.yaml. Mirrors load_publications.
    See docs/session_plan_curation_platform_signal.md."""
    with (CONFIG_DIR / "curation_sources.yaml").open() as f:
        raw = yaml.safe_load(f)
    return [
        CurationSource(
            name=s["name"],
            homepage=s.get("homepage"),
            feed_url=s["feed_url"],
            weight=float(s.get("weight", 1.0)),
            enabled=s.get("enabled", True),
            notes=s.get("notes"),
        )
        for s in raw.get("sources", [])
    ]

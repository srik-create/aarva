"""Config loaders for Aarva.

YAML files in this directory are the source of truth for all tunable parameters.
This module exposes typed accessors so the rest of the codebase reads structured
objects, not raw dicts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


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


def _resolve_path(rel_or_abs: str, project_root: Path) -> Path:
    p = Path(rel_or_abs)
    return p if p.is_absolute() else (project_root / p).resolve()


def load_pipeline_config(project_root: Path | None = None) -> PipelineConfig:
    """Load aarva/config/pipeline.yaml. Paths are resolved relative to the project root."""
    if project_root is None:
        # Default: two parents up from this file (aarva/config/__init__.py → project root).
        project_root = CONFIG_DIR.parent.parent
    with (CONFIG_DIR / "pipeline.yaml").open() as f:
        raw = yaml.safe_load(f)

    paths = raw.get("paths", {})
    ingestion = raw.get("ingestion", {})
    filters = raw.get("filters", {})

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
        )
        for p in raw.get("publications", [])
    ]

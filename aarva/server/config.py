"""Server-specific configuration.

Pulls values from environment variables (12-factor style). No
provider-specific env-var names referenced here — only generic ones
that any hosting platform can supply.

Order of precedence for each setting:
  1. Environment variable (`AARVA_SERVER_*` or, where conventional,
     the standard short name like `PORT` or `HOST`)
  2. Falls back to a sensible default for local development

Env vars consumed:
  HOST                       — bind address (default 127.0.0.1 dev / 0.0.0.0 prod)
  PORT                       — bind port (default 8000)
  AARVA_DB_PATH              — SQLite DB path (default aarva/data/aarva.db).
                               On Render with persistent disk attached,
                               this would be /data/aarva.db.
  AARVA_LOG_LEVEL            — INFO (default) | DEBUG | WARNING
  AARVA_SERVER_PUBLIC_URL    — public-facing URL base, used for
                               canonical links + RSS feed_link
                               (default 'http://localhost:8000' for dev,
                               'https://aarva.app' once deployed).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    db_path: str
    log_level: str
    public_url: str

    @property
    def is_production(self) -> bool:
        """Heuristic: bind to 0.0.0.0 or a non-localhost public URL
        means production. Helpful for toggling debug behaviours."""
        return self.host == "0.0.0.0" or not self.public_url.startswith(
            "http://localhost"
        )


def load_server_config() -> ServerConfig:
    """Pull server config from env vars, fall back to dev defaults."""
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    db_path = os.environ.get("AARVA_DB_PATH", "aarva/data/aarva.db")
    log_level = os.environ.get("AARVA_LOG_LEVEL", "INFO").upper()
    public_url = os.environ.get(
        "AARVA_SERVER_PUBLIC_URL",
        f"http://localhost:{port}",
    )
    return ServerConfig(
        host=host,
        port=port,
        db_path=db_path,
        log_level=log_level,
        public_url=public_url.rstrip("/"),
    )

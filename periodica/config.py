"""Process-level configuration from environment variables.

Everything the user can change at runtime lives in the database (see settings.py);
this module only holds what must be known before the database is opened.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Env:
    data_root: Path
    config_dir: Path
    host: str
    port: int
    secure_cookies: bool
    log_level: str
    cover_memory_mb: int
    # OFFLINE_MODE: no qBittorrent, no Jellyfin, no scan API, whatever the saved settings say.
    offline: bool = False

    @property
    def db_path(self) -> Path:
        return self.config_dir / "periodica.db"


def load_env() -> Env:
    return Env(
        data_root=Path(os.environ.get("DATA_ROOT", "/data")),
        config_dir=Path(os.environ.get("CONFIG_DIR", "/config")),
        host=os.environ.get("HOST", "0.0.0.0"),  # noqa: S104  # nosec B104
        port=int(os.environ.get("PORT", "8765")),
        secure_cookies=_bool(os.environ.get("SECURE_COOKIES"), False),
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        cover_memory_mb=int(os.environ.get("COVER_MEMORY_MB", "1024")),
        offline=_bool(os.environ.get("OFFLINE_MODE"), False),
    )

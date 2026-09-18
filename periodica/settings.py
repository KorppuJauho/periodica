"""User-editable settings, stored in SQLite."""

from __future__ import annotations

import json
import os
import posixpath
import re
import secrets

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .db import Database
from .issues import MONTH, NUMBER
from .metadata import (
    DEFAULT_MONTHLY_TITLE_FORMAT,
    DEFAULT_NUMBERED_TITLE_FORMAT,
    DEFAULT_TITLE_FORMAT,
    validate_title_format,
)

_LIBRARY_ID_RE = re.compile(r"[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

SECRET_FIELDS = {"qbit_password", "jellyfin_api_key", "api_key"}
# What OFFLINE_MODE forces, whatever is stored. The stored values are left alone for when it is removed.
OFFLINE_OVERRIDES = {"download_client": "none", "jellyfin_enabled": False, "api_enabled": False}
OFFLINE_MESSAGE = "offline mode: network connections are disabled (OFFLINE_MODE)"
# Settings fields that belong to a library (settings field -> libraries column). Until the settings pages
# know about libraries, these read and write the first library, so a single-library setup looks the same.
LIBRARY_FIELDS = {
    "source_dir": "source_dir", "dest_dir": "dest_dir", "qbit_category": "category",
    "title_format": "title_format", "monthly_title_format": "monthly_title_format",
    "numbered_title_format": "numbered_title_format", "language": "language", "cover_width": "cover_width",
    "retention_days": "retention_days", "jellyfin_library_id": "jellyfin_library_id",
    "jellyfin_library_name": "jellyfin_library_name",
}


def _abs_posix(value: str) -> str:
    """Paths as qBittorrent reports them (always POSIX in a Linux container)."""
    value = (value or "").strip()
    if not value:
        return ""
    if not value.startswith("/") or "\x00" in value or "\\" in value:
        raise ValueError("must be an absolute path like /downloads/...")
    return posixpath.normpath(value)


def _abs_local(value: str) -> str:
    """Paths on this machine (POSIX in the container; native when running tests elsewhere)."""
    value = (value or "").strip()
    if not value:
        return ""
    if "\x00" in value or not os.path.isabs(value):
        raise ValueError("must be an absolute path like /data/...")
    return os.path.normpath(value)


def required_abs(value: str) -> str:
    value = _abs_local(value)
    if not value or os.path.dirname(value) == value:
        raise ValueError("must be an absolute path like /data/...")
    return value


def clean_category(value: str) -> str:
    value = (value or "").strip()
    if len(value) > 100 or any(ch in value for ch in "\x00\r\n"):
        raise ValueError("invalid category")
    return value


def clean_library_id(value: str) -> str:
    value = (value or "").strip().lower()
    if value and not _LIBRARY_ID_RE.fullmatch(value):
        raise ValueError("invalid Jellyfin library id")
    return value


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    # Library
    source_dir: str = "/data/torrents/books/news"
    dest_dir: str = "/data/media/books/news"
    scan_interval_minutes: int = Field(default=5, ge=0, le=1440)  # 0 = no scheduled scans, triggers only
    title_format: str = DEFAULT_TITLE_FORMAT
    monthly_title_format: str = DEFAULT_MONTHLY_TITLE_FORMAT
    numbered_title_format: str = DEFAULT_NUMBERED_TITLE_FORMAT
    language: str = Field(default="en", pattern=r"^[a-z]{2,3}(-[A-Za-z]{2})?$")
    cover_width: int = Field(default=600, ge=150, le=2000)
    # Logging. "debug" from the Logs page is temporary and does not change this; this is the level
    # the app starts at and returns to.
    log_level: str = Field(default="info", pattern=r"^(info|debug)$")
    log_file_mb: int = Field(default=5, ge=0, le=50)      # one file; 0 = write no log files at all
    log_files_kept: int = Field(default=5, ge=1, le=20)   # rotated copies, so the ceiling is mb * kept

    # Download client. "none" = watch the source folder: no qBittorrent, and nothing is ever deleted.
    download_client: str = Field(default="qbittorrent", pattern=r"^(qbittorrent|none)$")
    settle_minutes: int = Field(default=5, ge=1, le=1440)  # folder mode: unchanged this long = finished
    qbit_url: str = ""
    qbit_username: str = ""
    qbit_password: str = ""
    qbit_category: str = ""
    path_map_remote: str = ""
    path_map_local: str = ""

    # Warn when no new download has arrived in the category for this many hours (0 = off)
    stale_download_hours: int = Field(default=36, ge=0, le=720)

    # Retention / automatic delete
    retention_enabled: bool = False
    retention_armed: bool = False
    retention_days: int = Field(default=7, ge=1, le=3650)
    grace_hours: int = Field(default=24, ge=0, le=720)
    max_torrent_deletions_per_run: int = Field(default=20, ge=1, le=500)

    # Jellyfin (optional). Switching it off keeps the URL and key.
    jellyfin_enabled: bool = True
    jellyfin_url: str = ""
    jellyfin_api_key: str = ""
    jellyfin_library_id: str = ""  # refresh only this library; empty = all libraries
    jellyfin_library_name: str = ""

    # Network safety
    allow_public_hosts: bool = False

    # Set by the store from OFFLINE_MODE; never saved.
    offline: bool = Field(default=False, exclude=True)

    # Scripted "scan now" (e.g. qBittorrent's "run on torrent finished"). Off = the endpoint does not exist.
    api_enabled: bool = True
    api_key: str = ""

    @field_validator("source_dir", "dest_dir")
    @classmethod
    def _required_abs(cls, value: str) -> str:
        return required_abs(value)

    @field_validator("path_map_remote")
    @classmethod
    def _remote_abs(cls, value: str) -> str:
        return _abs_posix(value)

    @field_validator("path_map_local")
    @classmethod
    def _local_abs(cls, value: str) -> str:
        return _abs_local(value)

    @field_validator("title_format")
    @classmethod
    def _title(cls, value: str) -> str:
        return validate_title_format(value)

    @field_validator("monthly_title_format")
    @classmethod
    def _monthly_title(cls, value: str) -> str:
        return validate_title_format(value, MONTH)

    @field_validator("numbered_title_format")
    @classmethod
    def _numbered_title(cls, value: str) -> str:
        return validate_title_format(value, NUMBER)

    def title_format_for(self, period: str) -> str:
        return {MONTH: self.monthly_title_format, NUMBER: self.numbered_title_format}.get(period, self.title_format)

    @field_validator("qbit_category")
    @classmethod
    def _category(cls, value: str) -> str:
        return clean_category(value)

    @field_validator("qbit_url", "jellyfin_url", "qbit_username")
    @classmethod
    def _strip(cls, value: str) -> str:
        return (value or "").strip()

    @property
    def uses_qbit(self) -> bool:
        return self.download_client == "qbittorrent"

    @property
    def qbit_configured(self) -> bool:
        return bool(self.qbit_url and self.qbit_category)

    @property
    def download_category(self) -> str:
        """The category stored with each download seen (folder downloads have none)."""
        return self.qbit_category if self.uses_qbit else ""

    @property
    def retention_active(self) -> bool:
        """Deleting needs qBittorrent: in folder mode the retention settings are kept but do nothing."""
        return self.retention_enabled and self.uses_qbit

    @field_validator("jellyfin_library_id")
    @classmethod
    def _library_id(cls, value: str) -> str:
        return clean_library_id(value)

    @field_validator("jellyfin_library_name")
    @classmethod
    def _library_name(cls, value: str) -> str:
        return "".join(ch for ch in (value or "") if ch.isprintable())[:100]

    @property
    def jellyfin_configured(self) -> bool:
        return bool(self.jellyfin_url and self.jellyfin_api_key)

    @property
    def jellyfin_active(self) -> bool:
        """Configured and switched on: the only case in which Jellyfin is ever contacted."""
        return self.jellyfin_enabled and self.jellyfin_configured


class SettingsStore:
    def __init__(self, db: Database, offline: bool = False):
        self.db = db
        self.offline = offline

    def effective(self, settings: Settings) -> Settings:
        """The settings the app runs with: with OFFLINE_MODE, the network features are forced off."""
        if not self.offline:
            return settings
        return settings.model_copy(update={**OFFLINE_OVERRIDES, "offline": True})

    def load(self) -> Settings:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        data = {}
        for row in rows:
            try:
                data[row["key"]] = json.loads(row["value"])
            except ValueError:
                continue
        library = self._first_library()
        if library is not None:
            for field, column in LIBRARY_FIELDS.items():
                data[field] = library[column]
        try:
            return self.effective(Settings(**data))
        except ValueError:
            # A bad stored value must never brick the app; fall back field by field.
            settings = Settings()
            for key, value in data.items():
                try:
                    setattr(settings, key, value)
                except (ValueError, AttributeError):
                    continue
            return self.effective(settings)

    def _first_library(self):
        with self.db.connect() as conn:
            return conn.execute("SELECT * FROM libraries ORDER BY position, id LIMIT 1").fetchone()

    def save(self, settings: Settings) -> None:
        data = settings.model_dump()
        with self.db.connect() as conn:
            first = conn.execute("SELECT id FROM libraries ORDER BY position, id LIMIT 1").fetchone()
            if first is not None:
                assignments = ", ".join(f"{column} = ?" for column in LIBRARY_FIELDS.values())
                conn.execute(f"UPDATE libraries SET {assignments} WHERE id = ?",  # noqa: S608  # nosec B608
                             [data[field] for field in LIBRARY_FIELDS] + [first["id"]])
            for key, value in data.items():
                if first is not None and key in LIBRARY_FIELDS:
                    continue   # stored with the library
                if self.offline and key in OFFLINE_OVERRIDES:
                    continue   # forced at runtime only; keep what was stored before OFFLINE_MODE
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, json.dumps(value)),
                )

    def ensure_api_key(self) -> Settings:
        settings = self.load()
        if not settings.api_key:
            settings.api_key = secrets.token_hex(24)
            self.save(settings)
        return settings

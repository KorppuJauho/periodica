"""Libraries: one qBittorrent category -> one source folder -> one destination -> one Jellyfin library.

Connections (qBittorrent, Jellyfin), the scan API and the automatic-delete switches are shared and
live in Settings; everything that differs per library lives here.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .formats import DEFAULT_PRIORITY, FORMATS, parse_priority, priority_suffixes
from .issues import MONTH, NUMBER
from .metadata import (
    DEFAULT_MONTHLY_TITLE_FORMAT,
    DEFAULT_NUMBERED_TITLE_FORMAT,
    DEFAULT_TITLE_FORMAT,
    validate_title_format,
)
from .paths import is_within

DEFAULT_SOURCE_DIR = "/data/torrents/books/news"
DEFAULT_DEST_DIR = "/data/media/books/news"
DEFAULT_LANGUAGE = "en"
DEFAULT_COVER_WIDTH = 600
DEFAULT_RETENTION_DAYS = 7


class Library(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    id: int = 0
    name: str = Field(default="News", min_length=1, max_length=60)
    category: str = ""
    source_dir: str = DEFAULT_SOURCE_DIR
    dest_dir: str = DEFAULT_DEST_DIR
    jellyfin_library_id: str = ""
    jellyfin_library_name: str = ""
    title_format: str = DEFAULT_TITLE_FORMAT
    monthly_title_format: str = DEFAULT_MONTHLY_TITLE_FORMAT
    numbered_title_format: str = DEFAULT_NUMBERED_TITLE_FORMAT
    language: str = Field(default=DEFAULT_LANGUAGE, pattern=r"^[a-z]{2,3}(-[A-Za-z]{2})?$")
    cover_width: int = Field(default=DEFAULT_COVER_WIDTH, ge=150, le=2000)
    retention_days: int = Field(default=DEFAULT_RETENTION_DAYS, ge=1, le=3650)
    enabled: bool = True
    position: int = 0
    # File types a download may contain besides the issues: skipped, never linked, deleted with the torrent.
    extra_extensions: str = "nfo"
    # When a download has an issue in several formats, the first of these is linked.
    format_priority: str = DEFAULT_PRIORITY
    # Warn when nothing new has arrived for this many hours (0 = never). A daily paper and a monthly comic differ.
    stale_download_hours: int = Field(default=36, ge=0, le=720)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = " ".join("".join(ch for ch in (value or "") if ch.isprintable()).split())
        if not value:
            raise ValueError("a name is required")
        return value

    @field_validator("category")
    @classmethod
    def _category(cls, value: str) -> str:
        from .settings import clean_category

        return clean_category(value)

    @field_validator("source_dir", "dest_dir")
    @classmethod
    def _folders(cls, value: str) -> str:
        from .settings import required_abs

        return required_abs(value)

    @field_validator("jellyfin_library_id")
    @classmethod
    def _jellyfin_id(cls, value: str) -> str:
        from .settings import clean_library_id

        return clean_library_id(value)

    @field_validator("jellyfin_library_name")
    @classmethod
    def _jellyfin_name(cls, value: str) -> str:
        return "".join(ch for ch in (value or "") if ch.isprintable())[:100]

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

    @field_validator("extra_extensions")
    @classmethod
    def _extra_extensions(cls, value: str) -> str:
        items = []
        for raw in re.split(r"[\s,;]+", (value or "").strip().lower()):
            item = raw.lstrip(".")
            if not item:
                continue
            if not re.fullmatch(r"[a-z0-9]{1,10}", item):
                raise ValueError(f"'{raw}' is not a file extension (letters and digits only, e.g. txt)")
            if f".{item}" in FORMATS:
                raise ValueError(f"{item} files are the issues themselves and can't be an extra")
            if item not in items:
                items.append(item)
        if len(items) > 20:
            raise ValueError("at most 20 extra file types")
        return ", ".join(items)

    @field_validator("format_priority")
    @classmethod
    def _format_priority(cls, value: str) -> str:
        return parse_priority(value)

    @property
    def priority(self) -> list[str]:
        return priority_suffixes(self.format_priority)

    @property
    def extras(self) -> set[str]:
        return {f".{item.strip()}" for item in self.extra_extensions.split(",") if item.strip()}

    def title_format_for(self, period: str) -> str:
        return {MONTH: self.monthly_title_format, NUMBER: self.numbered_title_format}.get(period, self.title_format)

    @property
    def jellyfin_target(self) -> tuple[str, str]:
        """What to ask Jellyfin to scan for this library: (library id or "" for all, display name)."""
        return self.jellyfin_library_id, self.jellyfin_library_name


def library_conflicts(library: Library, others: list[Library]) -> list[str]:
    """Rules between libraries: unique names and categories, and folders that never overlap.

    Overlapping folders would let one library link (or, through its category, delete) what belongs
    to another, so they are refused rather than guessed.
    """
    problems = []
    for other in others:
        if other.id == library.id:
            continue
        if other.name.casefold() == library.name.casefold():
            problems.append(f"the name '{library.name}' is already used")
        if library.category and other.category == library.category:
            problems.append(f"category '{library.category}' already belongs to library '{other.name}'")
        pairs = (
            ("source folder", library.source_dir, "source folder", other.source_dir),
            ("destination folder", library.dest_dir, "destination folder", other.dest_dir),
            ("destination folder", library.dest_dir, "source folder", other.source_dir),
            ("source folder", library.source_dir, "destination folder", other.dest_dir),
        )
        for mine_label, mine, theirs_label, theirs in pairs:
            if is_within(Path(mine), Path(theirs)) or is_within(Path(theirs), Path(mine)):
                problems.append(f"the {mine_label} overlaps the {theirs_label} of library '{other.name}'")
    return problems


def slugify(name: str) -> str:
    """A category / folder name suggestion from a library name: "Comic Books" -> "comic-books"."""
    slug = re.sub(r"[^a-z0-9._-]+", "-", (name or "").strip().lower()).strip("-.")
    return slug[:60]


def suggest_destination(name: str, existing: list[Library]) -> str:
    """Next to the other libraries' destinations, e.g. /data/media/books/comics beside .../news."""
    slug = slugify(name) or "library"
    base = Path(existing[0].dest_dir).parent if existing else Path(DEFAULT_DEST_DIR).parent
    return str(base / slug)


def path_tail(path: str, parts: int = 2) -> list[str]:
    return [p for p in path.replace("\\", "/").split("/") if p][-parts:]


def suggest_jellyfin_library(jellyfin_libraries: list[dict], dest_dir: str) -> str:
    """The Jellyfin library whose folder ends like the destination (Jellyfin mounts it elsewhere)."""
    tail = path_tail(dest_dir)
    for lib in jellyfin_libraries:
        if any(path_tail(loc) == tail for loc in lib.get("locations", [])):
            return str(lib["id"])
    return ""


def jellyfin_path_hint(dest_dir: str, jellyfin_libraries: list[dict]) -> str:
    """How Jellyfin probably sees the destination, from where its other libraries live."""
    for lib in jellyfin_libraries:
        for location in lib.get("locations", []):
            parent = posixpath.dirname(location.rstrip("/"))
            if parent and parent != "/":
                return posixpath.join(parent, Path(dest_dir).name)
    return f"<Jellyfin's path to {dest_dir}>"

"""The book formats Periodica links, how their files are checked, and where their covers come from.

Archives are untrusted input: only the zip central directory and at most one small entry are read, nothing is
extracted to disk, entry names are never used as paths, XML with a DTD or entities is refused before xml.etree
sees it, and images are not decoded. RAR files are recognised by their header only (reading them would need unrar).
"""

from __future__ import annotations

import os
import posixpath
import re
import zipfile
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath
from typing import TypeVar
from xml.etree import ElementTree  # nosec B405

from .paths import UnsafePathError, ensure_no_symlinks

PDF, CBZ, CBR, EPUB = ".pdf", ".cbz", ".cbr", ".epub"
FORMATS = (CBZ, CBR, PDF, EPUB)
DEFAULT_PRIORITY = "cbz, pdf, epub, cbr"
FORMAT_LABELS = "pdf, cbz, cbr, epub"

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"
RAR_MAGICS = (b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")
EPUB_MIMETYPE = b"application/epub+zip"

MAX_ZIP_ENTRIES = 10_000
MAX_COVER_BYTES = 20 * 1024 * 1024
MAX_XML_BYTES = 1024 * 1024
MAX_RATIO = 100
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
COVER_NAMES = ("cover.jpg", "cover.png", "cover.webp")


class FormatError(Exception):
    """A file is not what its name says (or is not safe to read)."""


def book_suffix(name: str) -> str | None:
    """The book format of a file name (".pdf", ".cbz", ...), or None."""
    suffix = PurePosixPath(name).suffix.lower()
    return suffix if suffix in FORMATS else None


def strip_book_suffix(name: str) -> str:
    suffix = book_suffix(name)
    return name[: -len(suffix)] if suffix else name


# --- priority ------------------------------------------------------------------------------------------
def parse_priority(text: str) -> str:
    """Normalise a format priority: known formats once each, the missing ones appended in default order."""
    order: list[str] = []
    for raw in re.split(r"[\s,;>]+", (text or "").strip().lower()):
        item = raw.lstrip(".")
        if not item:
            continue
        if f".{item}" not in FORMATS:
            raise ValueError(f"'{raw}' is not a supported format ({FORMAT_LABELS})")
        if item in order:
            raise ValueError(f"{item} is listed twice")
        order.append(item)
    for item in DEFAULT_PRIORITY.split(", "):
        if item not in order:
            order.append(item)
    return ", ".join(order)


def priority_suffixes(text: str) -> list[str]:
    return [f".{item}" for item in parse_priority(text).split(", ")]


T = TypeVar("T")


def by_priority(items: Iterable[T], suffix_of: Callable[[T], str], priority: str) -> list[T]:
    """Items ordered best first (a stable sort, so equal formats keep their order)."""
    order = {suffix: rank for rank, suffix in enumerate(priority_suffixes(priority))}
    return sorted(items, key=lambda item: order.get(suffix_of(item), len(order)))


# --- checking a source file ----------------------------------------------------------------------------
def _is_image_name(name: str) -> bool:
    base = posixpath.basename(name)
    return (name.lower().endswith(IMAGE_SUFFIXES) and not base.startswith(".")
            and not name.startswith("__MACOSX/") and "/__MACOSX/" not in name)


def _open_zip(path: Path) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise FormatError(f"not a readable zip archive: {exc}") from exc
    if len(archive.infolist()) > MAX_ZIP_ENTRIES:
        archive.close()
        raise FormatError(f"archive has more than {MAX_ZIP_ENTRIES} entries")
    return archive


def _read_entry(archive: zipfile.ZipFile, name: str, limit: int) -> bytes:
    """One entry, refusing large or suspiciously compressed ones; the read itself is capped too."""
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise FormatError(f"{name} is missing from the archive") from exc
    if info.file_size > limit:
        raise FormatError(f"{name} is too large ({info.file_size} bytes)")
    if info.compress_size and info.file_size / info.compress_size > MAX_RATIO:
        raise FormatError(f"{name} is compressed suspiciously well")
    try:
        with archive.open(info) as fh:
            data = fh.read(limit + 1)
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError, NotImplementedError) as exc:
        raise FormatError(f"cannot read {name}: {exc}") from exc
    if len(data) > limit:
        raise FormatError(f"{name} is larger than it says")
    return data


def detect(path: Path, suffix: str) -> str:
    """The real format of a file named with ``suffix``; raises FormatError if it isn't one.

    A .cbr that is really a zip (common) is reported as .cbz, so its cover can be read.
    """
    with open(path, "rb") as fh:
        head = fh.read(8)
    if suffix == PDF:
        if not head.startswith(PDF_MAGIC):
            raise FormatError("not a PDF (bad header)")
        return PDF
    if suffix == CBR and any(head.startswith(magic) for magic in RAR_MAGICS):
        return CBR
    if suffix in (CBZ, CBR, EPUB):
        if not head.startswith(ZIP_MAGIC):
            raise FormatError(f"not a {suffix[1:]} file (bad header)")
        with _open_zip(path) as archive:
            names = archive.namelist()
            if suffix == EPUB:
                if "mimetype" not in names or _read_entry(archive, "mimetype", 100).strip() != EPUB_MIMETYPE:
                    raise FormatError("not an epub (no epub mimetype)")
                return EPUB
            if not any(_is_image_name(name) for name in names):
                raise FormatError("comic archive has no page images")
            return CBZ
    raise FormatError(f"unsupported format {suffix}")


def check_source(path: Path, source_root: Path) -> str:
    """Verify a source file is a regular, non-symlinked book inside the source root; return its real format."""
    ensure_no_symlinks(path, source_root)
    if not os.path.isfile(path) or os.path.islink(path):
        raise UnsafePathError(f"not a regular file: {path}")
    suffix = book_suffix(path.name)
    if suffix is None:
        raise FormatError(f"not a supported book file: {path.name}")
    if os.lstat(path).st_size < len(PDF_MAGIC):
        raise FormatError(f"file too small to be a book: {path}")
    try:
        return detect(path, suffix)
    except OSError as exc:
        raise FormatError(f"cannot read {path.name}: {exc}") from exc


# --- covers ------------------------------------------------------------------------------------------------
def image_type(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _natural_key(name: str) -> list:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def _xml(data: bytes) -> ElementTree.Element:
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise FormatError("XML with a DTD is not read")
    try:
        # No DTD (checked above) and size-limited by the caller.
        return ElementTree.fromstring(data)  # noqa: S314  # nosec B314
    except ElementTree.ParseError as exc:
        raise FormatError(f"bad XML: {exc}") from exc


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _epub_cover_name(archive: zipfile.ZipFile) -> str:
    container = _xml(_read_entry(archive, "META-INF/container.xml", MAX_XML_BYTES))
    rootfile = next((el.get("full-path") for el in container.iter() if _local(el.tag) == "rootfile"), None)
    if not rootfile:
        raise FormatError("epub has no package file")
    opf = _xml(_read_entry(archive, rootfile, MAX_XML_BYTES))
    items = {el.get("id"): el for el in opf.iter() if _local(el.tag) == "item"}
    href = next((el.get("href") for el in items.values() if "cover-image" in (el.get("properties") or "").split()),
                None)
    if href is None:
        cover_id = next((el.get("content") for el in opf.iter()
                         if _local(el.tag) == "meta" and el.get("name") == "cover"), None)
        item = items.get(cover_id) if cover_id else None
        href = item.get("href") if item is not None else None
    if not href:
        raise FormatError("epub names no cover")
    href = href.split("#", 1)[0]
    if href.startswith("/") or "\\" in href or "://" in href:
        raise FormatError("epub cover path is not relative")
    name = posixpath.normpath(posixpath.join(posixpath.dirname(rootfile), href))
    if name.startswith("../") or name == "..":
        raise FormatError("epub cover path leaves the book")
    return name


def extract_cover(path: Path, fmt: str, max_bytes: int = MAX_COVER_BYTES) -> tuple[bytes, str] | None:
    """The cover image of a cbz/epub as (bytes, ".jpg"/".png"/".webp"), or None when it has none we can use.

    Raises FormatError for an archive that cannot be read safely.
    """
    if fmt not in (CBZ, EPUB):
        return None
    with _open_zip(path) as archive:
        if fmt == CBZ:
            images = sorted((n for n in archive.namelist() if _is_image_name(n)), key=_natural_key)
            if not images:
                return None
            name = images[0]
        else:
            name = _epub_cover_name(archive)
        data = _read_entry(archive, name, max_bytes)
    kind = image_type(data)
    return (data, kind) if kind else None


def find_cover(issue_dir: Path) -> Path | None:
    """The cover file in an issue folder (cover.jpg, .png or .webp), never a symlink."""
    for name in COVER_NAMES:
        candidate = issue_dir / name
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    return None


MEDIA_TYPES = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

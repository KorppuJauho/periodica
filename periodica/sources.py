"""Where a scan finds downloads: qBittorrent's API (one category), or the source folder itself.

Both produce ``Download`` records with a file list relative to ``save_path``, so the scanner
treats a torrent and a folder entry the same way.

Folder mode has no download client to say what is finished, so a download counts as finished
when nothing in it has changed for the settle time, or when a finished-download API call
named it, and never while it still holds a partial file.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import posixpath
import stat
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .paths import PathMapping, is_within
from .qbittorrent import QbitClient, Torrent

log = logging.getLogger(__name__)

# Category stored for folder downloads. Never a real qBittorrent category (those are non-empty),
# so a manual delete, which requires the configured category, always refuses them.
FOLDER_CATEGORY = ""
FOLDER_STATE_DONE = "folder"
FOLDER_STATE_SETTLING = "settling"
PARTIAL_SUFFIXES = (".!qb", ".part", ".crdownload", ".tmp")
MAX_DEPTH = 6
MAX_FILES = 5000
MAX_REMOTE_PATH = 4096
# Finished-download API calls remembered for folder mode: {download id: time of the call}.
API_FINISHED_KEY = "api_finished"
API_FINISHED_MAX = 500


@dataclass(frozen=True)
class SourceFile:
    name: str   # relative to the download's save_path, POSIX separators
    size: int


@dataclass(frozen=True)
class Download:
    hash: str
    name: str
    category: str
    save_path: str
    state: str
    complete: bool
    completion_on: float | None
    size: int
    problem: str | None = None   # the download cannot be read safely; nothing in it is linked

    @classmethod
    def from_torrent(cls, t: Torrent) -> Download:
        return cls(hash=t.hash, name=t.name, category=t.category, save_path=t.save_path, state=t.state,
                   complete=t.complete, completion_on=t.completion_on, size=t.size)


class QbitSource:
    """Torrents in one category. qBittorrent reports its paths; the mapping translates them."""

    def __init__(self, client: QbitClient, mapping: PathMapping, category: str):
        self.client = client
        self.mapping = mapping
        self.category = category

    def downloads(self) -> list[Download]:
        return [Download.from_torrent(t) for t in self.client.torrents(self.category)]

    def files(self, download: Download) -> list[SourceFile]:
        return [SourceFile(f.name, f.size) for f in self.client.files(download.hash)]

    def local_path(self, download: Download, rel: str) -> str:
        return self.mapping.to_local(posixpath.join(download.save_path, rel))


def folder_download_id(name: str, library_id: int = 1) -> str:
    """Stable 40-hex id for a top-level entry, shaped like a torrent hash so the same routes accept it.

    Library 1 keeps the id it had before libraries existed, so its folder-mode history stays valid.
    """
    prefix = "folder:" if library_id == 1 else f"folder:{library_id}:"
    key = prefix + unicodedata.normalize("NFC", name)
    return hashlib.sha1(key.encode("utf-8"), usedforsecurity=False).hexdigest()


@dataclass
class _Entry:
    files: list[SourceFile] = field(default_factory=list)
    newest: float = 0.0
    size: int = 0
    partial: bool = False
    problem: str | None = None


class FolderSource:
    """Every visible entry directly inside the source folder is one download."""

    def __init__(self, source_root: Path, settle_seconds: float, now: float | None = None,
                 finished: set[str] | None = None, library_id: int = 1):
        self.root = Path(source_root)
        self.library_id = library_id
        self.settle_seconds = settle_seconds
        self.now = time.time() if now is None else now
        self.finished = finished or set()
        self._files: dict[str, list[SourceFile]] = {}

    def downloads(self) -> list[Download]:
        result = []
        with os.scandir(self.root) as it:
            entries = sorted(it, key=lambda e: e.name)
        for entry in entries:
            if entry.name.startswith("."):
                continue
            ident = folder_download_id(entry.name, self.library_id)
            info = self._read(entry)
            self._files[ident] = info.files
            settled = self.now - info.newest >= self.settle_seconds
            complete = not info.partial and (settled or ident in self.finished)
            result.append(Download(
                hash=ident, name=entry.name, category=FOLDER_CATEGORY, save_path=str(self.root),
                state=FOLDER_STATE_DONE if complete else FOLDER_STATE_SETTLING, complete=complete,
                completion_on=info.newest or None, size=info.size, problem=info.problem,
            ))
        return result

    def files(self, download: Download) -> list[SourceFile]:
        return self._files.get(download.hash, [])

    def local_path(self, download: Download, rel: str) -> str:
        return str(self.root.joinpath(*rel.split("/")))

    def _read(self, entry: os.DirEntry) -> _Entry:
        info = _Entry()
        if entry.is_symlink():
            info.problem = "symbolic link (not followed)"
            return info
        st = entry.stat(follow_symlinks=False)
        if stat.S_ISREG(st.st_mode):
            self._add(info, entry.name, entry.name, st)
            return info
        if not stat.S_ISDIR(st.st_mode):
            info.problem = "not a regular file or folder"
            return info
        info.newest = st.st_mtime
        base = Path(entry.path)
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            rel_dir = Path(dirpath).relative_to(base)
            depth = len(rel_dir.parts)
            with contextlib.suppress(OSError):
                # A rename (qBittorrent's "x.pdf.!qB" -> "x.pdf") changes the folder, not the file.
                info.newest = max(info.newest, os.lstat(dirpath).st_mtime)
            for name in list(dirnames):
                full = Path(dirpath, name)
                if name.startswith("."):
                    dirnames.remove(name)
                elif full.is_symlink():
                    dirnames.remove(name)
                    link = full.relative_to(self.root).as_posix()
                    info.problem = info.problem or f"symbolic link {link} (not followed)"
                elif depth + 1 > MAX_DEPTH:
                    dirnames.remove(name)
                    info.problem = info.problem or f"folders nested deeper than {MAX_DEPTH} levels"
            for name in filenames:
                if name.startswith("."):
                    continue
                full = Path(dirpath, name)
                rel = full.relative_to(self.root).as_posix()
                try:
                    fst = os.lstat(full)
                except OSError:
                    continue   # vanished while walking; the next scan sees the final state
                if stat.S_ISLNK(fst.st_mode):
                    info.problem = info.problem or f"symbolic link {rel} (not followed)"
                    continue
                if not stat.S_ISREG(fst.st_mode):
                    continue
                self._add(info, name, rel, fst)
                if len(info.files) > MAX_FILES:
                    info.problem = f"more than {MAX_FILES} files"
                    info.files = []
                    return info
        return info

    @staticmethod
    def _add(info: _Entry, name: str, rel: str, st: os.stat_result) -> None:
        info.newest = max(info.newest, st.st_mtime)
        if name.lower().endswith(PARTIAL_SUFFIXES):
            info.partial = True   # still downloading: not listed, and the download is not finished
            return
        info.files.append(SourceFile(rel, st.st_size))
        info.size += st.st_size


def download_for_path(remote_path: str, mapping: PathMapping, source_root: Path,
                      library_id: int = 1) -> tuple[str, str] | None:
    """The folder download a finished-download call refers to, as (id, top-level name); None if outside."""
    if (not isinstance(remote_path, str) or not remote_path.startswith("/") or len(remote_path) > MAX_REMOTE_PATH
            or "\x00" in remote_path or "\\" in remote_path or ".." in remote_path.split("/")):
        return None
    local = mapping.to_local(remote_path)
    root = os.path.realpath(source_root)
    if not is_within(local, root):
        return None
    rel = os.path.relpath(os.path.realpath(local), root)
    top = rel.split(os.sep)[0]
    if top in ("", ".", "..") or top.startswith("."):
        return None
    return folder_download_id(top, library_id), top

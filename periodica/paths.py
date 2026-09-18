"""Safe path handling. All filesystem names coming from torrents are untrusted."""

from __future__ import annotations

import os
import posixpath
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class UnsafePathError(ValueError):
    pass


_FORBIDDEN_CHARS = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')


def sanitize_component(name: str, max_len: int = 120) -> str:
    """Turn arbitrary text into a single safe path component."""
    name = unicodedata.normalize("NFC", name)
    name = _FORBIDDEN_CHARS.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    if not name or name in {".", ".."}:
        raise UnsafePathError("empty or invalid name")
    return name


def is_within(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    """True if path (after resolving symlinks) is root or inside it."""
    real_path = os.path.realpath(path)
    real_root = os.path.realpath(root)
    if real_path == real_root:
        return True
    return real_path.startswith(real_root.rstrip(os.sep) + os.sep)


def ensure_no_symlinks(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> None:
    """Raise if path is outside root or any existing component below root is a symlink."""
    abs_root = Path(os.path.abspath(root))
    abs_path = Path(os.path.abspath(path))
    try:
        rel = abs_path.relative_to(abs_root)
    except ValueError as exc:
        raise UnsafePathError(f"{abs_path} is outside {abs_root}") from exc
    current = abs_root
    for part in rel.parts:
        if part in {"", ".", ".."}:
            raise UnsafePathError(f"invalid component in {abs_path}")
        current = current / part
        if current.is_symlink():
            raise UnsafePathError(f"refusing symlink: {current}")
        if not os.path.lexists(current):
            break
    if not is_within(abs_path, abs_root):
        raise UnsafePathError(f"{abs_path} resolves outside {abs_root}")


def safe_child(root: str | os.PathLike[str], *parts: str) -> Path:
    """Join already-sanitised components under root, verifying the result stays inside."""
    result = Path(root)
    for part in parts:
        if part != sanitize_component(part, max_len=max(len(part), 1)):
            raise UnsafePathError(f"unsanitised component: {part!r}")
        result = result / part
    ensure_no_symlinks(result, root)
    return result


def validate_torrent_relpath(name: str) -> PurePosixPath:
    """Validate a file path reported by qBittorrent (relative to the torrent save path)."""
    if not name or "\x00" in name or "\\" in name:
        raise UnsafePathError(f"invalid torrent file path: {name!r}")
    # Check the raw components: PurePosixPath would silently collapse "a//b" and "./a".
    if name.startswith("/") or any(part in {"", ".", ".."} for part in name.split("/")):
        raise UnsafePathError(f"invalid torrent file path: {name!r}")
    return PurePosixPath(name)


@dataclass(frozen=True)
class PathMapping:
    """Maps paths as qBittorrent sees them to paths inside this container (like Sonarr)."""

    remote: str = ""
    local: str = ""

    def to_local(self, remote_path: str) -> str:
        path = posixpath.normpath(remote_path)
        if not self.remote or not self.local:
            return path
        remote_root = posixpath.normpath(self.remote)
        if path == remote_root:
            return posixpath.normpath(self.local)
        prefix = remote_root.rstrip("/") + "/"
        if path.startswith(prefix):
            return posixpath.join(posixpath.normpath(self.local), path[len(prefix):])
        return path


def same_filesystem(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    return os.stat(a).st_dev == os.stat(b).st_dev

"""Cover images rendered locally from page 1 of the PDF.

PDFs from torrents are untrusted input, so rendering happens in a separate ``pdftoppm``
process with a timeout and hard resource limits, never inside the web process.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404
import tempfile
import threading
from pathlib import Path

from .linker import MARKER_NAME
from .metadata import write_atomic
from .paths import ensure_no_symlinks

JPEG_MAGIC = b"\xff\xd8\xff"
MAX_COVER_BYTES = 10 * 1024 * 1024
RENDER_TIMEOUT_S = 60

_render_lock = threading.Semaphore(1)


class CoverError(Exception):
    pass


def _limits(memory_mb: int):  # pragma: no cover - runs in the child process
    def apply() -> None:
        import resource

        mem = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_CPU, (RENDER_TIMEOUT_S, RENDER_TIMEOUT_S))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_COVER_BYTES * 2, MAX_COVER_BYTES * 2))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return apply


def pdftoppm_available() -> bool:
    return shutil.which("pdftoppm") is not None


def render_cover(pdf: Path, out: Path, width: int, memory_mb: int = 1024) -> None:
    """Render page 1 of pdf to a JPEG at out (atomically)."""
    exe = shutil.which("pdftoppm")
    if exe is None:
        raise CoverError("pdftoppm (poppler-utils) is not installed")
    pdf = pdf.resolve()
    with _render_lock, tempfile.TemporaryDirectory(prefix="nl-cover-") as tmp:
        prefix = os.path.join(tmp, "cover")
        cmd = [
            exe, "-q", "-jpeg", "-jpegopt", "quality=85",
            "-f", "1", "-l", "1", "-singlefile",
            "-scale-to-x", str(int(width)), "-scale-to-y", "-1",
            str(pdf), prefix,
        ]
        kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
            "timeout": RENDER_TIMEOUT_S,
            "env": {"PATH": "/usr/bin:/bin", "HOME": tmp},
            "cwd": tmp,
        }
        if os.name == "posix":
            kwargs["preexec_fn"] = _limits(memory_mb)
        try:
            proc = subprocess.run(cmd, check=False, **kwargs)  # nosec B603
        except subprocess.TimeoutExpired as exc:
            raise CoverError("rendering timed out") from exc
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip()[:200]
            raise CoverError(f"pdftoppm failed (exit {proc.returncode}): {detail}")
        result = Path(prefix + ".jpg")
        if not result.is_file() or result.is_symlink():
            raise CoverError("pdftoppm produced no image")
        size = result.stat().st_size
        if size == 0 or size > MAX_COVER_BYTES:
            raise CoverError(f"unexpected cover size: {size} bytes")
        data = result.read_bytes()
    if not data.startswith(JPEG_MAGIC):
        raise CoverError("rendered cover is not a JPEG")
    write_atomic(out, data)


def update_folder_art(paper_dir: Path, dest_root: Path) -> bool:
    """Copy the newest issue's cover to <paper>/folder.<ext> so the paper folder has artwork."""
    from .formats import find_cover

    if not paper_dir.is_dir() or paper_dir.is_symlink():
        return False
    ensure_no_symlinks(paper_dir, dest_root)
    covers = sorted(
        (
            (d.name, cover) for d in paper_dir.iterdir()
            if d.is_dir() and not d.is_symlink() and (d / MARKER_NAME).is_file()
            and (cover := find_cover(d)) is not None
        ),
        reverse=True,
    )
    if not covers:
        return False
    newest = covers[0][1]
    target = paper_dir / f"folder{newest.suffix}"
    changed = write_atomic(target, newest.read_bytes())
    for suffix in (".jpg", ".png", ".webp"):
        stale = paper_dir / f"folder{suffix}"
        if stale != target and stale.is_file() and not stale.is_symlink():
            stale.unlink()   # one folder image, or Jellyfin may pick the old one
            changed = True
    return changed

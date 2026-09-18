"""Hardlinking issue files (pdf, cbz, cbr, epub) into the library layout and managing issue folders.

Layout (verified against Jellyfin's BookResolver: a folder with exactly one book file is one book,
and LocalImageProvider then picks up cover.jpg/folder.jpg from that folder):

    <dest>/<Paper>/folder.jpg
    <dest>/<Paper>/<Paper> 2026-09-15/<Paper> 2026-09-15.pdf      (or .cbz / .cbr / .epub; one per folder)
    <dest>/<Paper>/<Paper> 2026-09-15/metadata.opf
    <dest>/<Paper>/<Paper> 2026-09-15/cover.jpg                    (or cover.png / cover.webp)
    <dest>/<Paper>/<Paper> 2026-09-15/.periodica.json   (ownership marker)
"""

from __future__ import annotations

import errno
import json
import os
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .issues import IssueId
from .paths import UnsafePathError, ensure_no_symlinks, is_within, safe_child, sanitize_component

MARKER_NAME = ".periodica.json"
# Jellyfin skips (and never cleans up) a library whose root folder is empty, assuming an unmounted
# drive. This hidden file keeps the root non-empty; Jellyfin ignores dotfiles when building the library.
LIBRARY_KEEP_NAME = ".periodica-library"
LIBRARY_KEEP_TEXT = (
    b"Managed by Periodica. Keeps this folder non-empty so Jellyfin still scans the library\n"
    b"(and removes deleted issues) when no newspapers are present. Safe to leave in place.\n"
)


class LinkError(Exception):
    pass


class CrossDeviceError(LinkError):
    pass


class LinkConflictError(LinkError):
    pass


@dataclass(frozen=True)
class IssuePaths:
    paper_dir: Path
    issue_dir: Path
    book: Path     # the issue file: <label>.pdf, .cbz, .cbr or .epub
    opf: Path
    cover: Path

    @property
    def marker(self) -> Path:
        return self.issue_dir / MARKER_NAME


def issue_paths(dest_root: Path, display_name: str, issue: IssueId | date, suffix: str = ".pdf") -> IssuePaths:
    issue = issue if isinstance(issue, IssueId) else IssueId.for_day(issue)
    paper = sanitize_component(display_name)
    label = sanitize_component(f"{paper} {issue.label}")
    paper_dir = safe_child(dest_root, paper)
    issue_dir = safe_child(dest_root, paper, label)
    return IssuePaths(
        paper_dir=paper_dir,
        issue_dir=issue_dir,
        book=issue_dir / f"{label}{suffix}",
        opf=issue_dir / "metadata.opf",
        cover=issue_dir / "cover.jpg",
    )


def link_book(source: Path, paths: IssuePaths, dest_root: Path) -> bool:
    """Create the issue folder and hardlink the issue file. Returns True if a new link was made."""
    ensure_no_symlinks(paths.issue_dir, dest_root)
    paths.issue_dir.mkdir(parents=True, exist_ok=True)
    ensure_no_symlinks(paths.book, dest_root)
    if os.path.lexists(paths.book):
        return _verify_existing(source, paths.book)
    try:
        os.link(source, paths.book, follow_symlinks=False)
    except FileExistsError:
        return _verify_existing(source, paths.book)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise CrossDeviceError(
                "source and destination are on different filesystems/mounts; mount the common parent "
                "(e.g. /srv/data:/data) so hardlinks work"
            ) from exc
        raise LinkError(f"hardlink failed: {exc}") from exc
    return True


def replace_with_link(source: Path, paths: IssuePaths, dest_root: Path) -> None:
    """Replace the library file with a hardlink to the download (the user's Fix for a copy).

    Only inside an issue folder Periodica made; the new link is made under a temporary name first and
    moved over the old file in one step, so the folder never lacks its file.
    """
    if not is_managed_issue_dir(paths.issue_dir, dest_root):
        raise UnsafePathError(f"{paths.issue_dir} has no Periodica marker; not replacing its file")
    ensure_no_symlinks(paths.book, dest_root)
    temp = paths.issue_dir / f".{paths.book.name}.nl-tmp"
    if os.path.lexists(temp):
        os.unlink(temp)
    try:
        os.link(source, temp, follow_symlinks=False)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise CrossDeviceError("source and destination are on different filesystems/mounts") from exc
        raise LinkError(f"hardlink failed: {exc}") from exc
    try:
        os.replace(temp, paths.book)
    except OSError:
        os.unlink(temp)
        raise


def _verify_existing(source: Path, target: Path) -> bool:
    if os.path.islink(target):
        raise UnsafePathError(f"refusing symlink at {target}")
    if os.path.samefile(source, target):
        return False
    raise LinkConflictError(f"{target} exists and is a different file; not overwriting")


def write_marker(paths: IssuePaths, paper: str, issue: IssueId | date, torrent_hash: str) -> None:
    from .metadata import write_atomic

    issue = issue if isinstance(issue, IssueId) else IssueId.for_day(issue)
    data = json.dumps(
        {"paper": paper, "date": issue.nominal_date.isoformat(), "key": issue.key, "period": issue.period,
         "torrent": torrent_hash, "v": 2},
        ensure_ascii=False,
    ).encode()
    write_atomic(paths.marker, data)


def is_managed_issue_dir(issue_dir: Path, dest_root: Path) -> bool:
    try:
        ensure_no_symlinks(issue_dir, dest_root)
    except UnsafePathError:
        return False
    marker = issue_dir / MARKER_NAME
    return (
        issue_dir.is_dir()
        and not issue_dir.is_symlink()
        and marker.is_file()
        and not marker.is_symlink()
        and issue_dir.parent.parent.resolve() == dest_root.resolve()
    )


def remove_issue_dir(issue_dir: Path, dest_root: Path) -> bool:
    """Delete an issue folder we created. Refuses anything not carrying our marker."""
    ensure_library_keep_file(dest_root)
    if not os.path.lexists(issue_dir):
        return False
    if not is_within(issue_dir, dest_root) or not is_managed_issue_dir(issue_dir, dest_root):
        raise UnsafePathError(f"refusing to delete unmanaged folder: {issue_dir}")
    shutil.rmtree(issue_dir)
    cleanup_paper_dir(issue_dir.parent, dest_root)
    return True


def move_issue_dir(old_dir: Path, new_paths: IssuePaths, dest_root: Path) -> None:
    """Move an issue folder (e.g. after renaming a paper) and rename its issue file to match."""
    if not is_managed_issue_dir(old_dir, dest_root):
        raise UnsafePathError(f"refusing to move unmanaged folder: {old_dir}")
    if os.path.lexists(new_paths.issue_dir):
        raise LinkConflictError(f"{new_paths.issue_dir} already exists")
    ensure_no_symlinks(new_paths.paper_dir, dest_root)
    new_paths.paper_dir.mkdir(parents=True, exist_ok=True)
    os.rename(old_dir, new_paths.issue_dir)
    from .formats import book_suffix

    for entry in sorted(new_paths.issue_dir.iterdir()):
        if book_suffix(entry.name) and entry.is_file() and not entry.is_symlink():
            target = new_paths.issue_dir / (new_paths.book.stem + entry.suffix.lower())
            if entry.name != target.name:
                os.rename(entry, target)
            break
    cleanup_paper_dir(old_dir.parent, dest_root)


def prepare_library_root(dest_dir: str, data_root: Path) -> bool:
    """Create the destination folder (if its parent exists) and its keep-file, so a Jellyfin library created
    before the first scan is not empty. Only inside the data root, never through symlinks."""
    dest = Path(dest_dir)
    if not dest_dir or not is_within(dest, data_root) or not dest.parent.is_dir():
        return False
    ensure_no_symlinks(dest, data_root)
    dest.mkdir(exist_ok=True)
    ensure_library_keep_file(dest)
    return True


def ensure_library_keep_file(dest_root: Path) -> None:
    """Make sure the library root is never empty (see LIBRARY_KEEP_NAME)."""
    from .metadata import write_atomic

    if not dest_root.is_dir() or dest_root.is_symlink():
        return
    keep = dest_root / LIBRARY_KEEP_NAME
    if keep.is_symlink():
        raise UnsafePathError(f"refusing symlink at {keep}")
    if not keep.is_file():
        write_atomic(keep, LIBRARY_KEEP_TEXT)


def cleanup_paper_dir(paper_dir: Path, dest_root: Path) -> None:
    """Remove a paper folder once it holds no issues (only our folder.jpg, or nothing)."""
    if paper_dir.resolve() == dest_root.resolve() or not paper_dir.is_dir() or paper_dir.is_symlink():
        return
    if not is_within(paper_dir, dest_root):
        return
    entries = list(paper_dir.iterdir())
    if any(e.name != "folder.jpg" for e in entries):
        return
    for entry in entries:
        if entry.is_file() and not entry.is_symlink():
            entry.unlink()
    paper_dir.rmdir()

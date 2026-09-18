"""Library health: is a linked issue still whole in the library?

A scan never repairs a damaged issue on its own; it records the problem and waits for the user's Fix.
"""

from __future__ import annotations

import os
from pathlib import Path

from .formats import find_cover
from .linker import IssuePaths

MISSING = "missing"
COPY = "copy"
METADATA = "metadata"
COVER = "cover"

PROBLEMS = {
    MISSING: "issue file or folder missing from the library",
    COPY: "the file in the library is not a link to the download (a copy or another file)",
    METADATA: "metadata.opf missing",
    COVER: "cover image missing",
}
FIXED = {
    MISSING: "linked again",
    COPY: "replaced with a link to the download",
    METADATA: "metadata written again",
    COVER: "cover rendered again",
}


def _plain_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def paths_of(issue_dir: Path, suffix: str = ".pdf") -> IssuePaths:
    """The files of an issue folder, from the folder alone (its name is the issue label)."""
    return IssuePaths(paper_dir=issue_dir.parent, issue_dir=issue_dir, book=issue_dir / f"{issue_dir.name}{suffix}",
                      opf=issue_dir / "metadata.opf", cover=issue_dir / "cover.jpg")


def check_issue(paths: IssuePaths, source: Path | None, cover_expected: bool) -> str | None:
    """The first problem of a linked issue, or None when it is intact.

    Without a source (the download is gone) only the folder and the issue file are checked.
    """
    if not paths.issue_dir.is_dir() or paths.issue_dir.is_symlink() or not os.path.lexists(paths.book):
        return MISSING
    if paths.book.is_symlink():
        return COPY
    if source is None:
        return None
    try:
        same = os.path.samefile(source, paths.book)
    except OSError:
        same = True  # the download itself is unreadable: reported as an issue error, not as library health
    if not same:
        return COPY
    if not _plain_file(paths.opf):
        return METADATA
    if cover_expected and find_cover(paths.issue_dir) is None:
        return COVER
    return None

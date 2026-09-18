"""The project's own name is used consistently; the name it was renamed from must not come back."""

from __future__ import annotations

import re
from pathlib import Path

import periodica

ROOT = Path(__file__).resolve().parent.parent
# Spelled in pieces so this file itself doesn't contain the old name.
OLD = re.compile("news" + r"[ _-]?" + "linker", re.IGNORECASE)
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache", "node_modules",
             "img", "build", "dist", "config"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".ico", ".gz", ".pdf", ".db", ".pyc"}


def project_files():
    for path in ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.is_file() and path.suffix.lower() not in SKIP_SUFFIXES and path.name != "LICENSE":
            yield path


def test_no_trace_of_the_old_name():
    offenders = []
    for path in project_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if OLD.search(text) or OLD.search(path.name):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], f"the old project name is still in: {offenders}"


def test_version_is_a_release_number():
    assert re.fullmatch(r"\d+\.\d+\.\d+", periodica.__version__)

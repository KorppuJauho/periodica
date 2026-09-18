"""Parse newspaper file names such as ``Côte-Nord.Gazette.2026.09.15.pdf``.

Two kinds of name are recognised:

- a daily issue: ``Name.2026.09.15`` or ``Name 15.09.2026`` (a trailing tag is allowed);
- a year and a number: ``Name.2026.09`` or ``Name 38-2026``. Whether the number is a month or an
  issue number is decided per newspaper later (see numbering.py), because the name alone cannot
  tell. These names must end with the number: ``Name.2026.02.30`` is an invalid daily date, not
  "February 2026" with a tag.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

from .paths import UnsafePathError, sanitize_component

_SEP = r"[\s._-]"
_DAILY_PATTERNS = [
    # Name.2026.09.15 / Name 2026-09-15 (optionally followed by a tag)
    re.compile(
        rf"^(?P<paper>.+?){_SEP}+(?P<y>\d{{4}}){_SEP}(?P<m>\d{{1,2}}){_SEP}(?P<d>\d{{1,2}})(?:{_SEP}.*)?$"
    ),
    # Name 15.09.2026
    re.compile(
        rf"^(?P<paper>.+?){_SEP}+(?P<d>\d{{1,2}}){_SEP}(?P<m>\d{{1,2}}){_SEP}(?P<y>\d{{4}})(?:{_SEP}.*)?$"
    ),
]
_YEAR_NUMBER_PATTERNS = [
    # Name.2026.09 / Name 2026-38
    re.compile(rf"^(?P<paper>.+?){_SEP}+(?P<y>\d{{4}}){_SEP}(?P<n>\d{{1,3}})$"),
    # Name 09.2026 / Name 38-2026
    re.compile(rf"^(?P<paper>.+?){_SEP}+(?P<n>\d{{1,3}}){_SEP}(?P<y>\d{{4}})$"),
]
MIN_YEAR, MAX_YEAR = 1900, 2100


@dataclass(frozen=True)
class ParsedIssue:
    paper: str
    issue_date: date | None = None  # a daily issue
    year: int = 0                   # a year + number name
    number: int = 0
    period: str = ""                # set by a user's pattern (day/month/number); "" = decided later

    @property
    def is_daily(self) -> bool:
        return self.issue_date is not None


def clean_paper_name(raw: str) -> str:
    name = re.sub(r"[._]+", " ", raw)
    name = re.sub(r"\s+", " ", name).strip(" -")
    return sanitize_component(name)


def _paper(raw: str) -> str | None:
    try:
        return clean_paper_name(raw)
    except UnsafePathError:
        return None


def parse_issue_filename(filename: str) -> ParsedIssue | None:
    """Return what an issue file name (pdf, cbz, cbr, epub) describes, or None if it doesn't look like one."""
    from .formats import book_suffix, strip_book_suffix

    filename = unicodedata.normalize("NFC", filename)
    if book_suffix(filename) is None:
        return None
    stem = strip_book_suffix(filename)
    for pattern in _DAILY_PATTERNS:
        match = pattern.match(stem)
        if not match:
            continue
        try:
            issue_date = date(int(match["y"]), int(match["m"]), int(match["d"]))
        except ValueError:
            continue
        if not MIN_YEAR <= issue_date.year <= MAX_YEAR:
            continue
        paper = _paper(match["paper"])
        if paper:
            return ParsedIssue(paper=paper, issue_date=issue_date)
    for pattern in _YEAR_NUMBER_PATTERNS:
        match = pattern.match(stem)
        if not match:
            continue
        year, number = int(match["y"]), int(match["n"])
        if not (MIN_YEAR <= year <= MAX_YEAR and 1 <= number <= 999):
            continue
        paper = _paper(match["paper"])
        if paper:
            return ParsedIssue(paper=paper, year=year, number=number)
    return None

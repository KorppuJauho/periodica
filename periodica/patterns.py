"""File name patterns a user teaches Periodica, e.g. ``DW.{year}.No.{number}`` for ``DW.2026.No.38.pdf``.

A pattern is written the way the name looks, with placeholders for the parts that change. It is
compiled from escaped literal text and fixed expressions: the user's text is never used as a regular
expression. The placeholders say what kind of issue a name is, so no monthly/numbered guess is needed.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

from .formats import strip_book_suffix
from .issues import DAY, MONTH, NUMBER
from .parser import MAX_YEAR, MIN_YEAR, ParsedIssue, clean_paper_name
from .paths import UnsafePathError

MAX_LENGTH = 120
MAX_NAME_LENGTH = 255      # longer names can't exist on the filesystems we read; also bounds matching time
MAX_FREE_TEXT = 2          # {paper} and {any} together
MAX_PATTERNS_PER_LIBRARY = 30
PLACEHOLDERS = {
    "year": r"(?P<year>\d{4})",
    "month": r"(?P<month>\d{1,2})",
    "day": r"(?P<day>\d{1,2})",
    "number": r"(?P<number>\d{1,4})",
    "paper": r"(?P<paper>.+?)",
    "any": r".*?",
}
KIND_LABELS = {DAY: "daily", MONTH: "monthly", NUMBER: "numbered"}
_TOKEN = re.compile(r"\{([a-z]*)\}|([\s._-]+)|([{}])|([^{}\s._-]+)")
_SEP = r"[\s._-]+"
_YEAR = re.compile(r"(?<!\d)(\d{4})(?!\d)")
_DIGITS = re.compile(r"\d+")


@dataclass(frozen=True)
class NamePattern:
    text: str
    kind: str                  # day | month | number
    has_paper: bool
    regex: re.Pattern

    def parse(self, stem: str, publication: str = "") -> ParsedIssue | None:
        """What a name (without extension) describes under this pattern, or None."""
        stem = strip_book_suffix(unicodedata.normalize("NFC", stem))
        if len(stem) > MAX_NAME_LENGTH:
            return None
        match = self.regex.fullmatch(stem)
        if match is None:
            return None
        year = int(match["year"])
        if not MIN_YEAR <= year <= MAX_YEAR:
            return None
        paper = publication
        if not paper:
            try:
                paper = clean_paper_name(match["paper"])
            except (UnsafePathError, IndexError):
                return None
        if self.kind == DAY:
            try:
                issue_date = date(year, int(match["month"]), int(match["day"]))
            except ValueError:
                return None
            return ParsedIssue(paper=paper, issue_date=issue_date, period=DAY)
        if self.kind == MONTH:
            month = int(match["month"])
            if not 1 <= month <= 12:
                return None
            return ParsedIssue(paper=paper, year=year, number=month, period=MONTH)
        number = int(match["number"])
        if not 1 <= number <= 999:
            return None
        return ParsedIssue(paper=paper, year=year, number=number, period=NUMBER)


def compile_pattern(text: str) -> NamePattern:
    """Compile a pattern, or raise ValueError with a message for the user."""
    text = strip_book_suffix(unicodedata.normalize("NFC", (text or "").strip()))
    if not text:
        raise ValueError("the pattern is empty")
    if len(text) > MAX_LENGTH:
        raise ValueError(f"the pattern is longer than {MAX_LENGTH} characters")
    if any(not ch.isprintable() for ch in text):
        raise ValueError("the pattern contains control characters")
    parts: list[str] = []
    seen: list[str] = []
    for placeholder, separator, brace, literal in _TOKEN.findall(text):
        if separator:
            parts.append(_SEP)
        elif brace:
            raise ValueError("braces are only allowed around a placeholder such as {year}")
        elif literal:
            parts.append(re.escape(literal))
        else:
            if placeholder not in PLACEHOLDERS:
                raise ValueError(f"unknown placeholder {{{placeholder}}}; use "
                                 + ", ".join(f"{{{name}}}" for name in PLACEHOLDERS))
            if placeholder != "any" and placeholder in seen:
                raise ValueError(f"{{{placeholder}}} can be used only once")
            seen.append(placeholder)
            parts.append(PLACEHOLDERS[placeholder])
    if "year" not in seen:
        raise ValueError("the pattern needs {year}")
    if seen.count("paper") + seen.count("any") > MAX_FREE_TEXT:
        raise ValueError(f"use {{paper}} and {{any}} at most {MAX_FREE_TEXT} times together")
    if "number" in seen and ("month" in seen or "day" in seen):
        raise ValueError("{number} is an issue number; it can't be combined with {month} or {day}")
    if "day" in seen and "month" not in seen:
        raise ValueError("{day} needs {month} as well")
    kind = DAY if "day" in seen else MONTH if "month" in seen else NUMBER if "number" in seen else ""
    if not kind:
        raise ValueError("the pattern needs {number}, or {month} (with {day} for a daily issue)")
    return NamePattern(text=text, kind=kind, has_paper="paper" in seen,
                       regex=re.compile("".join(parts), re.IGNORECASE))


def validate_publication(pattern: NamePattern, publication: str) -> str:
    """The fixed publication name for a pattern ("" = taken from {paper})."""
    publication = " ".join((publication or "").split())
    if not publication:
        if not pattern.has_paper:
            raise ValueError("give the publication a name (or use {paper} in the pattern)")
        return ""
    try:
        return clean_paper_name(publication)
    except UnsafePathError as exc:
        raise ValueError(f"the publication name is not valid: {exc}") from exc


def suggest_pattern(filename: str) -> tuple[str, str, str]:
    """(pattern, publication name, kind) guessed from a file name; ("", "", "") if nothing fits.

    The text before the year stays literal and, cleaned up, becomes the publication name; the numbers
    after it become {month}/{day} when they form a date, otherwise {number}.
    """
    stem = strip_book_suffix(unicodedata.normalize("NFC", filename).replace("{", "").replace("}", ""))
    year_match = next((m for m in _YEAR.finditer(stem) if MIN_YEAR <= int(m.group(1)) <= MAX_YEAR), None)
    if year_match is None:
        return "", "", ""
    before, after = stem[:year_match.start()], stem[year_match.end():]
    numbers = list(_DIGITS.finditer(after))
    if not numbers:
        return "", "", ""
    kind = NUMBER
    if len(numbers) >= 2:
        try:
            date(int(year_match.group(1)), int(numbers[0].group()), int(numbers[1].group()))
            kind = DAY
        except ValueError:
            kind = NUMBER
    if kind == DAY:
        first, second = numbers[0], numbers[1]
        tail = after[:first.start()] + "{month}" + after[first.end():second.start()] + "{day}"
        rest = after[second.end():]
    else:
        first = numbers[0]
        tail = after[:first.start()] + "{number}"
        rest = after[first.end():]
    pattern = before + "{year}" + tail + ("{any}" if rest.strip(" ._-") else "")
    try:
        publication = clean_paper_name(before) if before.strip(" ._-") else ""
    except UnsafePathError:
        publication = ""
    try:
        compile_pattern(pattern)
    except ValueError:
        return "", "", ""
    return pattern, publication, kind

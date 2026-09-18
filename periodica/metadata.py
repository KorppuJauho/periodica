"""Local metadata for Jellyfin (built-in OPF reader in Jellyfin 12; Bookshelf plugin on 10.11).

Verified against jellyfin-plugin-bookshelf ``OpfReader``: it reads dc:title, dc:description,
dc:publisher (studio), dc:date (premiere date), dc:subject (genres), dc:language and the
``calibre:series`` / ``calibre:series_index`` / ``calibre:title_sort`` meta elements, which
must be in the OPF namespace. dc:creator is deliberately omitted: Jellyfin turns creators
into Person entries, which makes no sense for a newspaper.
"""

from __future__ import annotations

import os
import re
import string

# Only used to *write* XML, never to parse untrusted input.
import xml.etree.ElementTree as ET  # nosec B405
from datetime import date
from pathlib import Path

from . import __version__
from .issues import DAY, MONTH, NUMBER, IssueId

OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"

ET.register_namespace("", OPF_NS)
ET.register_namespace("dc", DC_NS)
ET.register_namespace("opf", OPF_NS)

WEEKDAYS = {
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "fi": ["maanantai", "tiistai", "keskiviikko", "torstai", "perjantai", "lauantai", "sunnuntai"],
    "sv": ["måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag"],
}
MONTHS = {
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"],
    "fi": ["tammikuu", "helmikuu", "maaliskuu", "huhtikuu", "toukokuu", "kesäkuu", "heinäkuu", "elokuu",
           "syyskuu", "lokakuu", "marraskuu", "joulukuu"],
    "sv": ["januari", "februari", "mars", "april", "maj", "juni", "juli", "augusti", "september", "oktober",
           "november", "december"],
}

# Which fields each kind of issue can use in its title. A monthly title has no day, and a numbered
# issue has no date at all, so offering those fields would only produce misleading titles.
TITLE_FIELDS_BY_PERIOD = {
    DAY: {"paper", "day", "month", "year", "dd", "mm", "iso", "weekday"},
    MONTH: {"paper", "month", "year", "mm", "iso", "month_name"},
    NUMBER: {"paper", "year", "number", "nn"},
}
TITLE_FIELDS = TITLE_FIELDS_BY_PERIOD[DAY]
DEFAULT_TITLE_FORMAT = "{paper} {iso}"
DEFAULT_MONTHLY_TITLE_FORMAT = "{paper} {iso}"
DEFAULT_NUMBERED_TITLE_FORMAT = "{paper} {year} #{nn}"
_SPEC_RE = re.compile(r"[<>^=]?[0-9]{0,3}d?")


def _as_issue(issue: IssueId | date) -> IssueId:
    return issue if isinstance(issue, IssueId) else IssueId.for_day(issue)


def validate_title_format(fmt: str, period: str = DAY) -> str:
    """Only allow plain named fields; blocks attribute/index access like ``{paper.__class__}``."""
    allowed = TITLE_FIELDS_BY_PERIOD[period]
    if not fmt or len(fmt) > 200:
        raise ValueError("title format must be 1-200 characters")
    try:
        parsed = list(string.Formatter().parse(fmt))
    except ValueError as exc:
        raise ValueError(f"invalid title format: {exc}") from exc
    used = set()
    for _literal, field, spec, conversion in parsed:
        if field is None:
            continue
        if field not in allowed:
            raise ValueError(f"unknown field {{{field}}}; allowed: {', '.join(sorted(allowed))}")
        if conversion is not None:
            raise ValueError("conversions (!r, !s) are not allowed")
        if spec and not _SPEC_RE.fullmatch(spec):
            raise ValueError(f"unsupported format spec: {spec!r}")
        used.add(field)
    if not used:
        raise ValueError("title format must contain at least one field, e.g. {paper}")
    return fmt


def title_values(paper: str, issue: IssueId | date, language: str) -> dict[str, object]:
    issue = _as_issue(issue)
    lang = language.split("-")[0]
    if issue.period == NUMBER:
        return {"paper": paper, "year": issue.year, "number": issue.number, "nn": f"{issue.number:02d}"}
    values: dict[str, object] = {
        "paper": paper,
        "month": issue.month,
        "year": issue.year,
        "mm": f"{issue.month:02d}",
        "iso": issue.key,
    }
    if issue.period == MONTH:
        values["month_name"] = MONTHS.get(lang, MONTHS["en"])[issue.month - 1]
        return values
    d = issue.nominal_date
    values.update({
        "day": d.day,
        "dd": f"{d.day:02d}",
        "weekday": WEEKDAYS.get(lang, WEEKDAYS["en"])[d.weekday()],
    })
    return values


def render_title(fmt: str, paper: str, issue: IssueId | date, language: str) -> str:
    issue = _as_issue(issue)
    validate_title_format(fmt, issue.period)
    return fmt.format(**title_values(paper, issue, language))


def _description(paper: str, issue: IssueId, language: str) -> str:
    values = title_values(paper, issue, language)
    if issue.period == NUMBER:
        return f"{paper} – {issue.number}/{issue.year}"
    if issue.period == MONTH:
        return f"{paper} – {values['month_name']} {issue.year}"
    return f"{paper} – {values['weekday']} {values['day']}.{issue.month}.{issue.year}"


def build_opf(paper: str, issue: IssueId | date, title: str, language: str) -> bytes:
    issue = _as_issue(issue)

    package = ET.Element(f"{{{OPF_NS}}}package", {"version": "2.0", "unique-identifier": "uid"})
    md = ET.SubElement(package, f"{{{OPF_NS}}}metadata")

    def dc(tag: str, text: str, attrs: dict[str, str] | None = None) -> None:
        el = ET.SubElement(md, f"{{{DC_NS}}}{tag}", attrs or {})
        el.text = text

    def meta(name: str, content: str) -> None:
        ET.SubElement(md, f"{{{OPF_NS}}}meta", {"name": name, "content": content})

    dc("identifier", f"periodica:{paper}:{issue.key}", {"id": "uid", f"{{{OPF_NS}}}scheme": "periodica"})
    dc("title", title)
    dc("language", language)
    dc("date", issue.opf_date)
    dc("publisher", paper)
    dc("subject", "News")
    dc("description", _description(paper, issue, language))
    meta("calibre:series", paper)
    meta("calibre:series_index", issue.series_index)
    meta("calibre:title_sort", f"{paper} {issue.key}")
    meta("periodica:version", __version__)

    return ET.tostring(package, encoding="utf-8", xml_declaration=True) + b"\n"


def write_atomic(path: Path, data: bytes) -> bool:
    """Write data to path via temp file + rename. Returns False if content was already identical."""
    if path.is_symlink():
        raise OSError(f"refusing to write through symlink: {path}")
    try:
        if path.is_file() and path.stat().st_size == len(data) and path.read_bytes() == data:
            return False
    except OSError:
        pass
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o644)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return True

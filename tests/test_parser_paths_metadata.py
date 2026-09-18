from __future__ import annotations

import unicodedata
import xml.etree.ElementTree as ET
from datetime import date

import pytest

from periodica.metadata import DC_NS, OPF_NS, build_opf, render_title, validate_title_format
from periodica.parser import parse_issue_filename
from periodica.paths import (
    PathMapping,
    UnsafePathError,
    is_within,
    sanitize_component,
    validate_torrent_relpath,
)


@pytest.mark.parametrize(
    ("filename", "paper", "day"),
    [
        ("Chronicle.2026.09.15.pdf", "Chronicle", date(2026, 9, 15)),
        ("Côte-Nord.Gazette.2026.09.15.pdf", "Côte-Nord Gazette", date(2026, 9, 15)),
        ("Evening.Post.2026.09.15.pdf", "Evening Post", date(2026, 9, 15)),
        ("Lakeside-Journal.2026.09.15.pdf", "Lakeside-Journal", date(2026, 9, 15)),
        ("Harbour-Times.2026.09.15.PDF", "Harbour-Times", date(2026, 9, 15)),
        ("City Tribune 2026-01-02.pdf", "City Tribune", date(2026, 1, 2)),
        ("City Tribune 02.01.2026.pdf", "City Tribune", date(2026, 1, 2)),
        ("Riverside_2026_03_04_SCANNED.pdf", "Riverside", date(2026, 3, 4)),
    ],
)
def test_parse_valid_names(filename, paper, day):
    parsed = parse_issue_filename(filename)
    assert parsed is not None
    assert parsed.paper == paper
    assert parsed.issue_date == day


def test_parse_normalises_decomposed_unicode():
    nfd = unicodedata.normalize("NFD", "Côte-Nord.Gazette.2026.09.15.pdf")
    parsed = parse_issue_filename(nfd)
    assert parsed is not None
    assert parsed.paper == unicodedata.normalize("NFC", "Côte-Nord Gazette")


@pytest.mark.parametrize(
    "filename",
    [
        "Chronicle.2026.09.15.nfo",
        "Chronicle.2026.02.30.pdf",  # invalid date
        "Chronicle.1850.01.01.pdf",  # implausible year
        "Readme.pdf",
        ".2026.09.15.pdf",
        "...2026.09.15.pdf",
    ],
)
def test_parse_rejects(filename):
    assert parse_issue_filename(filename) is None


def test_sanitize_component():
    assert sanitize_component('  Bad/Name:*?"<>|  ') == "Bad Name"
    with pytest.raises(UnsafePathError):
        sanitize_component("..")
    with pytest.raises(UnsafePathError):
        sanitize_component("/// ")
    assert len(sanitize_component("x" * 500)) == 120


@pytest.mark.parametrize("bad", ["", "/etc/passwd", "a/../../b", "a//b", "a\\b", "a\x00b", "./a"])
def test_torrent_relpath_rejects(bad):
    with pytest.raises(UnsafePathError):
        validate_torrent_relpath(bad)


def test_torrent_relpath_accepts_nested():
    assert str(validate_torrent_relpath("Pack 15 09 2026/Paper.2026.09.15/Paper.2026.09.15.pdf")).endswith(".pdf")


def test_path_mapping():
    m = PathMapping("/downloads", "/data/torrents")
    assert m.to_local("/downloads/news/a.pdf") == "/data/torrents/news/a.pdf"
    assert m.to_local("/downloads") == "/data/torrents"
    assert m.to_local("/downloads-other/a.pdf") == "/downloads-other/a.pdf"  # prefix must match a whole component
    assert PathMapping().to_local("/x/../y/a.pdf") == "/y/a.pdf"


def test_is_within(tmp_path):
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    assert is_within(root / "a", root)
    assert not is_within(tmp_path / "rootx", root)
    assert not is_within(root / ".." / "elsewhere", root)


def test_title_format_validation():
    assert render_title("{paper} {day}.{month}.{year}", "Post", date(2026, 9, 15), "en") == "Post 15.9.2026"
    assert render_title("{weekday} {dd}.{mm}.", "Post", date(2026, 9, 15), "sv") == "tisdag 15.09."
    assert render_title("{paper} {day:02d}", "Post", date(2026, 9, 5), "en") == "Post 05"
    for bad in ["{paper.__class__}", "{paper[0]}", "{0}", "{}", "{paper!r}", "{paper:{year}}", "no fields", "{nope}"]:
        with pytest.raises(ValueError):
            validate_title_format(bad)


def test_opf_is_readable_the_way_bookshelf_reads_it():
    data = build_opf('Côte-Nord <Gazette> & "Co"', date(2026, 9, 15), "Title & <more>", "en")
    root = ET.fromstring(data)
    ns = {"opf": OPF_NS, "dc": DC_NS}
    assert root.tag == f"{{{OPF_NS}}}package"
    assert root.find(".//dc:title", ns).text == "Title & <more>"
    assert root.find(".//dc:date", ns).text == "2026-09-15"
    assert root.find(".//dc:publisher", ns).text == 'Côte-Nord <Gazette> & "Co"'
    assert root.find(".//dc:creator", ns) is None  # would become a Person in Jellyfin
    metas = {m.get("name"): m.get("content") for m in root.findall(".//opf:meta", ns)}
    assert metas["calibre:series"] == 'Côte-Nord <Gazette> & "Co"'
    assert metas["calibre:series_index"] == "20260915"
    assert int(metas["calibre:series_index"]) < 2**31  # Bookshelf converts to Int32
    assert metas["calibre:title_sort"].endswith("2026-09-15")

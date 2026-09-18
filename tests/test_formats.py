"""Book formats: checking source files, reading covers from archives, and the format priority."""

from __future__ import annotations

import os
import zipfile

import pytest

from periodica.formats import (
    CBR,
    CBZ,
    EPUB,
    PDF,
    FormatError,
    book_suffix,
    by_priority,
    check_source,
    extract_cover,
    parse_priority,
)
from periodica.libraries import Library
from periodica.parser import parse_issue_filename
from periodica.paths import UnsafePathError
from periodica.patterns import compile_pattern, suggest_pattern

from .helpers import JPEG, PNG, _zip, can_symlink, make_cbr, make_cbz, make_epub, make_pdf


@pytest.fixture
def src(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    return root


def put(root, name, data):
    path = root / name
    path.write_bytes(data)
    return path


# --- checking ------------------------------------------------------------------------------------
@pytest.mark.parametrize("name, data, expected", [
    ("a.pdf", make_pdf(), PDF),
    ("a.cbz", make_cbz(), CBZ),
    ("a.epub", make_epub(), EPUB),
    ("a.cbr", make_cbr(5), CBR),
    ("a.cbr", make_cbr(4), CBR),
    ("a.CBR", make_cbz(), CBZ),   # a zip named .cbr: common, and its cover can be read
])
def test_valid_files(src, name, data, expected):
    assert check_source(put(src, name, data), src) == expected


@pytest.mark.parametrize("name, data, message", [
    ("a.cbz", make_cbz({"notes.txt": b"x"}), "no page images"),
    ("a.cbz", make_cbz({"__MACOSX/p1.jpg": JPEG, ".hidden.jpg": JPEG}), "no page images"),
    ("a.epub", _zip({"x.html": b"x"}), "epub mimetype"),
    ("a.epub", _zip({"mimetype": b"text/plain"}), "epub mimetype"),
    ("a.pdf", b"MZ\x90\x00 not a pdf", "bad header"),
    ("a.cbr", b"MZ\x90\x00 not a rar", "bad header"),
    ("a.cbz", b"PK\x03\x04 but broken", "not a readable zip"),
    ("a.mobi", b"BOOKMOBI" * 4, "not a supported book"),
    ("a.pdf", b"%PD", "too small"),
])
def test_invalid_files(src, name, data, message):
    with pytest.raises(FormatError, match=message):
        check_source(put(src, name, data), src)


def test_too_many_entries(src, monkeypatch):
    from periodica import formats

    monkeypatch.setattr(formats, "MAX_ZIP_ENTRIES", 3)
    path = put(src, "a.cbz", make_cbz({f"p{i}.jpg": JPEG for i in range(4)}))
    with pytest.raises(FormatError, match="more than 3 entries"):
        check_source(path, src)


def test_symlinks_and_outside_files_are_refused(src, tmp_path):
    outside = put(tmp_path, "outside.cbz", make_cbz())
    with pytest.raises(UnsafePathError):
        check_source(outside, src)
    if can_symlink(tmp_path):
        link = src / "link.cbz"
        os.symlink(outside, link)
        with pytest.raises(UnsafePathError):
            check_source(link, src)


# --- covers --------------------------------------------------------------------------------------
def test_cbz_cover_is_the_first_page_in_natural_order(src):
    path = put(src, "a.cbz", make_cbz())
    assert extract_cover(path, CBZ) == (JPEG, ".jpg"), "p2 comes before p10; __MACOSX is skipped"


def test_epub_cover_image_property_and_meta_cover(src):
    assert extract_cover(put(src, "a.epub", make_epub()), EPUB) == (PNG, ".png")
    legacy = ('<package xmlns="http://www.idpf.org/2007/opf" version="2.0"><metadata>'
              '<meta name="cover" content="cov"/></metadata><manifest>'
              '<item id="cov" href="images/cover.png" media-type="image/png"/></manifest></package>')
    assert extract_cover(put(src, "b.epub", make_epub(legacy)), EPUB) == (PNG, ".png")


def test_no_usable_cover(src):
    assert extract_cover(put(src, "a.cbz", make_cbz({"p1.jpg": b"not an image"})), CBZ) is None
    assert extract_cover(put(src, "a.cbr", make_cbr()), CBR) is None
    assert extract_cover(put(src, "a.pdf", make_pdf()), PDF) is None


@pytest.mark.parametrize("opf, message", [
    ('<!DOCTYPE p [<!ENTITY a "aaaa">]><package>&a;</package>', "DTD"),
    ('<package><manifest><item id="c" href="../../etc/passwd" properties="cover-image"/></manifest></package>',
     "leaves the book"),
    ('<package><manifest><item id="c" href="/abs.png" properties="cover-image"/></manifest></package>',
     "not relative"),
    ('<package><manifest/></package>', "names no cover"),
    ('<package', "bad XML"),
])
def test_hostile_epubs(src, opf, message):
    with pytest.raises(FormatError, match=message):
        extract_cover(put(src, "a.epub", make_epub(opf)), EPUB)


def test_large_and_bomb_entries_are_refused(src):
    big = put(src, "big.cbz", make_cbz({"p1.jpg": JPEG + b"\x00" * 2000}))
    with pytest.raises(FormatError, match="too large"):
        extract_cover(big, CBZ, max_bytes=1000)
    bomb = src / "bomb.cbz"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("p1.jpg", JPEG + b"\x00" * 1_000_000)
    with pytest.raises(FormatError, match="suspiciously"):
        extract_cover(bomb, CBZ)


# --- names and priority --------------------------------------------------------------------------
def test_every_format_is_parsed_and_patterned():
    for suffix in (".pdf", ".cbz", ".CBR", ".epub"):
        assert parse_issue_filename(f"Chronicle.2026.09.15{suffix}").paper == "Chronicle"
        assert compile_pattern("DW.{year}.No.{number}").parse(f"DW.2026.No.38{suffix}", "Duck Weekly").number == 38
    assert parse_issue_filename("Chronicle.2026.09.15.mobi") is None
    assert suggest_pattern("DW.2026.No.38.cbz")[0] == "DW.{year}.No.{number}"
    assert book_suffix("x/y.EPUB") == ".epub" and book_suffix("y.txt") is None


def test_priority():
    assert parse_priority("") == "cbz, pdf, epub, cbr"
    assert parse_priority("PDF epub") == "pdf, epub, cbz, cbr"
    assert parse_priority(".cbr > .cbz") == "cbr, cbz, pdf, epub"
    for bad, message in (("mobi", "not a supported format"), ("pdf, pdf", "twice")):
        with pytest.raises(ValueError, match=message):
            parse_priority(bad)
    files = [".pdf", ".epub", ".cbr"]
    assert by_priority(files, lambda s: s, "cbz, cbr, pdf, epub") == [".cbr", ".pdf", ".epub"]
    assert by_priority(files, lambda s: s, "") == [".pdf", ".epub", ".cbr"], "cbr is last by default"
    assert by_priority(files, lambda s: s, "epub, pdf") == [".epub", ".pdf", ".cbr"]


def test_library_fields():
    assert Library().format_priority == "cbz, pdf, epub, cbr"
    assert Library(format_priority="pdf").priority == [".pdf", ".cbz", ".epub", ".cbr"]
    for extra in ("cbz", "cbr", "epub", "pdf"):
        with pytest.raises(ValueError, match="issues themselves"):
            Library(extra_extensions=extra)

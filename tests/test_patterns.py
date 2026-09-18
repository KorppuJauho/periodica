"""User-taught file name patterns and allowed extra file types."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import date

import pytest

from periodica import db as db_module
from periodica.db import Database
from periodica.issues import DAY, MONTH, NUMBER
from periodica.libraries import Library
from periodica.manual_delete import check_deletable
from periodica.parser import parse_issue_filename
from periodica.patterns import compile_pattern, suggest_pattern, validate_publication
from periodica.repo import Repo

from .helpers import write_torrent_files
from .test_libraries import add_magazines, pdfs
from .test_scanner_retention import add_pack

DUCK = "DW.{year}.No.{number}"


# --- the pattern language ------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["DW.2026.No.38", "dw 2026 no 38", "DW_2026-No-038", "DW.2026.No.38.pdf"])
def test_pattern_matches_the_way_names_are_written(name):
    parsed = compile_pattern(DUCK).parse(name, "Duck Weekly")
    assert (parsed.paper, parsed.year, parsed.number, parsed.period) == ("Duck Weekly", 2026, 38, NUMBER)


@pytest.mark.parametrize("name", ["DW.2026.No.38x", "DW.1899.No.3", "DW.2101.No.3", "XDW.2026.No.38",
                                  "DW.2026.No.0", "DW.2026.No.1000"])
def test_pattern_rejects(name):
    assert compile_pattern(DUCK).parse(name, "Duck Weekly") is None


def test_old_years_are_fine():
    assert compile_pattern(DUCK).parse("DW.1955.No.3", "Duck Weekly").year == 1955
    assert parse_issue_filename("Old.Comic.1965.12.pdf").year == 1965
    assert parse_issue_filename("Old.Paper.1952.03.14.pdf").issue_date == date(1952, 3, 14)
    assert parse_issue_filename("Name.2026.02.30.pdf") is None, "invalid dates still don't guess"


def test_daily_and_monthly_kinds_and_paper_from_the_name():
    daily = compile_pattern("{paper} - {day}.{month}.{year}")
    parsed = daily.parse("Evening Post - 15.09.2026")
    assert (parsed.paper, parsed.issue_date, parsed.period) == ("Evening Post", date(2026, 9, 15), DAY)
    assert daily.parse("Evening Post - 31.02.2026") is None
    monthly = compile_pattern("Mag {month}-{year}{any}")
    parsed = monthly.parse("mag 09-2026 (scan)", "Business Monthly")
    assert (parsed.paper, parsed.year, parsed.number, parsed.period) == ("Business Monthly", 2026, 9, MONTH)
    assert monthly.parse("Mag 13-2026", "x") is None


@pytest.mark.parametrize("text, message", [
    ("", "empty"),
    ("DW.No.{number}", "needs {year}"),
    ("DW.{year}", "needs {number}"),
    ("{year}.{month}.{number}", "can't be combined"),
    ("{year}.{day}", "needs {month}"),
    ("{year}.{year}.{number}", "only once"),
    ("{paper}{any}{any}.{year}.{number}", "at most 2"),
    ("{foo}.{year}.{number}", "unknown placeholder"),
    ("a{b.{year}.{number}", "braces"),
    ("x" * 121 + "{year}{number}", "longer than"),
])
def test_bad_patterns_are_explained(text, message):
    with pytest.raises(ValueError, match=message):
        compile_pattern(text)


def test_regex_looking_text_is_literal():
    pattern = compile_pattern("a+b*.{year}.{number}")
    assert pattern.parse("a+b*.2026.1", "X") is not None
    assert pattern.parse("aab.2026.1", "X") is None


def test_hostile_names_are_fast():
    pattern = compile_pattern("{paper}{any}.{year}.No.{number}")
    started = time.perf_counter()
    for _ in range(20):
        assert pattern.parse("a." * 127 + "x") is None     # the longest name a filesystem allows
    assert pattern.parse("a." * 5000 + "x") is None        # longer: refused without matching
    assert time.perf_counter() - started < 1.0


def test_publication_name_rules():
    with pytest.raises(ValueError, match="give the publication a name"):
        validate_publication(compile_pattern(DUCK), "")
    assert validate_publication(compile_pattern("{paper}.{year}.{number}"), "") == ""
    assert validate_publication(compile_pattern(DUCK), "  Duck   Weekly ") == "Duck Weekly"
    assert "/" not in validate_publication(compile_pattern(DUCK), "../etc"), "made safe for a folder name"
    with pytest.raises(ValueError):
        validate_publication(compile_pattern(DUCK), "...")


@pytest.mark.parametrize("filename, expected", [
    ("DW.2026.No.38.pdf", (DUCK, "DW", NUMBER)),
    ("Some Mag 2026-09.pdf", ("Some Mag {year}-{number}", "Some Mag", NUMBER)),
    ("Daily 2026.09.15 extra.pdf", ("Daily {year}.{month}.{day}{any}", "Daily", DAY)),
    ("nothing.pdf", ("", "", "")),
])
def test_suggestions(filename, expected):
    assert suggest_pattern(filename) == expected


# --- scanning --------------------------------------------------------------------------------------------
def duck_pack(world, library, n=5, files=("DW.2026.No.38.pdf", "DW.2026.No.38.jpg", "DW.2026.No.38.txt")) -> str:
    folder = "DW.2026.No.38"
    rel = [f"{folder}/{name}" for name in files]
    source = world.downloads / "magazines"
    write_torrent_files(source, rel)
    for name in rel:
        if not name.endswith(".pdf"):
            (source / name).write_bytes(b"not a pdf")
    return world.qbit.add(n, folder, library.category, "/downloads/magazines", rel, local_save_path=source)


def test_a_pattern_and_allowed_extras_make_a_download_linkable_and_deletable(world):
    comics = add_magazines(world)
    h = duck_pack(world, comics)
    report = world.scanner.run()
    assert report.linked == 0 and report.unmatched == 3
    assert not world.repo.torrent(h)["files_ok"]

    world.repo.add_name_pattern(comics.id, DUCK, "Duck Weekly")
    world.repo.update_library(comics.model_copy(update={"extra_extensions": "nfo, jpg, txt"}))
    report = world.scanner.run()
    assert report.linked == 1 and report.unmatched == 0
    issue = world.repo.issue_by_key("Duck Weekly", "2026#038", comics.id)
    assert issue is not None and issue.period == NUMBER and issue.issue_label == "2026 #38"
    folder = world.dest.parent / "magazines" / "Duck Weekly" / "Duck Weekly 2026 #38"
    assert (folder / "Duck Weekly 2026 #38.pdf").is_file()
    opf = (folder / "metadata.opf").read_text(encoding="utf-8")
    assert "Duck Weekly" in opf and "DW.2026" not in opf
    assert world.repo.torrent(h)["files_ok"]
    assert check_deletable(world.repo, world.store.load(), h)["hash"] == h


def test_patterns_belong_to_their_library(world):
    comics = add_magazines(world)
    world.repo.add_name_pattern(comics.id, "{paper}.{year}.No.{number}", "")
    write_torrent_files(world.source, ["Pack/Other.2026.No.5.pdf"])
    world.qbit.add(7, "Pack", "news", "/downloads/news", ["Pack/Other.2026.No.5.pdf"], local_save_path=world.source)
    report = world.scanner.run()
    assert report.linked == 0 and report.unmatched == 1, "the News library doesn't use Comics' patterns"


def test_built_in_rules_still_apply_after_patterns(world):
    comics = add_magazines(world)
    world.repo.add_name_pattern(comics.id, DUCK, "Duck Weekly")
    add_pack(world, 1, date(2026, 9, 15))
    assert world.scanner.run().linked == 3


def test_a_patterns_kind_wins_over_the_numbering_guess(world):
    comics = add_magazines(world)
    world.repo.add_name_pattern(comics.id, "Mag.{year}.{number}", "Monthly Mag")
    rel = ["Pack/Mag.2026.09.pdf"]
    write_torrent_files(world.downloads / "magazines", rel)
    world.qbit.add(8, "Pack", "magazines", "/downloads/magazines", rel,
                   local_save_path=world.downloads / "magazines")
    world.scanner.run()
    assert world.repo.issue_by_key("Monthly Mag", "2026#009", comics.id) is not None, \
        "09 is issue 9 because the pattern says {number}, not September"


def test_broken_stored_pattern_is_skipped(world):
    comics = add_magazines(world)
    with world.db.connect() as conn:
        conn.execute("INSERT INTO name_patterns (library_id, pattern, paper, created_at) VALUES (?, ?, ?, 1)",
                     (comics.id, "{nonsense}", "x"))
    add_pack(world, 1, date(2026, 9, 15))
    assert world.scanner.run().error is None


# --- extras and storage -----------------------------------------------------------------------------------
@pytest.mark.parametrize("value, expected", [
    ("nfo", "nfo"), (".TXT, nfo;jpg  jpg", "txt, nfo, jpg"), ("", ""),
])
def test_extra_extensions_are_normalised(value, expected):
    assert Library(extra_extensions=value).extra_extensions == expected


@pytest.mark.parametrize("value", ["pdf", "cbz", "epub", "../x", "a" * 11, ",".join(f"e{i}" for i in range(21))])
def test_extra_extensions_are_validated(value):
    with pytest.raises(ValueError):
        Library(extra_extensions=value)


def test_upgrade_from_schema_4(tmp_path, monkeypatch):
    path = tmp_path / "periodica.db"
    migrations = list(db_module.MIGRATIONS)
    monkeypatch.setattr(db_module, "MIGRATIONS", migrations[:4])
    Database(path).migrate()
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES ('probe', ?)", (json.dumps(1),))
        conn.execute("INSERT INTO papers (library_id, name, created_at) VALUES (1, 'Chronicle', 1)")
    monkeypatch.setattr(db_module, "MIGRATIONS", migrations)
    db = Database(path)
    db.migrate()
    assert list(tmp_path.glob("periodica.db.bak-v4-*"))
    repo = Repo(db)
    assert repo.library(1).extra_extensions == "nfo"
    assert repo.paper("Chronicle", 1) is not None and repo.name_patterns(1) == []


def test_deleting_a_library_removes_its_patterns(world):
    comics = add_magazines(world)
    world.repo.add_name_pattern(comics.id, DUCK, "Duck Weekly")
    world.repo.delete_library(comics.id, "test")
    assert world.repo.name_patterns(comics.id) == []
    assert pdfs(world.dest) == []

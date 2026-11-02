"""Patterns that cover the folders above a file: a dated pack whose files carry only the publication name."""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path, PurePosixPath

import pytest

from periodica.health import DUPLICATE
from periodica.issues import DAY, NUMBER
from periodica.parser import parse_issue_filename
from periodica.patterns import MAX_PARTS, compile_pattern, suggest_pattern
from periodica.scanner import Scanner

from .helpers import write_torrent_files
from .test_web import setup_admin

PACK = "{any}.{year}.{month}.{day}/{any}/{paper}"
FILES = ["Daily.Pack.2026.10.18/Evening.Post/Evening.Post.pdf",
         "Daily.Pack.2026.10.18/Chronicle.Weekend/Chronicle.Weekend.pdf"]


def add_dated_pack(world, n=1, files=FILES) -> str:
    write_torrent_files(world.source, files)
    return world.qbit.add(n, files[0].split("/")[0], "news", "/downloads/news", files, local_save_path=world.source)


# --- the pattern language ------------------------------------------------------------------------------
def test_a_folder_pattern_reads_the_date_from_the_folder_and_the_name_from_the_file():
    pattern = compile_pattern(PACK)
    assert (pattern.parts, pattern.kind) == (3, DAY)
    parsed = pattern.parse(FILES[0])
    assert (parsed.paper, parsed.issue_date, parsed.period) == ("Evening Post", date(2026, 10, 18), DAY)
    assert pattern.parse("Top/" + FILES[1]).paper == "Chronicle Weekend", "only the trailing parts count"


@pytest.mark.parametrize("path", [
    "Evening.Post.pdf",                                    # fewer parts than the pattern
    "Evening.Post/Evening.Post.pdf",
    "Daily.Pack.2026.02.30/Evening.Post/Evening.Post.pdf",  # not a date
    "Daily.Pack/Evening.Post/Evening.Post.pdf",            # no date at all
])
def test_a_folder_pattern_rejects(path):
    assert compile_pattern(PACK).parse(path) is None


def test_placeholders_stay_inside_their_part():
    pattern = compile_pattern("{paper}/{year}.No.{number}")
    parsed = pattern.parse("Duck.Weekly/2026.No.38.cbz")
    assert (parsed.paper, parsed.year, parsed.number, parsed.period) == ("Duck Weekly", 2026, 38, NUMBER)
    assert compile_pattern("{paper}.{year}.No.{number}").parse("Duck/Weekly.2026.No.38.cbz").paper == "Weekly", \
        "a file-name pattern reads the file name only"
    assert compile_pattern("Pack.{year}.{month}.{day}/{paper}").parse("Pack.2026.10.18/x/Evening.Post.pdf") is None


def test_file_name_patterns_still_read_only_the_file_name():
    pattern = compile_pattern("DW.{year}.No.{number}")
    assert pattern.parts == 1
    assert pattern.parse("DW.2026.No.38/DW.2026.No.38.pdf", "Duck Weekly").number == 38
    assert pattern.shown("DW.2026.No.38/DW.2026.No.38.pdf") == "DW.2026.No.38.pdf"


@pytest.mark.parametrize("text, message", [
    ("Pack.{year}.{month}.{day}//{paper}", "folder part of the pattern is empty"),
    ("/Pack.{year}.{month}.{day}/{paper}", "folder part of the pattern is empty"),
    ("Pack.{year}.{month}.{day}\\{paper}", "separate folders with /"),
    ("a/b/c/{year}.{number}/{paper}", f"at most {MAX_PARTS - 1} folders"),
    ("{any}{any}{any}.{year}.{number}/{paper}", "at most 2 times together in each part"),
    ("{paper}/{year}.{number}/{paper}", "only once"),
    ("{year}.{month}/{year}.{day}/{paper}", "only once"),
])
def test_bad_folder_patterns_are_explained(text, message):
    with pytest.raises(ValueError, match=message):
        compile_pattern(text)


def test_each_part_may_use_two_free_text_placeholders():
    pattern = compile_pattern("{any}.{year}.{month}.{day}{any}/{any}/{paper}{any}")
    assert pattern.parse("Big.Pack.2026.10.18.v2/Evening.Post/Evening.Post.final.pdf") is not None


def test_hostile_paths_are_fast():
    pattern = compile_pattern("{any}{any}.{year}.{month}.{day}/{any}{any}/{paper}{any}")
    name = "a." * 127 + "x"
    started = time.perf_counter()
    for _ in range(20):
        assert pattern.parse(f"{name}/{name}/{name}.pdf") is None
    assert pattern.parse("a." * 5000 + "/x/y.pdf") is None, "a name longer than any filesystem allows is refused"
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize("path, expected", [
    (FILES[0], ("Daily.Pack.{year}.{month}.{day}/{any}/{paper}", "", DAY)),
    ("Daily.Pack.2026.10.18/Evening.Post.pdf", ("Daily.Pack.{year}.{month}.{day}/{paper}", "", DAY)),
    ("Comics.2026.38/Duck.Weekly/Duck.Weekly.cbz", ("Comics.{year}.{number}/{any}/{paper}", "", NUMBER)),
    ("DW.2026.No.38/DW.2026.No.38.pdf", ("DW.{year}.No.{number}", "DW", NUMBER)),   # the file name wins
    ("Pack/Evening.Post/Evening.Post.pdf", ("", "", "")),
])
def test_suggestions_use_the_nearest_dated_folder(path, expected):
    assert suggest_pattern(path) == expected
    if expected[0]:
        compile_pattern(expected[0])


# --- scanning --------------------------------------------------------------------------------------------
def test_a_folder_pattern_links_every_publication_in_a_dated_pack(world):
    add_dated_pack(world)
    report = world.scanner.run()
    assert report.linked == 0 and report.unmatched == 2, "the file names alone carry no date"

    world.repo.add_name_pattern(1, PACK, "")
    report = world.scanner.run()
    assert report.linked == 2 and report.unmatched == 0
    for paper in ("Evening Post", "Chronicle Weekend"):
        folder = world.dest / paper / f"{paper} 2026-10-18"
        assert (folder / f"{paper} 2026-10-18.pdf").is_file()
        assert world.repo.issue_by_key(paper, "2026-10-18", 1) is not None


# --- the Fix form ------------------------------------------------------------------------------------------
def test_fix_suggests_a_folder_pattern_and_previews_the_whole_pack(client):
    token = setup_admin(client)
    world = client.world
    h = add_dated_pack(world)
    world.scanner.run()
    page = client.get("/settings/libraries/1/patterns/new", params={"torrent": h, "path": FILES[0]}).text
    assert 'value="Daily.Pack.{year}.{month}.{day}/{any}/{paper}"' in page
    assert f'name="filename" value="{FILES[0]}"' in page

    r = client.post("/settings/libraries/1/patterns/preview", headers={"X-CSRF-Token": token},
                    data={"pattern": PACK, "publication": "", "filename": FILES[0]})
    assert r.json() == {
        "ok": True, "kind": "day",
        "message": f"{FILES[0]} → Evening Post 2026-10-18 (daily)",
        "matches": [f"{FILES[1]} → Chronicle Weekend 2026-10-18 (daily)"],
    }

    r = client.post("/settings/libraries/1/patterns", follow_redirects=False, data={
        "csrf_token": token, "pattern": PACK, "publication": "", "filename": FILES[0], "torrent": h})
    assert r.status_code == 303
    assert world.scanner.run().linked == 2


# --- a pack that mixes dated and undated file names ------------------------------------------------------
MIXED = ["Daily.Pack.2026.10.18/Evening.Post.2026.10.18/Evening.Post.2026.10.18.pdf",
         "Daily.Pack.2026.10.18/Chronicle.Weekend/Chronicle.Weekend.pdf"]


def _parse_as_in_1_1_0(self, lib, rel):
    """Patterns before the built-in rules even for folder patterns: how 1.1.0 read a mixed pack."""
    for pattern, publication in self._patterns.get(lib.id, []):
        parsed = pattern.parse(rel, publication)
        if parsed is not None:
            return parsed
    return parse_issue_filename(PurePosixPath(rel).name)


def test_a_folder_pattern_leaves_names_the_built_in_rules_read(world):
    add_dated_pack(world, files=MIXED)
    world.repo.add_name_pattern(1, PACK, "")
    report = world.scanner.run()
    assert report.linked == 2 and report.unmatched == 0
    assert {p.name for p in world.repo.papers(1)} == {"Evening Post", "Chronicle Weekend"}
    assert world.repo.issue_by_key("Evening Post", "2026-10-18", 1) is not None


def test_duplicates_left_by_a_pattern_are_reported_and_removed_on_fix(world, monkeypatch):
    add_dated_pack(world, files=MIXED)
    assert world.scanner.run().linked == 1, "the dated file is linked by the built-in rules"
    right = world.repo.issue_by_key("Evening Post", "2026-10-18", 1)

    # 1.1.0 then read the dated file as a publication called "Evening Post 2026 10 18".
    world.repo.add_name_pattern(1, PACK, "")
    with monkeypatch.context() as m:
        m.setattr(Scanner, "_parse", _parse_as_in_1_1_0)
        m.setattr(Scanner, "_duplicate", lambda *a, **kw: None)
        assert world.scanner.run().linked == 2
    wrong = world.repo.issue_by_key("Evening Post 2026 10 18", "2026-10-18", 1)
    wrong_dir = Path(wrong.dest_dir)
    assert wrong_dir.is_dir()

    report = world.scanner.run()
    assert report.linked == 0 and report.health_problems == 1
    assert world.repo.issue_by_key("Evening Post 2026 10 18", "2026-10-18", 1).problem == DUPLICATE
    assert world.repo.issue_by_key("Evening Post", "2026-10-18", 1).problem is None

    assert world.repo.set_fix_requested([wrong.id]) == 1
    report = world.scanner.run()
    assert report.fixed == 1 and report.health_problems == 0
    assert not wrong_dir.exists() and not wrong_dir.parent.exists(), "the duplicate's folders are gone"
    assert world.repo.issue_by_key("Evening Post 2026 10 18", "2026-10-18", 1).status == "excluded"
    assert Path(right.dest_dir).is_dir(), "the issue the file belongs to stays"
    assert Path(world.repo.issue_by_key("Chronicle Weekend", "2026-10-18", 1).dest_dir).is_dir()
    for source in MIXED:
        assert (world.source / source).is_file(), "the download is not touched"
    again = world.scanner.run()
    assert again.linked == 0 and again.health_problems == 0


def test_health_page_explains_duplicates(client, monkeypatch):
    setup_admin(client)
    world = client.world
    add_dated_pack(world, files=MIXED)
    world.scanner.run()
    world.repo.add_name_pattern(1, PACK, "")
    with monkeypatch.context() as m:
        m.setattr(Scanner, "_parse", _parse_as_in_1_1_0)
        m.setattr(Scanner, "_duplicate", lambda *a, **kw: None)
        world.scanner.run()
    world.scanner.run()
    page = client.get("/health").text
    assert "duplicate: its file is now read as another issue of the same download" in page
    assert "Evening Post 2026 10 18" in page and "Fix all (1)" in page
    assert "A duplicate's folder is deleted from the library" in page

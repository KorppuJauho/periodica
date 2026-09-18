"""Scanning downloads with cbz, cbr and epub issues: one file per issue, covers, duplicates and health."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import date

from periodica import db as db_module
from periodica.db import Database
from periodica.manual_delete import check_deletable
from periodica.repo import Repo

from .helpers import JPEG, PNG, write_torrent_files
from .test_folder_source import folder_mode
from .test_libraries import add_magazines
from .test_libraries_ui import form_of
from .test_patterns import DUCK
from .test_web import setup_admin


def comics(world, **changes):
    library = add_magazines(world, name="Comics", **changes)
    world.repo.add_name_pattern(library.id, DUCK, "Duck Weekly")
    return library


def pack(world, n, files, folder="DW.2026.No.38", category="magazines"):
    rel = [f"{folder}/{name}" for name in files]
    source = world.downloads / category
    write_torrent_files(source, rel)
    return world.qbit.add(n, folder, category, f"/downloads/{category}", rel, local_save_path=source)


def issue_dir(world, label="Duck Weekly 2026 #38"):
    return world.dest.parent / "magazines" / "Duck Weekly" / label


def the_issue(world, library):
    return world.repo.issue_by_key("Duck Weekly", "2026#038", library.id)


def test_only_the_preferred_format_is_linked(world):
    library = comics(world, format_priority="cbr, cbz, pdf")
    world.repo.update_library(library.model_copy(update={"extra_extensions": "nfo, txt"}))
    h = pack(world, 1, ["DW.2026.No.38.pdf", "DW.2026.No.38.cbr", "DW.2026.No.38.txt"])
    report = world.scanner.run()
    assert report.linked == 1 and report.unmatched == 0 and report.issue_errors == 0
    folder = issue_dir(world)
    assert sorted(p.name for p in folder.iterdir()) == [
        ".periodica.json", "Duck Weekly 2026 #38.cbr", "metadata.opf"], "no cover is read from a RAR"
    issue = the_issue(world, library)
    assert (issue.file_ext, issue.cover_status) == (".cbr", "none")
    assert world.repo.torrent(h)["files_ok"], "the pdf belongs to the issue: nothing blocks delete"
    assert check_deletable(world.repo, world.store.load(), h)["hash"] == h
    assert world.scanner.run().health_problems == 0


def test_by_default_a_pdf_wins_over_a_cbr(world):
    """A RAR archive gives no cover, so it comes last unless the library says otherwise."""
    library = comics(world)
    pack(world, 1, ["DW.2026.No.38.pdf", "DW.2026.No.38.cbr"])
    world.scanner.run()
    assert the_issue(world, library).file_ext == ".pdf"
    assert (issue_dir(world) / "Duck Weekly 2026 #38.pdf").is_file()


def test_cbz_links_with_its_first_page_as_cover(world):
    library = comics(world)
    pack(world, 1, ["DW.2026.No.38.cbz"])
    report = world.scanner.run()
    assert report.linked == 1 and report.covers == 1
    folder = issue_dir(world)
    assert (folder / "cover.jpg").read_bytes() == JPEG
    assert (folder.parent / "folder.jpg").read_bytes() == JPEG
    issue = the_issue(world, library)
    assert (issue.file_ext, issue.cover_status) == (".cbz", "ok")
    assert os.path.samefile(folder / "Duck Weekly 2026 #38.cbz",
                            world.downloads / "magazines" / "DW.2026.No.38" / "DW.2026.No.38.cbz")


def test_epub_links_with_a_png_cover_and_pdf_beats_epub(world):
    library = comics(world)
    pack(world, 1, ["DW.2026.No.38.epub"])
    world.scanner.run()
    folder = issue_dir(world)
    assert (folder / "cover.png").read_bytes() == PNG and not (folder / "cover.jpg").exists()
    assert (folder.parent / "folder.png").is_file()
    assert the_issue(world, library).file_ext == ".epub"

    pack(world, 2, ["DW.2026.No.39.pdf", "DW.2026.No.39.epub"], folder="DW.2026.No.39")
    world.scanner.run()
    assert world.repo.issue_by_key("Duck Weekly", "2026#039", library.id).file_ext == ".pdf"


def test_a_broken_file_falls_back_to_the_next_format(world):
    library = comics(world)
    h = pack(world, 1, ["DW.2026.No.38.pdf", "DW.2026.No.38.cbz"])
    (world.downloads / "magazines" / "DW.2026.No.38" / "DW.2026.No.38.cbz").write_bytes(b"PK\x03\x04 broken")
    report = world.scanner.run()
    assert report.linked == 1 and report.issue_errors == 0
    assert the_issue(world, library).file_ext == ".pdf"
    assert any("instead of an unreadable file" in a["message"] for a in world.repo.activity(50))
    assert world.repo.torrent(h)["files_ok"]


def test_a_later_download_in_a_better_format_is_a_duplicate(world):
    library = comics(world)
    first = pack(world, 1, ["DW.2026.No.38.pdf"])
    world.scanner.run()
    pack(world, 2, ["DW.2026.No.38.cbz"], folder="DW 38 comic")
    report = world.scanner.run()
    assert report.duplicates == 1 and report.linked == 0 and report.health_problems == 0
    issue = the_issue(world, library)
    assert (issue.file_ext, issue.torrent_hash) == (".pdf", first)
    assert sorted(p.suffix for p in issue_dir(world).iterdir() if p.stem.startswith("Duck")) == [".pdf"]
    world.scanner.run(trigger="Manual scan")
    assert "1 already in the library from another download" in world.repo.activity(1)[0]["message"]

    # The first download goes: the pdf stays, only its presence is checked.
    del world.qbit.torrents[first]
    assert world.scanner.run().health_problems == 0
    shutil.rmtree(issue_dir(world))
    report = world.scanner.run()
    assert report.health_problems == 1
    (problem,) = world.repo.issue_problems()
    assert problem["problem"] == "missing" and not problem["download_present"]


def test_the_same_pdf_in_two_downloads_is_not_a_copy(world):
    comics(world)
    pack(world, 1, ["DW.2026.No.38.pdf"])
    pack(world, 2, ["DW.2026.No.38.pdf"], folder="Duck again")
    report = world.scanner.run()
    assert report.linked == 1 and report.duplicates == 1 and report.health_problems == 0
    assert world.scanner.run().health_problems == 0


def test_a_linked_issue_keeps_its_format(world):
    """Linked as pdf while cbr was an extra (1.3.0); after the upgrade the cbr is preferred but nothing moves."""
    library = comics(world)
    pack(world, 1, ["DW.2026.No.38.pdf"])
    world.scanner.run()
    write_torrent_files(world.downloads / "magazines", ["DW.2026.No.38/DW.2026.No.38.cbr"])
    world.qbit.files[next(iter(world.qbit.files))].append({"name": "DW.2026.No.38/DW.2026.No.38.cbr",
                                                            "size": 100, "progress": 1.0})
    report = world.scanner.run()
    assert report.linked == 0 and report.already_linked == 1 and report.health_problems == 0
    assert the_issue(world, library).file_ext == ".pdf"


def test_health_and_fix_for_a_cbz(world):
    library = comics(world)
    pack(world, 1, ["DW.2026.No.38.cbz"])
    world.scanner.run()
    (issue_dir(world) / "Duck Weekly 2026 #38.cbz").unlink()
    assert world.scanner.run().health_problems == 1
    issue = the_issue(world, library)
    world.repo.set_fix_requested([issue.id])
    assert world.scanner.run().fixed == 1
    assert (issue_dir(world) / "Duck Weekly 2026 #38.cbz").is_file()
    (issue_dir(world) / "cover.jpg").unlink()
    world.scanner.run()
    assert the_issue(world, library).problem == "cover"


def test_a_renamed_publication_moves_a_cbz(world):
    library = comics(world)
    pack(world, 1, ["DW.2026.No.38.cbz"])
    world.scanner.run()
    world.repo.update_paper("Duck Weekly", "Duck Monthly", True, None, library.id)
    report = world.scanner.run()
    assert report.moved == 1 and report.health_problems == 0
    folder = world.dest.parent / "magazines" / "Duck Monthly" / "Duck Monthly 2026 #38"
    assert (folder / "Duck Monthly 2026 #38.cbz").is_file() and (folder / "cover.jpg").is_file()


def test_folder_mode_links_a_cbz(world):
    from .test_folder_source import age

    write_torrent_files(world.source, ["Chronicle.2026.09.15.cbz"])
    age(world.source / "Chronicle.2026.09.15.cbz", 10)
    folder_mode(world)
    assert world.scanner.run().linked == 1
    assert (world.dest / "Chronicle" / "Chronicle 2026-09-15" / "Chronicle 2026-09-15.cbz").is_file()


def test_library_page_saves_the_priority(client):
    token = setup_admin(client)
    world = client.world
    library = add_magazines(world)
    url = f"/settings/libraries/{library.id}"
    assert 'name="format_priority" value="cbz, pdf, epub, cbr"' in client.get(url).text
    r = client.post(url, data={"csrf_token": token, **form_of(library, format_priority="mobi")})
    assert r.status_code == 400 and "not a supported format" in r.text
    r = client.post(url, data={"csrf_token": token, **form_of(library, format_priority="PDF epub")},
                    follow_redirects=False)
    assert r.status_code == 303
    assert world.repo.library(library.id).format_priority == "pdf, epub, cbz, cbr"
    assert 'name="format_priority" value="cbz, pdf, epub, cbr"' in client.get("/settings/libraries/new").text


def test_a_chosen_priority_survives_the_cbr_last_migration(tmp_path, monkeypatch):
    """Migration 8 only rewrites libraries still on the very first default."""
    path = tmp_path / "periodica.db"
    migrations = list(db_module.MIGRATIONS)
    monkeypatch.setattr(db_module, "MIGRATIONS", migrations[:7])
    Database(path).migrate()
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE libraries SET format_priority = 'cbr, cbz, pdf, epub' WHERE id = 1")
    monkeypatch.setattr(db_module, "MIGRATIONS", migrations)
    Database(path).migrate()
    assert Repo(Database(path)).library(1).format_priority == "cbr, cbz, pdf, epub"


def test_upgrade_from_schema_6(tmp_path, monkeypatch):
    path = tmp_path / "periodica.db"
    migrations = list(db_module.MIGRATIONS)
    monkeypatch.setattr(db_module, "MIGRATIONS", migrations[:6])
    Database(path).migrate()
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES ('probe', ?)", (json.dumps(1),))
        conn.execute("UPDATE libraries SET extra_extensions = 'nfo, cbr, txt' WHERE id = 1")
        conn.execute("INSERT INTO issues (library_id, paper, issue_key, issue_label, period, issue_date, "
                     "source_path, status, updated_at) VALUES (1, 'Chronicle', '2026-09-15', '2026-09-15', 'day', "
                     "'2026-09-15', '/x.pdf', 'linked', 1)")
    monkeypatch.setattr(db_module, "MIGRATIONS", migrations)
    db = Database(path)
    db.migrate()
    assert list(tmp_path.glob("periodica.db.bak-v6-*"))
    repo = Repo(db)
    (row,) = repo.linked_issues()
    assert row.file_ext == ".pdf"
    library = repo.library(1)
    assert (library.extra_extensions, library.format_priority) == ("nfo, txt", "cbz, pdf, epub, cbr")


def test_dates_in_old_style_names_still_link_as_pdf(world):
    from .test_scanner_retention import add_pack

    add_pack(world, 1, date(2026, 9, 15))
    assert world.scanner.run().linked == 3
    assert {i.file_ext for i in world.repo.linked_issues()} == {".pdf"}

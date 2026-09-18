"""Library health: damaged issues are reported and wait for the user's Fix."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import date

import pytest

from periodica import covers
from periodica import db as db_module
from periodica.db import Database
from periodica.repo import Repo

from .test_folder_source import folder_mode, pack
from .test_libraries import _wait_idle, add_magazine_pack, add_magazines, use_jellyfin
from .test_scanner_retention import add_pack

DAY = date(2026, 9, 15)


def post(world):
    return next(i for i in world.repo.linked_issues() if i.paper == "Evening Post")


def linked(world):
    add_pack(world, 1, DAY)
    assert world.scanner.run().linked == 3
    issue = post(world)
    folder = world.dest / "Evening Post" / "Evening Post 2026-09-15"
    assert issue.dest_dir == str(folder)
    return issue, folder


def source_of(world):
    return world.source / "Daily Newspapers 15 09 2026" / "Evening.Post.2026.09.15" / "Evening.Post.2026.09.15.pdf"


def health_logs(world):
    return [a["message"] for a in world.repo.activity(1000) if a["message"].startswith("Library health")]


@pytest.fixture
def fake_covers(monkeypatch):
    def render(pdf, cover, width, memory):
        cover.write_bytes(b"jpeg")

    monkeypatch.setattr(covers, "pdftoppm_available", lambda: True)
    monkeypatch.setattr(covers, "render_cover", render)
    monkeypatch.setattr(covers, "update_folder_art", lambda *args: None)


def test_a_deleted_issue_is_reported_not_relinked(world):
    issue, folder = linked(world)
    shutil.rmtree(folder)
    report = world.scanner.run()
    assert report.linked == 0 and report.health_problems == 1
    assert not folder.exists(), "nothing is re-created without Fix"
    row = world.repo.issue(issue.id)
    assert (row.status, row.problem) == ("linked", "missing") and row.problem_since
    assert len(health_logs(world)) == 1 and "issue file or folder missing" in health_logs(world)[0]

    world.scanner.run(trigger="Manual scan")
    assert len(health_logs(world)) == 1, "a known problem is not logged again"
    assert "1 damaged in the library" in world.repo.activity(1)[0]["message"]


def test_fix_links_it_again_and_refreshes_jellyfin(world):
    jf = use_jellyfin(world)
    issue, folder = linked(world)
    _wait_idle(world.scanner.jellyfin)
    jf.folders.clear()
    shutil.rmtree(folder)
    world.scanner.run()
    assert world.repo.set_fix_requested([issue.id]) == 1
    report = world.scanner.run()
    assert report.fixed == 1 and report.linked == 0 and report.health_problems == 0
    assert os.path.samefile(source_of(world), folder / "Evening Post 2026-09-15.pdf")
    assert (folder / "metadata.opf").is_file() and (folder / ".periodica.json").is_file()
    row = world.repo.issue(issue.id)
    assert (row.problem, row.fix_requested) == (None, False)
    assert "fixed (linked again)" in health_logs(world)[0]
    _wait_idle(world.scanner.jellyfin)
    assert jf.folders, "the library is refreshed after a fix"


def test_a_copy_is_reported_and_replaced_with_a_link(world):
    issue, folder = linked(world)
    pdf = folder / "Evening Post 2026-09-15.pdf"
    data = pdf.read_bytes()
    pdf.unlink()
    pdf.write_bytes(data)
    report = world.scanner.run()
    assert report.health_problems == 1 and report.issue_errors == 0
    assert world.repo.issue(issue.id).problem == "copy"
    assert not os.path.samefile(source_of(world), pdf)

    world.repo.set_fix_requested([issue.id])
    assert world.scanner.run().fixed == 1
    assert os.path.samefile(source_of(world), pdf)
    assert not list(folder.glob(".*nl-tmp")), "no temporary file is left behind"
    assert world.repo.issue(issue.id).problem is None


def test_a_copy_is_only_replaced_in_a_folder_periodica_made(world):
    issue, folder = linked(world)
    pdf = folder / "Evening Post 2026-09-15.pdf"
    pdf.unlink()
    pdf.write_bytes(b"%PDF-1.4 someone else's file")
    world.scanner.run()
    (folder / ".periodica.json").unlink()
    world.repo.set_fix_requested([issue.id])
    report = world.scanner.run()
    assert report.fixed == 0 and report.issue_errors == 1
    assert pdf.read_bytes() == b"%PDF-1.4 someone else's file"
    assert world.repo.issue(issue.id).problem == "copy"


def test_missing_metadata_is_reported_and_written_again(world):
    issue, folder = linked(world)
    (folder / "metadata.opf").unlink()
    world.scanner.run()
    assert world.repo.issue(issue.id).problem == "metadata"
    assert not (folder / "metadata.opf").exists()
    world.repo.set_fix_requested([issue.id])
    assert world.scanner.run().fixed == 1
    assert (folder / "metadata.opf").is_file()


def test_missing_cover_is_reported_and_rendered_again(world, fake_covers):
    issue, folder = linked(world)
    assert world.repo.issue(issue.id).cover_status == "ok"
    (folder / "cover.jpg").unlink()
    world.scanner.run()
    assert world.repo.issue(issue.id).problem == "cover"
    assert not (folder / "cover.jpg").exists()
    world.repo.set_fix_requested([issue.id])
    assert world.scanner.run().fixed == 1
    assert (folder / "cover.jpg").is_file()


def test_a_failed_cover_is_not_a_health_problem(world, monkeypatch):
    monkeypatch.setattr(covers, "pdftoppm_available", lambda: False)
    issue, folder = linked(world)
    assert world.repo.issue(issue.id).cover_status == "failed"
    assert world.scanner.run().health_problems == 0


def test_putting_the_file_back_clears_the_problem(world):
    issue, folder = linked(world)
    backup = folder.parent / "backup"
    shutil.move(folder, backup)
    world.scanner.run()
    assert world.repo.issue(issue.id).problem == "missing"
    shutil.move(backup, folder)
    assert world.scanner.run().health_problems == 0
    assert world.repo.issue(issue.id).problem is None
    assert "is whole again" in health_logs(world)[0]


def test_an_issue_whose_download_is_gone_can_only_be_removed(world):
    issue, folder = linked(world)
    world.qbit.torrents.clear()
    assert world.scanner.run().health_problems == 0, "the library copy is intact"
    shutil.rmtree(folder)
    assert world.repo.set_fix_requested([issue.id]) == 0, "nothing known to fix yet"
    report = world.scanner.run()
    assert report.health_problems == 1 and report.fixed == 0
    row = world.repo.issue(issue.id)
    assert (row.problem, row.fix_requested) == ("missing", False)
    assert "download is gone" in health_logs(world)[0]
    world.repo.set_fix_requested([issue.id])
    world.scanner.run()
    assert "cannot fix" in health_logs(world)[0] and not world.repo.issue(issue.id).fix_requested

    world.repo.set_issue_status(issue.id, "excluded")   # Remove from library
    row = world.repo.issue(issue.id)
    assert (row.problem, row.fix_requested) == (None, False)
    assert world.scanner.run().health_problems == 0


def test_dry_run_counts_without_storing(world):
    issue, folder = linked(world)
    shutil.rmtree(folder)
    report = world.scanner.run(dry_run=True)
    assert report.health_problems == 1 and report.would_link == 0
    assert world.repo.issue(issue.id).problem is None and health_logs(world) == []


def test_folder_mode_checks_the_same_way(world):
    pack(world)
    folder_mode(world)
    world.scanner.run()
    issue = post(world)
    shutil.rmtree(issue.dest_dir)
    assert world.scanner.run().health_problems == 1
    world.repo.set_fix_requested([issue.id])
    assert world.scanner.run().fixed == 1


def test_libraries_are_checked_separately(world):
    magazines = add_magazines(world)
    add_pack(world, 1, DAY)
    add_magazine_pack(world, 2, DAY)
    world.scanner.run()
    shutil.rmtree(world.dest.parent / "magazines" / "Harbour Monthly")
    report = world.scanner.run()
    assert report.health_problems == 1
    (problem,) = world.repo.issue_problems()
    assert (problem["library_id"], problem["library_name"], problem["label"]) == (
        magazines.id, "Magazines", "Harbour Monthly")
    assert problem["download_present"] == 1
    assert world.repo.issue_problems({1}) == []
    assert world.repo.library_overview()[magazines.id]["problems"] == 1
    assert world.repo.library_overview()[1]["problems"] == 0


def test_a_new_destination_links_as_before(world):
    issue, folder = linked(world)
    world.settings(dest_dir=str(world.dest.parent / "papers"))
    report = world.scanner.run()
    assert report.linked == 3 and report.health_problems == 0


def test_a_renamed_publication_is_still_moved(world):
    issue, folder = linked(world)
    world.repo.update_paper("Evening Post", "Evening Herald", True, None, 1)
    report = world.scanner.run()
    assert report.moved == 1 and report.health_problems == 0
    assert (world.dest / "Evening Herald" / "Evening Herald 2026-09-15" / "Evening Herald 2026-09-15.pdf").is_file()

    # A renamed publication whose old folder was deleted is reported, not linked into the new name.
    shutil.rmtree(world.dest / "Evening Herald")
    world.repo.update_paper("Evening Post", "Evening Star", True, None, 1)
    report = world.scanner.run()
    assert report.moved == 0 and report.linked == 0 and report.health_problems == 1
    assert not (world.dest / "Evening Star").exists()


def test_only_problem_issues_can_be_marked_for_fixing(world):
    issue, folder = linked(world)
    assert world.repo.set_fix_requested([issue.id]) == 0
    assert world.repo.set_fix_requested([]) == 0


def test_upgrade_from_schema_5(tmp_path, monkeypatch):
    path = tmp_path / "periodica.db"
    migrations = list(db_module.MIGRATIONS)
    monkeypatch.setattr(db_module, "MIGRATIONS", migrations[:5])
    Database(path).migrate()
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES ('probe', ?)", (json.dumps(1),))
        conn.execute("INSERT INTO issues (library_id, paper, issue_key, issue_label, period, issue_date, "
                     "source_path, status, updated_at) VALUES (1, 'Chronicle', '2026-09-15', '2026-09-15', 'day', "
                     "'2026-09-15', '/x.pdf', 'linked', 1)")
    monkeypatch.setattr(db_module, "MIGRATIONS", migrations)
    db = Database(path)
    db.migrate()
    assert list(tmp_path.glob("periodica.db.bak-v5-*"))
    (row,) = Repo(db).linked_issues()
    assert (row.paper, row.problem, row.fix_requested) == ("Chronicle", None, False)

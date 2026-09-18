"""Folder mode: no qBittorrent, the source folder itself is the list of downloads."""

from __future__ import annotations

import os
import re
import time
from datetime import date
from pathlib import Path

import pytest

from periodica.manual_delete import ManualDeleteError, check_deletable
from periodica.paths import PathMapping
from periodica.scanner import config_problems
from periodica.sources import FOLDER_CATEGORY, FolderSource, download_for_path, folder_download_id

from .helpers import can_symlink, write_torrent_files
from .test_scanner_retention import add_pack, library_pdfs
from .test_web import csrf_of, setup_admin

PACK = "Daily Newspapers 15 09 2026"


def age(path: Path, minutes: float) -> None:
    """Make everything under path look unchanged for this long."""
    stamp = time.time() - minutes * 60
    targets = [path] + (list(path.rglob("*")) if path.is_dir() else [])
    for target in targets:
        if not target.is_symlink():
            os.utime(target, (stamp, stamp))


def folder_mode(world, **changes):
    def refuse(settings):
        raise AssertionError("qBittorrent must never be contacted in folder mode")

    world.scanner.qbit_factory = refuse
    return world.settings(download_client="none", **changes)


def pack(world, day=date(2026, 9, 15), minutes=10, **kwargs) -> Path:
    add_pack(world, 1, day, **kwargs)
    folder = world.source / f"Daily Newspapers {day:%d %m %Y}"
    age(folder, minutes)
    return folder


def test_a_settled_folder_is_linked(world):
    folder_mode(world)
    pack(world)
    report = world.scanner.run()
    assert report.error is None, report.error
    assert report.torrents == 1 and report.linked == 3
    assert "Evening Post 2026-09-15.pdf" in library_pdfs(world)
    row = world.repo.torrent(folder_download_id(PACK))
    assert row["category"] == FOLDER_CATEGORY and row["name"] == PACK and row["files_ok"]


def test_a_changing_folder_waits_for_the_settle_time(world):
    folder_mode(world, settle_minutes=5)
    pack(world, minutes=2)
    report = world.scanner.run()
    assert report.torrents == 1 and report.linked == 0
    age(world.source / PACK, 6)
    assert world.scanner.run().linked == 3


@pytest.mark.parametrize("partial", ["Chronicle.2026.09.16.pdf.!qB", "Evening.Post.2026.09.16.pdf.part"])
def test_partial_files_keep_a_download_unfinished(world, partial):
    folder_mode(world)
    folder = pack(world)
    (folder / partial).write_bytes(b"%PDF-partial")
    age(folder, 60)
    assert world.scanner.run().linked == 0
    (folder / partial).unlink()
    assert world.scanner.run().linked == 0, "removing it is a change too: the settle time starts again"
    age(folder, 10)
    assert world.scanner.run().linked == 3


def test_a_rename_inside_a_subfolder_restarts_the_settle_time(world):
    folder_mode(world, settle_minutes=5)
    folder = pack(world, minutes=60)
    sub = folder / "Chronicle.2026.09.15"
    stamp = time.time()
    os.utime(sub, (stamp, stamp))
    assert world.scanner.run().linked == 0


def test_single_files_and_hidden_entries(world):
    folder_mode(world)
    write_torrent_files(world.source, ["Chronicle.2026.09.14.pdf", ".hidden/Chronicle.2026.09.13.pdf",
                                       "Loose/.DS_Store.pdf"])
    age(world.source, 10)
    report = world.scanner.run()
    assert report.linked == 1 and report.torrents == 2   # the file and "Loose"; ".hidden" is skipped
    assert library_pdfs(world) == ["Chronicle 2026-09-14.pdf"]


def test_ids_are_stable_and_unicode_normalised():
    assert folder_download_id("Café") == folder_download_id("Café")
    assert re.fullmatch(r"[0-9a-f]{40}", folder_download_id(PACK))
    assert folder_download_id(PACK) != folder_download_id(PACK + " ")


def test_symlinks_are_never_followed(world, tmp_path):
    if not can_symlink(tmp_path):
        pytest.skip("symlinks not available")
    folder_mode(world)
    outside = tmp_path / "outside"
    write_torrent_files(outside, ["Chronicle.2026.09.12.pdf"])
    folder = pack(world)
    os.symlink(outside, folder / "linked-dir")
    os.symlink(outside / "Chronicle.2026.09.12.pdf", world.source / "Chronicle.2026.09.12.pdf")
    age(world.source, 10)
    report = world.scanner.run()
    assert report.linked == 0, "a download containing a symlink is not linked at all"
    reasons = {u["path"]: u["reason"] for u in world.repo.unmatched()}
    assert "symbolic link" in reasons[PACK] and "symbolic link" in reasons["Chronicle.2026.09.12.pdf"]
    assert not any("2026-09-12" in name for name in library_pdfs(world))


def test_walk_limits(world, monkeypatch):
    import periodica.sources as sources

    monkeypatch.setattr(sources, "MAX_FILES", 3)
    folder_mode(world)
    pack(world)   # 3 PDFs + 3 NFOs
    assert world.scanner.run().linked == 0
    (row,) = world.repo.unmatched()
    assert row["path"] == PACK and "more than 3 files" in row["reason"]

    monkeypatch.setattr(sources, "MAX_FILES", 5000)
    monkeypatch.setattr(sources, "MAX_DEPTH", 0)
    assert world.scanner.run().linked == 0, "the papers are one folder down"
    assert "nested deeper" in world.repo.unmatched()[0]["reason"]


def test_daily_papers_date_a_monthly_file_in_the_same_folder(world):
    folder_mode(world)
    write_torrent_files(world.source, [f"{PACK}/Business.Monthly.2026.09.pdf"])
    pack(world)
    world.scanner.run()
    assert "Business Monthly 2026-09.pdf" in library_pdfs(world)


def test_unmatched_and_ignore_work_in_folder_mode(client):
    token = setup_admin(client)
    world = client.world
    folder_mode(world)
    write_torrent_files(world.source, [f"{PACK}/Special edition.pdf"])
    pack(world)
    assert world.scanner.run().unmatched == 1
    r = client.post("/unmatched/ignore", data={"csrf_token": token, "torrent_hash": folder_download_id(PACK),
                                               "path": f"{PACK}/Special edition.pdf"}, follow_redirects=False)
    assert r.status_code == 303
    assert world.scanner.run().unmatched == 0


def test_nothing_is_deleted_in_folder_mode(world):
    folder_mode(world, retention_enabled=True, retention_armed=True, retention_days=1, grace_hours=0)
    pack(world, minutes=5 * 24 * 60)
    report = world.scanner.run()
    assert report.linked == 3 and report.skipped_expired == 0, "expiry does not apply either"
    world.scanner.run()
    assert report.torrents_deleted == 0 and report.issues_removed == 0
    assert len(library_pdfs(world)) == 3 and (world.source / PACK).is_dir()
    assert world.repo.pending_deletions() == []


def test_manual_delete_is_refused(client):
    token = setup_admin(client)
    world = client.world
    folder_mode(world)
    pack(world)
    world.scanner.run()
    h = folder_download_id(PACK)
    with pytest.raises(ManualDeleteError, match="needs qBittorrent"):
        check_deletable(world.repo, world.store.load(), h)
    assert client.get(f"/torrents/{h}/delete").status_code == 409
    assert client.post(f"/torrents/{h}/delete", data={"csrf_token": token, "confirm": "yes"}).status_code == 409
    assert client.post("/deletions/arm", data={"csrf_token": token, "confirm": "yes"}).status_code == 409
    assert client.post("/deletions/approve", data={"csrf_token": token}).status_code == 409
    page = client.get("/deletions").text
    assert "Deleting needs qBittorrent" in page and f"/torrents/{h}/delete" not in page
    assert (world.source / PACK).is_dir()


def test_folder_rows_are_never_deletable_even_after_switching_back(world):
    folder_mode(world)
    pack(world)
    world.scanner.run()
    settings = world.settings(download_client="qbittorrent")
    with pytest.raises(ManualDeleteError):
        check_deletable(world.repo, settings, folder_download_id(PACK))


def test_switching_modes_keeps_issues_without_duplicates(world):
    h = add_pack(world, 1, date(2026, 9, 15))
    age(world.source / PACK, 10)
    world.scanner.run()
    ids = {i.issue_key + i.paper: i.id for i in world.repo.linked_issues()}
    assert {i.torrent_hash for i in world.repo.linked_issues()} == {h}

    qbit_factory = world.scanner.qbit_factory
    folder_mode(world)
    report = world.scanner.run()
    assert report.linked == 0 and report.already_linked == 3
    issues = world.repo.linked_issues()
    assert {i.issue_key + i.paper: i.id for i in issues} == ids
    assert {i.torrent_hash for i in issues} == {folder_download_id(PACK)}
    assert not world.repo.torrent(h)["present"]

    world.scanner.qbit_factory = qbit_factory
    world.settings(download_client="qbittorrent")
    report = world.scanner.run()
    assert report.linked == 0 and report.already_linked == 3
    assert {i.torrent_hash for i in world.repo.linked_issues()} == {h}
    assert len(library_pdfs(world)) == 3


def test_config_problems_depend_on_the_mode(world):
    s = world.settings(qbit_url="", qbit_category="")
    assert any("qBittorrent" in p for p in config_problems(s, world.env))
    s = world.settings(download_client="none")
    assert config_problems(s, world.env) == []


def test_download_for_path():
    root = Path("/data/torrents/news").resolve()
    mapping = PathMapping("/downloads", str(root.parent))
    assert download_for_path(f"/downloads/news/{PACK}", mapping, root) == (folder_download_id(PACK), PACK)
    assert download_for_path(f"/downloads/news/{PACK}/Chronicle/x.pdf", mapping, root)[1] == PACK
    for bad in ("/downloads/news", "/downloads/other/x", "/downloads/news/../other", "relative/path",
                "/downloads/news/.hidden", "/downloads/news/a\\b", "/downloads/news/a\x00", "/" + "a" * 5000):
        assert download_for_path(bad, mapping, root) is None, bad


def test_folder_source_reports_newest_change(tmp_path):
    write_torrent_files(tmp_path, ["A/x.pdf", "A/sub/y.pdf"])
    age(tmp_path / "A", 30)
    newer = tmp_path / "A" / "sub" / "y.pdf"
    stamp = time.time() - 60
    os.utime(newer, (stamp, stamp))
    (download,) = FolderSource(tmp_path, settle_seconds=120).downloads()
    assert not download.complete and download.completion_on == pytest.approx(stamp, abs=1)
    (download,) = FolderSource(tmp_path, settle_seconds=30).downloads()
    assert download.complete


# --- settings and pages ------------------------------------------------------------------------
def test_switching_to_folder_mode_needs_confirmation_and_keeps_qbittorrent_settings(client):
    token = setup_admin(client)
    world = client.world
    r = client.post("/settings/client", data={
        "csrf_token": token, "download_client": "none", "settle_minutes": "7", "qbit_category": "news",
        "path_map_remote": "/downloads", "path_map_local": str(world.downloads),
    })
    assert r.status_code == 200 and "Confirm changes" in r.text
    assert "Downloads in the source folder" in r.text
    assert world.store.load().uses_qbit, "not applied before confirming"
    confirm = re.search(r'name="token" value="([^"]+)"', r.text).group(1)
    client.post("/settings-confirm", data={"csrf_token": token, "token": confirm, "action": "confirm"})
    saved = world.store.load()
    assert not saved.uses_qbit and saved.settle_minutes == 7
    assert saved.qbit_url and saved.qbit_password == "adminadmin", "qBittorrent settings are kept"

    page = client.get("/settings?section=retention").text
    assert "Automatic delete needs qBittorrent" in page and "disabled" in page
    dashboard = client.get("/").text
    assert "Not used: watching the source folder" in dashboard and "conn-off" in dashboard
    assert "RSS rule" not in client.get("/system").text


def test_folder_mode_with_the_api_on_needs_a_category(client):
    token = setup_admin(client)
    client.world.settings(qbit_category="")   # no library has a category
    data = {"csrf_token": token, "download_client": "none", "settle_minutes": "5"}
    r = client.post("/settings/client", data=data)
    assert r.status_code == 400 and "required while the scan API is on" in r.text
    assert client.world.store.load().uses_qbit
    client.post("/system/api", data={"csrf_token": csrf_of(client.get("/system").text), "enabled": "false"})
    r = client.post("/settings/client", data={**data, "csrf_token": csrf_of(client.get("/system").text)})
    assert r.status_code == 200 and "Confirm changes" in r.text


def test_keep_is_disabled_in_folder_mode(client):
    setup_admin(client)
    world = client.world
    folder_mode(world)
    pack(world)
    world.scanner.run()
    page = client.get("/papers/Chronicle").text
    assert "Keep only matters for automatic delete" in page

"""Several libraries: each with its own category, folders, Jellyfin library and days to keep."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import date

import pytest

from periodica import db as db_module
from periodica.db import Database
from periodica.jellyfin import RefreshCoordinator
from periodica.libraries import Library, library_conflicts
from periodica.manual_delete import delete_torrent_now
from periodica.repo import Repo
from periodica.settings import Settings, SettingsStore
from periodica.sources import API_FINISHED_KEY, folder_download_id

from .helpers import write_torrent_files
from .test_folder_source import age, folder_mode
from .test_jellyfin_refresh import CONFIGURED, _wait_idle
from .test_scanner_retention import DAY, add_pack
from .test_web import setup_admin

LIB1_JF = "0c0e1e7b5a6b4b0a9f0e3a1d2c3b4a59"
LIB2_JF = "1c0e1e7b5a6b4b0a9f0e3a1d2c3b4a59"


class TargetJellyfin:
    """Records which Jellyfin libraries were asked to scan."""

    def __init__(self):
        self.folders: list[str] = []
        self.full = 0

    def __call__(self, settings):
        return self

    def refresh_library_folder(self, library_id):
        self.folders.append(library_id)

    def refresh_library(self):
        self.full += 1


def add_magazines(world, **changes) -> Library:
    source = world.downloads / "magazines"
    source.mkdir(parents=True, exist_ok=True)
    world.qbit.categories["magazines"] = {}
    values = {"name": "Magazines", "category": "magazines", "source_dir": str(source),
              "dest_dir": str(world.dest.parent / "magazines"), "jellyfin_library_id": LIB2_JF,
              "jellyfin_library_name": "Magazines"}
    library = Library(**{**values, **changes})
    library_id = world.repo.add_library(library)
    return world.repo.library(library_id)


def add_magazine_pack(world, n, day: date, *, age_days=0.0, papers=("Chronicle", "Harbour.Monthly")) -> str:
    folder = f"Magazines {day:%d %m %Y}"
    files = []
    for paper in papers:
        stem = f"{paper}.{day:%Y.%m.%d}" if paper == "Chronicle" else f"{paper}.{day:%Y.%m}"
        files.append(f"{folder}/{stem}/{stem}.pdf")
    source = world.downloads / "magazines"
    write_torrent_files(source, files)
    return world.qbit.add(n, folder, "magazines", "/downloads/magazines", files,
                          completion_on=time.time() - age_days * DAY, local_save_path=source)


def pdfs(path) -> list[str]:
    return sorted(p.name for p in path.rglob("*.pdf")) if path.exists() else []


def use_jellyfin(world) -> TargetJellyfin:
    jf = TargetJellyfin()
    world.scanner.jellyfin = RefreshCoordinator(world.repo, jf)
    world.settings(jellyfin_url=CONFIGURED.jellyfin_url, jellyfin_api_key=CONFIGURED.jellyfin_api_key,
                   jellyfin_library_id=LIB1_JF, jellyfin_library_name="News")
    return jf


# --- model and storage ----------------------------------------------------------------------------
def test_first_library_is_what_the_settings_pages_edit(world):
    (first,) = world.repo.libraries()
    assert (first.id, first.name, first.category) == (1, "News", "news")
    assert first.source_dir == str(world.source) and first.dest_dir == str(world.dest)
    world.settings(dest_dir=str(world.dest.parent / "papers"), retention_days=12, qbit_category="dailies")
    first = world.repo.library(1)
    assert (first.dest_dir, first.retention_days, first.category) == (str(world.dest.parent / "papers"), 12,
                                                                      "dailies")
    with world.db.connect() as conn:
        assert conn.execute("SELECT 1 FROM settings WHERE key = 'dest_dir'").fetchone() is None, \
            "library values are stored with the library, not twice"
    assert world.store.load().retention_days == 12


def test_defaults_match_between_settings_library_and_migration(tmp_path):
    fresh = Repo(Database(tmp_path / "fresh.db"))
    fresh.db.migrate()
    (first,) = fresh.libraries()
    defaults = Library()
    settings = Settings()
    for field in ("title_format", "monthly_title_format", "numbered_title_format",
                  "language", "cover_width", "retention_days", "category"):
        assert getattr(first, field) == getattr(defaults, field), field
    for field in ("source_dir", "dest_dir"):   # stored as written; the model normalises for this OS
        assert os.path.normpath(getattr(first, field)) == os.path.normpath(getattr(defaults, field)), field
    for field in ("source_dir", "dest_dir", "title_format", "language", "cover_width", "retention_days"):
        assert getattr(settings, field) == getattr(defaults, field), field


def test_upgrade_from_1_2_0_builds_the_first_library(tmp_path, monkeypatch):
    path = tmp_path / "periodica.db"
    migrations = list(db_module.MIGRATIONS)
    monkeypatch.setattr(db_module, "MIGRATIONS", migrations[:3])
    Database(path).migrate()
    stored = {"source_dir": "/data/torrents/papers", "dest_dir": "/data/media/papers", "qbit_category": "papers",
              "language": "fi", "cover_width": 800, "retention_days": 14, "title_format": "{paper} {iso}",
              "jellyfin_library_id": LIB1_JF, "jellyfin_library_name": "Papers", "qbit_url": "http://10.0.0.2:8080"}
    with sqlite3.connect(path) as conn:
        for key, value in stored.items():
            conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))
        conn.execute("INSERT INTO papers (name, created_at, numbering) VALUES ('Chronicle', 1, 'month')")
        conn.execute("INSERT INTO issues (id, paper, issue_key, issue_label, period, issue_date, torrent_hash, "
                     "source_path, dest_dir, status, keep, linked_at, updated_at) VALUES (5, 'Chronicle', "
                     "'2026-09', '2026-09', 'month', '2026-09-01', 'abc', '/x.pdf', '/y', 'linked', 1, 1, 1)")
        conn.execute("INSERT INTO numbering_evidence (paper, year, number, seen_at) VALUES ('Chronicle', 2026, 1, 1)")
        conn.execute("INSERT INTO torrents (hash, name, category, state, last_seen) "
                     "VALUES ('abc', 'Pack', 'papers', 'stalledUP', 1)")
        conn.execute("INSERT INTO ignored_files (torrent_hash, path, ignored_at, ignored_by) "
                     "VALUES ('abc', 'Pack/x.pdf', 1, 'admin')")

    monkeypatch.setattr(db_module, "MIGRATIONS", migrations)
    db = Database(path)
    db.migrate()
    assert list(tmp_path.glob("periodica.db.bak-v3-*"))
    repo = Repo(db)
    (library,) = repo.libraries()
    assert (library.id, library.name, library.category, library.source_dir, library.dest_dir) == (
        1, "News", "papers", os.path.normpath("/data/torrents/papers"), os.path.normpath("/data/media/papers"))
    assert (library.language, library.cover_width, library.retention_days, library.title_format) == (
        "fi", 800, 14, "{paper} {iso}")
    assert (library.jellyfin_library_id, library.jellyfin_library_name) == (LIB1_JF, "Papers")
    issue = repo.issue_by_key("Chronicle", "2026-09", 1)
    assert issue is not None and issue.id == 5 and issue.keep and issue.library_id == 1
    assert repo.paper("Chronicle", 1).numbering == "month"
    assert repo.january_numbers("Chronicle", 2026, 1) == {1}
    assert repo.torrent("abc")["library_id"] == 1
    assert repo.ignored_paths("abc") == {"Pack/x.pdf"}
    settings = SettingsStore(db).load()
    assert settings.qbit_category == "papers" and settings.qbit_url == "http://10.0.0.2:8080"


@pytest.mark.parametrize("changes, problem", [
    ({"name": "news"}, "name 'news' is already used"),
    ({"category": "news"}, "category 'news' already belongs"),
    ({"source_dir": "SOURCE/sub"}, "source folder overlaps the source folder"),
    ({"dest_dir": "DEST"}, "destination folder overlaps the destination folder"),
    ({"dest_dir": "SOURCE/library"}, "destination folder overlaps the source folder"),
])
def test_library_conflicts(world, changes, problem):
    values = {"name": "Other", "category": "other", "source_dir": str(world.downloads / "other"),
              "dest_dir": str(world.dest.parent / "other")}
    for key, value in changes.items():
        values[key] = value.replace("SOURCE", str(world.source)).replace("DEST", str(world.dest))
    found = library_conflicts(Library(**values), world.repo.libraries())
    assert any(problem in p for p in found), found
    assert library_conflicts(Library(**{**values, **{k: v for k, v in values.items() if k not in changes},
                                        "name": "Clean", "category": "clean",
                                        "source_dir": str(world.downloads / "clean"),
                                        "dest_dir": str(world.dest.parent / "clean")}),
                             world.repo.libraries()) == []


# --- scanning -------------------------------------------------------------------------------------
def test_each_library_links_only_its_own_category(world):
    magazines = add_magazines(world)
    jf = use_jellyfin(world)
    add_pack(world, 1, date(2026, 9, 15))
    magazine_hash = add_magazine_pack(world, 2, date(2026, 9, 15))
    report = world.scanner.run()
    assert report.error is None, report.error
    assert report.torrents == 2 and report.linked == 5
    assert pdfs(world.dest) == ["Chronicle 2026-09-15.pdf", "Côte-Nord Gazette 2026-09-15.pdf",
                                "Evening Post 2026-09-15.pdf"]
    assert pdfs(world.dest.parent / "magazines") == ["Chronicle 2026-09-15.pdf", "Harbour Monthly 2026-09.pdf"]
    # The same publication name in two libraries stays two publications.
    assert {(p.library_id, p.name) for p in world.repo.papers() if p.name == "Chronicle"} == {
        (1, "Chronicle"), (magazines.id, "Chronicle")}
    assert world.repo.torrent(magazine_hash)["library_id"] == magazines.id
    _wait_idle(world.scanner.jellyfin)
    assert sorted(jf.folders) == [LIB1_JF, LIB2_JF] and jf.full == 0

    # Only the library that changed is refreshed.
    add_magazine_pack(world, 3, date(2026, 9, 16), papers=("Harbour.Monthly",))
    report = world.scanner.run()
    _wait_idle(world.scanner.jellyfin)
    assert report.linked == 0 and report.already_linked >= 1
    jf.folders.clear()
    add_magazine_pack(world, 4, date(2026, 10, 1), papers=("Harbour.Monthly",))
    world.scanner.run()
    _wait_idle(world.scanner.jellyfin)
    assert jf.folders == [LIB2_JF]


def test_a_library_without_a_jellyfin_library_asks_for_a_full_scan(world):
    add_magazines(world, jellyfin_library_id="", jellyfin_library_name="")
    jf = use_jellyfin(world)
    add_pack(world, 1, date(2026, 9, 15))
    add_magazine_pack(world, 2, date(2026, 9, 15))
    world.scanner.run()
    _wait_idle(world.scanner.jellyfin)
    assert jf.full == 1 and jf.folders == [], "one scan of everything covers both"


def test_disabled_library_is_left_alone(world):
    magazines = add_magazines(world)
    add_magazine_pack(world, 2, date(2026, 9, 15))
    world.scanner.run()
    linked = pdfs(world.dest.parent / "magazines")
    assert linked
    world.repo.update_library(magazines.model_copy(update={"enabled": False}))
    world.settings(retention_enabled=True, retention_armed=True, retention_days=1, grace_hours=0)
    del world.qbit.torrents[next(iter(world.qbit.torrents))]   # gone from qBittorrent too
    report = world.scanner.run()
    assert report.error is None and report.issues_removed == 0
    assert pdfs(world.dest.parent / "magazines") == linked, "a disabled library's issues are never removed"
    assert all(t["present"] for t in world.repo.torrents() if t["library_id"] == magazines.id)


def test_a_misconfigured_library_is_skipped_and_the_others_still_run(world):
    add_magazines(world, source_dir=str(world.downloads / "missing"))
    add_pack(world, 1, date(2026, 9, 15))
    report = world.scanner.run(trigger="Manual scan by admin")
    assert report.linked == 3
    assert "library 'Magazines'" in report.error and "does not exist" in report.error
    assert any("some libraries were skipped" in a["message"] for a in world.repo.activity(20))


def test_overlapping_libraries_are_both_skipped(world):
    """Only possible by editing the database (the settings pages refuse it); nothing is guessed."""
    add_magazines(world, source_dir=str(world.source / "inside"))
    (world.source / "inside").mkdir()
    add_pack(world, 1, date(2026, 9, 15))
    report = world.scanner.run()
    assert report.linked == 0 and "overlaps" in report.error


def test_days_to_keep_are_per_library(world):
    add_magazines(world, retention_days=30)
    add_pack(world, 1, date(2026, 9, 1), age_days=10)
    magazine_hash = add_magazine_pack(world, 2, date(2026, 9, 1), age_days=10)
    world.settings(retention_enabled=True, retention_armed=True, retention_days=7, grace_hours=0)
    report = world.scanner.run()   # no grace period: queued and deleted in the same scan
    assert report.torrents_deleted == 1
    assert world.qbit.deleted and world.qbit.deleted[0][0] != magazine_hash
    assert pdfs(world.dest) == [] and pdfs(world.dest.parent / "magazines")


def test_manual_delete_uses_the_torrents_own_library(client):
    setup_admin(client)
    world = client.world
    add_magazines(world)
    magazine_hash = add_magazine_pack(world, 2, date(2026, 9, 15))
    world.scanner.run()
    settings = world.store.load()
    delete_torrent_now(world.scanner, world.repo, settings, world.scanner.qbit_factory,
                       world.scanner.jellyfin_factory, magazine_hash, "admin")
    assert world.qbit.deleted == [(magazine_hash, True)]
    assert pdfs(world.dest.parent / "magazines") == []


def test_folder_mode_watches_each_library_source(world):
    magazines = add_magazines(world)
    folder_mode(world)
    add_pack(world, 1, date(2026, 9, 15))
    add_magazine_pack(world, 2, date(2026, 9, 15))
    age(world.source, 10)
    age(world.downloads / "magazines", 10)
    report = world.scanner.run()
    assert report.error is None and report.torrents == 2 and report.linked == 5
    pack = "Magazines 15 09 2026"
    assert world.repo.torrent(folder_download_id(pack, magazines.id))["library_id"] == magazines.id
    assert folder_download_id(pack, magazines.id) != folder_download_id(pack)


# --- scan API -------------------------------------------------------------------------------------
def call(client, **data):
    key = client.world.store.load().api_key
    return client.post("/api/v1/scan", headers={"X-Api-Key": key}, data=data)


def test_api_accepts_every_library_category(client):
    setup_admin(client)
    add_magazines(client.world)
    assert call(client, category="magazines").json()["status"] == "scan queued"
    assert call(client, category="news").json()["status"] == "scan queued"
    assert call(client, category="movies").json()["status"] == "ignored"


def test_folder_mode_api_marks_the_download_of_the_named_library(client):
    setup_admin(client)
    world = client.world
    magazines = add_magazines(client.world)
    folder_mode(world)
    r = call(client, category="magazines", path="/downloads/magazines/Magazines 15 09 2026")
    assert r.json()["status"] == "scan queued"
    assert set(world.repo.get_state(API_FINISHED_KEY)) == {
        folder_download_id("Magazines 15 09 2026", magazines.id)}
    r = call(client, category="news", path="/downloads/magazines/Other pack")
    assert r.json()["status"] == "ignored", "the path must be inside the named library's source folder"


# --- Jellyfin -------------------------------------------------------------------------------------
def test_refresh_requests_merge_targets(world):
    jf = TargetJellyfin()
    coord = RefreshCoordinator(world.repo, jf)
    coord.request(CONFIGURED, "a", targets=[(LIB1_JF, "News")], wait=True)
    coord.request(CONFIGURED, "b", targets=[(LIB1_JF, "News"), (LIB2_JF, "Magazines")], wait=True)
    _wait_idle(coord)
    assert jf.folders == [LIB1_JF, LIB1_JF, LIB2_JF]
    coord.request(CONFIGURED, "c", targets=[], wait=True)
    assert jf.folders == [LIB1_JF, LIB1_JF, LIB2_JF], "nothing to scan, nothing asked"

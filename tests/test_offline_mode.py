"""OFFLINE_MODE: qBittorrent, Jellyfin and the scan API are forced off; saved settings are kept."""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest
from fastapi.testclient import TestClient

from periodica import netsafe
from periodica.app import create_app
from periodica.config import load_env
from periodica.jellyfin import RefreshCoordinator
from periodica.settings import SettingsStore

from .test_folder_source import PACK, age, folder_mode, pack
from .test_jellyfin_refresh import CONFIGURED, RecordingJellyfin, _wait_idle
from .test_scanner_retention import add_pack, library_pdfs
from .test_web import setup_admin

JELLYFIN = {"jellyfin_url": CONFIGURED.jellyfin_url, "jellyfin_api_key": CONFIGURED.jellyfin_api_key}


def refuse(settings):
    raise AssertionError("no client may be created in offline mode")


@pytest.fixture
def offline(world):
    """The app with OFFLINE_MODE on, over a database configured for qBittorrent + Jellyfin + API."""
    world.settings(retention_enabled=True, retention_armed=True, retention_days=1, grace_hours=0, **JELLYFIN)
    jf = RecordingJellyfin()
    app = create_app(dataclasses.replace(world.env, offline=True), qbit_factory=refuse,
                     jellyfin_factory=jf, run_scheduler=False)
    app.state.scanner.jellyfin = RefreshCoordinator(world.repo, jf)
    try:
        with TestClient(app, base_url="http://nas.local:8765") as c:
            c.world = world
            c.jellyfin = jf
            yield c
    finally:
        netsafe.OFFLINE = False


def stored(world):
    return SettingsStore(world.db).load()


def test_env_variable(monkeypatch):
    monkeypatch.setenv("OFFLINE_MODE", "true")
    assert load_env().offline
    monkeypatch.setenv("OFFLINE_MODE", "0")
    assert not load_env().offline
    monkeypatch.delenv("OFFLINE_MODE")
    assert not load_env().offline


def test_overrides_apply_at_runtime_only(world):
    store = SettingsStore(world.db, offline=True)
    s = store.load()
    assert s.offline and not s.uses_qbit and not s.jellyfin_enabled and not s.api_enabled
    s.settle_minutes = 9
    store.save(s)
    original = stored(world)
    assert original.uses_qbit and original.jellyfin_enabled and original.api_enabled and not original.offline
    assert original.settle_minutes == 9, "other settings are still saved"


def test_scan_links_and_contacts_nothing(offline):
    world = offline.world
    add_pack(world, 1, date(2026, 9, 15), age_days=30)
    age(world.source / PACK, 10)
    report = offline.app.state.scanner.run(trigger="test")
    assert report.error is None and report.linked == 3
    assert not report.jellyfin_refreshed and report.torrents_deleted == 0
    _wait_idle(offline.app.state.scanner.jellyfin)
    assert offline.jellyfin.full == 0 and offline.jellyfin.folders == []
    assert len(library_pdfs(world)) == 3 and (world.source / PACK).is_dir()
    # Settings passed in explicitly (a dry run from the settings page) are forced too.
    preview = offline.app.state.scanner.run(dry_run=True, settings=stored(world))
    assert preview.error is None


def test_http_clients_cannot_be_created(offline):
    with pytest.raises(netsafe.UnsafeUrlError, match="offline mode"):
        netsafe.SafeHttpClient("http://192.168.1.10:8080")


def test_scan_api_is_gone_and_cannot_be_turned_on(offline):
    token = setup_admin(offline)
    key = stored(offline.world).api_key
    assert offline.post("/api/v1/scan", headers={"X-Api-Key": key}, data={"category": "news"}).status_code == 404
    r = offline.post("/system/api", data={"csrf_token": token, "enabled": "true"}, follow_redirects=False)
    assert "msg=api_locked" in r.headers["location"]
    assert offline.post("/api/v1/scan", headers={"X-Api-Key": key}).status_code == 404
    page = offline.get("/system").text
    assert key not in page and "Turn API on" not in page and "locked by <code>OFFLINE_MODE</code>" in page


def test_connection_tests_and_jellyfin_scan_are_refused(offline):
    token = setup_admin(offline)
    assert offline.post("/settings-test/qbittorrent", data={"csrf_token": token}).status_code == 409
    assert offline.post("/settings-test/jellyfin", data={"csrf_token": token}).status_code == 409
    assert offline.post("/jellyfin/scan", data={"csrf_token": token}).status_code == 409
    assert offline.jellyfin.full == 0


def test_forms_cannot_turn_the_network_back_on(offline):
    token = setup_admin(offline)
    world = offline.world
    r = offline.post("/settings/client", data={"csrf_token": token, "download_client": "qbittorrent",
                                               "settle_minutes": "8", "qbit_category": "news"},
                    follow_redirects=False)
    assert r.status_code == 303, "nothing risky changed, so no confirmation page"
    r = offline.post("/settings/jellyfin", data={"csrf_token": token, "jellyfin_enabled": "on",
                                                 "jellyfin_url": JELLYFIN["jellyfin_url"]},
                    follow_redirects=False)
    assert r.status_code == 303
    effective = offline.app.state.store.load()
    assert not effective.uses_qbit and not effective.jellyfin_active and effective.settle_minutes == 8
    saved = stored(world)
    assert saved.uses_qbit and saved.jellyfin_enabled and saved.api_enabled


def test_pages_show_the_lock(offline):
    setup_admin(offline)
    for path in ("/", "/system", "/settings?section=client", "/deletions", "/unmatched"):
        assert "Offline mode" in offline.get(path).text, path
    dashboard = offline.get("/").text
    assert dashboard.count("Off: offline mode") == 3 and "conn-ok" not in dashboard
    client_page = offline.get("/settings?section=client").text
    assert "Locked to <strong>watching the source folder</strong>" in client_page
    jellyfin_page = offline.get("/settings?section=jellyfin").text
    assert "locked by <code>OFFLINE_MODE</code>" in jellyfin_page and "Scan library now" not in jellyfin_page


def test_round_trip_keeps_everything(world):
    """qBittorrent mode -> offline -> back: same issues, nothing relinked, the kept torrent stays kept."""
    h = add_pack(world, 1, date(2026, 9, 15))
    age(world.source / PACK, 10)
    world.scanner.run()
    ids = sorted(i.id for i in world.repo.linked_issues())
    world.repo.set_torrent_keep(h, True)
    qbit_factory = world.scanner.qbit_factory

    offline_store = SettingsStore(world.db, offline=True)
    world.scanner.store = offline_store
    world.scanner.qbit_factory = refuse
    report = world.scanner.run()
    assert report.linked == 0 and report.already_linked == 3

    world.scanner.store = world.store
    world.scanner.qbit_factory = qbit_factory
    report = world.scanner.run()
    assert report.linked == 0 and report.already_linked == 3
    assert sorted(i.id for i in world.repo.linked_issues()) == ids
    assert {i.torrent_hash for i in world.repo.linked_issues()} == {h}
    assert world.repo.torrent(h)["keep"] and world.repo.torrent(h)["present"]
    assert len(library_pdfs(world)) == 3


def test_ignore_choices_survive_a_mode_switch(world):
    special = f"{PACK}/Special edition.pdf"
    h = add_pack(world, 1, date(2026, 9, 15), extra_files=[special])
    age(world.source / PACK, 10)
    world.scanner.run()
    world.repo.ignore_file(h, special, "admin")
    qbit_factory = world.scanner.qbit_factory

    folder_mode(world)
    world.scanner.run()
    assert [i["path"] for i in world.repo.ignored_files()] == [], "the qBittorrent choice is not shown in folder mode"
    assert {u["path"] for u in world.repo.unmatched()} == {special}, "and the folder copy is listed once"

    world.scanner.qbit_factory = qbit_factory
    world.settings(download_client="qbittorrent")
    report = world.scanner.run()
    assert report.unmatched == 0, "the ignore choice made before the switch still applies"
    assert [i["path"] for i in world.repo.ignored_files()] == [special]


def test_removed_issues_stay_removed_in_folder_mode(world):
    pack(world)
    folder_mode(world)
    world.scanner.run()
    issue = next(i for i in world.repo.linked_issues() if i.paper == "Chronicle")
    import shutil

    shutil.rmtree(issue.dest_dir)
    world.repo.set_issue_status(issue.id, "removed")   # as automatic delete leaves it
    report = world.scanner.run()
    assert report.linked == 0
    assert world.repo.issue(issue.id).status == "removed"

"""Connection rows (in use / failing / not used) and the Jellyfin and scan API switches."""

from __future__ import annotations

from datetime import date

from periodica.jellyfin import RefreshCoordinator
from periodica.web.connections import BAD, IDLE, OFF, OK, connections

from .test_jellyfin_refresh import CONFIGURED, RecordingJellyfin, _wait_idle
from .test_scanner_retention import add_pack
from .test_web import csrf_of, setup_admin

JELLYFIN = {"jellyfin_url": CONFIGURED.jellyfin_url, "jellyfin_api_key": CONFIGURED.jellyfin_api_key}


def states(world) -> dict[str, str]:
    return {c.name: c.state for c in connections(world.store.load(), world.repo)}


def test_rows_follow_the_last_contact(world):
    assert states(world) == {"qBittorrent": IDLE, "Jellyfin": OFF, "Scan API": OK}
    world.scanner.run()
    assert states(world)["qBittorrent"] == OK
    world.qbit.fail = True
    world.scanner.run()
    row = next(c for c in connections(world.store.load(), world.repo) if c.name == "qBittorrent")
    assert row.state == BAD and row.detail
    world.settings(qbit_url="http://192.168.1.20:8080")
    assert states(world)["qBittorrent"] == IDLE, "a result for another address is not shown"
    world.settings(qbit_url="")
    assert states(world)["qBittorrent"] == BAD, "qBittorrent is still required"


def test_jellyfin_rows(world):
    jf = RecordingJellyfin()
    coord = RefreshCoordinator(world.repo, jf)
    s = world.settings(**JELLYFIN)
    assert states(world)["Jellyfin"] == IDLE
    coord.request(s, "scan", wait=True)
    _wait_idle(coord)
    assert states(world)["Jellyfin"] == OK
    s = world.settings(jellyfin_enabled=False)
    assert states(world)["Jellyfin"] == OFF
    coord.request(s, "scan", wait=True)
    _wait_idle(coord)
    assert jf.full == 1, "a switched-off Jellyfin is never contacted"


def test_switched_off_jellyfin_is_not_asked_to_scan(world):
    jf = RecordingJellyfin()
    world.scanner.jellyfin = RefreshCoordinator(world.repo, jf)
    world.settings(jellyfin_enabled=False, **JELLYFIN)
    add_pack(world, 1, date(2026, 9, 15))
    report = world.scanner.run()
    assert report.linked == 3 and not report.jellyfin_refreshed
    _wait_idle(world.scanner.jellyfin)
    assert jf.full == 0 and jf.folders == []


def test_jellyfin_switch_is_saved_and_keeps_the_key(client):
    token = setup_admin(client)
    world = client.world
    data = {"csrf_token": token, "jellyfin_url": JELLYFIN["jellyfin_url"],
            "jellyfin_api_key": JELLYFIN["jellyfin_api_key"], "jellyfin_enabled": "on"}
    client.post("/settings/jellyfin", data=data)
    assert world.store.load().jellyfin_active
    data.pop("jellyfin_enabled")
    data["jellyfin_api_key"] = ""
    client.post("/settings/jellyfin", data=data)
    saved = world.store.load()
    assert not saved.jellyfin_enabled and saved.jellyfin_api_key == JELLYFIN["jellyfin_api_key"]
    page = client.get("/").text
    assert "conn-off" in page and "Switched off" in page


def test_pages_show_the_connection_card(client):
    setup_admin(client)
    for path in ("/", "/system"):
        page = client.get(path).text
        assert "Connections" in page and "qBittorrent" in page and "Scan API" in page, path


def test_scan_api_can_be_switched_off(client):
    token = setup_admin(client)
    world = client.world
    key = world.store.load().api_key
    r = client.post("/system/api", data={"csrf_token": token, "enabled": "false"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=api_off" in r.headers["location"]
    assert not world.store.load().api_enabled
    assert client.post("/api/v1/scan", headers={"X-Api-Key": key}).status_code == 404
    assert client.post("/api/v1/scan", headers={"X-Api-Key": "wrong"}).status_code == 404
    assert client.app.state.scan_trigger is None
    page = client.get("/system").text
    assert key not in page, "the key is not shown while the API is off"
    assert "Turn API on" in page
    assert states(world)["Scan API"] == OFF

    token = csrf_of(page)
    client.post("/system/api", data={"csrf_token": token, "enabled": "true"})
    assert world.store.load().api_key == key, "the key survives"
    assert client.post("/api/v1/scan", headers={"X-Api-Key": key}).status_code == 202


def test_scan_api_switch_needs_csrf(client):
    setup_admin(client)
    r = client.post("/system/api", data={"csrf_token": "wrong", "enabled": "false"})
    assert r.status_code == 403
    assert client.world.store.load().api_enabled


def test_manual_jellyfin_library_scan(client):
    token = setup_admin(client)
    world = client.world
    jf = RecordingJellyfin()
    client.app.state.scanner.jellyfin = RefreshCoordinator(world.repo, jf)
    assert "Scan library now" not in client.get("/settings?section=jellyfin").text
    r = client.post("/jellyfin/scan", data={"csrf_token": token}, follow_redirects=False)
    assert "msg=jf_off" in r.headers["location"] and jf.full == 0

    world.settings(**JELLYFIN)
    assert "Scan library now" in client.get("/settings?section=jellyfin").text
    # Unsaved form fields are ignored: the saved URL and key are used.
    r = client.post("/jellyfin/scan", data={"csrf_token": token, "jellyfin_url": "http://10.0.0.99:8096"},
                    follow_redirects=False)
    assert "msg=jf_scan" in r.headers["location"]
    _wait_idle(client.app.state.scanner.jellyfin)
    assert jf.full == 1
    assert "manual library scan by admin" in world.repo.activity(1)[0]["message"]
    assert states(world)["Jellyfin"] == OK

    world.settings(jellyfin_enabled=False)
    client.post("/jellyfin/scan", data={"csrf_token": token})
    assert jf.full == 1
    assert client.post("/jellyfin/scan", data={"csrf_token": "wrong"}).status_code == 403

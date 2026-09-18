from __future__ import annotations

import re

import pytest

PASSWORD = "correct horse battery"


def csrf_of(html: str) -> str:
    match = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert match, "csrf meta tag missing"
    return match.group(1)


def setup_admin(client) -> str:
    r = client.post("/setup", data={"username": "admin", "password": PASSWORD, "password2": PASSWORD},
                    follow_redirects=False)
    assert r.status_code == 303
    return csrf_of(client.get("/").text)


def test_first_run_requires_setup_and_no_default_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    r = client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)
    assert r.status_code == 401


def test_setup_only_once(client):
    setup_admin(client)
    client.cookies.clear()
    r = client.post("/setup", data={"username": "evil", "password": PASSWORD, "password2": PASSWORD},
                    follow_redirects=False)
    assert r.headers["location"] == "/login"
    assert client.post("/login", data={"username": "evil", "password": PASSWORD}).status_code == 401


def test_weak_password_rejected(client):
    r = client.post("/setup", data={"username": "admin", "password": "short", "password2": "short"})
    assert r.status_code == 400
    assert "at least 10" in r.text


def test_security_headers_and_cookie_flags(client):
    r = client.post("/setup", data={"username": "admin", "password": PASSWORD, "password2": PASSWORD},
                    follow_redirects=False)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    page = client.get("/")
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["referrer-policy"] == "same-origin"
    assert page.headers["cache-control"] == "no-store"
    assert "<script>" not in page.text  # no inline scripts


def test_post_without_csrf_is_rejected(client):
    setup_admin(client)
    assert client.post("/scan").status_code == 403
    assert client.post("/scan", data={"csrf_token": "wrong"}).status_code == 403


def test_cross_origin_post_is_rejected(client):
    token = setup_admin(client)
    r = client.post("/scan", data={"csrf_token": token}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = client.post("/scan", data={"csrf_token": token}, headers={"Origin": "http://nas.local:8765"},
                    follow_redirects=False)
    assert r.status_code == 303


def test_browser_setup_with_real_browser_headers(client):
    # Firefox/Chrome form post from the page itself (regression: Origin: null used to be rejected).
    headers = {"Origin": "null", "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate"}
    r = client.post("/setup", data={"username": "admin", "password": PASSWORD, "password2": PASSWORD},
                    headers=headers, follow_redirects=False)
    assert r.status_code == 303
    token = csrf_of(client.get("/").text)
    r = client.post("/scan", data={"csrf_token": token},
                    headers={"Origin": "http://nas.local:8765", "Sec-Fetch-Site": "same-origin"},
                    follow_redirects=False)
    assert r.status_code == 303


@pytest.mark.parametrize("site", ["cross-site", "same-site", "none"])
def test_cross_site_fetch_metadata_is_rejected(client, site):
    token = setup_admin(client)
    r = client.post("/scan", data={"csrf_token": token}, headers={"Sec-Fetch-Site": site})
    assert r.status_code == 403
    client.cookies.clear()
    r = client.post("/login", data={"username": "admin", "password": PASSWORD}, headers={"Sec-Fetch-Site": site})
    assert r.status_code == 403


def test_null_origin_without_fetch_metadata_is_rejected(client):
    token = setup_admin(client)
    assert client.post("/scan", data={"csrf_token": token}, headers={"Origin": "null"}).status_code == 403


def test_login_throttling(client):
    setup_admin(client)
    client.cookies.clear()
    for _ in range(5):
        assert client.post("/login", data={"username": "admin", "password": "nope"}).status_code == 401
    r = client.post("/login", data={"username": "admin", "password": PASSWORD})
    assert r.status_code == 429


def test_logout_invalidates_session(client):
    token = setup_admin(client)
    session_cookie = client.cookies.get("nl_session")
    client.post("/logout", data={"csrf_token": token})
    client.cookies.set("nl_session", session_cookie)
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_flash_messages_cannot_be_injected(client):
    setup_admin(client)
    r = client.get("/?msg=<script>alert(1)</script>")
    assert "alert(1)" not in r.text


def library_form(world, **changes) -> dict:
    """What the library page posts for library 1, with changes."""
    lib = world.repo.library(1)
    data = {field: str(getattr(lib, field)) for field in (
        "name", "category", "source_dir", "dest_dir", "jellyfin_library_id", "jellyfin_library_name",
        "title_format", "monthly_title_format", "numbered_title_format", "language", "cover_width",
        "retention_days")}
    data.update({k: str(v) for k, v in changes.items()})
    return data


def test_risky_setting_requires_confirmation(client):
    token = setup_admin(client)
    world = client.world
    r = client.post("/settings/client", data={
        "csrf_token": token, "qbit_url": world.qbit.base_url, "qbit_username": "admin", "qbit_password": "",
        "path_map_remote": "/storage", "path_map_local": str(world.downloads),
    })
    assert r.status_code == 200
    assert "Confirm changes" in r.text
    assert world.store.load().path_map_remote == "/downloads"  # not applied yet
    confirm_token = re.search(r'name="token" value="([^"]+)"', r.text).group(1)
    r = client.post("/settings-confirm", data={"csrf_token": token, "token": confirm_token, "action": "confirm"},
                    follow_redirects=False)
    assert r.status_code == 303
    saved = world.store.load()
    assert saved.path_map_remote == "/storage"
    assert saved.qbit_password == "adminadmin"  # blank password field keeps the stored secret


def test_risky_library_change_requires_confirmation(client):
    token = setup_admin(client)
    world = client.world
    r = client.post("/settings/libraries/1", data={"csrf_token": token, **library_form(world, category="movies")})
    assert r.status_code == 200 and "Confirm changes" in r.text
    assert world.repo.library(1).category == "news"  # not applied yet
    confirm_token = re.search(r'name="token" value="([^"]+)"', r.text).group(1)
    assert 'action="/settings/libraries/1/confirm"' in r.text
    r = client.post("/settings/libraries/1/confirm", follow_redirects=False,
                    data={"csrf_token": token, "token": confirm_token, "action": "confirm"})
    assert r.status_code == 303
    assert world.repo.library(1).category == "movies"


def test_retention_change_disarms(client):
    token = setup_admin(client)
    world = client.world
    world.settings(retention_enabled=True, retention_armed=True)
    r = client.post("/settings/retention", data={
        "csrf_token": token, "retention_enabled": "on", "grace_hours": "12",
        "max_torrent_deletions_per_run": "20",
    }, follow_redirects=False)
    assert r.status_code == 303
    s = world.store.load()
    assert s.grace_hours == 12 and s.retention_armed is False

    world.settings(retention_armed=True)
    r = client.post("/settings/libraries/1", data={"csrf_token": token, **library_form(world, retention_days=3)},
                    follow_redirects=False)
    assert r.status_code == 303
    s = world.store.load()
    assert s.retention_days == 3 and s.retention_armed is False, "a library's days count too"


def test_arm_requires_explicit_confirmation(client):
    token = setup_admin(client)
    client.world.settings(retention_enabled=True)
    client.post("/deletions/arm", data={"csrf_token": token})
    assert client.world.store.load().retention_armed is False
    client.post("/deletions/arm", data={"csrf_token": token, "confirm": "yes"})
    assert client.world.store.load().retention_armed is True


def test_invalid_settings_show_errors(client):
    token = setup_admin(client)
    r = client.post("/settings/libraries/1", data={
        "csrf_token": token, **library_form(client.world, source_dir="relative/path",
                                            title_format="{paper.__class__}"),
    })
    assert r.status_code == 400
    assert "Source folder" in r.text and "Title format" in r.text
    r = client.post("/settings/general", data={"csrf_token": token, "scan_interval_minutes": "-1"})
    assert r.status_code == 400


def test_qbittorrent_test_endpoint(client):
    token = setup_admin(client)
    world = client.world
    r = client.post("/settings-test/qbittorrent", headers={"X-CSRF-Token": token}, data={
        "qbit_url": world.qbit.base_url, "qbit_username": "admin", "qbit_password": "", "qbit_category": "news",
        "path_map_remote": "", "path_map_local": "",
    })
    data = r.json()
    assert data["ok"] is True and "news" in data["categories"]


def test_manual_scan_marks_pending_and_status_reports_it(client):
    token = setup_admin(client)
    client.post("/scan", data={"csrf_token": token}, follow_redirects=False)
    assert client.app.state.scan_trigger == "Manual scan by admin"
    status = client.get("/scan/status").json()
    assert status["queued"] is True and status["running"] is False
    page = client.get("/")
    assert 'data-scan-pending="true"' in page.text and "Scanning…" in page.text


def test_dashboard_pending_deletions_is_a_number(client):
    setup_admin(client)
    page = client.get("/").text
    assert "False" not in page and ">0</span><span>Pending deletions" in page


def _linked_pack(client):
    from datetime import date

    from .test_scanner_retention import add_pack

    world = client.world
    h = add_pack(world, 1, date(2026, 9, 15))
    assert world.scanner.run().linked == 3
    return world, h


def test_manual_delete_requires_confirmation_then_deletes(client):
    token = setup_admin(client)
    world, h = _linked_pack(client)
    page = client.get(f"/torrents/{h}/delete")
    assert page.status_code == 200 and "cannot be undone" in page.text and "Chronicle 2026-09-15" in page.text
    assert "data-confirm" not in page.text  # the checkbox is the confirmation
    assert 'id="confirm-dialog"' in page.text
    assert "Jellyfin integration isn" in page.text
    assert h in client.get("/deletions").text

    r = client.post(f"/torrents/{h}/delete", data={"csrf_token": token}, follow_redirects=False)
    assert r.headers["location"].endswith("/delete") and world.qbit.deleted == []  # no checkbox -> nothing
    assert client.post(f"/torrents/{h}/delete", data={"confirm": "yes"}).status_code == 403  # no CSRF

    r = client.post(f"/torrents/{h}/delete", data={"csrf_token": token, "confirm": "yes"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=deleted" in r.headers["location"]
    assert world.qbit.deleted == [(h, True)]
    assert not list(world.dest.rglob("*.pdf"))
    assert not list(world.source.rglob("*.pdf"))
    # the library root must never be empty, or Jellyfin skips the library and keeps stale entries
    assert (world.dest / ".periodica-library").is_file()
    assert [p.name for p in world.dest.iterdir()] == [".periodica-library"]
    history = world.repo.deletion_history()
    assert history[0]["status"] == "done" and "manually by admin" in history[0]["detail"]
    assert h not in client.get("/deletions").text.split("History")[0]


def test_manual_delete_refuses_other_category_and_unexpected_content(client):
    token = setup_admin(client)
    world, h = _linked_pack(client)
    world.qbit.torrents[h]["category"] = "movies"  # changed in qBittorrent after the last scan
    r = client.post(f"/torrents/{h}/delete", data={"csrf_token": token, "confirm": "yes"})
    assert r.status_code == 409 and "category" in r.text
    assert world.qbit.deleted == []

    world.qbit.torrents[h]["category"] = "news"
    world.qbit.files[h].append({"name": "Daily Newspapers 15 09 2026/setup.exe", "size": 1, "progress": 1})
    world.scanner.run()
    page = client.get(f"/torrents/{h}/delete")
    assert "can't be deleted here" in page.text
    r = client.post(f"/torrents/{h}/delete", data={"csrf_token": token, "confirm": "yes"})
    assert r.status_code == 409 and world.qbit.deleted == []


def test_jellyfin_library_setting_validation(client):
    token = setup_admin(client)
    world = client.world
    world.settings(jellyfin_url="http://192.168.1.10:8096", jellyfin_api_key="a" * 32)
    base = {"csrf_token": token, **library_form(world, jellyfin_library_name="News")}
    r = client.post("/settings/libraries/1", data={**base, "jellyfin_library_id": "../../System/Restart"})
    assert r.status_code == 400 and "Jellyfin library" in r.text
    r = client.post("/settings/libraries/1", data={**base, "jellyfin_library_id": "0C0E1E7B5A6B4B0A9F0E3A1D2C3B4A59"},
                    follow_redirects=False)
    assert r.status_code == 303
    saved = world.repo.library(1)
    assert saved.jellyfin_library_id == "0c0e1e7b5a6b4b0a9f0e3a1d2c3b4a59" and saved.jellyfin_library_name == "News"


def test_dashboard_and_system_show_feed_health(client):
    from datetime import date

    from .test_scanner_retention import add_pack

    setup_admin(client)
    world = client.world
    add_pack(world, 1, date(2026, 9, 15), age_days=3)
    world.scanner.run()
    page = client.get("/").text
    assert "No new downloads" in page and "Last news download" in page
    system = client.get("/system").text
    assert "qBittorrent RSS rule adds to category" in system and "News" in system


def test_scan_interval_zero_disables_scheduled_scans(client):
    token = setup_admin(client)
    world = client.world
    r = client.post("/settings/general", data={"csrf_token": token, "scan_interval_minutes": "0"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings/libraries?msg=saved"
    assert world.store.load().scan_interval_minutes == 0
    assert "Scheduled scans off" in client.get("/").text
    r = client.post("/settings/general", data={"csrf_token": token, "scan_interval_minutes": "-1"})
    assert r.status_code == 400


def test_api_scan_requires_key(client, caplog):
    setup_admin(client)
    assert client.post("/api/v1/scan").status_code == 401
    with caplog.at_level("WARNING", logger="periodica"):
        assert client.post("/api/v1/scan", headers={"X-Api-Key": "nope"}).status_code == 401
    # A silently failing trigger is hard to debug from the outside, so the rejection is in the log.
    assert any("API scan rejected" in r.getMessage() for r in caplog.records)
    key = client.world.store.load().api_key
    assert client.post("/api/v1/scan", headers={"X-Api-Key": key}).status_code == 202


def test_api_scan_ignores_other_categories(client):
    setup_admin(client)
    app = client.app
    key = client.world.store.load().api_key
    r = client.post("/api/v1/scan", headers={"X-Api-Key": key}, data={"category": "movies"})
    assert r.json()["status"] == "ignored" and app.state.scan_trigger is None
    r = client.post("/api/v1/scan?category=", headers={"X-Api-Key": key})
    assert r.json()["status"] == "ignored"
    r = client.post("/api/v1/scan", headers={"X-Api-Key": key}, data={"category": "news"})
    assert r.json()["status"] == "scan queued"
    assert app.state.scan_trigger == "API scan (torrent finished in 'news')"
    # wrong key never reveals whether the category matched
    assert client.post("/api/v1/scan", headers={"X-Api-Key": "x"}, data={"category": "news"}).status_code == 401


def test_pages_render(client):
    setup_admin(client)
    for path in ["/", "/papers", "/deletions", "/unmatched", "/activity", "/system",
                 "/settings?section=library", "/settings?section=client", "/settings?section=retention",
                 "/settings?section=jellyfin", "/settings?section=account"]:
        r = client.get(path)
        assert r.status_code == 200, path


def test_build_label_shown(client, monkeypatch):
    from periodica import build_label

    monkeypatch.setenv("PERIODICA_COMMIT", "a0d8210ffffffffffffffffffffffffffffffff")
    assert "· a0d8210" in build_label()
    monkeypatch.setenv("PERIODICA_COMMIT", "<script>")
    assert "<" not in build_label()
    setup_admin(client)
    assert 'class="sidebar-build' in client.get("/").text


def test_logs_page_shows_app_logs_without_healthchecks(client):
    import logging

    from periodica.logbuffer import BUFFER, AccessNoiseFilter, install

    install()
    logging.getLogger("periodica.test").warning("hello <b>logs</b>")
    def access_record(path: str) -> logging.LogRecord:
        return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0, '%s - "%s %s HTTP/%s" %d',
                                 ("127.0.0.1:1", "GET", path, "1.1", 200), None)

    # Polled endpoints must not fill the log that is being polled for.
    assert AccessNoiseFilter().filter(access_record("/healthz")) is False
    assert AccessNoiseFilter().filter(access_record("/system/logs/tail?after=5")) is False
    assert AccessNoiseFilter().filter(access_record("/scan/status")) is False
    assert AccessNoiseFilter().filter(access_record("/papers")) is True
    assert any("hello" in line.message for line in BUFFER.lines("warning"))
    setup_admin(client)
    page = client.get("/system/logs?level=warning")
    assert page.status_code == 200
    assert "hello &lt;b&gt;logs&lt;/b&gt;" in page.text  # escaped

    logging.getLogger("uvicorn.access").warning("GET /papers")
    assert any("GET /papers" in line.message for line in BUFFER.lines("warning"))
    assert not any("GET /papers" in line.message for line in BUFFER.lines("warning", app_only=True))
    app_only = client.get("/system/logs?level=warning&source=app")
    assert "hello" in app_only.text and "GET /papers" not in app_only.text


def test_log_tail_returns_only_new_lines_and_notices_gaps():
    import logging

    from periodica.logbuffer import RingBufferHandler

    buffer = RingBufferHandler(capacity=3)
    for n in range(3):
        buffer.emit(logging.LogRecord("periodica.test", logging.INFO, __file__, 0, f"line {n}", None, None))
    lines, cursor, missed = buffer.since(0)
    assert [line.message for line in lines] == ["line 0", "line 1", "line 2"]
    assert cursor == 3 and missed == 0

    # Nothing new: no lines, same cursor.
    assert buffer.since(cursor)[0] == []

    # The buffer only keeps 3, so a client that stopped at line 0 missed lines 1 and 2.
    for n in range(3, 6):
        buffer.emit(logging.LogRecord("periodica.test", logging.INFO, __file__, 0, f"line {n}", None, None))
    lines, cursor, missed = buffer.since(1)
    assert [line.message for line in lines] == ["line 3", "line 4", "line 5"]
    assert cursor == 6 and missed == 2


def test_log_tail_endpoint_needs_a_session_and_follows_filters(client):
    import logging

    from periodica.logbuffer import BUFFER, install

    assert client.get("/system/logs/tail", follow_redirects=False).status_code in (303, 401, 403)
    setup_admin(client)
    install()
    logging.getLogger("periodica.test").warning("tail-me please")
    logging.getLogger("uvicorn.access").warning("GET /somewhere")

    data = client.get("/system/logs/tail?after=0&level=warning&source=app").json()
    messages = [line["message"] for line in data["lines"]]
    assert "tail-me please" in messages and "GET /somewhere" not in messages
    # The cursor belongs to the view being polled, so an app-only client cannot skip app lines.
    assert data["cursor"] == BUFFER.since(0, app_only=True)[1]

    # Asking again from the returned cursor gives nothing new.
    assert client.get(f"/system/logs/tail?after={data['cursor']}&level=warning&source=app").json()["lines"] == []


def test_browsing_does_not_evict_the_app_log(client):
    import logging

    from periodica.logbuffer import RingBufferHandler

    buffer = RingBufferHandler(capacity=5)
    buffer.emit(logging.LogRecord("periodica.scanner", logging.ERROR, __file__, 0, "the error you came for",
                                  None, None))
    for n in range(50):  # a browsing session: plenty of access lines
        buffer.emit(logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0, f"GET /papers {n}", None, None))

    messages = [line.message for line in buffer.lines()]
    assert "the error you came for" in messages, "app lines must survive a busy browsing session"
    assert len([m for m in messages if m.startswith("GET /papers")]) == 5


def test_debug_level_can_be_switched_on_and_expires(client):
    import logging

    from periodica import logbuffer

    setup_admin(client)
    logbuffer.install("info")
    logging.getLogger("periodica.test").debug("quiet by default")
    assert not any("quiet by default" in line.message for line in logbuffer.BUFFER.lines())

    token = setup_admin(client) if False else csrf_of(client.get("/").text)
    r = client.post("/system/logs/level?source=app&level=info", follow_redirects=False,
                    data={"detail": "debug", "csrf_token": token})
    # The redirect must keep the filters the page was showing.
    assert r.status_code == 303 and "source=app" in r.headers["location"] and "level=info" in r.headers["location"]
    # The page button is a temporary override: the stored setting stays where the user put it.
    assert client.world.store.load().log_level == "info"
    assert logbuffer.current_level() == "debug"
    logging.getLogger("periodica.test").debug("visible now")
    assert any("visible now" in line.message for line in logbuffer.BUFFER.lines())

    # Debug is temporary: once the window passes, the level falls back on its own.
    logbuffer.enforce_level(now=logbuffer.debug_expires_at() + 1)
    assert logbuffer.current_level() == "info"
    logging.getLogger("periodica.test").debug("quiet again")
    assert not any("quiet again" in line.message for line in logbuffer.BUFFER.lines())
    logbuffer.set_level("info")


def test_old_logs_address_still_works(client):
    setup_admin(client)
    r = client.get("/activity/logs?level=warning&source=app", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/system/logs?level=warning&source=app"


def test_logs_page_tells_configured_debug_from_a_temporary_session(client):
    from periodica import logbuffer

    token = setup_admin(client)
    logbuffer.install("info")
    logbuffer.set_level("info")

    page = client.get("/system/logs").text
    assert "Debug for 30 min" in page and "set in" not in page

    # A temporary session: offer to stop it, and count it down.
    client.post("/system/logs/level", data={"detail": "debug", "csrf_token": token})
    page = client.get("/system/logs").text
    assert "Stop debug logging" in page and "min left" in page

    # Debug from Settings is not a session: nothing to stop, and it must never read as ended.
    logbuffer.set_level("info")
    client.post("/settings/logging", data={"csrf_token": token, "log_level": "debug", "log_file_mb": "0",
                                           "log_files_kept": "5"})
    page = client.get("/system/logs").text
    assert "set in" in page and "Settings" in page
    assert "Stop debug logging" not in page and "min left" not in page
    assert client.get("/system/logs/tail?after=0").json()["temporary"] is False

    client.post("/settings/logging", data={"csrf_token": token, "log_level": "info", "log_file_mb": "5",
                                           "log_files_kept": "5"})
    logbuffer.set_level("info")


def test_log_files_are_written_rotated_and_downloadable(client):
    import logging

    from periodica import logbuffer

    setup_admin(client)
    env = client.world.env
    logbuffer.install("info")
    written = logbuffer.configure_file_logging(env.config_dir, True, max_mb=1, keep=3)
    assert written is not None and written.parent == logbuffer.log_dir(env.config_dir)

    logger = logging.getLogger("periodica.test")
    logger.info("a line for the file")
    assert "a line for the file" in written.read_text(encoding="utf-8")

    # Enough volume to rotate at 1 MB, but never more files than asked for.
    for n in range(4000):
        logger.info("padding %s %s", n, "x" * 500)
    names = [name for name, _, _ in logbuffer.log_files(env.config_dir)]
    assert 1 < len(names) <= 3, names

    page = client.get("/system/logs")
    assert "Log files" in page.text and names[0] in page.text
    downloaded = client.get(f"/system/logs/file?name={names[-1]}")
    assert downloaded.status_code == 200 and downloaded.headers["content-type"].startswith("text/plain")

    # Only our own files, by name: no paths, no guesses.
    for bad in ("../periodica.db", "periodica.log/../../periodica.db", "", "other.log"):
        assert client.get("/system/logs/file", params={"name": bad}).status_code == 404

    logbuffer.configure_file_logging(env.config_dir, False, 5, 5)


def test_log_file_settings_take_effect_without_a_restart(client):
    import logging

    from periodica import logbuffer

    token = setup_admin(client)
    r = client.post("/settings/logging", follow_redirects=False, data={
        "csrf_token": token, "log_level": "debug", "log_file_mb": "2", "log_files_kept": "4",
    })
    assert r.status_code == 303
    settings = client.world.store.load()
    assert (settings.log_level, settings.log_file_mb, settings.log_files_kept) == ("debug", 2, 4)
    assert logbuffer.current_level() == "debug"
    assert logbuffer.log_dir(client.world.env.config_dir).is_dir()

    # Size 0 turns file logging off: nothing more is written, existing files are left alone.
    written = logbuffer.log_dir(client.world.env.config_dir) / logbuffer.LOG_FILE_NAME
    before = written.stat().st_size
    client.post("/settings/logging", data={"csrf_token": token, "log_level": "info", "log_file_mb": "0",
                                           "log_files_kept": "4"})
    assert client.world.store.load().log_file_mb == 0
    logging.getLogger("periodica.test").info("this must not reach the file")
    assert written.is_file() and written.stat().st_size == before
    assert "Log files are off" in client.get("/system/logs").text
    logbuffer.set_level("info")


def test_logs_never_contain_secrets(client):
    from periodica import logbuffer

    logbuffer.install("debug")
    logbuffer.set_level("debug")
    setup_admin(client)
    world = client.world
    settings = world.store.load()
    secrets_used = [settings.qbit_password, settings.api_key]
    world.scanner.run(trigger="secret check")
    client.get("/system")
    blob = " ".join(line.message for line in logbuffer.BUFFER.lines(limit=5000))
    for secret in secrets_used:
        assert secret and secret not in blob, "a secret reached the log buffer"
    logbuffer.set_level("info")


def test_healthz_is_public(client):
    assert client.get("/healthz").text == "ok"

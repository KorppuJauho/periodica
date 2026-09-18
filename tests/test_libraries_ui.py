"""Settings → Libraries: list, add wizard (suggestions, checks, dry run), edit, disable and delete."""

from __future__ import annotations

import re
from datetime import date

from periodica.jellyfin import RefreshCoordinator

from .test_jellyfin_refresh import CONFIGURED, _wait_idle
from .test_libraries import LIB1_JF, LIB2_JF, TargetJellyfin, add_magazine_pack, add_magazines, pdfs
from .test_scanner_retention import add_pack
from .test_web import csrf_of, library_form, setup_admin


class ListingJellyfin(TargetJellyfin):
    """A Jellyfin that lists two Books libraries; records every call."""

    def __init__(self):
        super().__init__()
        self.calls: list[str] = []

    def libraries(self):
        self.calls.append("libraries")
        return [{"id": LIB1_JF, "name": "News", "type": "books", "locations": ["/media/books/news"]},
                {"id": LIB2_JF, "name": "Magazines", "type": "books", "locations": ["/media/books/magazines"]}]


def with_jellyfin(client) -> ListingJellyfin:
    jf = ListingJellyfin()
    client.app.state.jellyfin_factory = jf
    client.app.state.scanner.jellyfin = RefreshCoordinator(client.world.repo, jf)
    client.world.settings(jellyfin_url=CONFIGURED.jellyfin_url, jellyfin_api_key=CONFIGURED.jellyfin_api_key)
    return jf


def form_of(library, **changes) -> dict:
    data = {k: str(getattr(library, k)) for k in (
        "name", "category", "source_dir", "dest_dir", "jellyfin_library_id", "jellyfin_library_name",
        "title_format", "monthly_title_format", "numbered_title_format", "language", "cover_width",
        "retention_days")}
    data.update({k: str(v) for k, v in changes.items()})
    return data


def new_form(world, **changes) -> dict:
    data = {"name": "Magazines", "category": "magazines", "source_dir": str(world.downloads / "magazines"),
            "dest_dir": str(world.dest.parent / "magazines"), "jellyfin_library_id": "", "jellyfin_library_name": "",
            "title_format": "{paper} {iso}", "monthly_title_format": "{paper} {iso}",
            "numbered_title_format": "{paper} {year} #{nn}", "language": "en", "cover_width": "600",
            "retention_days": "14"}
    data.update(changes)
    return data


# --- pages ------------------------------------------------------------------------------------------
def test_settings_open_on_the_libraries_page(client):
    setup_admin(client)
    r = client.get("/settings", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings/libraries"
    r = client.get("/settings?section=library", follow_redirects=False)
    assert r.headers["location"] == "/settings/libraries/1", "old bookmarks open the first library"
    page = client.get("/settings/libraries").text
    assert "Add library" in page and "News" in page and "<code>news</code>" in page
    for section in ("client", "retention", "jellyfin"):
        text = client.get(f"/settings?section={section}").text
        assert "/settings/libraries/1" in text, section
    assert 'name="qbit_category"' not in client.get("/settings?section=client").text
    assert 'name="retention_days"' not in client.get("/settings?section=retention").text
    assert 'name="jellyfin_library_id"' not in client.get("/settings?section=jellyfin").text


def test_wizard_page_starts_empty(client):
    setup_admin(client)
    page = client.get("/settings/libraries/new").text
    assert 'data-library-wizard="new"' in page and "Create library" in page
    assert re.search(r'name="source_dir" value=""', page) and re.search(r'name="dest_dir" value=""', page)


# --- suggestions ------------------------------------------------------------------------------------
def test_suggestions_come_from_qbittorrent_and_jellyfin_without_changing_them(client):
    token = setup_admin(client)
    world = client.world
    jf = with_jellyfin(client)
    world.qbit.categories["magazines"] = {"name": "magazines", "savePath": "/downloads/magazines"}
    (world.downloads / "magazines").mkdir()
    world.qbit.requests.clear()
    r = client.post("/settings/libraries/suggest", headers={"X-CSRF-Token": token},
                    data={"library_id": "0", "name": "Magazines", "category": "", "source_dir": "", "dest_dir": ""})
    data = r.json()
    assert data["mode"] == "qbittorrent"
    by_name = {c["name"]: c for c in data["categories"]}
    assert by_name["news"]["used_by"] == "News" and by_name["magazines"]["used_by"] == ""
    assert by_name["magazines"]["save_path"] == "/downloads/magazines"
    assert by_name["magazines"]["local_path"].replace("\\", "/").endswith("/books/magazines")
    assert data["suggested"]["category"] == "magazines"
    assert data["suggested"]["source_dir"] == str(world.downloads / "magazines")
    assert data["suggested"]["dest_dir"] == str(world.dest.parent / "magazines")
    assert data["jellyfin"]["suggested"] == LIB2_JF, "matched by the destination's folder name"
    # Read-only: a login and the category list, nothing else; Jellyfin only listed its libraries.
    assert {path for _method, path, _params in world.qbit.requests} <= {
        "/api/v2/auth/login", "/api/v2/torrents/categories", "/api/v2/app/defaultSavePath"}
    assert all(method == "GET" for method, path, _ in world.qbit.requests if path != "/api/v2/auth/login")
    assert jf.calls == ["libraries"] and jf.folders == [] and jf.full == 0


def test_checks_explain_what_is_missing(client):
    token = setup_admin(client)
    world = client.world
    r = client.post("/settings/libraries/suggest", headers={"X-CSRF-Token": token}, data={
        "library_id": "0", "name": "Comics", "category": "comics",
        "source_dir": str(world.downloads / "comics"), "dest_dir": str(world.source / "comics"),
    })
    checks = {c["label"]: c for c in r.json()["checks"]}
    assert checks["Category 'comics' does not exist in qBittorrent"]["ok"] is False
    assert "Add category" in checks["Category 'comics' does not exist in qBittorrent"]["detail"]
    missing = checks["Source folder does not exist yet"]
    assert missing["level"] == "warn" and "creates it with the first download" in missing["detail"]
    assert "Source folder exists" not in checks
    assert checks["No overlap with other libraries"]["ok"] is False
    r = client.post("/settings/libraries/suggest", headers={"X-CSRF-Token": token},
                    data={"library_id": "0", "name": "X", "category": "news"})
    assert any(c["label"] == "Category 'news' is already used" and not c["ok"] for c in r.json()["checks"])


def test_suggest_needs_csrf(client):
    setup_admin(client)
    assert client.post("/settings/libraries/suggest", data={"name": "x"}).status_code == 403


def test_preview_is_a_dry_run_of_the_unsaved_library(client):
    token = setup_admin(client)
    world = client.world
    add_magazines_folders = world.downloads / "magazines"
    add_magazines_folders.mkdir()
    world.qbit.categories["magazines"] = {}
    add_magazine_pack(world, 2, date(2026, 9, 15))
    r = client.post("/settings/libraries/preview", headers={"X-CSRF-Token": token}, data=new_form(world))
    data = r.json()
    assert data["ok"] is True and data["report"]["would_link"] == 2, data
    assert world.repo.libraries()[-1].name == "News", "nothing saved"
    assert pdfs(world.dest.parent / "magazines") == [], "nothing linked"
    r = client.post("/settings/libraries/preview", headers={"X-CSRF-Token": token},
                    data=new_form(world, source_dir="relative"))
    assert r.json()["ok"] is False and "Source folder" in r.json()["message"]


# --- create -----------------------------------------------------------------------------------------
def test_create_library(client):
    token = setup_admin(client)
    world = client.world
    (world.downloads / "magazines").mkdir()
    world.qbit.categories["magazines"] = {}
    world.settings(retention_enabled=True, retention_armed=True)
    r = client.post("/settings/libraries", data={"csrf_token": token, **new_form(world)}, follow_redirects=False)
    assert r.status_code == 303 and "msg=library_added" in r.headers["location"]
    library = next(lib for lib in world.repo.libraries() if lib.name == "Magazines")
    assert (library.category, library.retention_days) == ("magazines", 14)
    assert r.headers["location"].startswith(f"/settings/libraries/{library.id}")
    assert world.store.load().retention_armed is False, "a new library needs a fresh look at the preview"
    add_magazine_pack(world, 2, date(2026, 9, 15))
    assert world.scanner.run().linked == 2


def test_create_refuses_conflicts_and_missing_category(client):
    token = setup_admin(client)
    world = client.world
    for changes, message in (({"category": "news"}, "already belongs"),
                             ({"category": ""}, "Category: required"),
                             ({"name": "news"}, "already used"),
                             ({"dest_dir": str(world.dest)}, "overlaps"),
                             ({"title_format": "{paper.__class__}"}, "Title format")):
        r = client.post("/settings/libraries", data={"csrf_token": token, **new_form(world, **changes)})
        assert r.status_code == 400 and message in r.text, (changes, r.text[:200])
    assert len(world.repo.libraries()) == 1


# --- edit, enable, delete -----------------------------------------------------------------------------
def test_risky_edit_cancels_that_librarys_pending_deletions_only(client):
    token = setup_admin(client)
    world = client.world
    magazines = add_magazines(world)
    news_hash = add_pack(world, 1, date(2026, 9, 1), age_days=10)
    magazine_hash = add_magazine_pack(world, 2, date(2026, 9, 1), age_days=40)
    world.settings(retention_enabled=True, retention_armed=True, retention_days=7, grace_hours=48)
    world.scanner.run()
    assert {d["ref"] for d in world.repo.pending_deletions() if d["kind"] == "torrent"} == {news_hash, magazine_hash}

    form = {"csrf_token": token, **form_of(magazines, dest_dir=world.dest.parent / "magazines2")}
    r = client.post(f"/settings/libraries/{magazines.id}", data=form)
    assert "Confirm changes" in r.text
    confirm = re.search(r'name="token" value="([^"]+)"', r.text).group(1)
    client.post(f"/settings/libraries/{magazines.id}/confirm",
                data={"csrf_token": token, "token": confirm, "action": "confirm"})
    assert world.repo.library(magazines.id).dest_dir == str(world.dest.parent / "magazines2")
    assert {d["ref"] for d in world.repo.pending_deletions() if d["kind"] == "torrent"} == {news_hash}
    assert world.store.load().retention_armed is False


def test_confirm_token_is_bound_to_the_library(client):
    token = setup_admin(client)
    world = client.world
    magazines = add_magazines(world)
    r = client.post("/settings/libraries/1", data={"csrf_token": token,
                                                   **library_form(world, category="dailies")})
    confirm = re.search(r'name="token" value="([^"]+)"', r.text).group(1)
    client.post(f"/settings/libraries/{magazines.id}/confirm",
                data={"csrf_token": token, "token": confirm, "action": "confirm"})
    assert world.repo.library(1).category == "news" and world.repo.library(magazines.id).category == "magazines"


def test_disable_and_enable(client):
    token = setup_admin(client)
    world = client.world
    magazines = add_magazines(world)
    client.post(f"/settings/libraries/{magazines.id}/enabled", data={"csrf_token": token, "enabled": "false"})
    assert not world.repo.library(magazines.id).enabled
    r = client.post("/settings/libraries/1/enabled", data={"csrf_token": token, "enabled": "false"},
                    follow_redirects=False)
    assert "msg=last_enabled" in r.headers["location"] and world.repo.library(1).enabled
    client.post(f"/settings/libraries/{magazines.id}/enabled", data={"csrf_token": token, "enabled": "true"})
    assert world.repo.library(magazines.id).enabled


def test_delete_forgets_the_library_but_not_its_files(client):
    token = setup_admin(client)
    world = client.world
    magazines = add_magazines(world)
    magazine_hash = add_magazine_pack(world, 2, date(2026, 9, 15))
    add_pack(world, 1, date(2026, 9, 15))
    world.scanner.run()
    linked = pdfs(world.dest.parent / "magazines")
    assert linked
    page = client.get(f"/settings/libraries/{magazines.id}/delete").text
    assert "Nothing on disk is touched" in page
    r = client.post(f"/settings/libraries/{magazines.id}/delete", data={"csrf_token": token},
                    follow_redirects=False)
    assert r.status_code == 303 and world.repo.library(magazines.id) is not None, "needs the confirm box"
    r = client.post(f"/settings/libraries/{magazines.id}/delete", data={"csrf_token": token, "confirm": "yes"},
                    follow_redirects=False)
    assert "msg=library_deleted" in r.headers["location"]
    assert world.repo.library(magazines.id) is None
    assert world.repo.torrent(magazine_hash) is None
    assert all(p.library_id == 1 for p in world.repo.papers())
    assert pdfs(world.dest.parent / "magazines") == linked and world.qbit.deleted == []
    assert len(pdfs(world.dest)) == 3, "the other library is untouched"
    # The last library stays.
    assert client.post("/settings/libraries/1/delete",
                       data={"csrf_token": token, "confirm": "yes"}).status_code == 409


def test_scan_one_library_in_jellyfin(client):
    token = setup_admin(client)
    jf = with_jellyfin(client)
    magazines = add_magazines(client.world)
    r = client.post(f"/settings/libraries/{magazines.id}/jellyfin-scan", data={"csrf_token": token},
                    follow_redirects=False)
    assert "msg=jf_scan" in r.headers["location"]
    _wait_idle(client.app.state.scanner.jellyfin)
    assert jf.folders == [LIB2_JF]
    # The Jellyfin page scans for every library; News has no Jellyfin library chosen, so that is a full scan.
    client.post("/jellyfin/scan", data={"csrf_token": csrf_of(client.get("/settings?section=jellyfin").text)})
    _wait_idle(client.app.state.scanner.jellyfin)
    assert jf.full == 1 and jf.folders == [LIB2_JF]


def test_qbittorrent_mode_needs_every_library_to_have_a_category(client):
    token = setup_admin(client)
    world = client.world
    add_magazines(world, category="")
    world.settings(download_client="none")
    r = client.post("/settings/client", data={"csrf_token": token, "download_client": "qbittorrent",
                                              "settle_minutes": "5"})
    assert r.status_code == 400 and "needs a qBittorrent category" in r.text


def test_relative_and_empty_category_paths_use_the_default_save_path(client):
    token = setup_admin(client)
    world = client.world
    world.qbit.categories.update({"comics": {"savePath": "comics"}, "zines": {"savePath": ""}})
    r = client.post("/settings/libraries/suggest", headers={"X-CSRF-Token": token},
                    data={"library_id": "0", "name": "Comics"})
    by_name = {c["name"]: c for c in r.json()["categories"]}
    assert by_name["comics"]["save_path"] == "/downloads/comics"
    assert by_name["comics"]["local_path"] == str(world.downloads / "comics")
    assert by_name["zines"]["save_path"] == "/downloads/zines"
    assert r.json()["suggested"]["source_dir"] == str(world.downloads / "comics")


def test_suggested_category_follows_the_name_until_typed(client):
    token = setup_admin(client)
    url, headers = "/settings/libraries/suggest", {"X-CSRF-Token": token}
    # The page filled in "comics" itself; the name changes: the suggestion follows.
    r = client.post(url, headers=headers, data={"library_id": "0", "name": "Comic Books", "category": "comics",
                                               "auto_fields": "category,source_dir,dest_dir"})
    assert r.json()["suggested"]["category"] == "comic-books"
    # Typed by the user: kept.
    r = client.post(url, headers=headers, data={"library_id": "0", "name": "Comic Books", "category": "comics",
                                               "auto_fields": "source_dir,dest_dir"})
    assert r.json()["suggested"]["category"] == "comics"


def test_destination_check_says_what_will_happen(client):
    token = setup_admin(client)
    world = client.world
    url, headers = "/settings/libraries/suggest", {"X-CSRF-Token": token}

    def labels(dest):
        r = client.post(url, headers=headers, data={"library_id": "0", "name": "X", "dest_dir": str(dest)})
        return {c["label"]: c for c in r.json()["checks"]}

    assert labels(world.dest.parent / "comics")["Destination folder will be created"]["ok"] is True
    (world.dest.parent / "comics").mkdir()
    assert "Destination folder exists" in labels(world.dest.parent / "comics")
    assert labels(world.dest.parent / "no" / "such")["Destination folder cannot be created"]["ok"] is False


def test_remove_card_is_as_wide_as_the_form(client):
    setup_admin(client)
    add_magazines(client.world)
    assert '<section class="card narrow">' in client.get("/settings/libraries/1").text

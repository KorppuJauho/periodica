"""The scan API in folder mode: category required, the content path marks a download finished."""

from __future__ import annotations

from periodica.sources import API_FINISHED_KEY, folder_download_id

from .test_folder_source import PACK, age, folder_mode, pack
from .test_web import setup_admin


def call(client, **data):
    key = client.world.store.load().api_key
    return client.post("/api/v1/scan", headers={"X-Api-Key": key}, data=data)


def remote(name: str) -> str:
    # conftest maps /downloads -> <data>/torrents/books; the source folder is .../books/news
    return f"/downloads/news/{name}"


def test_folder_mode_requires_the_category(client):
    setup_admin(client)
    folder_mode(client.world)
    for data in ({}, {"category": "movies"}, {"category": ""}):
        r = call(client, **data)
        assert r.status_code == 202 and r.json()["status"] == "ignored", data
    assert client.app.state.scan_trigger is None
    r = call(client, category="news")
    assert r.json()["status"] == "scan queued"
    assert client.app.state.scan_trigger == "API scan (download finished in 'news')"


def test_folder_mode_without_a_category_refuses_calls(client):
    setup_admin(client)
    folder_mode(client.world, qbit_category="")
    assert call(client, category="news").status_code == 409
    assert client.app.state.scan_trigger is None


def test_a_named_download_is_linked_before_the_settle_time(client):
    setup_admin(client)
    world = client.world
    folder_mode(world, settle_minutes=30)
    pack(world, minutes=1)
    assert world.scanner.run().linked == 0
    r = call(client, category="news", path=remote(PACK))
    assert r.json()["status"] == "scan queued"
    assert client.app.state.scan_trigger == f"API scan (download finished in 'news': {PACK})"
    assert folder_download_id(PACK) in world.repo.get_state(API_FINISHED_KEY)
    assert world.scanner.run().linked == 3

    # Forgotten once the download is gone.
    import shutil

    shutil.rmtree(world.source / PACK)
    world.scanner.run()
    assert world.repo.get_state(API_FINISHED_KEY) == {}


def test_a_path_to_a_file_names_its_top_level_download(client):
    setup_admin(client)
    world = client.world
    folder_mode(world, settle_minutes=30)
    pack(world, minutes=1)
    call(client, category="news", path=remote(f"{PACK}/Chronicle.2026.09.15/Chronicle.2026.09.15.pdf"))
    assert world.scanner.run().linked == 3


def test_paths_outside_the_source_folder_are_ignored(client):
    setup_admin(client)
    world = client.world
    folder_mode(world, settle_minutes=30)
    pack(world, minutes=1)
    for path in ("/downloads/movies/x", f"/downloads/news/../movies/{PACK}", "/elsewhere", "news/relative",
                 "/downloads/news"):
        r = call(client, category="news", path=path)
        assert r.status_code == 202 and r.json()["status"] == "ignored", path
    assert not world.repo.get_state(API_FINISHED_KEY)
    assert world.scanner.run().linked == 0


def test_a_named_download_with_partial_files_still_waits(client):
    setup_admin(client)
    world = client.world
    folder_mode(world, settle_minutes=30)
    folder = pack(world, minutes=60)
    (folder / "Evening.Post.2026.09.16.pdf.!qB").write_bytes(b"%PDF-")
    age(folder, 60)
    call(client, category="news", path=remote(PACK))
    assert world.scanner.run().linked == 0


def test_qbittorrent_mode_ignores_the_path(client):
    setup_admin(client)
    world = client.world
    r = call(client, category="news", path=remote(PACK))
    assert r.json()["status"] == "scan queued"
    assert client.app.state.scan_trigger == "API scan (torrent finished in 'news')"
    assert not world.repo.get_state(API_FINISHED_KEY)


def test_switched_off_api_is_not_found_in_folder_mode(client):
    setup_admin(client)
    world = client.world
    folder_mode(world, api_enabled=False)
    assert call(client, category="news", path=remote(PACK)).status_code == 404
    assert not world.repo.get_state(API_FINISHED_KEY)


def test_many_marks_are_capped(client, monkeypatch):
    import periodica.sources as sources

    monkeypatch.setattr(sources, "API_FINISHED_MAX", 3)
    setup_admin(client)
    world = client.world
    folder_mode(world)
    for i in range(5):
        call(client, category="news", path=remote(f"Pack {i}"))
    marks = world.repo.get_state(API_FINISHED_KEY)
    assert set(marks) == {folder_download_id(f"Pack {i}") for i in (2, 3, 4)}

"""Ignoring an unrecognised file so it stops protecting its torrent."""

from __future__ import annotations

from datetime import date

from periodica.manual_delete import check_deletable

from .test_numbering import add_pack as add_magazine_pack
from .test_scanner_retention import add_pack
from .test_web import csrf_of, setup_admin

SPECIAL = "Daily Newspapers 10 09 2026/Special edition.pdf"


def arm(world, days=7):
    world.store.save(world.settings(retention_enabled=True, retention_armed=True, retention_days=days,
                                    grace_hours=0))


def test_an_ignored_file_no_longer_protects_its_torrent(client):
    token = setup_admin(client)
    world = client.world
    h = add_pack(world, 1, date(2026, 9, 10), age_days=10, extra_files=[SPECIAL])
    arm(world)
    assert world.scanner.run().torrents_deleted == 0, "an unrecognised file protects the pack"

    page = client.get("/unmatched").text
    assert 'action="/unmatched/ignore"' in page
    assert "this file is deleted with it" in page, "the confirmation says what ignoring costs"

    r = client.post("/unmatched/ignore", follow_redirects=False,
                    data={"csrf_token": token, "torrent_hash": h, "path": SPECIAL})
    assert r.status_code == 303 and "msg=ignored" in r.headers["location"]
    assert world.repo.unmatched() == []
    assert [i["path"] for i in world.repo.ignored_files()] == [SPECIAL]
    assert "Ignored files" in client.get("/unmatched").text

    report = world.scanner.run()
    assert report.torrents_deleted == 1 and report.unmatched == 0
    world.scanner.run()
    assert world.repo.ignored_files() == [], "forgotten once a scan no longer sees the torrent"


def test_ignoring_also_allows_a_manual_delete(client):
    token = setup_admin(client)
    world = client.world
    h = add_pack(world, 1, date(2026, 9, 10), extra_files=[SPECIAL])
    world.scanner.run()
    assert not world.repo.torrent(h)["files_ok"]
    client.post("/unmatched/ignore", data={"csrf_token": token, "torrent_hash": h, "path": SPECIAL})
    world.scanner.run()
    assert check_deletable(world.repo, world.store.load(), h)["hash"] == h


def test_undo_makes_the_file_protect_its_torrent_again(client):
    token = setup_admin(client)
    world = client.world
    h = add_pack(world, 1, date(2026, 9, 10), age_days=10, extra_files=[SPECIAL])
    world.scanner.run()
    client.post("/unmatched/ignore", data={"csrf_token": token, "torrent_hash": h, "path": SPECIAL})
    token = csrf_of(client.get("/unmatched").text)
    r = client.post("/unmatched/unignore", follow_redirects=False,
                    data={"csrf_token": token, "torrent_hash": h, "path": SPECIAL})
    assert r.status_code == 303 and "msg=unignored" in r.headers["location"]
    arm(world)
    report = world.scanner.run()
    assert report.torrents_deleted == 0 and report.unmatched == 1
    assert client.post("/unmatched/unignore", data={"csrf_token": token, "torrent_hash": h,
                                                    "path": SPECIAL}).status_code == 404


def test_only_unrecognised_pdf_names_can_be_ignored(client):
    token = setup_admin(client)
    world = client.world
    h = add_pack(world, 1, date(2026, 9, 10),
                 extra_files=["Daily Newspapers 10 09 2026/setup.exe", "Daily Newspapers 10 09 2026/Notes.txt"])
    world.qbit.files[h].append({"name": "../../etc/evil.pdf", "size": 1, "progress": 1})
    world.scanner.run()
    rows = {u["path"]: u for u in world.repo.unmatched()}
    assert set(rows) == {"Daily Newspapers 10 09 2026/setup.exe", "Daily Newspapers 10 09 2026/Notes.txt",
                         "../../etc/evil.pdf"}
    assert 'action="/unmatched/ignore"' not in client.get("/unmatched").text
    for path in rows:
        r = client.post("/unmatched/ignore", data={"csrf_token": token, "torrent_hash": h, "path": path})
        assert r.status_code == 400, path
    assert client.post("/unmatched/ignore", data={"csrf_token": token, "torrent_hash": h,
                                                  "path": "not/listed.pdf"}).status_code == 400
    assert world.repo.ignored_files() == []


def test_an_undecided_magazine_is_not_ignorable(client):
    token = setup_admin(client)
    world = client.world
    h = add_magazine_pack(world, 1, date(2026, 1, 15), ["Mag.2026.01"])
    world.scanner.run()
    (row,) = world.repo.unmatched()
    r = client.post("/unmatched/ignore", data={"csrf_token": token, "torrent_hash": h, "path": row["path"]})
    assert r.status_code == 400, "undecided files get the Monthly / Numbered choice instead"


def test_ignoring_needs_a_signed_in_user_and_csrf(client):
    world = client.world
    h = add_pack(world, 1, date(2026, 9, 10), extra_files=[SPECIAL])
    world.scanner.run()
    r = client.post("/unmatched/ignore", data={"torrent_hash": h, "path": SPECIAL}, follow_redirects=False)
    assert r.status_code in (303, 401, 403)
    setup_admin(client)
    r = client.post("/unmatched/ignore", data={"csrf_token": "wrong", "torrent_hash": h, "path": SPECIAL})
    assert r.status_code == 403
    assert world.repo.ignored_files() == []

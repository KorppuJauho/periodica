"""The "no new downloads" warning, set per library."""

from __future__ import annotations

import json
import sqlite3
from datetime import date

from periodica import db as db_module
from periodica.db import Database
from periodica.repo import Repo

from .test_libraries import add_magazine_pack, add_magazines
from .test_libraries_ui import form_of
from .test_scanner_retention import add_pack
from .test_web import setup_admin


def warnings(world) -> list[str]:
    return [a["message"] for a in world.repo.activity(200) if a["message"].startswith("No new download")]


def test_each_library_has_its_own_threshold(client):
    setup_admin(client)
    world = client.world
    # A weekly comic library that may go quiet for a week, next to a daily newspaper library.
    magazines = add_magazines(world, stale_download_hours=24 * 8)
    add_pack(world, 1, date(2026, 9, 15), age_days=3)
    add_magazine_pack(world, 2, date(2026, 9, 1), age_days=5)
    world.scanner.run()

    page = client.get("/").text
    assert "No new downloads in News" in page, "3 days is too long for the news library (36 h)"
    assert "No new downloads in Magazines" not in page, "5 days is fine for a library that allows 8"
    assert warnings(world) == ["No new download in category 'news' for 72 h. Check the qBittorrent RSS rule and feed."]

    world.repo.update_library(world.repo.library(1).model_copy(update={"stale_download_hours": 0}))
    world.repo.update_library(magazines.model_copy(update={"stale_download_hours": 48}))
    world.scanner.run()
    page = client.get("/").text
    assert "No new downloads in News" not in page, "0 turns the warning off for that library"
    assert "No new downloads in Magazines" in page


def test_library_page_edits_the_threshold(client):
    token = setup_admin(client)
    world = client.world
    magazines = add_magazines(world)
    url = f"/settings/libraries/{magazines.id}"
    assert 'name="stale_download_hours" min="0" max="720" value="36"' in client.get(url).text
    r = client.post(url, data={"csrf_token": token, **form_of(magazines, stale_download_hours="999")})
    assert r.status_code == 400
    r = client.post(url, data={"csrf_token": token, **form_of(magazines, stale_download_hours="168")},
                    follow_redirects=False)
    assert r.status_code == 303 and world.repo.library(magazines.id).stale_download_hours == 168
    assert world.repo.library(1).stale_download_hours == 36, "other libraries keep theirs"

    world.repo.update_library(world.repo.library(1).model_copy(update={"stale_download_hours": 12}))
    assert 'name="stale_download_hours" min="0" max="720" value="12"' in client.get("/settings/libraries/new").text, \
        "a new library starts from the first library's value"
    assert 'name="stale_download_hours"' not in client.get("/settings?section=client").text, \
        "the shared download-client settings no longer carry it"


def test_upgrade_copies_the_shared_value_into_every_library(tmp_path, monkeypatch):
    path = tmp_path / "periodica.db"
    migrations = list(db_module.MIGRATIONS)
    monkeypatch.setattr(db_module, "MIGRATIONS", migrations[:8])
    Database(path).migrate()
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES ('stale_download_hours', ?)", (json.dumps(48),))
        conn.execute("INSERT INTO libraries (name, category, source_dir, dest_dir, title_format, "
                     "monthly_title_format, numbered_title_format, language, cover_width, retention_days, enabled, "
                     "position, created_at) SELECT 'Magazines', 'magazines', '/data/a', '/data/b', title_format, "
                     "monthly_title_format, numbered_title_format, language, cover_width, retention_days, 1, 2, 1 "
                     "FROM libraries WHERE id = 1")
    monkeypatch.setattr(db_module, "MIGRATIONS", migrations)
    db = Database(path)
    db.migrate()
    assert list(tmp_path.glob("periodica.db.bak-v8-*"))
    assert {lib.name: lib.stale_download_hours for lib in Repo(db).libraries()} == {"News": 48, "Magazines": 48}
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT 1 FROM settings WHERE key = 'stale_download_hours'").fetchone() is None


def test_a_fresh_install_defaults_to_36_hours(tmp_path):
    db = Database(tmp_path / "periodica.db")
    db.migrate()
    (library,) = Repo(db).libraries()
    assert library.stale_download_hours == 36

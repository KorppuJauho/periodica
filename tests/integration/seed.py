"""Seed the local UI test environment (scripts/test-local.sh ui).

Adds a few newspaper packs for recent days plus a movie torrent to the test qBittorrent and pre-fills
Periodica's settings so the web UI is ready to scan. Test environment only.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

from periodica.db import Database
from periodica.settings import Settings, SettingsStore

from .test_qbittorrent_it import DEST, PASSWORD, SOURCE, URL, USER, QbitAdmin, add_movie, add_news_pack


def main() -> None:
    admin = QbitAdmin()
    admin.wait_ready()
    admin.create_category("news", "/storage/torrents/books/news")
    admin.create_category("movies", "/storage/torrents/movies")
    add_movie(admin)
    for days_ago in (2, 1, 0):
        day = date.today() - timedelta(days=days_ago)
        add_news_pack(admin, day.strftime("%d %m %Y"), day.strftime("%Y.%m.%d"))
        print(f"added news pack for {day.isoformat()}")

    config_dir = Path(os.environ.get("NL_IT_APP_CONFIG", "/appconfig"))
    db = Database(config_dir / "periodica.db")
    db.migrate()
    store = SettingsStore(db)
    settings = store.load()
    settings = Settings(**{
        **settings.model_dump(),
        "source_dir": str(SOURCE), "dest_dir": str(DEST), "qbit_url": URL, "qbit_username": USER,
        "qbit_password": PASSWORD, "qbit_category": "news", "path_map_remote": "/storage", "path_map_local": "/data",
    })
    store.save(settings)
    print("Periodica settings pre-filled (qBittorrent, category, path mapping)")


if __name__ == "__main__":
    main()

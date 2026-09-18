from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from periodica.app import create_app
from periodica.config import Env
from periodica.db import Database
from periodica.qbittorrent import QbitClient
from periodica.repo import Repo
from periodica.scanner import Scanner
from periodica.settings import Settings, SettingsStore

from .helpers import FakeQbit


@dataclass
class World:
    env: Env
    db: Database
    store: SettingsStore
    repo: Repo
    qbit: FakeQbit
    scanner: Scanner
    source: Path
    dest: Path
    downloads: Path

    def settings(self, **changes) -> Settings:
        s = self.store.load()
        for key, value in changes.items():
            setattr(s, key, value)
        self.store.save(s)
        return s


@pytest.fixture
def world(tmp_path: Path) -> World:
    data = tmp_path / "data"
    downloads = data / "torrents" / "books"
    source = downloads / "news"
    dest = data / "media" / "books" / "news"
    source.mkdir(parents=True)
    dest.parent.mkdir(parents=True)
    config = tmp_path / "config"
    env = Env(data_root=data, config_dir=config, host="127.0.0.1", port=0, secure_cookies=False,
              log_level="info", cover_memory_mb=1024)
    db = Database(env.db_path)
    db.migrate()
    store = SettingsStore(db)
    repo = Repo(db)
    qbit = FakeQbit()

    s = Settings(source_dir=str(source), dest_dir=str(dest), qbit_url=FakeQbit.base_url,
                 qbit_username="admin", qbit_password="adminadmin", qbit_category="news",
                 path_map_remote="/downloads", path_map_local=str(downloads))
    store.save(s)

    def qbit_factory(settings: Settings) -> QbitClient:
        return QbitClient(settings.qbit_url, settings.qbit_username, settings.qbit_password, http=qbit)

    scanner = Scanner(env, store, repo, qbit_factory=qbit_factory)
    return World(env, db, store, repo, qbit, scanner, source, dest, downloads)


@pytest.fixture
def client(world):
    """The web app on top of ``world``, with a fake qBittorrent and no background scheduler."""
    def qbit_factory(settings):
        return QbitClient(settings.qbit_url, settings.qbit_username, settings.qbit_password, http=world.qbit)

    app = create_app(world.env, qbit_factory=qbit_factory, run_scheduler=False)
    with TestClient(app, base_url="http://nas.local:8765") as c:
        c.world = world
        yield c

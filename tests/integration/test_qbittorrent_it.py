"""End-to-end test against a real qBittorrent (see compose.yml in this folder).

Skipped unless NL_IT_QBIT_URL is set. Everything runs locally: the test writes newspaper PDFs,
builds matching .torrent files itself, adds them to qBittorrent and lets it verify the data,
then drives Periodica's real scanner, manual delete and automatic delete against it.
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import pytest

from periodica import covers
from periodica.config import Env
from periodica.db import Database
from periodica.libraries import Library
from periodica.manual_delete import delete_torrent_now
from periodica.qbittorrent import COMPLETE_STATES, QbitClient
from periodica.repo import Repo
from periodica.scanner import RSS_STATUS_KEY, Scanner, default_jellyfin_factory, default_qbit_factory
from periodica.settings import Settings, SettingsStore

from ..helpers import make_cbz, make_pdf

URL = os.environ.get("NL_IT_QBIT_URL", "")
USER = os.environ.get("NL_IT_QBIT_USER", "admin")
PASSWORD = os.environ.get("NL_IT_QBIT_PASSWORD", "")

pytestmark = pytest.mark.skipif(not URL, reason="integration test: set NL_IT_QBIT_URL (see tests/integration)")

DATA = Path("/data")
SOURCE = DATA / "torrents" / "books" / "news"
MOVIES = DATA / "torrents" / "movies"
DEST = DATA / "media" / "books" / "news"
# A second library with its own category and folders.
MAG_SOURCE = DATA / "torrents" / "books" / "magazines"
MAG_DEST = DATA / "media" / "books" / "magazines"
PAPERS = ("Chronicle", "Côte-Nord.Gazette", "Evening.Post")
# Packs also carry magazines: a monthly (the number is the month) and a numbered weekly.
MAGAZINES = ("Business.Monthly.{year}.{month}", "Duck.Weekly.{year}.38")


# --- minimal .torrent builder --------------------------------------------------------------------
def bencode(value) -> bytes:
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    if isinstance(value, dict):
        items = sorted((k.encode() if isinstance(k, str) else k, v) for k, v in value.items())
        return b"d" + b"".join(bencode(k) + bencode(v) for k, v in items) + b"e"
    raise TypeError(type(value))


def make_torrent(root: Path, rel_files: list[str], piece_length: int = 16384) -> tuple[bytes, str]:
    """Multi-file torrent for root/<rel_files>. Returns (torrent bytes, info hash v1)."""
    pieces, buffer, files = b"", b"", []
    for rel in rel_files:
        content = (root / rel).read_bytes()
        files.append({"length": len(content), "path": rel.split("/")})
        buffer += content
        while len(buffer) >= piece_length:
            pieces += hashlib.sha1(buffer[:piece_length]).digest()  # noqa: S324 - BitTorrent v1 format
            buffer = buffer[piece_length:]
    if buffer:
        pieces += hashlib.sha1(buffer).digest()  # noqa: S324
    info = {"name": root.name, "piece length": piece_length, "pieces": pieces, "files": files, "private": 1}
    return bencode({"info": info, "created by": "periodica integration test"}), \
        hashlib.sha1(bencode(info)).hexdigest()  # noqa: S324


# --- qBittorrent admin helper (test setup only; Periodica itself uses QbitClient) -----------------
class QbitAdmin:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def _request(self, path, data=None, headers=None, params=None):
        url = URL + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, data=data, headers={"Referer": URL, **(headers or {})})
        with self.opener.open(req, timeout=15) as resp:
            return resp.status, resp.read()

    def login(self) -> None:
        body = urllib.parse.urlencode({"username": USER, "password": PASSWORD}).encode()
        self._request("/api/v2/auth/login", data=body)

    def wait_ready(self, timeout=120) -> None:
        deadline = time.time() + timeout
        while True:
            try:
                self.login()
                self._request("/api/v2/app/version")
                return
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(2)

    def create_category(self, name: str, save_path: str) -> None:
        body = urllib.parse.urlencode({"category": name, "savePath": save_path}).encode()
        try:
            self._request("/api/v2/torrents/createCategory", data=body)
        except urllib.error.HTTPError as exc:
            if exc.code != 409:  # already exists
                raise

    def add_torrent(self, torrent: bytes, category: str, save_path: str) -> None:
        boundary = uuid.uuid4().hex
        parts = []
        for key, value in {"category": category, "savepath": save_path, "skip_checking": "false",
                           "stopped": "false", "paused": "false"}.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="torrents"; filename="t.torrent"\r\n'
                     f"Content-Type: application/x-bittorrent\r\n\r\n".encode() + torrent + b"\r\n")
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        self._request("/api/v2/torrents/add", data=body,
                      headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})

    def info(self, torrent_hash: str) -> dict | None:
        _, body = self._request("/api/v2/torrents/info", params={"hashes": torrent_hash})
        items = json.loads(body)
        return items[0] if items else None

    def wait_complete(self, torrent_hash: str, timeout=120) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            item = self.info(torrent_hash)
            if item and item["progress"] >= 1 and item["state"] in COMPLETE_STATES:
                return
            time.sleep(1)
        raise AssertionError(f"torrent {torrent_hash} did not complete: {self.info(torrent_hash)}")

    def wait_gone(self, torrent_hash: str, timeout=60) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.info(torrent_hash) is None:
                return
            time.sleep(1)
        raise AssertionError(f"torrent {torrent_hash} was not deleted")


# --- scenario helpers ---------------------------------------------------------------------------
def add_news_pack(admin: QbitAdmin, day: str, ymd: str) -> tuple[str, Path]:
    year, month, _day = ymd.split(".")
    stems = [f"{paper}.{ymd}" for paper in PAPERS] + [m.format(year=year, month=month) for m in MAGAZINES]
    return add_pack(admin, SOURCE / f"Daily Newspapers {day}", stems, "news", "/storage/torrents/books/news")


def add_magazine_pack(admin: QbitAdmin, day: str, ymd: str) -> tuple[str, Path]:
    year, month, _day = ymd.split(".")
    # "Chronicle" exists in the news library too: the two libraries must keep them apart.
    # ... and a comic as cbz only, linked with the cover taken from its first page.
    stems = [f"Chronicle.{ymd}", f"Harbour.Monthly.{year}.{month}", f"Duck.Weekly.{year}.38.cbz"]
    return add_pack(admin, MAG_SOURCE / f"Magazines {day}", stems, "magazines", "/storage/torrents/books/magazines")


def add_pack(admin: QbitAdmin, root: Path, stems: list[str], category: str, save_path: str) -> tuple[str, Path]:
    rel_files = []
    for stem in stems:
        files = (((stem, make_cbz()),) if stem.endswith(".cbz")
                 else ((f"{stem}.pdf", make_pdf(stem)), (f"{stem}.nfo", b"nfo")))
        if stem.endswith(".cbz"):
            stem = stem[:-4]
        for name, data in files:
            path = root / stem / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            rel_files.append(f"{stem}/{name}")
    torrent, info_hash = make_torrent(root, rel_files)
    admin.add_torrent(torrent, category, save_path)
    admin.wait_complete(info_hash)
    return info_hash, root


def add_movie(admin: QbitAdmin) -> tuple[str, Path]:
    root = MOVIES / "Some.Movie.2026"
    root.mkdir(parents=True, exist_ok=True)
    (root / "movie.mkv").write_bytes(os.urandom(50_000))
    torrent, info_hash = make_torrent(root, ["movie.mkv"])
    admin.add_torrent(torrent, "movies", "/storage/torrents/movies")
    admin.wait_complete(info_hash)
    return info_hash, root


def wait_until(predicate, timeout=30, message="condition not met"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.5)
    raise AssertionError(message)


def library_pdfs(dest: Path = DEST) -> list[str]:
    return sorted(p.name for p in dest.rglob("*.pdf")) if dest.exists() else []


# --- the scenario ---------------------------------------------------------------------------------
def test_end_to_end_against_real_qbittorrent(tmp_path, monkeypatch):
    admin = QbitAdmin()
    admin.wait_ready()
    admin.create_category("news", "/storage/torrents/books/news")
    admin.create_category("movies", "/storage/torrents/movies")
    admin.create_category("magazines", "/storage/torrents/books/magazines")
    MAG_SOURCE.mkdir(parents=True, exist_ok=True)

    # Periodica's own client logs in the way it does in production (qBittorrent 5.x: 200/204, empty body).
    assert QbitClient(URL, USER, PASSWORD).version().startswith("v")

    env = Env(data_root=DATA, config_dir=tmp_path / "config", host="127.0.0.1", port=0,
              secure_cookies=False, log_level="info", cover_memory_mb=1024)
    db = Database(env.db_path)
    db.migrate()
    store, repo = SettingsStore(db), Repo(db)
    store.save(Settings(
        source_dir=str(SOURCE), dest_dir=str(DEST), qbit_url=URL, qbit_username=USER, qbit_password=PASSWORD,
        qbit_category="news", path_map_remote="/storage", path_map_local="/data",
    ))
    magazines_id = repo.add_library(Library(name="Magazines", category="magazines", source_dir=str(MAG_SOURCE),
                                            dest_dir=str(MAG_DEST), retention_days=30))
    scanner = Scanner(env, store, repo, default_qbit_factory, default_jellyfin_factory)

    movie_hash, movie_root = add_movie(admin)
    news_hash, news_root = add_news_pack(admin, "15 09 2026", "2026.09.15")
    magazine_hash, magazine_root = add_magazine_pack(admin, "15 09 2026", "2026.09.15")

    # 1. Scan: link, metadata, covers, each library into its own folder; other categories untouched.
    report = scanner.run(trigger="Integration scan")
    assert report.error is None, report.error
    assert report.torrents == 2 and report.linked == 8 and report.unmatched == 0
    if covers.pdftoppm_available():
        assert report.covers == 8
    assert library_pdfs(MAG_DEST) == ["Chronicle 2026-09-15.pdf", "Harbour Monthly 2026-09.pdf"]
    assert repo.torrent(magazine_hash)["library_id"] == magazines_id
    comic = MAG_DEST / "Duck Weekly" / "Duck Weekly 2026 #38"
    assert (comic / "Duck Weekly 2026 #38.cbz").is_file() and (comic / "cover.jpg").is_file()
    assert os.path.samefile(comic / "Duck Weekly 2026 #38.cbz",
                            magazine_root / "Duck.Weekly.2026.38" / "Duck.Weekly.2026.38.cbz")
    assert library_pdfs() == ["Business Monthly 2026-09.pdf", "Chronicle 2026-09-15.pdf",
                              "Côte-Nord Gazette 2026-09-15.pdf", "Duck Weekly 2026 #38.pdf",
                              "Evening Post 2026-09-15.pdf"]
    source_pdf = news_root / "Evening.Post.2026.09.15" / "Evening.Post.2026.09.15.pdf"
    linked_pdf = DEST / "Evening Post" / "Evening Post 2026-09-15" / "Evening Post 2026-09-15.pdf"
    assert os.path.samefile(source_pdf, linked_pdf)
    assert (DEST / ".periodica-library").is_file()
    assert sorted(t["hash"] for t in repo.torrents()) == sorted([news_hash, magazine_hash])
    rss = repo.get_state(RSS_STATUS_KEY)
    assert rss["news"]["error"] is None and rss["magazines"]["error"] is None

    # 1b. Library health: a deleted issue folder is reported, and linked again only after Fix.
    import shutil

    shutil.rmtree(MAG_DEST / "Harbour Monthly")
    report = scanner.run()
    assert report.health_problems == 1 and report.linked == 0
    assert library_pdfs(MAG_DEST) == ["Chronicle 2026-09-15.pdf"]
    (problem,) = repo.issue_problems()
    assert repo.set_fix_requested([problem["id"]]) == 1
    report = scanner.run()
    assert report.fixed == 1 and report.health_problems == 0
    assert library_pdfs(MAG_DEST) == ["Chronicle 2026-09-15.pdf", "Harbour Monthly 2026-09.pdf"]

    # 2. Manual delete: torrent and files removed by qBittorrent, library cleaned, movie untouched.
    delete_torrent_now(scanner, repo, store.load(), default_qbit_factory, default_jellyfin_factory,
                       news_hash, "integration")
    admin.wait_gone(news_hash)
    wait_until(lambda: not source_pdf.exists(), message="qBittorrent did not delete the files")
    assert library_pdfs() == []
    assert [p.name for p in DEST.iterdir()] == [".periodica-library"]
    assert admin.info(movie_hash) is not None and (movie_root / "movie.mkv").is_file()
    assert admin.info(magazine_hash) is not None and len(library_pdfs(MAG_DEST)) == 2, \
        "the other library is untouched"

    # 3. Automatic delete: a new pack, retention armed, clock moved 8 days ahead.
    news_hash2, news_root2 = add_news_pack(admin, "16 09 2026", "2026.09.16")
    # The same monthly and numbered issue as in the deleted pack: removed issues are linked again.
    assert scanner.run().linked == 5
    settings = store.load()
    settings.retention_enabled, settings.retention_armed = True, True
    settings.retention_days, settings.grace_hours = 7, 0
    store.save(settings)
    # Shift only the scanner's clock (it decides what has expired). Patching time.time globally would also
    # make qBittorrent's session cookie look expired to the HTTP client.
    import types

    from periodica import scanner as scanner_module

    future = types.SimpleNamespace(time=lambda: time.time() + 8 * 86400, monotonic=time.monotonic)
    monkeypatch.setattr(scanner_module, "time", future)
    report = scanner.run()
    monkeypatch.undo()
    assert report.error is None, report.error
    # The magazines library keeps 30 days: only the news pack has expired.
    assert report.torrents_deleted == 1 and report.issues_removed == 5
    admin.wait_gone(news_hash2)
    wait_until(lambda: not any(news_root2.rglob("*.pdf")), message="files of the expired pack were not deleted")
    assert library_pdfs() == []
    assert repo.deletion_history()[0]["status"] == "done"

    # The movie torrent in another category, and the other library, survived everything.
    assert admin.info(movie_hash)["category"] == "movies"
    assert (movie_root / "movie.mkv").is_file()
    assert admin.info(magazine_hash)["category"] == "magazines"
    assert len(library_pdfs(MAG_DEST)) == 2 and any(magazine_root.rglob("*.pdf"))

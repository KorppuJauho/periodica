"""End-to-end test against a real Jellyfin 12 (and qBittorrent), see compose.yml in this folder.

Skipped unless NL_IT_JELLYFIN_URL is set. Jellyfin 12 reads metadata.opf and local images natively
(the Bookshelf plugin is deprecated), so no plugins or internet access are needed.

Checks what matters to a user: after a scan the News library shows each issue with the title, date,
publisher, series and cover from Periodica's files, and after a delete the issues disappear again
(including the case where the library folder has no newspapers left).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from periodica import covers
from periodica.config import Env
from periodica.db import Database
from periodica.jellyfin import JellyfinClient
from periodica.libraries import Library
from periodica.linker import prepare_library_root
from periodica.manual_delete import delete_torrent_now
from periodica.repo import Repo
from periodica.scanner import Scanner, default_jellyfin_factory, default_qbit_factory
from periodica.settings import Settings, SettingsStore

from .test_qbittorrent_it import (
    DATA,
    DEST,
    MAG_DEST,
    MAG_SOURCE,
    PASSWORD,
    SOURCE,
    URL,
    USER,
    QbitAdmin,
    add_magazine_pack,
    add_news_pack,
)

JELLYFIN_URL = os.environ.get("NL_IT_JELLYFIN_URL", "")
JF_USER = "it-admin"
JF_PASSWORD = "integration-only-password"
# The media folder as the Jellyfin container sees it (the shared data volume is mounted at /srv).
JF_NEWS_PATH = "/srv/media/books/news"
JF_MAGAZINES_PATH = "/srv/media/books/magazines"

pytestmark = pytest.mark.skipif(not (JELLYFIN_URL and URL),
                                reason="Jellyfin integration test: set NL_IT_JELLYFIN_URL (see tests/integration)")

# title -> (series / publisher, premiere date written as dc:date, series order)
EXPECTED_BOOKS = {
    "Chronicle 2026-09-15": ("Chronicle", "2026-09-15"),
    "Côte-Nord Gazette 2026-09-15": ("Côte-Nord Gazette", "2026-09-15"),
    "Evening Post 2026-09-15": ("Evening Post", "2026-09-15"),
    "Business Monthly 2026-09": ("Business Monthly", "2026-09-01"),  # a monthly: the 1st of its month
    "Duck Weekly 2026 #38": ("Duck Weekly", None),                   # a numbered issue has no date
}
EXPECTED_TITLES = set(EXPECTED_BOOKS)


class JellyfinAdmin:
    """Test-setup helper for Jellyfin's HTTP API (Periodica itself uses JellyfinClient)."""

    def __init__(self):
        self.token = ""

    def _auth_header(self) -> str:
        header = 'MediaBrowser Client="periodica-it", Device="ci", DeviceId="periodica-it", Version="1.0"'
        return header + (f', Token="{self.token}"' if self.token else "")

    def request(self, method: str, path: str, body=None, params=None):
        url = JELLYFIN_URL + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": self._auth_header(), "Content-Type": "application/json", "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None

    def wait_ready(self, timeout=240) -> dict:
        """Poll until Jellyfin reports its version; while it migrates its database it answers without one."""
        deadline = time.time() + timeout
        last: object = None
        while True:
            try:
                last = self.request("GET", "/System/Info/Public")
                if isinstance(last, dict) and last.get("Version"):
                    return last
            except (OSError, ValueError) as exc:
                last = exc
            if time.time() > deadline:
                raise AssertionError(f"Jellyfin was not ready in {timeout}s; last reply: {last!r}")
            time.sleep(3)

    def complete_setup(self) -> None:
        if self.wait_ready().get("StartupWizardCompleted"):
            return
        self.request("POST", "/Startup/Configuration",
                     {"UICulture": "en-US", "MetadataCountryCode": "US", "PreferredMetadataLanguage": "en"})
        self.request("GET", "/Startup/User")
        self.request("POST", "/Startup/User", {"Name": JF_USER, "Password": JF_PASSWORD})
        self.request("POST", "/Startup/Complete")

    def login(self) -> None:
        result = self.request("POST", "/Users/AuthenticateByName", {"Username": JF_USER, "Pw": JF_PASSWORD})
        self.token = result["AccessToken"]

    def create_api_key(self, app: str = "periodica") -> str:
        self.request("POST", "/Auth/Keys", params={"app": app})
        keys = self.request("GET", "/Auth/Keys")["Items"]
        return next(k["AccessToken"] for k in reversed(keys) if k.get("AppName") == app)

    def add_books_library(self, name: str, path: str) -> None:
        self.request("POST", "/Library/VirtualFolders",
                     {"LibraryOptions": {"EnableRealtimeMonitor": False, "PathInfos": [{"Path": path}]}},
                     params={"name": name, "collectionType": "books", "paths": path, "refreshLibrary": "true"})

    def items(self, parent_id: str, recursive: bool, item_type: str) -> list[dict]:
        result = self.request("GET", "/Items", params={
            "parentId": parent_id, "recursive": str(recursive).lower(), "includeItemTypes": item_type,
            "fields": "Path,ProductionYear,PremiereDate,SeriesName,Studios,SortName,Genres",
        })
        return result["Items"]


def _clean(text: str) -> str:
    """Approximate Jellyfin 12's sort-name cleaning for comparisons."""
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if ch.isalnum() or ch == " ").strip()


def wait_for(fetch, predicate, timeout=240, message="condition not met"):
    deadline = time.time() + timeout
    value = None
    while time.time() < deadline:
        value = fetch()
        if predicate(value):
            return value
        time.sleep(3)
    raise AssertionError(f"{message}; last value: {value}")


def test_jellyfin_shows_and_removes_issues(tmp_path):
    qbit = QbitAdmin()
    qbit.wait_ready()
    qbit.create_category("news", "/storage/torrents/books/news")
    qbit.create_category("magazines", "/storage/torrents/books/magazines")
    MAG_SOURCE.mkdir(parents=True, exist_ok=True)

    jf = JellyfinAdmin()
    info = jf.wait_ready()
    assert int(str(info["Version"]).split(".")[0]) >= 12, f"expected Jellyfin 12+, got {info['Version']}"
    jf.complete_setup()
    jf.login()
    api_key = jf.create_api_key()

    # Real-world order: Periodica is deployed first (its startup prepares the library folder), then the
    # Jellyfin library is created with its initial scan, then newspapers arrive.
    assert prepare_library_root(str(DEST), DATA)
    assert prepare_library_root(str(MAG_DEST), DATA)
    jf.add_books_library("News", JF_NEWS_PATH)
    jf.add_books_library("Magazines", JF_MAGAZINES_PATH)

    # Periodica finds the library the same way the Settings page does (Test button + suggestion).
    client = JellyfinClient(JELLYFIN_URL, api_key)
    libraries = client.libraries()
    news = next(lib for lib in libraries if lib["name"] == "News")
    magazines = next(lib for lib in libraries if lib["name"] == "Magazines")
    # Wait for the libraries' initial scans, so our later per-library refreshes have registered folders to scan.
    for name in ("News", "Magazines"):
        wait_for(lambda name=name: next(f for f in jf.request("GET", "/Library/VirtualFolders") if f["Name"] == name),
                 lambda folder: folder.get("RefreshStatus") in (None, "Idle"), timeout=120,
                 message=f"initial Jellyfin scan of {name} did not finish")
    assert any(loc.rstrip("/").endswith("/books/news") for loc in news["locations"])

    env = Env(data_root=DATA, config_dir=tmp_path / "config", host="127.0.0.1", port=0,
              secure_cookies=False, log_level="info", cover_memory_mb=1024)
    db = Database(env.db_path)
    db.migrate()
    store, repo = SettingsStore(db), Repo(db)
    store.save(Settings(
        source_dir=str(SOURCE), dest_dir=str(DEST), qbit_url=URL, qbit_username=USER, qbit_password=PASSWORD,
        qbit_category="news", path_map_remote="/storage", path_map_local="/data",
        jellyfin_url=JELLYFIN_URL, jellyfin_api_key=api_key,
        jellyfin_library_id=news["id"], jellyfin_library_name="News",
    ))
    repo.add_library(Library(name="Magazines", category="magazines", source_dir=str(MAG_SOURCE),
                             dest_dir=str(MAG_DEST), jellyfin_library_id=magazines["id"],
                             jellyfin_library_name="Magazines"))
    scanner = Scanner(env, store, repo, default_qbit_factory, default_jellyfin_factory)

    news_hash, _ = add_news_pack(qbit, "15 09 2026", "2026.09.15")

    # 1. Scan -> Periodica asks Jellyfin to scan only the News library -> issues appear with our metadata.
    report = scanner.run(trigger="Jellyfin integration scan")
    assert report.error is None, report.error
    assert report.linked == len(EXPECTED_BOOKS) and report.jellyfin_refreshed
    books = wait_for(lambda: jf.items(news["id"], True, "Book"),
                     lambda items: {b["Name"] for b in items} == EXPECTED_TITLES,
                     message="Jellyfin did not show the three issues with titles from metadata.opf")
    for book in books:
        paper, premiere = EXPECTED_BOOKS[book["Name"]]
        assert book.get("SeriesName") == paper, book
        assert paper in [s["Name"] for s in book.get("Studios", [])], book
        if premiere:
            assert book.get("ProductionYear") == 2026, book
            assert str(book.get("PremiereDate", "")).startswith(premiere), book
        else:
            # dc:date is the year alone; Jellyfin may keep it as the year or ignore it, but must not
            # invent a day.
            assert book.get("ProductionYear") in (2026, None), book
            assert not str(book.get("PremiereDate", "")).startswith("2026-09"), book
        # Jellyfin 12 cleans sort names (diacritics and punctuation removed, numbers padded):
        # "Côte-Nord Gazette 2026-09-15" -> "cotenord gazette 0020260915". Order is what matters.
        assert _clean(book.get("SortName", "")).startswith(_clean(paper)), book
        if premiere == "2026-09-15":
            assert _clean(book.get("SortName", "")).endswith("20260915"), book
        if covers.pdftoppm_available():
            assert "Primary" in book.get("ImageTags", {}), f"no cover for {book['Name']}"
    papers = jf.items(news["id"], False, "Folder")
    assert {p["Name"] for p in papers} == {paper for paper, _ in EXPECTED_BOOKS.values()}
    if covers.pdftoppm_available():
        assert all("Primary" in p.get("ImageTags", {}) for p in papers), "newspaper folders are missing folder.jpg"
    assert any("scan of library 'News' requested" in a["message"] for a in repo.activity(50))

    # 1b. A second library: only its own Jellyfin library is asked to scan, and its issues appear there.
    wait_idle(scanner)
    requests_before = refresh_requests(repo)
    add_magazine_pack(qbit, "15 09 2026", "2026.09.15")
    report = scanner.run(trigger="Jellyfin integration scan (magazines)")
    assert report.error is None and report.linked == 3, report
    wait_idle(scanner)
    requests = refresh_requests(repo)
    assert requests["Magazines"] == requests_before["Magazines"] + 1
    assert requests["News"] == requests_before["News"], "the unchanged News library is not scanned again"
    wait_for(lambda: jf.items(magazines["id"], True, "Book"),
             lambda items: {b["Name"] for b in items} == {"Chronicle 2026-09-15", "Harbour Monthly 2026-09",
                                                          "Duck Weekly 2026 #38"},
             message="Jellyfin did not show the magazines library's issues (including the cbz)")
    comic = next(b for b in jf.items(magazines["id"], True, "Book") if b["Name"] == "Duck Weekly 2026 #38")
    assert "Primary" in comic.get("ImageTags", {}), "no cover for the cbz issue"
    assert {b["Name"] for b in jf.items(news["id"], True, "Book")} == EXPECTED_TITLES

    # 2. Delete the only News torrent -> the library folder has no newspapers left, Jellyfin must still clean up.
    delete_torrent_now(scanner, repo, store.load(), default_qbit_factory, default_jellyfin_factory,
                       news_hash, "integration")
    assert (DEST / ".periodica-library").is_file()
    wait_for(lambda: jf.items(news["id"], True, "Book"), lambda items: items == [],
             message="Jellyfin still shows deleted issues")
    wait_for(lambda: jf.items(news["id"], False, "Folder"), lambda items: items == [],
             message="Jellyfin still shows deleted newspaper folders")
    assert len(jf.items(magazines["id"], True, "Book")) == 3, "the other library keeps its issues"


def wait_idle(scanner, timeout=120):
    deadline = time.time() + timeout
    while scanner.jellyfin.busy and time.time() < deadline:
        time.sleep(0.5)
    assert not scanner.jellyfin.busy, "Jellyfin refresh did not finish"


def refresh_requests(repo) -> dict[str, int]:
    messages = [a["message"] for a in repo.activity(500)]
    return {name: sum(f"scan of library '{name}' requested" in m for m in messages) for name in ("News", "Magazines")}

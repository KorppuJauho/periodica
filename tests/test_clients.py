from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from periodica.jellyfin import JellyfinClient, JellyfinError
from periodica.netsafe import HttpError, Response, SafeHttpClient, UnsafeUrlError, validate_url
from periodica.qbittorrent import QbitClient, QbitError

from .helpers import FakeQbit, hash_for


@pytest.mark.parametrize(
    "url",
    [
        "ftp://192.168.1.2",
        "file:///etc/passwd",
        "http://user:pw@192.168.1.2:8080",
        "http://192.168.1.2:8080/?x=1",
        "http://",
        "http://8.8.8.8:8080",
        "http://0.0.0.0:8080",
        "http://[::ffff:8.8.8.8]:8080",
    ],
)
def test_validate_url_rejects(url):
    with pytest.raises(UnsafeUrlError):
        validate_url(url)


@pytest.mark.parametrize(
    "url",
    ["http://192.168.1.2:8080", "http://10.0.0.5", "https://172.16.3.4:8443/qbt/", "http://127.0.0.1:8080",
     "http://100.100.1.2:8080", "http://[fd00::1]:8080"],
)
def test_validate_url_accepts_lan(url):
    assert not validate_url(url).endswith("/")


def test_public_allowed_only_when_opted_in():
    assert validate_url("http://8.8.8.8:8080", allow_public=True) == "http://8.8.8.8:8080"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "http://8.8.8.8/")
            self.end_headers()
        elif self.path.startswith("/big"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"x" * (16 * 1024 * 1024 + 10))
        elif self.path.startswith("/Library/VirtualFolders"):
            body = json.dumps([
                {"Name": "Movies", "ItemId": "f137a2dd21bbc1b99aa5c0f6bf02a805", "CollectionType": "movies",
                 "Locations": ["/media/movies"]},
                {"Name": "News", "ItemId": "0c0e1e7b5a6b4b0a9f0e3a1d2c3b4a59", "CollectionType": "books",
                 "Locations": ["/media/books/news"]},
                {"Name": "Broken", "ItemId": "../../etc", "Locations": []},
            ]).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/System/Info"):
            if self.headers.get("Authorization") != 'MediaBrowser Token="abcdef0123456789abcdef0123456789"':
                self.send_response(401)
                self.end_headers()
                return
            body = json.dumps({"ServerName": "nas", "Version": "10.11.0"}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"hello")

    def do_POST(self):
        _Handler.posts.append(self.path)
        if self.path.startswith("/Items/ffffffffffffffffffffffffffffffff/"):
            self.send_response(404)
        else:
            self.send_response(204)
        self.end_headers()

    posts: ClassVar[list[str]] = []


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_http_client_does_not_follow_redirects(server):
    client = SafeHttpClient(server)
    assert client.request("GET", "/ok").body == b"hello"
    with pytest.raises(HttpError, match="redirect"):
        client.request("GET", "/redirect")


def test_http_client_caps_response_size(server):
    with pytest.raises(HttpError, match="too large"):
        SafeHttpClient(server).request("GET", "/big")


def test_http_client_connection_error():
    with pytest.raises(HttpError):
        SafeHttpClient("http://127.0.0.1:9", timeout=2).request("GET", "/")


def test_jellyfin_client(server):
    good = JellyfinClient(server, "abcdef0123456789abcdef0123456789")
    assert good.system_info()["ServerName"] == "nas"
    good.refresh_library()
    libraries = good.libraries()
    assert [lib["name"] for lib in libraries] == ["Movies", "News"]  # invalid ids dropped
    good.refresh_library_folder("0C0E1E7B5A6B4B0A9F0E3A1D2C3B4A59")
    assert _Handler.posts[-1].startswith("/Items/0c0e1e7b5a6b4b0a9f0e3a1d2c3b4a59/Refresh?metadataRefreshMode=Default")
    with pytest.raises(JellyfinError, match="no longer exists"):
        good.refresh_library_folder("f" * 32)
    with pytest.raises(JellyfinError, match="invalid library id"):
        good.refresh_library_folder("../System/Restart")
    with pytest.raises(JellyfinError, match="rejected"):
        JellyfinClient(server, "ffffffffffffffffffffffffffffffff").system_info()
    with pytest.raises(JellyfinError, match="invalid"):
        JellyfinClient(server, 'x"\r\nInjected: 1')


def test_qbit_login_and_category_filter():
    fake = FakeQbit()
    fake.add(1, "news pack", "news", "/downloads/news", ["a.pdf"])
    fake.add(2, "movie", "movies", "/downloads/movies", ["m.mkv"])
    client = QbitClient(fake.base_url, "admin", "adminadmin", http=fake)
    assert [t.hash for t in client.torrents("news")] == [hash_for(1)]
    fake.leak_other_categories = True
    assert [t.hash for t in client.torrents("news")] == [hash_for(1)]
    with pytest.raises(QbitError):
        client.torrents("")
    with pytest.raises(QbitError):
        client.torrents(" news ")


@pytest.mark.parametrize(("legacy", "status"), [(False, 200), (False, 204), (True, 200)],
                         ids=["qbittorrent-5.0", "qbittorrent-5.2-204", "qbittorrent-4"])
def test_qbit_login_success_both_api_styles(legacy, status):
    fake = FakeQbit(legacy=legacy, login_status=status)
    assert QbitClient(fake.base_url, "admin", "adminadmin", http=fake).version() == "v5.0.2"


@pytest.mark.parametrize("legacy", [False, True], ids=["qbittorrent-5", "qbittorrent-4"])
def test_qbit_bad_password(legacy):
    fake = FakeQbit(legacy=legacy)
    with pytest.raises(QbitError, match="login failed"):
        QbitClient(fake.base_url, "admin", "wrong", http=fake).version()


def test_qbit_errors_include_http_status_and_reply():
    fake = FakeQbit()
    fake.request = lambda *a, **k: Response(401, b"Unauthorized")
    with pytest.raises(QbitError, match=r"HTTP 401: Unauthorized"):
        QbitClient(fake.base_url, "", "", http=fake).version()


def test_qbit_banned_ip():
    fake = FakeQbit()
    fake.request = lambda *a, **k: Response(403, b"banned")
    with pytest.raises(QbitError, match="banned"):
        QbitClient(fake.base_url, "admin", "adminadmin", http=fake).login()


@pytest.mark.parametrize("legacy", [False, True], ids=["qbittorrent-5", "qbittorrent-4"])
def test_qbit_relogin_on_expired_session(legacy):
    fake = FakeQbit(legacy=legacy)
    client = QbitClient(fake.base_url, "admin", "adminadmin", http=fake)
    assert client.version() == "v5.0.2"
    fake.logged_in = False  # session expired server-side
    assert client.version() == "v5.0.2"


def test_qbit_delete_rechecks_state():
    fake = FakeQbit()
    h = fake.add(1, "pack", "news", "/downloads/news", ["a.pdf"], state="downloading", progress=0.5)
    client = QbitClient(fake.base_url, "admin", "adminadmin", http=fake)
    with pytest.raises(QbitError, match="not complete"):
        client.delete_with_files(h, "news")
    fake.torrents[h].update(state="stalledUP", progress=1.0)
    with pytest.raises(QbitError, match="category"):
        client.delete_with_files(h, "movies")
    client.delete_with_files(h, "news")
    assert fake.deleted == [(h, True)]

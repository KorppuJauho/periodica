"""Test helpers: tiny valid PDFs and a fake qBittorrent Web API speaking through the real client."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from periodica.netsafe import HttpError, Response


def make_pdf(label: str = "News") -> bytes:
    """A minimal one-page PDF with a coloured rectangle (renders with pdftoppm)."""
    content = b"0.1 0.3 0.6 rg 20 20 160 240 re f 1 1 1 rg 40 200 120 20 re f"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 280] /Contents 4 0 R /Resources << >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n% " + label.encode()[:40] + b"\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def hash_for(n: int) -> str:
    return f"{n:040x}"


class FakeQbit:
    """In-memory qBittorrent API. Plugged into QbitClient via the ``http`` parameter."""

    base_url = "http://qbittorrent.test:8080"

    def __init__(self, username: str = "admin", password: str = "adminadmin", legacy: bool = False,
                 login_status: int = 200):
        self.login_status = login_status  # qBittorrent 5.2 answers a successful login with 204
        self.legacy = legacy  # True: qBittorrent 4.x responses ("Ok."/"Fails.", 403); False: 5.x (empty/401)
        self.username = username
        self.password = password
        self.logged_in = False
        self.torrents: dict[str, dict] = {}
        self.files: dict[str, list[dict]] = {}
        self.categories = {"news": {}, "movies": {}}
        self.deleted: list[tuple[str, bool]] = []
        self.local_paths: dict[str, Path | None] = {}
        self.requests: list[tuple[str, str, dict]] = []
        self.fail = False
        self.leak_other_categories = False
        self.rss_rules = {
            "News": {"enabled": True, "affectedFeeds": ["https://tracker.example/rss"],
                     "torrentParams": {"category": "news"}},
            "Movies": {"enabled": True, "affectedFeeds": [], "assignedCategory": "movies"},
        }

    def add(self, n: int, name: str, category: str, save_path: str, files: list[str], *,
            state: str = "stalledUP", progress: float = 1.0, completion_on: float | None = None,
            size: int = 1000, local_save_path: Path | None = None) -> str:
        h = hash_for(n)
        self.torrents[h] = {
            "hash": h, "name": name, "category": category, "save_path": save_path, "state": state,
            "progress": progress, "completion_on": completion_on if completion_on is not None else time.time(),
            "size": size,
        }
        self.files[h] = [{"name": f, "size": 100, "progress": progress} for f in files]
        self.local_paths[h] = local_save_path
        return h

    def request(self, method, path, params=None, form=None, headers=None) -> Response:
        params = params or {}
        form = form or {}
        self.requests.append((method, path, {**params, **form}))
        if self.fail:
            raise HttpError("connection refused")
        if path == "/api/v2/auth/login":
            if form.get("username") == self.username and form.get("password") == self.password:
                self.logged_in = True
                return Response(self.login_status, b"Ok." if self.legacy else b"")
            return Response(200, b"Fails.") if self.legacy else Response(401, b"Unauthorized")
        if not self.logged_in:
            return Response(403, b"Forbidden") if self.legacy else Response(401, b"Unauthorized")
        if path == "/api/v2/app/defaultSavePath":
            return Response(200, b"/downloads")
        if path == "/api/v2/app/version":
            return Response(200, b"v5.0.2")
        if path == "/api/v2/torrents/categories":
            return Response(200, json.dumps(self.categories).encode())
        if path == "/api/v2/torrents/info":
            items = list(self.torrents.values())
            if "hashes" in params:
                wanted = set(params["hashes"].split("|"))
                items = [t for t in items if t["hash"] in wanted or params["hashes"] == "all"]
            elif "category" in params and not self.leak_other_categories:
                items = [t for t in items if t["category"] == params["category"]]
            return Response(200, json.dumps(items).encode())
        if path == "/api/v2/app/preferences":
            return Response(200, json.dumps({"rss_processing_enabled": True, "rss_auto_downloading_enabled": True,
                                             "proxy_password": "must-not-be-stored"}).encode())
        if path == "/api/v2/rss/rules":
            return Response(200, json.dumps(self.rss_rules).encode())
        if path == "/api/v2/torrents/files":
            return Response(200, json.dumps(self.files.get(params.get("hash"), [])).encode())
        if path == "/api/v2/torrents/delete":
            assert method == "POST"
            hashes = form["hashes"].split("|")
            assert "all" not in hashes, "client must never send hashes=all"
            delete_files = form.get("deleteFiles") == "true"
            for h in hashes:
                torrent = self.torrents.pop(h, None)
                if torrent is None:
                    continue
                self.deleted.append((h, delete_files))
                if delete_files:
                    self._delete_data(self.local_paths.get(h), self.files.pop(h, []))
            return Response(200, b"")
        return Response(404, b"Not Found")

    def _delete_data(self, save_path: Path | None, files: list[dict]) -> None:
        if save_path is None:
            return
        roots = set()
        for f in files:
            p = save_path / f["name"]
            if p.is_file():
                p.unlink()
            roots.add(save_path / Path(f["name"]).parts[0])
        for root in roots:
            if root.is_dir():
                shutil.rmtree(root)


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60 + b"\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60


def _zip(entries: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def make_cbz(pages: dict[str, bytes] | None = None) -> bytes:
    """A comic archive; by default page 2 comes before page 10 only with natural sorting."""
    return _zip(pages if pages is not None else {
        "__MACOSX/._p001.jpg": b"junk", "Issue/p10.jpg": PNG, "Issue/p2.jpg": JPEG, "ComicInfo.xml": b"<x/>"})


def make_epub(opf: str | None = None, cover: bytes = PNG, extra: dict[str, bytes] | None = None) -> bytes:
    opf = opf if opf is not None else (
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata/><manifest>'
        '<item id="c" href="images/cover.png" media-type="image/png" properties="cover-image"/>'
        '<item id="t" href="text.xhtml" media-type="application/xhtml+xml"/></manifest></package>')
    entries = {
        "mimetype": b"application/epub+zip",
        "META-INF/container.xml": b'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                                  b'<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
        "OEBPS/content.opf": opf.encode(),
        "OEBPS/images/cover.png": cover,
        "OEBPS/text.xhtml": b"<html/>",
    }
    entries.update(extra or {})
    return _zip(entries)


def make_cbr(version: int = 5) -> bytes:
    """Only the RAR signature and some bytes: Periodica never reads further."""
    return (b"Rar!\x1a\x07\x01\x00" if version == 5 else b"Rar!\x1a\x07\x00") + b"\x00" * 64


def write_torrent_files(save_path: Path, files: list[str], pdf: bytes | None = None) -> None:
    for name in files:
        p = save_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if name.lower().endswith(".pdf"):
            p.write_bytes(pdf or make_pdf(p.stem))
        elif name.lower().endswith(".cbz"):
            p.write_bytes(make_cbz())
        elif name.lower().endswith(".epub"):
            p.write_bytes(make_epub())
        elif name.lower().endswith(".cbr"):
            p.write_bytes(make_cbr())
        else:
            p.write_text("nfo")


def can_symlink(tmp: Path) -> bool:
    try:
        os.symlink(tmp, tmp / "probe-link")
    except (OSError, NotImplementedError):
        return False
    (tmp / "probe-link").unlink()
    return True

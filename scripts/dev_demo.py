"""Run the web UI locally against a fake qBittorrent with sample newspapers (development only).

    python scripts/dev_demo.py  ->  http://127.0.0.1:8765  (demo login: demo / demo-password)
    python scripts/dev_demo.py --folder  ->  folder mode (no qBittorrent) on http://127.0.0.1:8766
    python scripts/dev_demo.py --offline ->  OFFLINE_MODE on http://127.0.0.1:8767
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from fastapi import Request

from periodica import covers
from periodica.app import create_app
from periodica.auth import AuthService
from periodica.config import Env
from periodica.db import Database
from periodica.libraries import Library
from periodica.qbittorrent import QbitClient
from periodica.repo import Repo
from periodica.settings import Settings, SettingsStore
from tests.helpers import FakeQbit, make_cbz, make_epub, write_torrent_files

PAPERS = ["Chronicle", "Morning.Herald", "Côte-Nord.Gazette", "Evening.Post", "Lakeside-Journal", "Harbour-Times",
          "City.Tribune"]


def _png(width: int, height: int, rgb: tuple[int, int, int], band: tuple[int, int, int] | None = None) -> bytes:
    """A flat PNG, optionally with a lighter band across the top, so demo covers look like covers."""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    body = b"\x00" + bytes(rgb) * width
    top = b"\x00" + bytes(band or rgb) * width
    band_rows = height // 5 if band else 0
    raw = top * band_rows + body * (height - band_rows)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def _demo_cover(name: str, width: int) -> bytes:
    """A different colour per publication, so the demo pages don't look empty."""
    palette = [(176, 58, 46), (33, 97, 140), (30, 110, 90), (125, 60, 152), (183, 121, 31), (52, 73, 94)]
    colour = palette[sum(name.encode()) % len(palette)]
    lighter = tuple(min(255, part + 90) for part in colour)
    return _png(width, int(width * 1.4), colour, lighter)


def main() -> None:
    offline = "--offline" in sys.argv
    folder_mode = "--folder" in sys.argv or offline
    port = 8767 if offline else 8766 if folder_mode else 8765
    root = Path(tempfile.mkdtemp(prefix="periodica-demo-"))
    downloads = root / "data" / "torrents" / "books"
    source = downloads / "news"
    dest = root / "data" / "media" / "books" / "news"
    source.mkdir(parents=True)
    dest.parent.mkdir(parents=True)
    env = Env(data_root=root / "data", config_dir=root / "config", host="127.0.0.1", port=port,
              secure_cookies=False, log_level="info", cover_memory_mb=1024, offline=offline)
    db = Database(env.db_path)
    db.migrate()
    SettingsStore(db).save(Settings(
        source_dir=str(source), dest_dir=str(dest), qbit_url=FakeQbit.base_url, qbit_username="admin",
        qbit_password="adminadmin", qbit_category="news", path_map_remote="/downloads", path_map_local=str(downloads),
        # Offline mode keeps the saved qBittorrent choice and overrides it at runtime.
        download_client="none" if folder_mode and not offline else "qbittorrent",
        jellyfin_url="http://192.168.1.10:8096" if offline else "",
        jellyfin_api_key="a" * 32 if offline else "",
    ))
    AuthService(db).create_first_user("demo", "demo-password")

    # A second library with its own category and folders; "comics" exists in qBittorrent but has no library yet,
    # so the Add-library wizard has something to suggest.
    magazines = downloads / "magazines"
    magazines.mkdir()
    Repo(db).add_library(Library(name="Magazines", category="magazines", source_dir=str(magazines),
                                 dest_dir=str(dest.parent / "magazines"), retention_days=30))
    (downloads / "comics").mkdir()

    fake = FakeQbit()
    fake.categories.update({"news": {"savePath": "/downloads/news"},
                            "magazines": {"savePath": "/downloads/magazines"},
                            "comics": {"savePath": "/downloads/comics"}})
    for n in range(4):
        day = date.today() - timedelta(days=n)
        folder = f"Daily Newspapers {day:%d %m %Y}"
        files = []
        for paper in PAPERS:
            stem = f"{paper}.{day:%Y.%m.%d}"
            files += [f"{folder}/{stem}/{stem}.nfo", f"{folder}/{stem}/{stem}.pdf"]
        if n == 0:
            # A monthly magazine (the number matches the month), a numbered weekly (the number cannot be a
            # month), and a file no rule recognises.
            files.append(f"{folder}/Business.Monthly.{day:%Y.%m}/Business.Monthly.{day:%Y.%m}.pdf")
            files.append(f"{folder}/Duck.Weekly.{day.year}.38/Duck.Weekly.{day.year}.38.pdf")
            files.append(f"{folder}/Special edition.pdf")
        write_torrent_files(source, files)
        fake.add(n + 1, folder, "news", "/downloads/news", files, completion_on=time.time() - n * 86400,
                 size=400_000_000, local_save_path=source)

    # A January pack: "Quarterly.YYYY.01" could be the January issue or issue 1, so it waits for a choice.
    january = date(date.today().year, 1, 15)
    folder = f"Daily Newspapers {january:%d %m %Y}"
    files = [f"{folder}/Chronicle.{january:%Y.%m.%d}/Chronicle.{january:%Y.%m.%d}.pdf",
             f"{folder}/Quarterly.{january.year}.01/Quarterly.{january.year}.01.pdf"]
    write_torrent_files(source, files)
    fake.add(50, folder, "news", "/downloads/news", files, completion_on=time.time() - 3600, size=90_000_000,
             local_save_path=source)
    fake.add(99, "Some.Movie.2026.1080p", "movies", "/downloads/movies", ["movie.mkv"])
    day = date.today()
    folder = f"Magazines {day:%d %m %Y}"
    files = [f"{folder}/Chronicle.{day:%Y.%m.%d}/Chronicle.{day:%Y.%m.%d}.pdf",
             f"{folder}/Harbour.Monthly.{day:%Y.%m}/Harbour.Monthly.{day:%Y.%m}.pdf"]
    write_torrent_files(magazines, files)
    fake.add(60, folder, "magazines", "/downloads/magazines", files, completion_on=time.time() - 7200,
             size=60_000_000, local_save_path=magazines)
    # A comic as cbz and pdf (the cbz is linked, with its first page as the cover) and an epub with its own cover.
    comic = f"Bay.Comics.{day.year}.41"
    reader = f"Harbour.Reader.{day:%Y.%m.%d}"
    extra = [f"{folder}/{comic}/{comic}.pdf", f"{folder}/{comic}/{comic}.cbz", f"{folder}/{reader}/{reader}.epub"]
    write_torrent_files(magazines, extra)
    (magazines / folder / comic / f"{comic}.cbz").write_bytes(make_cbz({"page1.png": _demo_cover(comic, 200)}))
    (magazines / folder / reader / f"{reader}.epub").write_bytes(make_epub(cover=_demo_cover(reader, 200)))
    fake.add(62, f"Comics {day:%d %m %Y}", "magazines", "/downloads/magazines", extra,
             completion_on=time.time() - 5400, size=30_000_000, local_save_path=magazines)
    # A comic whose name no built-in rule reads: try "Fix" on the Unmatched page.
    duck = ["DW.2026.No.38/DW.2026.No.38.pdf", "DW.2026.No.38/DW.2026.No.38.cbr", "DW.2026.No.38/DW.2026.No.38.txt"]
    write_torrent_files(magazines, duck)
    fake.add(61, "DW.2026.No.38", "magazines", "/downloads/magazines", duck, completion_on=time.time() - 3600,
             size=90_000_000, local_save_path=magazines)

    if folder_mode:
        # Everything already written has "settled"; the newest pack settles 5 minutes after start.
        old = time.time() - 3600
        for path in downloads.rglob("*"):
            os.utime(path, (old, old))
        fresh = source / f"Daily Newspapers {date.today():%d %m %Y}"
        for path in [fresh, *fresh.rglob("*")]:
            os.utime(path, None)

    def qbit_factory(settings: Settings) -> QbitClient:
        return QbitClient(settings.qbit_url, settings.qbit_username, settings.qbit_password, http=fake)

    if not covers.pdftoppm_available():
        # Demo only: without poppler (e.g. on Windows) every PDF cover would fail and the pages would look
        # broken. Draw a plain placeholder instead; the real app never does this.
        def placeholder(pdf, out, width, memory_mb=1024):
            out.write_bytes(_demo_cover(pdf.stem, int(width)))

        covers.pdftoppm_available = lambda: True
        covers.render_cover = placeholder

    app = create_app(env, qbit_factory=qbit_factory)
    if "--no-auth" in sys.argv:
        # Dev-only shortcut for UI work: every request acts as the demo user. Never used by the real app.
        from periodica.auth import Session
        from periodica.web.deps import require_csrf, require_session

        demo = Session(user_id=1, username="demo", csrf_token="dev", token_hash="dev")

        async def fake_session(request: Request):
            request.state.session = demo
            return demo

        app.dependency_overrides[require_session] = fake_session
        app.dependency_overrides[require_csrf] = fake_session
    print(f"Demo data in {root}\nLogin: demo / demo-password")
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()

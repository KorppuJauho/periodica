"""SQLite storage with simple versioned migrations."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at REAL NOT NULL
    );
    CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        csrf_token TEXT NOT NULL,
        created_at REAL NOT NULL,
        last_seen REAL NOT NULL
    );
    CREATE TABLE papers (
        name TEXT PRIMARY KEY,
        display_name TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        retention_days INTEGER,
        created_at REAL NOT NULL
    );
    CREATE TABLE torrents (
        hash TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        category TEXT NOT NULL,
        state TEXT NOT NULL,
        completion_on REAL,
        size INTEGER NOT NULL DEFAULT 0,
        files_ok INTEGER NOT NULL DEFAULT 0,
        problem TEXT,
        keep INTEGER NOT NULL DEFAULT 0,
        present INTEGER NOT NULL DEFAULT 1,
        last_seen REAL NOT NULL
    );
    CREATE TABLE issues (
        id INTEGER PRIMARY KEY,
        paper TEXT NOT NULL,
        issue_date TEXT NOT NULL,
        torrent_hash TEXT,
        source_path TEXT NOT NULL,
        dest_dir TEXT,
        status TEXT NOT NULL,
        cover_status TEXT NOT NULL DEFAULT 'pending',
        cover_attempted_at REAL,
        error TEXT,
        keep INTEGER NOT NULL DEFAULT 0,
        linked_at REAL,
        updated_at REAL NOT NULL,
        UNIQUE (paper, issue_date)
    );
    CREATE INDEX issues_torrent ON issues(torrent_hash);
    CREATE TABLE unmatched (
        torrent_hash TEXT NOT NULL,
        path TEXT NOT NULL,
        reason TEXT NOT NULL,
        seen_at REAL NOT NULL,
        PRIMARY KEY (torrent_hash, path)
    );
    CREATE TABLE deletions (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('torrent', 'issue')),
        ref TEXT NOT NULL,
        label TEXT NOT NULL,
        size INTEGER NOT NULL DEFAULT 0,
        due_at REAL NOT NULL,
        created_at REAL NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'done', 'kept', 'cancelled', 'failed')),
        finished_at REAL,
        detail TEXT
    );
    CREATE UNIQUE INDEX deletions_one_pending ON deletions(kind, ref) WHERE status = 'pending';
    CREATE TABLE activity (
        id INTEGER PRIMARY KEY,
        ts REAL NOT NULL,
        level TEXT NOT NULL,
        message TEXT NOT NULL
    );
    CREATE INDEX activity_ts ON activity(ts);
    """,
    # 2: monthly and numbered issues. An issue is identified by a canonical key (2026-09-15, 2026-09,
    # 2026#038) instead of its date; issue_date stays a real date (1st of month / 1 January) because
    # retention and sorting rely on it. SQLite cannot change a UNIQUE constraint in place, so the
    # table is rebuilt with the same ids (pending deletions refer to issues by id).
    """
    CREATE TABLE issues_v2 (
        id INTEGER PRIMARY KEY,
        paper TEXT NOT NULL,
        issue_key TEXT NOT NULL,
        issue_label TEXT NOT NULL,
        period TEXT NOT NULL DEFAULT 'day' CHECK (period IN ('day', 'month', 'number')),
        issue_number INTEGER,
        issue_date TEXT NOT NULL,
        torrent_hash TEXT,
        source_path TEXT NOT NULL,
        dest_dir TEXT,
        status TEXT NOT NULL,
        cover_status TEXT NOT NULL DEFAULT 'pending',
        cover_attempted_at REAL,
        error TEXT,
        keep INTEGER NOT NULL DEFAULT 0,
        linked_at REAL,
        updated_at REAL NOT NULL,
        UNIQUE (paper, issue_key)
    );
    INSERT INTO issues_v2 (id, paper, issue_key, issue_label, period, issue_number, issue_date, torrent_hash,
                           source_path, dest_dir, status, cover_status, cover_attempted_at, error, keep,
                           linked_at, updated_at)
        SELECT id, paper, issue_date, issue_date, 'day', NULL, issue_date, torrent_hash, source_path, dest_dir,
               status, cover_status, cover_attempted_at, error, keep, linked_at, updated_at
        FROM issues;
    DROP TABLE issues;
    ALTER TABLE issues_v2 RENAME TO issues;
    CREATE INDEX issues_torrent ON issues(torrent_hash);

    -- Month or issue number, per newspaper: the user's choice wins over what was detected.
    ALTER TABLE papers ADD COLUMN numbering TEXT CHECK (numbering IN ('month', 'number'));
    ALTER TABLE papers ADD COLUMN numbering_detected TEXT CHECK (numbering_detected IN ('month', 'number'));
    ALTER TABLE papers ADD COLUMN numbering_reason TEXT;

    -- Numbers seen for a newspaper in an ambiguous January, to notice a second, different one.
    CREATE TABLE numbering_evidence (
        paper TEXT NOT NULL,
        year INTEGER NOT NULL,
        number INTEGER NOT NULL,
        seen_at REAL NOT NULL,
        PRIMARY KEY (paper, year, number)
    );
    """,
    # 3: files the user chose to ignore, so they no longer keep their torrent out of automatic delete.
    """
    CREATE TABLE ignored_files (
        torrent_hash TEXT NOT NULL,
        path TEXT NOT NULL,
        ignored_at REAL NOT NULL,
        ignored_by TEXT NOT NULL,
        PRIMARY KEY (torrent_hash, path)
    );
    """,
    # 4: libraries. What used to be one set of settings (category, folders, Jellyfin library, title
    # formats, days to keep) becomes library 1, built from the stored settings; publications, issues,
    # numbering evidence and downloads now belong to a library. The fallbacks below are the Settings
    # defaults of 1.2.0 (a test keeps them in step with libraries.py). Issue ids are kept.
    """
    CREATE TABLE libraries (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        category TEXT NOT NULL DEFAULT '',
        source_dir TEXT NOT NULL,
        dest_dir TEXT NOT NULL,
        jellyfin_library_id TEXT NOT NULL DEFAULT '',
        jellyfin_library_name TEXT NOT NULL DEFAULT '',
        title_format TEXT NOT NULL,
        monthly_title_format TEXT NOT NULL,
        numbered_title_format TEXT NOT NULL,
        language TEXT NOT NULL,
        cover_width INTEGER NOT NULL,
        retention_days INTEGER NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        position INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL
    );
    CREATE UNIQUE INDEX libraries_category ON libraries(category) WHERE category != '';
    INSERT INTO libraries (id, name, category, source_dir, dest_dir, jellyfin_library_id, jellyfin_library_name,
                           title_format, monthly_title_format, numbered_title_format, language, cover_width,
                           retention_days, enabled, position, created_at)
    VALUES (
        1, 'News',
        COALESCE((SELECT json_extract(value, '$') FROM settings WHERE key = 'qbit_category'), ''),
        COALESCE((SELECT json_extract(value, '$') FROM settings WHERE key = 'source_dir'),
                 '/data/torrents/books/news'),
        COALESCE((SELECT json_extract(value, '$') FROM settings WHERE key = 'dest_dir'), '/data/media/books/news'),
        COALESCE((SELECT json_extract(value, '$') FROM settings WHERE key = 'jellyfin_library_id'), ''),
        COALESCE((SELECT json_extract(value, '$') FROM settings WHERE key = 'jellyfin_library_name'), ''),
        COALESCE((SELECT json_extract(value, '$') FROM settings WHERE key = 'title_format'), '{paper} {iso}'),
        COALESCE((SELECT json_extract(value, '$') FROM settings WHERE key = 'monthly_title_format'), '{paper} {iso}'),
        COALESCE((SELECT json_extract(value, '$') FROM settings WHERE key = 'numbered_title_format'),
                 '{paper} {year} #{nn}'),
        COALESCE((SELECT json_extract(value, '$') FROM settings WHERE key = 'language'), 'en'),
        COALESCE((SELECT json_extract(value, '$') FROM settings WHERE key = 'cover_width'), 600),
        COALESCE((SELECT json_extract(value, '$') FROM settings WHERE key = 'retention_days'), 7),
        1, 0, strftime('%s', 'now')
    );

    CREATE TABLE papers_v4 (
        library_id INTEGER NOT NULL DEFAULT 1,
        name TEXT NOT NULL,
        display_name TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        retention_days INTEGER,
        created_at REAL NOT NULL,
        numbering TEXT CHECK (numbering IN ('month', 'number')),
        numbering_detected TEXT CHECK (numbering_detected IN ('month', 'number')),
        numbering_reason TEXT,
        PRIMARY KEY (library_id, name)
    );
    INSERT INTO papers_v4 (library_id, name, display_name, enabled, retention_days, created_at, numbering,
                           numbering_detected, numbering_reason)
        SELECT 1, name, display_name, enabled, retention_days, created_at, numbering, numbering_detected,
               numbering_reason
        FROM papers;
    DROP TABLE papers;
    ALTER TABLE papers_v4 RENAME TO papers;

    CREATE TABLE issues_v4 (
        id INTEGER PRIMARY KEY,
        library_id INTEGER NOT NULL DEFAULT 1,
        paper TEXT NOT NULL,
        issue_key TEXT NOT NULL,
        issue_label TEXT NOT NULL,
        period TEXT NOT NULL DEFAULT 'day' CHECK (period IN ('day', 'month', 'number')),
        issue_number INTEGER,
        issue_date TEXT NOT NULL,
        torrent_hash TEXT,
        source_path TEXT NOT NULL,
        dest_dir TEXT,
        status TEXT NOT NULL,
        cover_status TEXT NOT NULL DEFAULT 'pending',
        cover_attempted_at REAL,
        error TEXT,
        keep INTEGER NOT NULL DEFAULT 0,
        linked_at REAL,
        updated_at REAL NOT NULL,
        UNIQUE (library_id, paper, issue_key)
    );
    INSERT INTO issues_v4 (id, library_id, paper, issue_key, issue_label, period, issue_number, issue_date,
                           torrent_hash, source_path, dest_dir, status, cover_status, cover_attempted_at, error,
                           keep, linked_at, updated_at)
        SELECT id, 1, paper, issue_key, issue_label, period, issue_number, issue_date, torrent_hash, source_path,
               dest_dir, status, cover_status, cover_attempted_at, error, keep, linked_at, updated_at
        FROM issues;
    DROP TABLE issues;
    ALTER TABLE issues_v4 RENAME TO issues;
    CREATE INDEX issues_torrent ON issues(torrent_hash);

    CREATE TABLE numbering_evidence_v4 (
        library_id INTEGER NOT NULL DEFAULT 1,
        paper TEXT NOT NULL,
        year INTEGER NOT NULL,
        number INTEGER NOT NULL,
        seen_at REAL NOT NULL,
        PRIMARY KEY (library_id, paper, year, number)
    );
    INSERT INTO numbering_evidence_v4 (library_id, paper, year, number, seen_at)
        SELECT 1, paper, year, number, seen_at FROM numbering_evidence;
    DROP TABLE numbering_evidence;
    ALTER TABLE numbering_evidence_v4 RENAME TO numbering_evidence;

    ALTER TABLE torrents ADD COLUMN library_id INTEGER NOT NULL DEFAULT 1;
    """,
    # 5: file name patterns taught by the user, and the extra file types a library's downloads may contain.
    """
    CREATE TABLE name_patterns (
        id INTEGER PRIMARY KEY,
        library_id INTEGER NOT NULL,
        pattern TEXT NOT NULL,
        paper TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        UNIQUE (library_id, pattern)
    );
    ALTER TABLE libraries ADD COLUMN extra_extensions TEXT NOT NULL DEFAULT 'nfo';
    """,
    # 6: library health. A linked issue whose files are damaged waits for the user's Fix.
    """
    ALTER TABLE issues ADD COLUMN problem TEXT;
    ALTER TABLE issues ADD COLUMN problem_since REAL;
    ALTER TABLE issues ADD COLUMN fix_requested INTEGER NOT NULL DEFAULT 0;
    """,
    # 7: more book formats. The file linked for an issue, a format priority per library, and cbz/cbr/epub
    # (issues now) removed from the libraries' extra file types.
    """
    ALTER TABLE issues ADD COLUMN file_ext TEXT NOT NULL DEFAULT '.pdf';
    ALTER TABLE libraries ADD COLUMN format_priority TEXT NOT NULL DEFAULT 'cbz, pdf, epub, cbr';
    UPDATE libraries SET extra_extensions = TRIM(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
        ', ' || extra_extensions || ', ', ', cbz, ', ', '), ', cbr, ', ', '), ', epub, ', ', '),
        ', cbz, ', ', '), ', cbr, ', ', '), ', epub, ', ', '), ', ');
    """,
    # 8: cbr last by default, because Periodica cannot read a cover out of a RAR archive.
    """
    UPDATE libraries SET format_priority = 'cbz, pdf, epub, cbr' WHERE format_priority = 'cbz, cbr, pdf, epub';
    """,
]


BACKUPS_TO_KEEP = 5


class DatabaseTooNewError(RuntimeError):
    pass


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A connection inside a single transaction (committed on success, rolled back on error)."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.path.exists()
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > len(MIGRATIONS):
                raise DatabaseTooNewError(
                    f"{self.path} was upgraded by a newer Periodica (schema {version}, this version supports "
                    f"{len(MIGRATIONS)}). Use the newer image again, or restore a backup "
                    f"({self.path.name}.bak-v*) from before the upgrade."
                )
            if not new_file and 0 < version < len(MIGRATIONS):
                backup = self.backup(conn, f"bak-v{version}")
                logging.getLogger(__name__).warning("Database upgrade from schema %s: backup saved to %s",
                                                    version, backup)
            for index, script in enumerate(MIGRATIONS, start=1):
                if index <= version:
                    continue
                conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {index};\nCOMMIT;")
        finally:
            conn.close()
        if new_file and os.name == "posix":
            os.chmod(self.path, 0o600)

    def backup(self, conn: sqlite3.Connection, label: str) -> Path:
        """Consistent copy of the database next to it (SQLite online backup), keeping the newest few."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.path.with_name(f"{self.path.name}.{label}-{stamp}")
        dest = sqlite3.connect(target)
        try:
            conn.backup(dest)
        finally:
            dest.close()
        if os.name == "posix":
            os.chmod(target, 0o600)
        backups = sorted(self.path.parent.glob(f"{self.path.name}.bak-*"), key=lambda p: p.stat().st_mtime)
        for old in backups[:-BACKUPS_TO_KEEP]:
            old.unlink(missing_ok=True)
        return target

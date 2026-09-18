from __future__ import annotations

import sqlite3

import pytest

from periodica import app_version, build_label
from periodica import db as db_module
from periodica.db import Database, DatabaseTooNewError


def test_version_label_release_vs_dev(monkeypatch):
    monkeypatch.delenv("PERIODICA_VERSION", raising=False)
    assert app_version().endswith("-dev")
    monkeypatch.setenv("PERIODICA_VERSION", "1.2.3")
    monkeypatch.setenv("PERIODICA_COMMIT", "a0d8210" + "0" * 33)
    assert build_label() == "1.2.3 · a0d8210"
    monkeypatch.setenv("PERIODICA_VERSION", "1.2.3<script>")
    assert app_version().endswith("-dev")


def test_upgrade_makes_backup_and_keeps_data(tmp_path, monkeypatch):
    path = tmp_path / "periodica.db"
    Database(path).migrate()
    current = len(db_module.MIGRATIONS)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES ('probe', '1')")
    monkeypatch.setattr(db_module, "MIGRATIONS", [*db_module.MIGRATIONS, "CREATE TABLE extra (x INTEGER);"])
    Database(path).migrate()
    backups = list(tmp_path.glob(f"periodica.db.bak-v{current}-*"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == current
        assert conn.execute("SELECT value FROM settings WHERE key = 'probe'").fetchone()[0] == "1"
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == current + 1


def test_upgrade_from_1_0_0_keeps_issues_and_their_ids(tmp_path, monkeypatch):
    """A real 1.0.0 database (schema 1) upgrades to monthly/numbered support without losing anything."""
    path = tmp_path / "periodica.db"
    all_migrations = list(db_module.MIGRATIONS)
    monkeypatch.setattr(db_module, "MIGRATIONS", all_migrations[:1])
    Database(path).migrate()
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO papers (name, created_at) VALUES ('Chronicle', 1)")
        conn.execute(
            "INSERT INTO issues (id, paper, issue_date, source_path, dest_dir, status, keep, linked_at, updated_at) "
            "VALUES (7, 'Chronicle', '2026-09-15', '/data/x.pdf', '/data/media/y', 'linked', 1, 10, 10)")
        conn.execute("INSERT INTO deletions (kind, ref, label, due_at, created_at, status) "
                     "VALUES ('issue', '7', 'Chronicle 2026-09-15', 1, 1, 'pending')")

    monkeypatch.setattr(db_module, "MIGRATIONS", all_migrations)
    Database(path).migrate()
    assert list(tmp_path.glob("periodica.db.bak-v1-*")), "a 1.0.0 database must be backed up first"

    from periodica.repo import Repo

    repo = Repo(Database(path))
    issue = repo.issue_by_key("Chronicle", "2026-09-15")
    assert issue is not None and issue.id == 7, "pending deletions refer to issues by id"
    assert (issue.period, issue.issue_label, issue.keep, issue.dest_dir) == ("day", "2026-09-15", True,
                                                                             "/data/media/y")
    paper = repo.paper("Chronicle")
    assert paper is not None and paper.numbering is None and paper.effective_numbering is None


def test_new_database_has_no_backup(tmp_path):
    Database(tmp_path / "periodica.db").migrate()
    assert not list(tmp_path.glob("*.bak-*"))


def test_refuses_database_from_newer_version(tmp_path):
    path = tmp_path / "periodica.db"
    Database(path).migrate()
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version = 99")
    with pytest.raises(DatabaseTooNewError, match="newer Periodica"):
        Database(path).migrate()

"""Database queries used by the scanner, retention and web UI."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from .db import Database
from .issues import IssueId
from .libraries import Library

ACTIVITY_KEEP_DAYS = 30


@dataclass(frozen=True)
class Paper:
    name: str
    display_name: str | None
    enabled: bool
    retention_days: int | None
    numbering: str | None = None           # the user's choice: "month" / "number"
    numbering_detected: str | None = None  # what the scanner concluded
    numbering_reason: str | None = None
    library_id: int = 1

    @property
    def label(self) -> str:
        return self.display_name or self.name

    @property
    def effective_numbering(self) -> str | None:
        return self.numbering or self.numbering_detected


@dataclass(frozen=True)
class IssueRow:
    id: int
    paper: str
    issue_date: str
    torrent_hash: str | None
    source_path: str
    dest_dir: str | None
    status: str
    cover_status: str
    cover_attempted_at: float | None
    error: str | None
    keep: bool
    linked_at: float | None
    issue_key: str = ""
    issue_label: str = ""
    period: str = "day"
    issue_number: int | None = None
    library_id: int = 1
    problem: str | None = None       # library health: missing | copy | metadata | cover
    problem_since: float | None = None
    fix_requested: bool = False
    file_ext: str = ".pdf"           # the format linked into the library

    @property
    def identity(self) -> IssueId:
        return IssueId.from_stored(self.period, self.issue_date, self.issue_number)


def _paper(row) -> Paper:
    return Paper(row["name"], row["display_name"], bool(row["enabled"]), row["retention_days"],
                 row["numbering"], row["numbering_detected"], row["numbering_reason"], row["library_id"])


def _issue(row) -> IssueRow:
    return IssueRow(
        id=row["id"], paper=row["paper"], issue_date=row["issue_date"], torrent_hash=row["torrent_hash"],
        source_path=row["source_path"], dest_dir=row["dest_dir"], status=row["status"],
        cover_status=row["cover_status"], cover_attempted_at=row["cover_attempted_at"], error=row["error"],
        keep=bool(row["keep"]), linked_at=row["linked_at"], issue_key=row["issue_key"],
        issue_label=row["issue_label"], period=row["period"], issue_number=row["issue_number"],
        library_id=row["library_id"], problem=row["problem"], problem_since=row["problem_since"],
        fix_requested=bool(row["fix_requested"]), file_ext=row["file_ext"],
    )


LIBRARY_COLUMNS = ("name", "category", "source_dir", "dest_dir", "jellyfin_library_id", "jellyfin_library_name",
                   "title_format", "monthly_title_format", "numbered_title_format", "language", "cover_width",
                   "retention_days", "enabled", "position", "extra_extensions", "format_priority",
                   "stale_download_hours")


def _library(row) -> Library:
    data = dict(row)
    data["enabled"] = bool(data["enabled"])
    return Library(**data)


# Rows of one table whose download was seen in this category (folder downloads have category "").
# Rows without a download row are included, so nothing can be orphaned forever.
# The library a pending or finished deletion belongs to (through its torrent or issue).
_DELETION_LIBRARY = ("(CASE d.kind WHEN 'torrent' THEN (SELECT t.library_id FROM torrents t WHERE t.hash = d.ref) "
                     "ELSE (SELECT i.library_id FROM issues i WHERE i.id = CAST(d.ref AS INTEGER)) END)")
_OF_CATEGORY = ("SELECT DISTINCT x.torrent_hash FROM {table} x LEFT JOIN torrents t ON t.hash = x.torrent_hash "
                "WHERE (t.category = ? AND t.library_id = ?) OR t.hash IS NULL")


class Repo:
    def __init__(self, db: Database):
        self.db = db

    # --- activity ---------------------------------------------------------------------------
    def log(self, level: str, message: str) -> None:
        now = time.time()
        with self.db.connect() as conn:
            conn.execute("INSERT INTO activity (ts, level, message) VALUES (?, ?, ?)",
                         (now, level, message[:2000]))
            conn.execute("DELETE FROM activity WHERE ts < ?", (now - ACTIVITY_KEEP_DAYS * 86400,))

    def activity(self, limit: int = 200, level: str | None = None) -> list[dict]:
        sql = "SELECT ts, level, message FROM activity"
        args: list = []
        if level:
            sql += " WHERE level = ?"
            args.append(level)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    # --- key/value state --------------------------------------------------------------------
    def get_state(self, key: str, default=None):
        with self.db.connect() as conn:
            row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default

    def set_state(self, key: str, value) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

    def delete_state(self, key: str) -> None:
        with self.db.connect() as conn:
            conn.execute("DELETE FROM state WHERE key = ?", (key,))

    # --- libraries --------------------------------------------------------------------------
    def libraries(self, enabled_only: bool = False) -> list[Library]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM libraries WHERE enabled = 1 OR ? ORDER BY position, id",
                                (0 if enabled_only else 1,)).fetchall()
        return [_library(r) for r in rows]

    def library(self, library_id: int) -> Library | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM libraries WHERE id = ?", (library_id,)).fetchone()
        return _library(row) if row else None

    def add_library(self, library: Library) -> int:
        data = library.model_dump()
        with self.db.connect() as conn:
            if not data["position"]:
                data["position"] = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM libraries").fetchone()[0]
            cursor = conn.execute(
                f"INSERT INTO libraries ({', '.join(LIBRARY_COLUMNS)}, created_at) "  # noqa: S608  # nosec B608
                f"VALUES ({', '.join('?' for _ in LIBRARY_COLUMNS)}, ?)",
                [data[c] if c != "enabled" else int(data[c]) for c in LIBRARY_COLUMNS] + [time.time()],
            )
            return int(cursor.lastrowid or 0)

    def update_library(self, library: Library) -> None:
        data = library.model_dump()
        with self.db.connect() as conn:
            conn.execute(
                f"UPDATE libraries SET {', '.join(f'{c} = ?' for c in LIBRARY_COLUMNS)} "  # noqa: S608  # nosec B608
                "WHERE id = ?",
                [data[c] if c != "enabled" else int(data[c]) for c in LIBRARY_COLUMNS] + [library.id],
            )

    def delete_library(self, library_id: int, reason: str) -> None:
        """Forget a library and everything recorded for it. Files on disk are not touched."""
        now = time.time()
        with self.db.connect() as conn:
            hashes = [r["hash"] for r in conn.execute("SELECT hash FROM torrents WHERE library_id = ?",
                                                      (library_id,))]
            issue_ids = [str(r["id"]) for r in conn.execute("SELECT id FROM issues WHERE library_id = ?",
                                                            (library_id,))]
            for kind, refs in (("torrent", hashes), ("issue", issue_ids)):
                for ref in refs:
                    conn.execute("UPDATE deletions SET status = 'cancelled', finished_at = ?, detail = ? "
                                 "WHERE kind = ? AND ref = ? AND status = 'pending'", (now, reason, kind, ref))
            for torrent_hash in hashes:
                conn.execute("DELETE FROM unmatched WHERE torrent_hash = ?", (torrent_hash,))
                conn.execute("DELETE FROM ignored_files WHERE torrent_hash = ?", (torrent_hash,))
            for table in ("issues", "papers", "numbering_evidence", "torrents", "name_patterns"):
                conn.execute(f"DELETE FROM {table} WHERE library_id = ?", (library_id,))  # noqa: S608  # nosec B608
            conn.execute("DELETE FROM libraries WHERE id = ?", (library_id,))

    def library_overview(self) -> dict[int, dict]:
        """Per library: linked issues, publications, unmatched files and the newest download."""
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT l.id, "
                "(SELECT COUNT(*) FROM issues i WHERE i.library_id = l.id AND i.status = 'linked') AS issues, "
                "(SELECT COUNT(*) FROM papers p WHERE p.library_id = l.id) AS papers, "
                "(SELECT COUNT(*) FROM unmatched u JOIN torrents t ON t.hash = u.torrent_hash "
                " WHERE t.library_id = l.id AND t.present = 1) AS unmatched, "
                "(SELECT MAX(t.completion_on) FROM torrents t WHERE t.library_id = l.id) AS last_download, "
                "(SELECT COUNT(*) FROM issues i WHERE i.library_id = l.id AND i.status = 'linked' "
                " AND i.problem IS NOT NULL) AS problems "
                "FROM libraries l").fetchall()
        return {r["id"]: dict(r) for r in rows}

    # --- name patterns ------------------------------------------------------------------------
    def name_patterns(self, library_id: int) -> list[dict]:
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM name_patterns WHERE library_id = ? ORDER BY id", (library_id,))]

    def add_name_pattern(self, library_id: int, pattern: str, paper: str) -> None:
        with self.db.connect() as conn:
            conn.execute("INSERT INTO name_patterns (library_id, pattern, paper, created_at) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(library_id, pattern) DO UPDATE SET paper = excluded.paper",
                         (library_id, pattern, paper, time.time()))

    def delete_name_pattern(self, library_id: int, pattern_id: int) -> bool:
        with self.db.connect() as conn:
            return conn.execute("DELETE FROM name_patterns WHERE id = ? AND library_id = ?",
                                (pattern_id, library_id)).rowcount > 0

    def library_issue_counts(self) -> dict[int, int]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT library_id, COUNT(*) AS n FROM issues WHERE status = 'linked' "
                                "GROUP BY library_id").fetchall()
        return {r["library_id"]: r["n"] for r in rows}

    # --- papers -----------------------------------------------------------------------------
    def ensure_paper(self, name: str, library_id: int = 1) -> Paper:
        with self.db.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO papers (library_id, name, created_at) VALUES (?, ?, ?)",
                         (library_id, name, time.time()))
            return _paper(conn.execute("SELECT * FROM papers WHERE library_id = ? AND name = ?",
                                       (library_id, name)).fetchone())

    def papers(self, library_id: int | None = None) -> list[Paper]:
        sql = "SELECT * FROM papers"
        args: list = []
        if library_id is not None:
            sql += " WHERE library_id = ?"
            args.append(library_id)
        sql += " ORDER BY name COLLATE NOCASE, library_id"
        with self.db.connect() as conn:
            return [_paper(r) for r in conn.execute(sql, args)]

    def paper(self, name: str, library_id: int = 1) -> Paper | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM papers WHERE library_id = ? AND name = ?",
                               (library_id, name)).fetchone()
        return _paper(row) if row else None

    def update_paper(self, name: str, display_name: str | None, enabled: bool, retention_days: int | None,
                     library_id: int = 1) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE papers SET display_name = ?, enabled = ?, retention_days = ? WHERE library_id = ? "
                "AND name = ?",
                (display_name or None, int(enabled), retention_days, library_id, name),
            )

    def set_paper_numbering(self, name: str, choice: str | None, library_id: int = 1) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE papers SET numbering = ? WHERE library_id = ? AND name = ?",
                         (choice or None, library_id, name))

    def set_paper_detected(self, name: str, detected: str | None, reason: str | None, library_id: int = 1) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE papers SET numbering_detected = ?, numbering_reason = ? "
                         "WHERE library_id = ? AND name = ?", (detected, reason, library_id, name))

    def january_numbers(self, paper: str, year: int, library_id: int = 1) -> set[int]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT number FROM numbering_evidence WHERE library_id = ? AND paper = ? "
                                "AND year = ?", (library_id, paper, year)).fetchall()
        return {r["number"] for r in rows}

    def add_january_number(self, paper: str, year: int, number: int, library_id: int = 1) -> None:
        with self.db.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO numbering_evidence (library_id, paper, year, number, seen_at) "
                         "VALUES (?, ?, ?, ?, ?)", (library_id, paper, year, number, time.time()))

    def paper_stats(self) -> dict[tuple[int, str], dict]:
        """Per (library id, publication): linked issue count, latest label, latest cover id, periods."""
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT i.library_id, i.paper, COUNT(*) AS n, "
                "(SELECT j.issue_label FROM issues j WHERE j.library_id = i.library_id AND j.paper = i.paper "
                " AND j.status = 'linked' ORDER BY j.issue_key DESC LIMIT 1) AS latest, "
                "MAX(CASE WHEN i.cover_status = 'ok' THEN i.id END) AS latest_id, "
                "GROUP_CONCAT(DISTINCT i.period) AS periods "
                "FROM issues i WHERE i.status = 'linked' GROUP BY i.library_id, i.paper"
            ).fetchall()
        return {(r["library_id"], r["paper"]): {"count": r["n"], "latest": r["latest"], "latest_id": r["latest_id"],
                                                "periods": set((r["periods"] or "").split(","))} for r in rows}

    # --- torrents ---------------------------------------------------------------------------
    def upsert_torrent(self, *, hash: str, name: str, category: str, state: str, completion_on: float | None,
                       size: int, files_ok: bool, problem: str | None, library_id: int = 1) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO torrents (hash, name, category, state, completion_on, size, files_ok, problem, "
                "present, last_seen, library_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?) "
                "ON CONFLICT(hash) DO UPDATE SET name = excluded.name, category = excluded.category, "
                "state = excluded.state, completion_on = excluded.completion_on, size = excluded.size, "
                "files_ok = excluded.files_ok, problem = excluded.problem, present = 1, "
                "last_seen = excluded.last_seen, library_id = excluded.library_id",
                (hash, name, category, state, completion_on, size, int(files_ok), problem, time.time(),
                 library_id),
            )

    def mark_torrents_absent(self, present_hashes: set[str], library_ids: set[int] | None = None) -> None:
        """Mark downloads not seen by this scan as gone, only for the libraries that were scanned."""
        with self.db.connect() as conn:
            rows = conn.execute("SELECT hash, library_id FROM torrents WHERE present = 1").fetchall()
            for row in rows:
                if library_ids is not None and row["library_id"] not in library_ids:
                    continue
                if row["hash"] not in present_hashes:
                    conn.execute("UPDATE torrents SET present = 0 WHERE hash = ?", (row["hash"],))

    def kept_torrents(self) -> set[str]:
        with self.db.connect() as conn:
            return {r["hash"] for r in conn.execute("SELECT hash FROM torrents WHERE keep = 1")}

    def set_torrent_keep(self, torrent_hash: str, keep: bool) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE torrents SET keep = ? WHERE hash = ?", (int(keep), torrent_hash))
            conn.execute("UPDATE issues SET keep = ? WHERE torrent_hash = ?", (int(keep), torrent_hash))

    def torrent(self, torrent_hash: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM torrents WHERE hash = ?", (torrent_hash,)).fetchone()
        return dict(row) if row else None

    def present_torrents(self) -> list[dict]:
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT t.*, (SELECT COUNT(*) FROM issues i WHERE i.torrent_hash = t.hash AND i.status = 'linked') "
                "AS linked_issues FROM torrents t WHERE t.present = 1 ORDER BY t.completion_on DESC, t.name")]

    def last_download_at(self, category: str, library_id: int | None = None) -> float | None:
        """Newest completion time of any download ever seen in the category (deleted ones included)."""
        sql = "SELECT MAX(completion_on) FROM torrents WHERE category = ?"
        args: list = [category]
        if library_id is not None:
            sql += " AND library_id = ?"
            args.append(library_id)
        with self.db.connect() as conn:
            row = conn.execute(sql, args).fetchone()
        return row[0] if row and row[0] else None

    def mark_torrent_gone(self, torrent_hash: str) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE torrents SET present = 0 WHERE hash = ?", (torrent_hash,))

    def torrents(self) -> list[dict]:
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM torrents ORDER BY completion_on DESC")]

    # --- issues -----------------------------------------------------------------------------
    def issue(self, issue_id: int) -> IssueRow | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        return _issue(row) if row else None

    def issue_by_key(self, paper: str, issue_key: str, library_id: int = 1) -> IssueRow | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM issues WHERE library_id = ? AND paper = ? AND issue_key = ?",
                               (library_id, paper, issue_key)).fetchone()
        return _issue(row) if row else None

    def issues_for_paper(self, paper: str, library_id: int = 1) -> list[IssueRow]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM issues WHERE library_id = ? AND paper = ? ORDER BY issue_key DESC",
                                (library_id, paper))
            return [_issue(r) for r in rows]

    def linked_issues(self, library_ids: set[int] | None = None) -> list[IssueRow]:
        with self.db.connect() as conn:
            rows = [_issue(r) for r in conn.execute("SELECT * FROM issues WHERE status = 'linked'")]
        return rows if library_ids is None else [r for r in rows if r.library_id in library_ids]

    def issues_for_torrent(self, torrent_hash: str) -> list[IssueRow]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM issues WHERE torrent_hash = ?", (torrent_hash,))
            return [_issue(r) for r in rows]

    def save_issue(self, *, paper: str, issue: IssueId, torrent_hash: str | None, source_path: str,
                   dest_dir: str | None, status: str, error: str | None = None,
                   cover_status: str | None = None, cover_attempted: bool = False,
                   linked: bool = False, library_id: int = 1, file_ext: str | None = None) -> None:
        now = time.time()
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO issues (library_id, paper, issue_key, issue_label, period, issue_number, issue_date, "
                "torrent_hash, source_path, dest_dir, status, error, "
                "cover_status, cover_attempted_at, linked_at, updated_at, file_ext) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 'pending'), ?, ?, ?, COALESCE(?, '.pdf')) "
                "ON CONFLICT(library_id, paper, issue_key) DO UPDATE SET torrent_hash = excluded.torrent_hash, "
                "source_path = excluded.source_path, dest_dir = COALESCE(excluded.dest_dir, issues.dest_dir), "
                "status = excluded.status, error = excluded.error, "
                "cover_status = COALESCE(?, issues.cover_status), "
                "cover_attempted_at = COALESCE(excluded.cover_attempted_at, issues.cover_attempted_at), "
                "linked_at = COALESCE(issues.linked_at, excluded.linked_at), updated_at = excluded.updated_at, "
                "file_ext = CASE WHEN ? IS NULL THEN issues.file_ext ELSE excluded.file_ext END",
                (library_id, paper, issue.key, issue.label, issue.period, issue.number or None,
                 issue.nominal_date.isoformat(), torrent_hash, source_path, dest_dir, status, error, cover_status,
                 now if cover_attempted else None, now if linked else None, now, file_ext, cover_status,
                 file_ext),
            )

    def update_issue_identity(self, issue_id: int, issue: IssueId, dest_dir: str | None) -> None:
        """Relabel an issue (month <-> numbered) in place, keeping its id, history and keep flag."""
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE issues SET issue_key = ?, issue_label = ?, period = ?, issue_number = ?, issue_date = ?, "
                "dest_dir = COALESCE(?, dest_dir), updated_at = ? WHERE id = ?",
                (issue.key, issue.label, issue.period, issue.number or None, issue.nominal_date.isoformat(),
                 dest_dir, time.time(), issue_id),
            )

    def set_issue_status(self, issue_id: int, status: str, error: str | None = None) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE issues SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                         (status, error, time.time(), issue_id))
            if status != "linked":
                conn.execute("UPDATE issues SET problem = NULL, problem_since = NULL, fix_requested = 0 "
                             "WHERE id = ?", (issue_id,))

    # --- library health -------------------------------------------------------------------------
    def set_issue_problem(self, issue_id: int, problem: str) -> bool:
        """Record a health problem. Returns True when it is new (or a different one)."""
        with self.db.connect() as conn:
            row = conn.execute("SELECT problem FROM issues WHERE id = ?", (issue_id,)).fetchone()
            if row is None or row["problem"] == problem:
                return False
            conn.execute("UPDATE issues SET problem = ?, problem_since = ? WHERE id = ?",
                         (problem, time.time(), issue_id))
            return True

    def clear_issue_problem(self, issue_id: int) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE issues SET problem = NULL, problem_since = NULL, fix_requested = 0 WHERE id = ?",
                         (issue_id,))

    def set_fix_requested(self, issue_ids: list[int], requested: bool = True) -> int:
        """Ask the next scan to repair these issues. Only linked issues with a problem are marked."""
        if not issue_ids:
            return 0
        with self.db.connect() as conn:
            marks = ",".join("?" * len(issue_ids))
            return conn.execute(
                f"UPDATE issues SET fix_requested = ? WHERE id IN ({marks}) "  # noqa: S608  # nosec B608
                "AND status = 'linked' AND problem IS NOT NULL", (int(requested), *issue_ids)).rowcount

    def issue_problems(self, library_ids: set[int] | None = None) -> list[dict]:
        """Linked issues with a health problem, with their publication label and library name."""
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT i.*, COALESCE(p.display_name, i.paper) AS label, l.name AS library_name, "
                "t.present AS download_present "
                "FROM issues i JOIN libraries l ON l.id = i.library_id "
                "LEFT JOIN papers p ON p.library_id = i.library_id AND p.name = i.paper "
                "LEFT JOIN torrents t ON t.hash = i.torrent_hash "
                "WHERE i.status = 'linked' AND i.problem IS NOT NULL "
                "ORDER BY l.position, l.id, label, i.issue_key DESC").fetchall()
        result = [dict(r) for r in rows]
        return result if library_ids is None else [r for r in result if r["library_id"] in library_ids]

    def set_issue_keep(self, issue_id: int, keep: bool) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE issues SET keep = ? WHERE id = ?", (int(keep), issue_id))

    def reset_cover(self, issue_id: int) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE issues SET cover_status = 'pending', cover_attempted_at = NULL WHERE id = ?",
                         (issue_id,))

    def issue_counts(self) -> dict[str, int]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM issues GROUP BY status").fetchall()
            covers_failed = conn.execute(
                "SELECT COUNT(*) FROM issues WHERE status = 'linked' AND cover_status = 'failed'").fetchone()[0]
        counts = {r["status"]: r["n"] for r in rows}
        counts["covers_failed"] = covers_failed
        return counts

    # --- unmatched --------------------------------------------------------------------------
    def record_unmatched(self, torrent_hash: str, path: str, reason: str) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO unmatched (torrent_hash, path, reason, seen_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(torrent_hash, path) DO UPDATE SET reason = excluded.reason, seen_at = excluded.seen_at",
                (torrent_hash, path, reason, time.time()),
            )

    def clear_unmatched_for(self, torrent_hash: str) -> None:
        with self.db.connect() as conn:
            conn.execute("DELETE FROM unmatched WHERE torrent_hash = ?", (torrent_hash,))

    def prune_unmatched(self, present_hashes: set[str], category: str, library_id: int = 1) -> None:
        with self.db.connect() as conn:
            for row in conn.execute(_OF_CATEGORY.format(table="unmatched"), (category, library_id)).fetchall():
                if row["torrent_hash"] not in present_hashes:
                    conn.execute("DELETE FROM unmatched WHERE torrent_hash = ?", (row["torrent_hash"],))

    def unmatched(self) -> list[dict]:
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT u.*, t.name AS torrent_name, COALESCE(t.library_id, 1) AS library_id FROM unmatched u "
                "LEFT JOIN torrents t ON t.hash = u.torrent_hash WHERE t.present = 1 OR t.hash IS NULL "
                "ORDER BY u.seen_at DESC")]

    def unmatched_row(self, torrent_hash: str, path: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM unmatched WHERE torrent_hash = ? AND path = ?",
                               (torrent_hash, path)).fetchone()
        return dict(row) if row else None

    # --- ignored files ----------------------------------------------------------------------
    def ignore_file(self, torrent_hash: str, path: str, username: str) -> None:
        with self.db.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO ignored_files (torrent_hash, path, ignored_at, ignored_by) "
                         "VALUES (?, ?, ?, ?)", (torrent_hash, path, time.time(), username))
            conn.execute("DELETE FROM unmatched WHERE torrent_hash = ? AND path = ?", (torrent_hash, path))

    def unignore_file(self, torrent_hash: str, path: str) -> bool:
        with self.db.connect() as conn:
            return conn.execute("DELETE FROM ignored_files WHERE torrent_hash = ? AND path = ?",
                                (torrent_hash, path)).rowcount > 0

    def ignored_paths(self, torrent_hash: str) -> set[str]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT path FROM ignored_files WHERE torrent_hash = ?", (torrent_hash,))
            return {r["path"] for r in rows}

    def ignored_files(self) -> list[dict]:
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT i.*, t.name AS torrent_name, COALESCE(t.library_id, 1) AS library_id FROM ignored_files i "
                "LEFT JOIN torrents t ON t.hash = i.torrent_hash WHERE t.present = 1 OR t.hash IS NULL "
                "ORDER BY i.ignored_at DESC")]

    def prune_ignored(self, present_hashes: set[str], category: str, library_id: int = 1) -> None:
        """Forget ignore choices for downloads of this library and category (mode) that are gone."""
        with self.db.connect() as conn:
            for row in conn.execute(_OF_CATEGORY.format(table="ignored_files"),
                                    (category, library_id)).fetchall():
                if row["torrent_hash"] not in present_hashes:
                    conn.execute("DELETE FROM ignored_files WHERE torrent_hash = ?", (row["torrent_hash"],))

    # --- deletions --------------------------------------------------------------------------
    def pending_deletions(self) -> list[dict]:
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT d.*, " + _DELETION_LIBRARY + " AS library_id "  # nosec B608
                "FROM deletions d WHERE d.status = 'pending' ORDER BY d.due_at, d.id")]

    def deletion_history(self, limit: int = 200) -> list[dict]:
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT d.*, " + _DELETION_LIBRARY + " AS library_id "  # nosec B608
                "FROM deletions d WHERE d.status != 'pending' ORDER BY COALESCE(d.finished_at, d.created_at) "
                "DESC LIMIT ?", (limit,))]

    def deletion(self, deletion_id: int) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM deletions WHERE id = ?", (deletion_id,)).fetchone()
        return dict(row) if row else None

    def ensure_pending(self, kind: str, ref: str, label: str, size: int, due_at: float) -> None:
        with self.db.connect() as conn:
            exists = conn.execute("SELECT 1 FROM deletions WHERE kind = ? AND ref = ? AND status = 'pending'",
                                  (kind, ref)).fetchone()
            if exists:
                conn.execute("UPDATE deletions SET label = ?, size = ? WHERE kind = ? AND ref = ? "
                             "AND status = 'pending'", (label, size, kind, ref))
            else:
                conn.execute(
                    "INSERT INTO deletions (kind, ref, label, size, due_at, created_at, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'pending')", (kind, ref, label, size, due_at, time.time()))

    def finish_deletion(self, deletion_id: int, status: str, detail: str | None = None) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE deletions SET status = ?, finished_at = ?, detail = ? WHERE id = ?",
                         (status, time.time(), detail, deletion_id))

    def note_deletion(self, deletion_id: int, detail: str) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE deletions SET detail = ? WHERE id = ?", (detail, deletion_id))

    def make_due_now(self, deletion_id: int) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE deletions SET due_at = ? WHERE id = ? AND status = 'pending'",
                         (time.time() - 1, deletion_id))

    def record_deletion(self, kind: str, ref: str, label: str, size: int, status: str, detail: str) -> None:
        now = time.time()
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO deletions (kind, ref, label, size, due_at, created_at, status, finished_at, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (kind, ref, label, size, now, now, status, now, detail))

    def cancel_pending_for(self, kind: str, ref: str, reason: str) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE deletions SET status = 'cancelled', finished_at = ?, detail = ? "
                         "WHERE kind = ? AND ref = ? AND status = 'pending'", (time.time(), reason, kind, ref))

    def cancel_pending(self, reason: str, keep_keys: set[tuple[str, str]] | None = None) -> int:
        cancelled = 0
        with self.db.connect() as conn:
            for row in conn.execute("SELECT id, kind, ref FROM deletions WHERE status = 'pending'").fetchall():
                if keep_keys is not None and (row["kind"], row["ref"]) in keep_keys:
                    continue
                conn.execute("UPDATE deletions SET status = 'cancelled', finished_at = ?, detail = ? WHERE id = ?",
                             (time.time(), reason, row["id"]))
                cancelled += 1
        return cancelled

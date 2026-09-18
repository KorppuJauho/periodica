"""One scan: for each library, discover its downloads, link issues, write metadata/covers; then retention."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import covers, health
from .config import Env
from .formats import CBR, CBZ, EPUB, PDF, FormatError, book_suffix, by_priority, check_source, extract_cover, find_cover
from .issues import DAY, MONTH, NUMBER, IssueId
from .jellyfin import JellyfinClient, RefreshCoordinator
from .libraries import Library, library_conflicts
from .linker import (
    CrossDeviceError,
    LinkError,
    ensure_library_keep_file,
    is_managed_issue_dir,
    issue_paths,
    link_book,
    move_issue_dir,
    replace_with_link,
    write_marker,
)
from .metadata import build_opf, render_title, write_atomic
from .netsafe import HttpError, UnsafeUrlError
from .numbering import JANUARY_AMBIGUOUS, decide, reference_month
from .parser import parse_issue_filename
from .paths import PathMapping, UnsafePathError, is_within, same_filesystem, validate_torrent_relpath
from .patterns import NamePattern, compile_pattern
from .qbittorrent import QbitClient, QbitError
from .repo import IssueRow, Paper, Repo
from .retention import (
    RetentionRunner,
    TorrentItem,
    TorrentSnapshot,
    fallback_reference,
    is_expired,
    plan_retention,
)
from .settings import LIBRARY_FIELDS, Settings, SettingsStore
from .sources import API_FINISHED_KEY, FOLDER_CATEGORY, Download, FolderSource, QbitSource

log = logging.getLogger(__name__)

ALLOWED_EXTRA_EXTENSIONS = {".nfo"}
COVER_RETRY_SECONDS = 6 * 3600
LAST_SCAN_KEY = "last_scan"
RSS_STATUS_KEY = "rss_status"            # {category: status}
STALE_WARNED_KEY = "stale_download_warned"  # {library id: completion time warned about}
UNMATCHED_KEY = "unmatched_seen"
QBIT_STATUS_KEY = "qbit_status"
RSS_CHECK_INTERVAL = 30 * 60
# Unmatched reason for a year + number name that cannot be classified yet; the UI offers a choice.
UNCLEAR_REASON = "monthly or numbered?"
UNRECOGNISED_REASON = ("file name not recognised as an issue (<Paper>.<YYYY>.<MM>.<DD>.pdf or <Paper>.<YYYY>.<NN>.pdf;"
                       " also .cbz, .cbr, .epub)")

Source = QbitSource | FolderSource


@dataclass
class ScanReport:
    started: float = field(default_factory=time.time)
    finished: float | None = None
    dry_run: bool = False
    error: str | None = None
    torrents: int = 0
    linked: int = 0
    would_link: int = 0
    already_linked: int = 0
    moved: int = 0
    covers: int = 0
    cover_failures: int = 0
    unmatched: int = 0
    skipped_expired: int = 0
    skipped_disabled: int = 0
    issue_errors: int = 0
    torrents_deleted: int = 0
    issues_removed: int = 0
    health_problems: int = 0   # linked issues whose files are damaged (waiting for Fix)
    fixed: int = 0
    duplicates: int = 0        # issues already in the library from another download
    retention_blocked: str | None = None
    trigger: str | None = None
    jellyfin_refreshed: bool = False
    messages: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.linked or self.moved or self.covers or self.torrents_deleted or self.issues_removed
                    or self.fixed)

    def as_dict(self) -> dict:
        data = {k: v for k, v in self.__dict__.items() if k != "messages"}
        data["messages"] = self.messages[-50:]
        return data


def global_problems(settings: Settings) -> list[str]:
    return ["qBittorrent URL is not set"] if settings.uses_qbit and not settings.qbit_url else []


def library_problems(library: Library, settings: Settings, env: Env,
                     others: Sequence[Library] = ()) -> list[str]:
    problems = []
    if settings.uses_qbit and not library.category:
        problems.append("qBittorrent category is not set")
    for label, value in (("Source folder", library.source_dir), ("Destination folder", library.dest_dir)):
        if not is_within(value, env.data_root):
            problems.append(f"{label} must be inside {env.data_root}")
    src, dst = Path(library.source_dir), Path(library.dest_dir)
    if not src.is_dir():
        problems.append(f"Source folder {src} does not exist")
    dst_existing = dst if dst.exists() else dst.parent
    if not dst_existing.is_dir():
        problems.append(f"Destination folder {dst} (or its parent) does not exist")
    if src.is_dir() and dst_existing.is_dir():
        if is_within(dst, src) or is_within(src, dst):
            problems.append("Source and destination folders must not be inside each other")
        elif not same_filesystem(src, dst_existing):
            problems.append("Source and destination are on different filesystems; hardlinks are impossible")
    problems.extend(library_conflicts(library, list(others)))
    return problems


def library_from_settings(settings: Settings, base: Library | None = None) -> Library:
    """The first library as the (library-unaware) settings pages see and edit it."""
    values = {column: getattr(settings, field) for field, column in LIBRARY_FIELDS.items()}
    if base is None:
        return Library.model_construct(**values)
    return base.model_copy(update=values)


def config_problems(settings: Settings, env: Env) -> list[str]:
    """Problems of the shared settings and of the first library (what the settings pages edit)."""
    return global_problems(settings) + library_problems(library_from_settings(settings), settings, env)


def all_problems(settings: Settings, env: Env, libraries: list[Library]) -> list[str]:
    """Everything that keeps (part of) the scanner idle, for the dashboard and settings pages."""
    enabled = [lib for lib in libraries if lib.enabled]
    problems = global_problems(settings) + ([] if enabled else ["no library is enabled"])
    for lib in enabled:
        found = library_problems(lib, settings, env, libraries)
        problems.extend(f"{lib.name}: {p}" if len(libraries) > 1 else p for p in found)
    return problems


def default_qbit_factory(settings: Settings) -> QbitClient:
    return QbitClient(settings.qbit_url, settings.qbit_username, settings.qbit_password,
                      allow_public=settings.allow_public_hosts)


def default_jellyfin_factory(settings: Settings) -> JellyfinClient:
    return JellyfinClient(settings.jellyfin_url, settings.jellyfin_api_key,
                          allow_public=settings.allow_public_hosts)


class Scanner:
    def __init__(self, env: Env, store: SettingsStore, repo: Repo,
                 qbit_factory: Callable[[Settings], QbitClient] = default_qbit_factory,
                 jellyfin_factory: Callable[[Settings], JellyfinClient] = default_jellyfin_factory):
        self.env = env
        self.store = store
        self.repo = repo
        self.qbit_factory = qbit_factory
        self.jellyfin_factory = jellyfin_factory
        self._lock = threading.Lock()
        self.retention = RetentionRunner(repo)
        self.jellyfin = RefreshCoordinator(repo, jellyfin_factory)
        # Per-scan state, reset at the start of every run.
        self._paper_cache: dict[tuple[int, str], Paper] = {}
        self._numbering_changed: set[tuple[int, str]] = set()
        self._numbered_papers: dict[str, set[str]] = {}
        self._unmatched_count: dict[str, int] = {}
        self._changed_libraries: set[int] = set()
        self._touched: dict[int, set[Path]] = {}
        self._patterns: dict[int, list[tuple[NamePattern, str]]] = {}

    @property
    def running(self) -> bool:
        return self._lock.locked()

    def try_lock(self) -> bool:
        """Hold the scan lock for another filesystem-changing operation (e.g. a manual delete)."""
        return self._lock.acquire(blocking=False)

    def unlock(self) -> None:
        self._lock.release()

    def run(self, dry_run: bool = False, settings: Settings | None = None, trigger: str | None = None,
            libraries: list[Library] | None = None) -> ScanReport:
        """Run one scan. Scheduled scans (no trigger) only log to Activity when something happened;
        triggered scans (manual/API) always log their result.

        ``libraries`` (dry runs from the library pages) scans just these, possibly unsaved, libraries.
        """
        if libraries is not None and not dry_run:
            raise ValueError("only a dry run can scan unsaved libraries")
        report = ScanReport(dry_run=dry_run, trigger=trigger)
        if not self._lock.acquire(blocking=False):
            report.error = "a scan is already running"
            log.info("scan skipped, another scan is running (%s)", trigger or "scheduled")
            if trigger and not dry_run:
                self.repo.log("info", f"{trigger}: skipped, a scan is already running")
            return report
        log.info("scan started: %s%s", trigger or "scheduled", " (dry run)" if dry_run else "")
        try:
            self._run(report, self.store.effective(settings or self.store.load()), libraries)
        except Exception as exc:  # never let the background loop die
            log.exception("scan failed")
            report.error = f"unexpected error: {type(exc).__name__}: {exc}"
            if not dry_run:
                self.repo.log("error", f"Scan failed: {report.error}")
        finally:
            report.finished = time.time()
            if not dry_run:
                self.repo.set_state(LAST_SCAN_KEY, report.as_dict())
            self._lock.release()
            log.info(
                "scan finished in %.1f s: %d torrent(s), %d linked, %d cover(s), %d moved, %d already linked, "
                "%d unmatched, %d issue error(s), %d torrent(s) deleted, %d issue(s) removed%s",
                report.finished - report.started, report.torrents, report.linked, report.covers, report.moved,
                report.already_linked, report.unmatched, report.issue_errors, report.torrents_deleted,
                report.issues_removed, f"; error: {report.error}" if report.error else "",
            )
        return report

    # ----------------------------------------------------------------------------------------
    def _libraries(self, s: Settings) -> list[Library]:
        """All libraries; the first one carries the values of ``s`` (a settings-page dry run may change them)."""
        libraries = self.repo.libraries()
        if libraries:
            libraries[0] = library_from_settings(s, libraries[0])
        return libraries

    def _run(self, report: ScanReport, s: Settings, only: list[Library] | None = None) -> None:
        all_libraries = self._libraries(s)
        if only is None:
            enabled = [lib for lib in all_libraries if lib.enabled]
        else:
            replaced = {lib.id for lib in only if lib.id}
            all_libraries = [lib for lib in all_libraries if lib.id not in replaced] + only
            enabled = list(only)
        problems = global_problems(s) + ([] if enabled else ["no library is enabled"])
        several = len(enabled) > 1
        ready: list[Library] = []
        skipped: list[str] = []
        for lib in enabled:
            found = library_problems(lib, s, self.env, all_libraries)
            if found:
                skipped.extend(f"library '{lib.name}': {p}" if several else p for p in found)
            else:
                ready.append(lib)
        if problems or not ready:
            report.error = "; ".join(problems + skipped)
            log.warning("scan not run, configuration incomplete: %s", report.error)
            if report.trigger and not report.dry_run:
                self.repo.log("error", f"{report.trigger} not run: {report.error}")
            return
        if skipped:
            report.error = "; ".join(skipped)
            log.warning("libraries skipped, configuration incomplete: %s", report.error)
            if report.trigger and not report.dry_run:
                self.repo.log("error", f"{report.trigger}: some libraries were skipped: {report.error}")

        now = time.time()
        self._paper_cache = {}
        self._numbering_changed = set()
        self._numbered_papers = {}   # download id -> publications with year + number files in it
        self._unmatched_count = {}   # download id -> unmatched files already counted in the report
        self._changed_libraries = set()
        self._touched = {}
        self._seen_issues: set[int] = set()
        self._patterns = {lib.id: self._load_patterns(lib.id) for lib in ready}

        client: QbitClient | None = None
        if s.uses_qbit:
            try:
                client = self.qbit_factory(s)
            except (QbitError, HttpError, UnsafeUrlError) as exc:
                self._qbit_unreachable(report, s, exc)
                return
        finished = set(self.repo.get_state(API_FINISHED_KEY) or {}) if client is None else set()

        if not report.dry_run:
            for lib in ready:
                dest_root = Path(lib.dest_dir)
                dest_root.mkdir(exist_ok=True)
                ensure_library_keep_file(dest_root)
                # A numbering choice made in the UI since the last scan is applied before anything else.
                self._reconcile_numbering(lib, report)

        # First look at every download, then link. A publication can be decided part-way through (a second
        # January number, or a number that cannot be a month), and its files seen earlier in this pass
        # must be read again with that decision: left alone they would stay undecided until the next
        # scan, or be linked a second time under the label the publication just stopped using.
        inspected: list[tuple[Library, Source, Download, TorrentSnapshot]] = []
        scanned: list[Library] = []
        decided: set[tuple[int, str]] = set()
        try:
            for lib in ready:
                source: Source
                if client is not None:
                    source = QbitSource(client, PathMapping(s.path_map_remote, s.path_map_local), lib.category)
                    try:
                        downloads = source.downloads()
                    except (QbitError, HttpError, UnsafeUrlError) as exc:
                        self._qbit_unreachable(report, s, exc)
                        return
                    category = lib.category
                else:
                    source = FolderSource(Path(lib.source_dir), s.settle_minutes * 60, now, finished, lib.id)
                    try:
                        downloads = source.downloads()
                    except OSError as exc:
                        message = f"source folder{f' of {lib.name}' if several else ''}: {exc}"
                        report.error = "; ".join(filter(None, [report.error, message]))
                        log.error("cannot read the source folder of %s: %s", lib.name, exc)
                        if not report.dry_run:
                            self.repo.log("error", f"{report.trigger or 'Scan'}: {message}; nothing changed there")
                        continue
                    category = FOLDER_CATEGORY
                scanned.append(lib)
                for download in downloads:
                    if download.category != category:
                        continue
                    report.torrents += 1
                    inspected.append((lib, source, download, self._inspect(lib, source, download, report)))
            decided = set(self._numbering_changed)
            if decided:
                for index, (lib, source, download, _snap) in enumerate(inspected):
                    if {(lib.id, paper) for paper in self._numbered_papers.get(download.hash, set())} & decided:
                        report.unmatched -= self._unmatched_count.get(download.hash, 0)
                        inspected[index] = (lib, source, download, self._inspect(lib, source, download, report))
        except (QbitError, HttpError) as exc:
            report.error = f"qBittorrent: {exc}"
            if not report.dry_run:
                self._qbit_status(s, str(exc))
                self.repo.log("error", f"Scan aborted while listing files, nothing changed: {exc}")
            return
        if client is not None and not report.dry_run:
            self._qbit_status(s, None)
        if not scanned:
            return
        snapshots = [snap for *_rest, snap in inspected]
        if decided and not report.dry_run:
            for lib in scanned:
                self._reconcile_numbering(lib, report, only={paper for lib_id, paper in decided if lib_id == lib.id})

        for lib, _source, download, snap in inspected:
            if not download.complete:
                continue
            for item in snap.items:
                try:
                    self._process_item(lib, item, download, s, now, report)
                except CrossDeviceError as exc:
                    report.error = str(exc)
                    self.repo.log("error", str(exc))
                    return

        if report.dry_run:
            return

        scanned_ids = {lib.id for lib in scanned}
        present = {snap.hash for snap in snapshots}
        self.repo.mark_torrents_absent(present, scanned_ids)
        for lib in scanned:
            # Only forget rows that belong to this library and mode: a switch to folder mode and back
            # must not lose the ignore choices made for qBittorrent torrents (or the other way round).
            category = lib.category if client is not None else FOLDER_CATEGORY
            self.repo.prune_unmatched(present, category, lib.id)
            self.repo.prune_ignored(present, category, lib.id)
        self._warn_new_unmatched(s, scanned)

        for lib in scanned:
            dest_root = Path(lib.dest_dir)
            for paper_dir in self._touched.get(lib.id, set()):
                try:
                    covers.update_folder_art(paper_dir, dest_root)
                except (OSError, UnsafePathError) as exc:
                    report.messages.append(f"folder art for {paper_dir.name}: {exc}")

        if client is not None:
            for lib in scanned:
                self._feed_health(client, s, lib, now, several)
            plan = plan_retention(
                now, s, {(p.library_id, p.name): p for p in self.repo.papers()}, snapshots,
                self.repo.linked_issues(scanned_ids), self.repo.kept_torrents(), {lib.id: lib for lib in scanned},
            )
            outcome = self.retention.apply(plan, s, client, {lib.id: Path(lib.dest_dir) for lib in scanned},
                                           now=now)
            report.torrents_deleted = outcome.torrents_deleted
            report.issues_removed = outcome.issues_removed
            report.retention_blocked = outcome.blocked
            report.messages.extend(outcome.errors)
            self._changed_libraries |= outcome.libraries
        else:
            # Folder mode: nothing can be deleted without qBittorrent, whatever the retention settings say.
            self._forget_api_finished(present)
            for lib in scanned:
                self._stale_warning(s, lib, now, several)

        for lib in scanned:
            self._check_unseen(lib, report)

        targets = [lib.jellyfin_target for lib in scanned if lib.id in self._changed_libraries]
        if targets and s.jellyfin_active:
            self.jellyfin.request(s, report.trigger or "scan", targets=targets)
            report.jellyfin_refreshed = True  # requested; the result is logged to Activity when Jellyfin finishes

        if report.changed or report.issue_errors:
            unmatched = f", {report.unmatched} unmatched file(s)" if report.unmatched else ""
            if report.fixed:
                unmatched += f", {report.fixed} fixed"
            if report.health_problems:
                unmatched += f", {report.health_problems} damaged in the library (see Library health)"
            if report.duplicates:
                unmatched += f", {report.duplicates} already in the library from another download"
            deleted = (f"{report.torrents_deleted} torrents deleted, {report.issues_removed} issues removed, "
                       if s.uses_qbit else "")
            self.repo.log(
                "info" if not report.issue_errors else "warning",
                f"{report.trigger or 'Scan'}: {report.linked} linked, {report.covers} covers, {report.moved} moved, "
                f"{deleted}{report.issue_errors} errors{unmatched}",
            )
        elif report.trigger:
            extra = f", {report.unmatched} unmatched file(s)" if report.unmatched else ""
            if report.health_problems:
                extra += f", {report.health_problems} damaged in the library (see Library health)"
            if report.duplicates:
                extra += f", {report.duplicates} already in the library from another download"
            if report.retention_blocked:
                extra += f"; automatic delete paused: {report.retention_blocked}"
            where = ("torrent(s) in " + ("category" if len(scanned) == 1 else "the libraries' categories")
                     if s.uses_qbit else "download(s) in the source folder" + ("s" if len(scanned) > 1 else ""))
            self.repo.log(
                "info",
                f"{report.trigger}: nothing new. {report.torrents} {where}, "
                f"{report.already_linked} issue(s) already in the library{extra}",
            )

    def _qbit_unreachable(self, report: ScanReport, s: Settings, exc: Exception) -> None:
        report.error = f"qBittorrent: {exc}"
        log.error("scan aborted, qBittorrent unreachable: %s", exc)
        if not report.dry_run:
            self._qbit_status(s, str(exc))
            self.repo.log("error", f"{report.trigger or 'Scan'} aborted, nothing changed. {report.error}")

    def _qbit_status(self, s: Settings, error: str | None) -> None:
        """Last contact, for the connection rows on the dashboard and Status page."""
        self.repo.set_state(QBIT_STATUS_KEY, {"ok": error is None, "error": (error or "")[:300],
                                              "at": time.time(), "url": s.qbit_url})

    def _warn_new_unmatched(self, s: Settings, scanned: list[Library]) -> None:
        """Warn once per file. Unmatched files are easy to miss, and they keep their torrent out of retention."""
        rows = self.repo.unmatched()
        current = {f"{row['torrent_hash']}:{row['path']}": row for row in rows}
        known = set(self.repo.get_state(UNMATCHED_KEY) or [])
        new = [row for key, row in sorted(current.items()) if key not in known]
        undecided = [row for row in new if row["reason"].startswith(UNCLEAR_REASON)]
        unrecognised = [row for row in new if not row["reason"].startswith(UNCLEAR_REASON)]

        def names(items: list) -> str:
            text = ", ".join(Path(row["path"]).name for row in items[:3])
            return text + (f" and {len(items) - 3} more" if len(items) > 3 else "")

        if len(scanned) > 1:
            where = "the libraries " + ", ".join(f"'{lib.name}'" for lib in scanned)
        elif s.uses_qbit:
            where = f"category '{scanned[0].category}'"
        else:
            where = "the source folder"
        if unrecognised:
            log.warning("%d unrecognised file(s) in %s: %s", len(unrecognised), where,
                        "; ".join(row["path"] for row in unrecognised))
            consequence = (", and a torrent containing them is never deleted automatically" if s.uses_qbit else "")
            self.repo.log(
                "warning",
                f"{len(unrecognised)} file(s) in {where} were not recognised as issues: "
                f"{names(unrecognised)}. They stay out of the library{consequence}.",
            )
        if undecided:
            log.warning("%d file(s) wait for a monthly/numbered decision: %s", len(undecided),
                        "; ".join(f"{row['path']} ({row['reason']})" for row in undecided))
            self.repo.log(
                "warning",
                f"Cannot tell whether {names(undecided)} is a monthly or a numbered issue. Choose on the Unmatched "
                "page; until then the file is not linked"
                + (" and its torrent is not deleted automatically." if s.uses_qbit else "."),
            )
        if set(current) != known:
            self.repo.set_state(UNMATCHED_KEY, sorted(current))

    def _feed_health(self, client: QbitClient, s: Settings, lib: Library, now: float, several: bool) -> None:
        """Check the RSS rule for the library's category (at most every 30 min), then the stale warning."""
        statuses = self._rss_statuses()
        previous = statuses.get(lib.category) or {}
        if now - previous.get("checked_at", 0) >= RSS_CHECK_INTERVAL:
            try:
                status = client.rss_status(lib.category)
                status["error"] = None
            except (QbitError, HttpError) as exc:
                status = {"error": str(exc)[:300]}
            statuses[lib.category] = {**status, "category": lib.category, "checked_at": now}
            self.repo.set_state(RSS_STATUS_KEY, statuses)
        self._stale_warning(s, lib, now, several)

    def _rss_statuses(self) -> dict:
        stored = self.repo.get_state(RSS_STATUS_KEY) or {}
        if "category" in stored:   # a single status, as stored before libraries existed
            return {stored["category"]: stored}
        return stored

    def _stale_warning(self, s: Settings, lib: Library, now: float, several: bool) -> None:
        """Warn once per library when its downloads have stopped arriving."""
        category = lib.category if s.uses_qbit else FOLDER_CATEGORY
        last = self.repo.last_download_at(category, lib.id)
        stale = last is not None and bool(s.stale_download_hours) and now - last > s.stale_download_hours * 3600
        warned = self.repo.get_state(STALE_WARNED_KEY)
        if not isinstance(warned, dict):   # a single value, as stored before libraries existed
            warned = {} if warned is None else {"1": warned}
        key = str(lib.id)
        if stale and last is not None and warned.get(key) != last:
            hours = int((now - last) / 3600)
            if s.uses_qbit:
                message = (f"No new download in category '{lib.category}' for {hours} h. "
                           "Check the qBittorrent RSS rule and feed.")
            else:
                folder = f"the source folder of {lib.name}" if several else "the source folder"
                message = f"No new download in {folder} for {hours} h. Check whatever fills it."
            self.repo.log("warning", message)
            warned[key] = last
            self.repo.set_state(STALE_WARNED_KEY, warned)
        elif not stale and key in warned:
            del warned[key]
            if warned:
                self.repo.set_state(STALE_WARNED_KEY, warned)
            else:
                self.repo.delete_state(STALE_WARNED_KEY)

    def _forget_api_finished(self, present: set[str]) -> None:
        marks = self.repo.get_state(API_FINISHED_KEY) or {}
        kept = {h: at for h, at in marks.items() if h in present}
        if kept != marks:
            self.repo.set_state(API_FINISHED_KEY, kept)

    def _load_patterns(self, library_id: int) -> list[tuple[NamePattern, str]]:
        patterns = []
        for row in self.repo.name_patterns(library_id) if library_id else []:
            try:
                patterns.append((compile_pattern(row["pattern"]), row["paper"]))
            except ValueError as exc:   # stored before a rule tightened: skip it, don't stop the scan
                log.warning("ignoring the stored pattern %r: %s", row["pattern"], exc)
        return patterns

    def _parse(self, lib: Library, filename: str):
        """The library's own patterns first, then the built-in rules."""
        for pattern, publication in self._patterns.get(lib.id, []):
            parsed = pattern.parse(filename, publication)
            if parsed is not None:
                return parsed
        return parse_issue_filename(filename)

    def _inspect(self, lib: Library, source: Source, torrent: Download, report: ScanReport) -> TorrentSnapshot:
        source_root = Path(lib.source_dir)
        snap = TorrentSnapshot(hash=torrent.hash, name=torrent.name, category=torrent.category,
                               complete=torrent.complete, completion_on=torrent.completion_on, size=torrent.size,
                               library_id=lib.id)
        if not torrent.complete and not torrent.problem:
            snap.problems.append("not complete")
            return snap
        unmatched: list[tuple[str, str]] = []
        recognised = []
        self._numbered_papers.pop(torrent.hash, None)
        if torrent.problem:
            # A folder download that cannot be read safely: shown on Unmatched, nothing in it is linked.
            snap.problems.append(torrent.problem)
            unmatched.append((torrent.name, torrent.problem))
        ignored = self.repo.ignored_paths(torrent.hash)
        for f in ([] if torrent.problem else source.files(torrent)):
            try:
                rel = validate_torrent_relpath(f.name)
            except UnsafePathError:
                snap.problems.append(f"unsafe file path {f.name!r}")
                unmatched.append((f.name, "unsafe file path"))
                continue
            suffix = rel.suffix.lower()
            if book_suffix(rel.name) is None:
                if suffix not in ALLOWED_EXTRA_EXTENSIONS and suffix not in lib.extras:
                    snap.problems.append(f"unexpected file {rel.name}")
                    unmatched.append((str(rel), "unexpected file type"))
                continue
            local = source.local_path(torrent, str(rel))
            if not is_within(local, source_root):
                snap.problems.append(f"{rel.name} is outside the source folder")
                unmatched.append((str(rel), f"outside source folder (resolved to {local})"))
                continue
            parsed = self._parse(lib, rel.name)
            if parsed is None:
                if str(rel) in ignored:
                    # The user chose to ignore it: not an issue, and no longer a reason to keep the torrent.
                    log.debug("ignored file in %s: %s", torrent.name, rel)
                    continue
                snap.problems.append(f"unrecognised name {rel.name}")
                unmatched.append((str(rel), UNRECOGNISED_REASON))
                log.debug("unmatched file in %s: %s", torrent.name, rel)
                continue
            recognised.append((rel, local, f.size, parsed))

        # Daily papers in the same torrent say which month it belongs to.
        ref = reference_month([p.issue_date for *_, p in recognised if p.issue_date], torrent.completion_on)
        found: dict[tuple[str, str], list[TorrentItem]] = {}
        for rel, local, size, parsed in recognised:
            if parsed.issue_date is not None:
                issue: IssueId | None = IssueId.for_day(parsed.issue_date)
                problem = ""
            elif parsed.period == MONTH:   # a user's pattern says what the number is
                issue, problem = IssueId.for_month(parsed.year, parsed.number), ""
            elif parsed.period == NUMBER:
                issue, problem = IssueId.for_number(parsed.year, parsed.number), ""
            else:
                self._numbered_papers.setdefault(torrent.hash, set()).add(parsed.paper)
                issue, problem = self._classify(lib, parsed, ref, report.dry_run)
            if issue is None:
                snap.problems.append(f"{rel.name}: {problem}")
                unmatched.append((str(rel), problem))
                log.debug("undecided file in %s: %s (%s)", torrent.name, rel, problem)
                continue
            found.setdefault((parsed.paper, issue.key), []).append(
                TorrentItem(parsed.paper, issue, local, size, lib.id, rel.suffix.lower()))
        # One file per issue: the best format by the library's priority, the others kept as fallbacks.
        for group in found.values():
            best, *others = by_priority(group, lambda item: item.ext, lib.format_priority)
            snap.items.append(replace(best, alternatives=tuple((o.ext, o.local_path) for o in others)))
        report.unmatched += len(unmatched)
        self._unmatched_count[torrent.hash] = len(unmatched)
        if not report.dry_run:
            self.repo.upsert_torrent(
                hash=torrent.hash, name=torrent.name, category=torrent.category, state=torrent.state,
                completion_on=torrent.completion_on, size=torrent.size, files_ok=snap.files_ok,
                problem=snap.problems[0] if snap.problems else None, library_id=lib.id,
            )
            self.repo.clear_unmatched_for(torrent.hash)
            for path, reason in unmatched:
                self.repo.record_unmatched(torrent.hash, path, reason)
        return snap

    def _paper_state(self, library_id: int, name: str, dry_run: bool) -> Paper:
        paper = self._paper_cache.get((library_id, name))
        if paper is None:
            found = self.repo.paper(name, library_id) if dry_run else self.repo.ensure_paper(name, library_id)
            paper = found or Paper(name, None, True, None, library_id=library_id)
            self._paper_cache[(library_id, name)] = paper
        return paper

    def _classify(self, lib: Library, parsed, ref: tuple[int, int] | None,
                  dry_run: bool) -> tuple[IssueId | None, str]:
        """Month or issue number for a ``Name.YYYY.NN`` file (rules in numbering.py)."""
        paper = self._paper_state(lib.id, parsed.paper, dry_run)
        in_january = ref is not None and ref[1] == 1 and parsed.number in JANUARY_AMBIGUOUS
        seen = self.repo.january_numbers(parsed.paper, ref[0], lib.id) if in_january and ref else set()
        decision = decide(paper.numbering, paper.numbering_detected, parsed.year, parsed.number, ref, seen)
        if in_january and ref and not dry_run:
            self.repo.add_january_number(parsed.paper, ref[0], parsed.number, lib.id)
        if decision.unclear:
            return None, f"{UNCLEAR_REASON} {decision.reason}"

        if decision.detected != paper.numbering_detected and not dry_run:
            self.repo.set_paper_detected(parsed.paper, decision.detected, decision.reason, lib.id)
            if paper.numbering_detected == MONTH and decision.detected == NUMBER and not paper.numbering:
                self.repo.log("warning", f"{paper.label} looks like a numbered magazine ({decision.reason}); "
                                         "issues already in the library are relabelled")
            log.info("numbering of %s: %s (%s)", parsed.paper, decision.detected, decision.reason)
            paper = self._paper_cache[(lib.id, parsed.paper)] = Paper(
                paper.name, paper.display_name, paper.enabled, paper.retention_days, paper.numbering,
                decision.detected, decision.reason, lib.id)
            self._numbering_changed.add((lib.id, parsed.paper))

        if decision.period == MONTH:
            if parsed.number > 12:
                return None, f"{parsed.number} is not a month, but {paper.label} is set to monthly"
            return IssueId.for_month(parsed.year, parsed.number), ""
        return IssueId.for_number(parsed.year, parsed.number), ""

    def _reconcile_numbering(self, lib: Library, report: ScanReport, only: set[str] | None = None) -> None:
        """Relabel monthly/numbered issues whose publication is now counted the other way."""
        dest_root = Path(lib.dest_dir)
        touched = self._touched.setdefault(lib.id, set())
        papers = {p.name: p for p in self.repo.papers(lib.id)}
        for issue in self.repo.linked_issues({lib.id}):
            if issue.period == DAY or (only is not None and issue.paper not in only):
                continue
            paper = papers.get(issue.paper)
            wanted = paper.effective_numbering if paper else None
            if paper is None or wanted is None or wanted == issue.period:
                continue
            current = issue.identity
            display = paper.label
            try:
                new = (IssueId.for_number(current.year, current.month) if wanted == NUMBER
                       else IssueId.for_month(current.year, current.number))
            except ValueError:
                self.repo.log("warning", f"{display} {issue.issue_label} cannot be shown as a month; it stays "
                                         "a numbered issue")
                continue
            if self.repo.issue_by_key(issue.paper, new.key, lib.id):
                self.repo.log("warning", f"Cannot relabel {display} {issue.issue_label}: {new.label} already exists")
                continue
            try:
                new_paths = issue_paths(dest_root, display, new)
                old_dir = Path(issue.dest_dir) if issue.dest_dir else None
                if old_dir and is_managed_issue_dir(old_dir, dest_root):
                    move_issue_dir(old_dir, new_paths, dest_root)
                    touched.update({old_dir.parent, new_paths.paper_dir})
                if new_paths.issue_dir.is_dir():
                    write_marker(new_paths, issue.paper, new, issue.torrent_hash or "")
                    title = render_title(lib.title_format_for(new.period), display, new, lib.language)
                    write_atomic(new_paths.opf, build_opf(display, new, title, lib.language))
            except (LinkError, UnsafePathError, OSError, ValueError) as exc:
                self.repo.log("error", f"Could not relabel {display} {issue.issue_label}: {exc}")
                continue
            self.repo.update_issue_identity(issue.id, new, str(new_paths.issue_dir))
            report.moved += 1
            self._changed_libraries.add(lib.id)
            log.info("relabelled %s %s as %s", display, issue.issue_label, new.label)
            self.repo.log("info", f"Relabelled {display} {issue.issue_label} as {new.label}")

    def _process_item(self, lib: Library, item: TorrentItem, torrent: Download, s: Settings, now: float,
                      report: ScanReport) -> None:
        issue = item.issue
        source_root, dest_root = Path(lib.source_dir), Path(lib.dest_dir)
        touched = self._touched.setdefault(lib.id, set())
        paper = self.repo.ensure_paper(item.paper, lib.id) if not report.dry_run else (
            self.repo.paper(item.paper, lib.id) or None)
        if paper is not None and not paper.enabled:
            report.skipped_disabled += 1
            return
        existing = self.repo.issue_by_key(item.paper, issue.key, lib.id)
        if (existing and existing.status == "linked" and existing.torrent_hash
                and existing.torrent_hash != torrent.hash and not self._same_file(existing, item)):
            # Already in the library from another download: that one stays linked, whatever its format.
            # (The same file seen under a new download id, e.g. after switching modes, is not a duplicate.)
            report.duplicates += 1
            return
        if existing:
            self._seen_issues.add(existing.id)
        if existing and existing.status == "excluded":
            return
        if existing and existing.status == "removed" and not s.uses_qbit:
            # Removed by automatic delete while its pack kept seeding. Folder mode cannot tell whether it
            # has expired, so it stays out of the library (Relink on the publication page still works).
            return
        keep = bool(existing and existing.keep)
        days = (paper.retention_days if paper and paper.retention_days else lib.retention_days)
        reference = torrent.completion_on or fallback_reference(
            issue, existing.linked_at if existing else None, now)
        is_linked = bool(existing and existing.status == "linked")
        if s.retention_active and not keep and not is_linked and is_expired(reference, days, now):
            report.skipped_expired += 1
            return

        display = paper.label if paper else item.paper
        watched = existing if self._watched(existing, dest_root) else None
        candidates = [(item.ext, item.local_path), *item.alternatives]
        if watched:
            # An issue in the library keeps the format it was linked in.
            candidates = [c for c in candidates if c[0] == watched.file_ext]
            if not candidates:
                report.already_linked += 1
                return
        try:
            paths = issue_paths(dest_root, display, issue, candidates[0][0])
        except UnsafePathError as exc:
            self._issue_error(lib, item, torrent, None, f"invalid name: {exc}", report)
            return

        move_from = (Path(existing.dest_dir) if existing and existing.dest_dir
                     and Path(existing.dest_dir) != paths.issue_dir
                     and is_managed_issue_dir(Path(existing.dest_dir), dest_root) else None)
        if report.dry_run:
            problem = (health.check_issue(paths, Path(candidates[0][1]), watched.cover_status == "ok")
                       if watched and move_from is None else None)
            if problem:
                report.health_problems += 1
            elif paths.book.exists():
                report.already_linked += 1
            else:
                report.would_link += 1
            return

        moved = False
        fixing: str | None = None
        source, fmt, rejected = None, PDF, []
        for suffix, local in candidates:
            try:
                fmt = check_source(Path(local), source_root)
            except (FormatError, UnsafePathError, OSError) as exc:
                rejected.append(f"{Path(local).name}: {exc}")
                continue
            source = Path(local)
            if suffix != paths.book.suffix:
                paths = replace(paths, book=paths.book.with_name(paths.book.stem + suffix))
            break
        if source is None:
            self._issue_error(lib, item, torrent, None, "; ".join(rejected), report)
            return
        if rejected:
            self.repo.log("warning", f"{display} {issue.label}: linked {source.name} instead of an unreadable "
                                     f"file ({'; '.join(rejected)})")
        try:
            if move_from is not None:
                move_issue_dir(move_from, paths, dest_root)
                touched.add(move_from.parent)
                report.moved += 1
                moved = True
            if watched:
                problem = health.check_issue(paths, source, watched.cover_status == "ok")
                if problem and not watched.fix_requested:
                    self._health_problem(lib, watched, display, problem, report)
                    return
                if problem:
                    fixing = problem
                elif watched.problem:
                    self.repo.clear_issue_problem(watched.id)
                    self.repo.log("info", f"Library health: {display} {issue.label} is whole again")
            if fixing == health.COPY:
                replace_with_link(source, paths, dest_root)
                created = False
            else:
                created = link_book(source, paths, dest_root)
            write_marker(paths, item.paper, issue, torrent.hash)
            title = render_title(lib.title_format_for(issue.period), display, issue, lib.language)
            write_atomic(paths.opf, build_opf(display, issue, title, lib.language))
        except CrossDeviceError:
            raise
        except (LinkError, UnsafePathError, OSError, ValueError) as exc:
            self._issue_error(lib, item, torrent, None, str(exc), report)
            return

        if fixing:
            report.fixed += 1
            self._changed_libraries.add(lib.id)
            self.repo.log("info", f"Library health: {display} {issue.label} fixed ({health.FIXED[fixing]})")
        elif created:
            report.linked += 1
            log.debug("linked %s -> %s", source, paths.book)
            self.repo.log("info", f"Linked {display} {issue.label}")
        else:
            report.already_linked += 1
        touched.add(paths.paper_dir)

        covers_before = report.covers
        cover_status, cover_attempted = self._ensure_cover(lib, paths, fmt, existing, now, report, display,
                                                           issue.label)
        if created or moved or report.covers > covers_before:
            self._changed_libraries.add(lib.id)
        self.repo.save_issue(
            paper=item.paper, issue=issue, torrent_hash=torrent.hash, source_path=item.local_path,
            dest_dir=str(paths.issue_dir), status="linked", cover_status=cover_status,
            cover_attempted=cover_attempted, linked=True, library_id=lib.id, file_ext=paths.book.suffix,
        )
        if fixing and watched:
            self.repo.clear_issue_problem(watched.id)

    @staticmethod
    def _same_file(existing: IssueRow, item: TorrentItem) -> bool:
        if not existing.dest_dir:
            return False
        linked = health.paths_of(Path(existing.dest_dir), existing.file_ext).book
        for _suffix, local in [(item.ext, item.local_path), *item.alternatives]:
            try:
                if os.path.samefile(local, linked):
                    return True
            except OSError:
                continue
        return False

    @staticmethod
    def _watched(existing: IssueRow | None, dest_root: Path) -> bool:
        """Is this issue linked into the library as it is now? After a destination change, issues are
        linked into the new folder as before instead of being reported missing."""
        return bool(existing and existing.status == "linked" and existing.dest_dir
                    and is_within(existing.dest_dir, str(dest_root)))

    def _health_problem(self, lib: Library, issue: IssueRow, display: str, problem: str, report: ScanReport,
                        gone: bool = False) -> None:
        report.health_problems += 1
        if self.repo.set_issue_problem(issue.id, problem):
            detail = "; its download is gone, so it can only be removed" if gone else "; press Fix on Library health"
            self.repo.log("warning", f"Library health: {lib.name}: {display} {issue.issue_label}: "
                                     f"{health.PROBLEMS[problem]}{detail}")

    def _check_unseen(self, lib: Library, report: ScanReport) -> None:
        """Linked issues no download provided in this scan: only the folder and the PDF can be checked."""
        dest_root = Path(lib.dest_dir)
        labels = {p.name: p.label for p in self.repo.papers(lib.id)}
        for issue in self.repo.linked_issues({lib.id}):
            if issue.id in self._seen_issues or not self._watched(issue, dest_root) or not issue.dest_dir:
                continue
            display = labels.get(issue.paper, issue.paper)
            problem = health.check_issue(health.paths_of(Path(issue.dest_dir), issue.file_ext), None, False)
            if problem:
                self._health_problem(lib, issue, display, problem, report, gone=True)
                if issue.fix_requested:
                    self.repo.set_fix_requested([issue.id], False)
                    self.repo.log("warning", f"Library health: cannot fix {display} {issue.issue_label}: "
                                             "its download is gone")
            elif issue.problem:
                self.repo.clear_issue_problem(issue.id)
                self.repo.log("info", f"Library health: {display} {issue.issue_label} is whole again")

    def _ensure_cover(self, lib: Library, paths, fmt: str, existing, now: float, report: ScanReport,
                      display: str, iso: str) -> tuple[str | None, bool]:
        if find_cover(paths.issue_dir) is not None:
            return "ok", False
        if fmt == CBR:
            return "none", False   # a RAR archive is not opened; Jellyfin may find a cover itself
        if (existing and existing.cover_status == "failed" and existing.cover_attempted_at
                and now - existing.cover_attempted_at < COVER_RETRY_SECONDS):
            return None, False
        if fmt in (CBZ, EPUB):
            try:
                found = extract_cover(paths.book, fmt)
                if found is None:
                    return "none", True
                data, suffix = found
                write_atomic(paths.issue_dir / f"cover{suffix}", data)
            except (FormatError, OSError) as exc:
                report.cover_failures += 1
                self.repo.log("warning", f"Cover for {display} {iso} failed: {exc}")
                return "failed", True
            report.covers += 1
            return "ok", True
        if not covers.pdftoppm_available():
            report.cover_failures += 1
            if "pdftoppm missing" not in report.messages:
                report.messages.append("pdftoppm missing")
                self.repo.log("error", "Covers cannot be rendered: pdftoppm (poppler-utils) is not installed")
            return "failed", True
        try:
            covers.render_cover(paths.book, paths.cover, lib.cover_width, self.env.cover_memory_mb)
        except (covers.CoverError, OSError) as exc:
            report.cover_failures += 1
            self.repo.log("warning", f"Cover for {display} {iso} failed: {exc}")
            return "failed", True
        report.covers += 1
        return "ok", True

    def _issue_error(self, lib: Library, item: TorrentItem, torrent: Download, dest_dir: str | None, message: str,
                     report: ScanReport) -> None:
        report.issue_errors += 1
        report.messages.append(f"{item.paper} {item.issue.label}: {message}")
        if report.dry_run:
            return
        existing = self.repo.issue_by_key(item.paper, item.issue.key, lib.id)
        status = existing.status if existing and existing.status == "linked" else "error"
        self.repo.save_issue(paper=item.paper, issue=item.issue, torrent_hash=torrent.hash,
                             source_path=item.local_path, dest_dir=dest_dir, status=status, error=message,
                             library_id=lib.id)
        self.repo.log("error", f"{item.paper} {item.issue.label}: {message}")

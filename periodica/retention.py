"""Automatic delete: plan what has expired, queue it with a grace period, then execute safely.

Rules:
- Media issues expire individually (per-paper override or global days).
- A torrent (and its files, via qBittorrent) is deleted only when *every* newspaper in it has
  expired, nothing in it is kept, it is complete, and its file list contains nothing unexpected.
- Nothing runs until retention is armed (after the user has seen a dry-run preview).
- More due torrent deletions than the per-run cap blocks the run until the user approves.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .issues import IssueId
from .libraries import Library
from .linker import remove_issue_dir
from .paths import UnsafePathError
from .qbittorrent import QbitError
from .repo import IssueRow, Paper, Repo
from .settings import Settings

ISSUE_CAP_MULTIPLIER = 25
APPROVAL_KEY = "retention_approval"
BLOCKED_KEY = "retention_blocked"
PREVIEW_KEY = "retention_preview"


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TorrentItem:
    paper: str
    issue: IssueId
    local_path: str
    size: int
    library_id: int = 1
    ext: str = ".pdf"
    # The same issue in other formats in this download, best first: (suffix, local path).
    alternatives: tuple[tuple[str, str], ...] = ()


@dataclass
class TorrentSnapshot:
    hash: str
    name: str
    category: str
    complete: bool
    completion_on: float | None
    size: int
    items: list[TorrentItem] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    library_id: int = 1

    @property
    def files_ok(self) -> bool:
        return not self.problems and bool(self.items)


@dataclass(frozen=True)
class PlannedTorrent:
    hash: str
    name: str
    size: int
    papers: tuple[str, ...]
    issue_ids: tuple[int, ...]
    category: str = ""
    library_id: int = 1


@dataclass(frozen=True)
class PlannedIssue:
    issue_id: int
    label: str
    dest_dir: str | None
    library_id: int = 1


@dataclass
class RetentionPlan:
    torrents: list[PlannedTorrent] = field(default_factory=list)
    issues: list[PlannedIssue] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    # (torrent name, timestamp when every newspaper in it has expired) for torrents not yet expired
    upcoming: list[tuple[str, float]] = field(default_factory=list)

    @property
    def keys(self) -> set[tuple[str, str]]:
        return {("torrent", t.hash) for t in self.torrents} | {("issue", str(i.issue_id)) for i in self.issues}

    def summary(self) -> dict:
        return {
            "torrents": [{"hash": t.hash, "name": t.name, "size": t.size, "papers": list(t.papers)}
                         for t in self.torrents],
            "issues": [{"id": i.issue_id, "label": i.label} for i in self.issues],
            "skipped": [{"label": label, "reason": reason} for label, reason in self.skipped],
            "total_size": sum(t.size for t in self.torrents),
            "upcoming": [{"name": name, "expires_at": ts} for name, ts in sorted(self.upcoming, key=lambda u: u[1])],
        }


def midnight_ts(d: date) -> float:
    return datetime(d.year, d.month, d.day).timestamp()


def fallback_reference(issue: IssueId, linked_at: float | None, now: float) -> float:
    """Age reference when the download's completion time is unknown.

    A daily issue is as old as its date. A monthly or numbered issue has only a nominal date
    (the 1st, or 1 January), which would make it look weeks old on arrival, so it counts from when
    it was linked instead.
    """
    if issue.is_daily:
        return midnight_ts(issue.nominal_date)
    return linked_at or now


def is_expired(reference_ts: float, days: int, now: float) -> bool:
    return now - reference_ts >= days * 86400


def plan_retention(
    now: float,
    settings: Settings,
    papers: dict[tuple[int, str], Paper],
    snapshots: list[TorrentSnapshot],
    linked_issues: list[IssueRow],
    kept_torrents: set[str],
    libraries: dict[int, Library],
) -> RetentionPlan:
    """Only snapshots and issues of the given (enabled, scanned) libraries are considered."""
    plan = RetentionPlan()

    def days_for(library_id: int, paper: str) -> int:
        row = papers.get((library_id, paper))
        if row and row.retention_days:
            return row.retention_days
        library = libraries.get(library_id)
        return library.retention_days if library else settings.retention_days

    issues_by_key = {(i.library_id, i.paper, i.issue_key): i for i in linked_issues}
    kept_issue_keys = {(i.library_id, i.paper, i.issue_key) for i in linked_issues if i.keep}

    def key(item: TorrentItem) -> tuple[int, str, str]:
        return item.library_id, item.paper, item.issue.key

    def reference(item: TorrentItem, snap: TorrentSnapshot) -> float:
        if snap.completion_on:
            return snap.completion_on
        row = issues_by_key.get(key(item))
        return fallback_reference(item.issue, row.linked_at if row else None, now)
    snapshot_by_hash = {s.hash: s for s in snapshots}
    planned_issue_ids: set[int] = set()

    for snap in snapshots:
        library = libraries.get(snap.library_id)
        if library is None or not library.category or snap.category != library.category:
            continue  # defence in depth; the scanner never passes these
        if not snap.items:
            continue
        refs = [(item, reference(item, snap)) for item in snap.items]
        if not all(is_expired(ref, days_for(item.library_id, item.paper), now) for item, ref in refs):
            plan.upcoming.append((snap.name, max(ref + days_for(item.library_id, item.paper) * 86400
                                                 for item, ref in refs)))
            continue
        if snap.hash in kept_torrents or any(key(i) in kept_issue_keys for i in snap.items):
            plan.skipped.append((snap.name, "kept"))
            continue
        if not snap.complete:
            plan.skipped.append((snap.name, "torrent is not complete"))
            continue
        if not snap.files_ok:
            plan.skipped.append((snap.name, f"unexpected content: {snap.problems[0]}"))
            continue
        issue_ids = tuple(issues_by_key[key(item)].id for item in snap.items if key(item) in issues_by_key)
        planned_issue_ids.update(issue_ids)
        plan.torrents.append(PlannedTorrent(
            hash=snap.hash, name=snap.name, size=snap.size,
            papers=tuple(sorted({i.paper for i in snap.items})), issue_ids=issue_ids,
            category=snap.category, library_id=snap.library_id,
        ))

    for issue in linked_issues:
        if issue.id in planned_issue_ids or issue.keep or issue.library_id not in libraries:
            continue
        issue_snap = snapshot_by_hash.get(issue.torrent_hash or "")
        if issue_snap and issue_snap.completion_on:
            ref = issue_snap.completion_on
        else:
            ref = fallback_reference(issue.identity, issue.linked_at, now)
        if is_expired(ref, days_for(issue.library_id, issue.paper), now):
            paper = papers.get((issue.library_id, issue.paper))
            label = f"{paper.label if paper else issue.paper} {issue.issue_label}"
            plan.issues.append(PlannedIssue(issue.id, label, issue.dest_dir, issue.library_id))
    return plan


@dataclass
class RetentionOutcome:
    armed: bool = False
    queued: int = 0
    torrents_deleted: int = 0
    issues_removed: int = 0
    blocked: str | None = None
    errors: list[str] = field(default_factory=list)
    libraries: set[int] = field(default_factory=set)   # libraries whose Jellyfin library changed

    @property
    def changed(self) -> bool:
        return bool(self.torrents_deleted or self.issues_removed)


class RetentionRunner:
    def __init__(self, repo: Repo):
        self.repo = repo

    def apply(self, plan: RetentionPlan, settings: Settings, client, dest_roots: dict[int, Path],
              now: float | None = None) -> RetentionOutcome:
        now = now if now is not None else time.time()
        outcome = RetentionOutcome(armed=settings.retention_armed)

        if not settings.retention_enabled:
            self.repo.cancel_pending("automatic delete is disabled")
            self.repo.delete_state(PREVIEW_KEY)
            return outcome

        self.repo.set_state(PREVIEW_KEY, {"at": now, **plan.summary()})
        if not settings.retention_armed:
            self.repo.cancel_pending("automatic delete is not armed")
            return outcome

        # Queue new items, cancel pending ones that no longer qualify (e.g. retention raised, kept).
        cancelled = self.repo.cancel_pending("no longer expired or kept", keep_keys=plan.keys)
        if cancelled:
            self.repo.log("info", f"Cancelled {cancelled} pending deletion(s) that no longer qualify")
        due_at = now + settings.grace_hours * 3600
        for t in plan.torrents:
            self.repo.ensure_pending("torrent", t.hash, f"{t.name} ({', '.join(t.papers)})", t.size, due_at)
        for i in plan.issues:
            self.repo.ensure_pending("issue", str(i.issue_id), i.label, 0, due_at)

        pending = self.repo.pending_deletions()
        outcome.queued = len(pending)
        due = [d for d in pending if d["due_at"] <= now]
        due_torrents = [d for d in due if d["kind"] == "torrent"]
        due_issues = [d for d in due if d["kind"] == "issue"]

        torrent_cap = settings.max_torrent_deletions_per_run
        issue_cap = torrent_cap * ISSUE_CAP_MULTIPLIER
        if len(due_torrents) > torrent_cap or len(due_issues) > issue_cap:
            approval = self.repo.get_state(APPROVAL_KEY) or {}
            approved = (
                approval.get("expires", 0) > now
                and approval.get("torrents", -1) >= len(due_torrents)
                and approval.get("issues", -1) >= len(due_issues)
            )
            if not approved:
                log.warning("automatic delete paused: %d torrent(s)/%d issue(s) due, cap is %d/%d",
                            len(due_torrents), len(due_issues), torrent_cap, issue_cap)
                outcome.blocked = (
                    f"{len(due_torrents)} torrent(s) and {len(due_issues)} issue(s) are due, which exceeds the "
                    f"safety limit ({torrent_cap} torrents / {issue_cap} issues per run). Review and approve on "
                    f"the Deletions page."
                )
                self.repo.set_state(BLOCKED_KEY, {"at": now, "torrents": len(due_torrents),
                                                  "issues": len(due_issues), "message": outcome.blocked})
                self.repo.log("warning", outcome.blocked)
                return outcome
            self.repo.delete_state(APPROVAL_KEY)
        self.repo.delete_state(BLOCKED_KEY)

        torrents_by_hash = {t.hash: t for t in plan.torrents}
        for d in due_torrents:
            planned = torrents_by_hash.get(d["ref"])
            if planned is None:
                continue
            log.info("retention: deleting torrent %s (%s), expired papers: %s",
                     planned.hash, planned.name, ", ".join(planned.papers))
            try:
                client.delete_with_files(planned.hash, planned.category)
            except QbitError as exc:
                self.repo.note_deletion(d["id"], f"qBittorrent: {exc}")
                outcome.errors.append(f"{planned.name}: {exc}")
                self.repo.log("error", f"Could not delete torrent {planned.name}: {exc}")
                continue
            removed = self.remove_issues(planned.issue_ids, dest_roots, outcome)
            outcome.torrents_deleted += 1
            outcome.libraries.add(planned.library_id)
            self.repo.finish_deletion(d["id"], "done", f"torrent and files deleted; {removed} issue(s) removed")
            self.repo.log("info", f"Deleted torrent {planned.name} with its files and {removed} library issue(s)")

        planned_issue_ids = {str(i.issue_id) for i in plan.issues}
        for d in due_issues:
            if d["ref"] not in planned_issue_ids:
                continue
            removed = self.remove_issues((int(d["ref"]),), dest_roots, outcome)
            if removed:
                self.repo.finish_deletion(d["id"], "done", "removed from library")
                self.repo.log("info", f"Removed {d['label']} from the library (expired)")
        return outcome

    def remove_issues(self, issue_ids, dest_roots: dict[int, Path], outcome: RetentionOutcome) -> int:
        removed = 0
        for issue_id in issue_ids:
            issue = self.repo.issue(issue_id)
            if issue is None or issue.status != "linked":
                continue
            dest_root = dest_roots.get(issue.library_id)
            if dest_root is None:
                continue   # its library is disabled or gone: never touch it
            try:
                if issue.dest_dir:
                    remove_issue_dir(Path(issue.dest_dir), dest_root)
            except (UnsafePathError, OSError) as exc:
                outcome.errors.append(f"{issue.paper} {issue.issue_date}: {exc}")
                self.repo.set_issue_status(issue.id, "linked", f"removal failed: {exc}")
                self.repo.log("error", f"Could not remove {issue.paper} {issue.issue_date}: {exc}")
                continue
            self.repo.set_issue_status(issue.id, "removed")
            outcome.issues_removed += 1
            outcome.libraries.add(issue.library_id)
            removed += 1
        return removed

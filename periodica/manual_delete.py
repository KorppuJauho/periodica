"""Manually delete one torrent (with its files) and its library issues, after explicit confirmation.

Uses the same safety rails as automatic delete: only torrents in the configured category that the
last scan saw as complete with nothing but recognised newspaper files. qBittorrent deletes the files;
the category and completion are re-checked by hash immediately before the delete call.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .netsafe import HttpError, UnsafeUrlError
from .qbittorrent import QbitError, validate_hash
from .retention import RetentionOutcome, RetentionRunner

log = logging.getLogger(__name__)


class ManualDeleteError(Exception):
    pass


NEEDS_QBIT = ("deleting needs qBittorrent; Periodica is watching the source folder "
              "(Settings → Download client)")


def check_deletable(repo, settings, torrent_hash: str) -> dict:
    """Return the torrent row if it may be deleted, else raise ManualDeleteError with the reason."""
    if not settings.uses_qbit:
        raise ManualDeleteError(NEEDS_QBIT)
    try:
        validate_hash(torrent_hash)
    except QbitError as exc:
        raise ManualDeleteError(str(exc)) from exc
    row = repo.torrent(torrent_hash)
    if row is None or not row["present"]:
        raise ManualDeleteError("torrent is not in the category anymore (run a scan to refresh the list)")
    library = repo.library(row["library_id"])
    if library is None or not library.enabled:
        raise ManualDeleteError("the torrent's library is disabled or gone")
    if not library.category or row["category"] != library.category:
        raise ManualDeleteError(f"torrent is not in category '{library.category}' of library '{library.name}'")
    if row["keep"]:
        raise ManualDeleteError("torrent is marked as kept; unkeep it under System, Status first")
    if not row["files_ok"]:
        reason = row["problem"] or "unknown"
        raise ManualDeleteError(
            f"not deleted by Periodica because of its content or state ({reason}). "
            "Check it in qBittorrent and delete it there if you are sure."
        )
    return row


def delete_torrent_now(scanner, repo, settings, qbit_factory, jellyfin_factory, torrent_hash: str,
                       username: str) -> str:
    row = check_deletable(repo, settings, torrent_hash)
    library = repo.library(row["library_id"])
    log.info("manual delete of %s (%s) requested by %s", torrent_hash, row["name"], username)
    if not scanner.try_lock():
        raise ManualDeleteError("a scan is running right now; try again in a moment")
    try:
        try:
            qbit_factory(settings).delete_with_files(torrent_hash, library.category)
        except (QbitError, HttpError, UnsafeUrlError) as exc:
            repo.log("error", f"Manual delete of {row['name']} by {username} failed: {exc}")
            raise ManualDeleteError(f"qBittorrent: {exc}") from exc

        issue_ids = [i.id for i in repo.issues_for_torrent(torrent_hash) if i.status == "linked"]
        outcome = RetentionOutcome()
        removed = RetentionRunner(repo).remove_issues(issue_ids, {library.id: Path(library.dest_dir)}, outcome)
        repo.mark_torrent_gone(torrent_hash)
        repo.cancel_pending_for("torrent", torrent_hash, f"deleted manually by {username}")
        detail = f"deleted manually by {username}; torrent and files removed, {removed} library issue(s) removed"
        if outcome.errors:
            detail += f"; {len(outcome.errors)} library folder(s) could not be removed"
        repo.record_deletion("torrent", torrent_hash, row["name"], row["size"], "done", detail)
        repo.log("warning", f"{username} deleted torrent {row['name']} with its files and {removed} library issue(s)")
    finally:
        scanner.unlock()

    scanner.jellyfin.request(settings, f"manual delete by {username}", targets=[library.jellyfin_target])
    message = f"Deleted {row['name']} and its files; removed {removed} issue(s) from the library."
    if outcome.errors:
        message += " Some library folders could not be removed; see Activity."
    return message

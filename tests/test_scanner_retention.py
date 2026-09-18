from __future__ import annotations

import os
import time
from datetime import date, timedelta

import pytest

from periodica.retention import BLOCKED_KEY, PREVIEW_KEY

from .helpers import write_torrent_files

DAY = 86400
PAPERS = ("Chronicle", "Côte-Nord.Gazette", "Evening.Post")


def add_pack(world, n, day: date, *, category="news", papers=PAPERS, age_days: float = 0.0,
             extra_files=(), state="stalledUP", progress=1.0, write=True, save="/downloads/news"):
    folder = f"Daily Newspapers {day:%d %m %Y}"
    files = []
    for paper in papers:
        stem = f"{paper}.{day:%Y.%m.%d}"
        files += [f"{folder}/{stem}/{stem}.nfo", f"{folder}/{stem}/{stem}.pdf"]
    files += list(extra_files)
    if write:
        write_torrent_files(world.source, files)
    return world.qbit.add(n, folder, category, save, files, state=state, progress=progress,
                          completion_on=time.time() - age_days * DAY, local_save_path=world.source)


def library_pdfs(world):
    return sorted(p.name for p in world.dest.rglob("*.pdf"))


# --- linking ------------------------------------------------------------------------------------
def test_scan_links_pack_with_metadata(world):
    add_pack(world, 1, date(2026, 9, 15))
    report = world.scanner.run()
    assert report.error is None, report.error
    assert report.linked == 3
    assert library_pdfs(world) == [
        "Chronicle 2026-09-15.pdf", "Côte-Nord Gazette 2026-09-15.pdf", "Evening Post 2026-09-15.pdf",
    ]
    issue_dir = world.dest / "Evening Post" / "Evening Post 2026-09-15"
    src = world.source / "Daily Newspapers 15 09 2026" / "Evening.Post.2026.09.15" / \
        "Evening.Post.2026.09.15.pdf"
    assert os.path.samefile(src, issue_dir / "Evening Post 2026-09-15.pdf")
    opf = (issue_dir / "metadata.opf").read_text("utf-8")
    assert "<dc:title>Evening Post 2026-09-15</dc:title>" in opf
    assert (issue_dir / ".periodica.json").is_file()
    assert (world.dest / ".periodica-library").is_file()
    assert not list(world.dest.rglob("*.nfo"))

    again = world.scanner.run()
    assert again.linked == 0 and again.already_linked == 3


def test_triggered_scan_always_logs_result_scheduled_does_not(world):
    add_pack(world, 1, date(2026, 9, 15))
    world.scanner.run()
    before = len(world.repo.activity(1000))
    world.scanner.run()  # scheduled, nothing new -> quiet
    assert len(world.repo.activity(1000)) == before
    world.scanner.run(trigger="Manual scan by jauho")
    latest = world.repo.activity(1)[0]["message"]
    assert latest.startswith("Manual scan by jauho: nothing new")
    assert "1 torrent(s) in category" in latest and "3 issue(s) already in the library" in latest


def test_triggered_scan_logs_config_problems(world):
    world.settings(qbit_category="")
    world.scanner.run(trigger="API scan")
    assert world.repo.activity(1)[0]["message"].startswith("API scan not run:")


def test_rss_status_is_recorded_without_other_preferences(world):
    import json as _json

    from periodica.scanner import RSS_STATUS_KEY

    world.scanner.run()
    status = world.repo.get_state(RSS_STATUS_KEY)["news"]   # one status per library category
    assert status["rules"] == [{"name": "News", "feeds": 1}] and status["auto_downloading"] is True
    assert "must-not-be-stored" not in _json.dumps(status)


def test_stale_download_warning_logged_once_and_cleared(world):
    from periodica.scanner import STALE_WARNED_KEY

    add_pack(world, 1, date(2026, 9, 15), age_days=2)
    world.scanner.run()
    world.scanner.run()
    warnings = [a for a in world.repo.activity(100) if "No new download" in a["message"]]
    assert len(warnings) == 1
    add_pack(world, 2, date(2026, 9, 16), age_days=0)
    world.scanner.run()
    assert world.repo.get_state(STALE_WARNED_KEY) is None


def test_incomplete_torrent_is_not_linked(world):
    add_pack(world, 1, date(2026, 9, 15), state="downloading", progress=0.4)
    report = world.scanner.run()
    assert report.error is None
    assert library_pdfs(world) == []


def test_dry_run_changes_nothing(world):
    add_pack(world, 1, date(2026, 9, 15))
    report = world.scanner.run(dry_run=True)
    assert report.would_link == 3
    assert library_pdfs(world) == []
    assert world.repo.papers() == []


def test_disabled_paper_is_skipped(world):
    add_pack(world, 1, date(2026, 9, 15))
    world.scanner.run()
    world.repo.update_paper("Chronicle", None, False, None)
    add_pack(world, 2, date(2026, 9, 16))
    report = world.scanner.run()
    assert report.skipped_disabled == 2
    assert "Chronicle 2026-09-16.pdf" not in library_pdfs(world)


def test_display_name_moves_existing_issues(world):
    add_pack(world, 1, date(2026, 9, 15), papers=("Evening.Post",))
    world.scanner.run()
    world.repo.update_paper("Evening Post", "EP", True, None)
    report = world.scanner.run()
    assert report.moved == 1
    assert library_pdfs(world) == ["EP 2026-09-15.pdf"]
    assert not (world.dest / "Evening Post").exists()


def test_unmatched_and_unsafe_files_are_reported_not_linked(world):
    add_pack(world, 1, date(2026, 9, 15), papers=("Chronicle",),
             extra_files=["Daily Newspapers 15 09 2026/Weird name.pdf"])
    world.qbit.files[next(iter(world.qbit.torrents))].append({"name": "../../etc/evil.pdf", "size": 1, "progress": 1})
    report = world.scanner.run()
    assert report.linked == 1
    reasons = {u["path"]: u["reason"] for u in world.repo.unmatched()}
    assert "not recognised" in reasons["Daily Newspapers 15 09 2026/Weird name.pdf"]
    assert reasons["../../etc/evil.pdf"] == "unsafe file path"


def test_scans_are_visible_in_the_technical_log(world, caplog):
    add_pack(world, 1, date(2026, 9, 15), papers=("Chronicle",),
             extra_files=["Daily Newspapers 15 09 2026/Special edition.pdf"])
    with caplog.at_level("INFO", logger="periodica"):
        world.scanner.run(trigger="API scan")
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("scan started: API scan") for m in messages)
    assert any("scan finished in" in m and "1 linked" in m for m in messages)
    assert any("unrecognised file(s)" in m and "Special edition.pdf" in m for m in messages)


def test_unmatched_files_are_warned_about_once(world):
    add_pack(world, 1, date(2026, 9, 15), papers=("Chronicle",),
             extra_files=["Daily Newspapers 15 09 2026/Special edition.pdf"])
    report = world.scanner.run(trigger="Scan")
    assert report.linked == 1 and report.unmatched == 1

    warnings = [a["message"] for a in world.repo.activity(50, level="warning")]
    assert any("Special edition.pdf" in m and "never deleted automatically" in m for m in warnings)
    summary = [a["message"] for a in world.repo.activity(50) if "1 linked" in a["message"]]
    assert summary and "1 unmatched file(s)" in summary[0]

    # A second scan finds the same file: the count stays visible, but it is not warned about again.
    world.scanner.run(trigger="Scan")
    repeats = [a for a in world.repo.activity(50, level="warning") if "Special edition.pdf" in a["message"]]
    assert len(repeats) == 1

    # A new unrecognised file is worth a new warning.
    add_pack(world, 2, date(2026, 9, 16), papers=("Chronicle",),
             extra_files=["Daily Newspapers 16 09 2026/Quarterly.2026.pdf"])
    world.scanner.run(trigger="Scan")
    warnings = [a["message"] for a in world.repo.activity(50, level="warning")]
    assert any("Quarterly.2026.pdf" in m for m in warnings)


def test_files_outside_source_dir_are_refused(world):
    h = add_pack(world, 1, date(2026, 9, 15), save="/downloads/elsewhere")
    report = world.scanner.run()
    assert report.linked == 0
    assert any("outside source folder" in u["reason"] for u in world.repo.unmatched())
    assert world.repo.torrents()[0]["hash"] == h


def test_fake_pdf_is_refused(world):
    add_pack(world, 1, date(2026, 9, 15), papers=("Chronicle",))
    pdf = next(world.source.rglob("*.pdf"))
    pdf.write_bytes(b"<html>not a pdf</html>")
    report = world.scanner.run()
    assert report.linked == 0 and report.issue_errors == 1


# --- category isolation -------------------------------------------------------------------------
def test_other_categories_are_never_touched(world):
    add_pack(world, 1, date(2026, 9, 15), category="movies")
    add_pack(world, 2, date(2026, 9, 16), category="")
    report = world.scanner.run()
    assert report.torrents == 0
    assert library_pdfs(world) == []


def test_wrong_category_from_api_is_rejected(world):
    add_pack(world, 1, date(2026, 9, 15), category="movies", age_days=100)
    world.qbit.leak_other_categories = True  # simulate an API/filter quirk
    world.settings(retention_enabled=True, retention_armed=True, retention_days=1, grace_hours=0)
    report = world.scanner.run()
    assert report.torrents == 0
    assert library_pdfs(world) == []
    assert world.qbit.deleted == []


def test_empty_category_keeps_scanner_idle(world):
    add_pack(world, 1, date(2026, 9, 15), category="")
    world.settings(qbit_category="")
    report = world.scanner.run()
    assert "category is not set" in report.error
    assert not [r for r in world.qbit.requests if r[1] == "/api/v2/torrents/info"]


def test_qbittorrent_unreachable_changes_nothing(world):
    add_pack(world, 1, date(2026, 9, 15), age_days=100)
    world.scanner.run()
    world.settings(retention_enabled=True, retention_armed=True, retention_days=1, grace_hours=0)
    world.qbit.fail = True
    report = world.scanner.run()
    assert "connection refused" in report.error
    assert len(library_pdfs(world)) == 3


# --- retention ----------------------------------------------------------------------------------
def test_retention_not_armed_only_previews(world):
    add_pack(world, 1, date(2026, 9, 1), age_days=10)
    world.settings(retention_enabled=True, retention_days=7, grace_hours=0)
    world.scanner.run()
    preview = world.repo.get_state(PREVIEW_KEY)
    assert len(preview["torrents"]) == 1
    assert world.qbit.deleted == []
    assert world.repo.pending_deletions() == []


def test_preview_lists_upcoming_expiry(world):
    add_pack(world, 1, date(2026, 9, 15), age_days=0.5)
    world.settings(retention_enabled=True, retention_days=1)
    world.scanner.run()
    preview = world.repo.get_state(PREVIEW_KEY)
    assert preview["torrents"] == []
    assert len(preview["upcoming"]) == 1
    expires_in = preview["upcoming"][0]["expires_at"] - time.time()
    assert 0.4 * DAY < expires_in < 0.6 * DAY


def test_expired_issues_are_not_linked_when_retention_on(world):
    add_pack(world, 1, date(2026, 9, 1), age_days=10)
    world.settings(retention_enabled=True, retention_days=7)
    report = world.scanner.run()
    assert report.skipped_expired == 3
    assert library_pdfs(world) == []


def test_retention_grace_then_delete_torrent_and_files(world):
    add_pack(world, 1, date(2026, 9, 10), age_days=3)
    world.scanner.run()
    assert len(library_pdfs(world)) == 3
    world.qbit.torrents[next(iter(world.qbit.torrents))]["completion_on"] = time.time() - 8 * DAY
    world.settings(retention_enabled=True, retention_armed=True, retention_days=7, grace_hours=24)

    world.scanner.run()
    pending = world.repo.pending_deletions()
    assert [p["kind"] for p in pending] == ["torrent"]
    assert world.qbit.deleted == []  # still in grace period

    world.repo.make_due_now(pending[0]["id"])
    report = world.scanner.run()
    assert report.torrents_deleted == 1 and report.issues_removed == 3
    assert world.qbit.deleted == [(pending[0]["ref"], True)]
    assert library_pdfs(world) == []
    assert not list(world.source.rglob("*.pdf"))  # fake qBittorrent removed its data
    assert not (world.dest / "Chronicle").exists()


def test_pack_waits_for_longest_paper_override(world):
    h = add_pack(world, 1, date(2026, 9, 10), age_days=1)
    world.scanner.run()
    world.repo.update_paper("Evening Post", None, True, 30)
    world.qbit.torrents[h]["completion_on"] = time.time() - 10 * DAY
    world.settings(retention_enabled=True, retention_armed=True, retention_days=7, grace_hours=0)
    report = world.scanner.run()
    assert world.qbit.deleted == []
    assert report.issues_removed == 2  # Chronicle + Côte-Nord Gazette removed from library
    assert library_pdfs(world) == ["Evening Post 2026-09-10.pdf"]
    again = world.scanner.run()  # expired issues are not relinked from the still-seeding pack
    assert again.linked == 0 and again.skipped_expired == 2


def test_kept_issue_protects_whole_pack(world):
    h = add_pack(world, 1, date(2026, 9, 10), age_days=1)
    world.scanner.run()
    issue = world.repo.issue_by_key("Chronicle", "2026-09-10")
    world.repo.set_issue_keep(issue.id, True)
    world.qbit.torrents[h]["completion_on"] = time.time() - 10 * DAY
    world.settings(retention_enabled=True, retention_armed=True, retention_days=7, grace_hours=0)
    world.scanner.run()
    assert world.qbit.deleted == []
    assert "Chronicle 2026-09-10.pdf" in library_pdfs(world)
    assert world.repo.get_state(PREVIEW_KEY)["skipped"][0]["reason"] == "kept"


def test_unexpected_content_blocks_torrent_deletion(world):
    add_pack(world, 1, date(2026, 9, 10), age_days=10, extra_files=["Daily Newspapers 10 09 2026/setup.exe"])
    world.settings(retention_enabled=True, retention_armed=True, retention_days=30, grace_hours=0)
    world.scanner.run()
    world.settings(retention_days=7)
    world.scanner.run()
    assert world.qbit.deleted == []
    skipped = world.repo.get_state(PREVIEW_KEY)["skipped"]
    assert "unexpected content" in skipped[0]["reason"]


def test_category_changed_just_before_delete_is_not_deleted(world):
    h = add_pack(world, 1, date(2026, 9, 10), age_days=10)
    world.settings(retention_enabled=True, retention_armed=False, retention_days=30)
    world.scanner.run()
    world.settings(retention_days=7, retention_armed=True, grace_hours=0)

    original = world.qbit.request

    def switch_category(method, path, params=None, form=None, headers=None):
        if path == "/api/v2/torrents/info" and params and "hashes" in params:
            world.qbit.torrents[h]["category"] = "movies"
        return original(method, path, params, form, headers)

    world.qbit.request = switch_category
    report = world.scanner.run()
    assert world.qbit.deleted == []
    assert report.torrents_deleted == 0
    assert "not 'news'" in (world.repo.pending_deletions()[0]["detail"] or "")


def test_safety_cap_blocks_until_approved(world):
    for n in range(1, 4):
        add_pack(world, n, date(2026, 9, n), age_days=1, papers=("Chronicle",))
    world.scanner.run()
    for t in world.qbit.torrents.values():
        t["completion_on"] = time.time() - 10 * DAY
    world.settings(retention_enabled=True, retention_armed=True, retention_days=7, grace_hours=0,
                   max_torrent_deletions_per_run=2)
    report = world.scanner.run()
    assert report.retention_blocked
    assert world.qbit.deleted == []
    blocked = world.repo.get_state(BLOCKED_KEY)
    world.repo.set_state("retention_approval", {"torrents": blocked["torrents"], "issues": blocked["issues"],
                                                "expires": time.time() + 3600})
    report = world.scanner.run()
    assert report.torrents_deleted == 3


def test_disabling_retention_cancels_pending(world):
    add_pack(world, 1, date(2026, 9, 10), age_days=1)
    world.scanner.run()
    world.qbit.torrents[next(iter(world.qbit.torrents))]["completion_on"] = time.time() - 10 * DAY
    world.settings(retention_enabled=True, retention_armed=True, retention_days=7, grace_hours=24)
    world.scanner.run()
    assert len(world.repo.pending_deletions()) == 1
    world.settings(retention_enabled=False)
    world.scanner.run()
    assert world.repo.pending_deletions() == []
    assert world.repo.deletion_history()[0]["status"] == "cancelled"


def test_manually_removed_torrent_only_removes_library_issue(world):
    h = add_pack(world, 1, date.today() - timedelta(days=10), age_days=1, papers=("Chronicle",))
    world.scanner.run()
    del world.qbit.torrents[h]
    world.settings(retention_enabled=True, retention_armed=True, retention_days=7, grace_hours=0)
    report = world.scanner.run()
    assert report.issues_removed == 1
    assert world.qbit.deleted == []
    assert list(world.source.rglob("*.pdf"))  # data untouched


@pytest.mark.parametrize("bad_hash", ["all", "ABC", "0" * 39, "0" * 40 + "|" + "1" * 40])
def test_invalid_hash_never_reaches_delete(world, bad_hash):
    from periodica.qbittorrent import QbitClient, QbitError

    client = QbitClient(world.qbit.base_url, "admin", "adminadmin", http=world.qbit)
    with pytest.raises(QbitError):
        client.delete_with_files(bad_hash, "news")
    assert not [r for r in world.qbit.requests if r[1] == "/api/v2/torrents/delete"]

"""Monthly and numbered issues: names, the month-or-number rules, metadata, and the scanner end to end."""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET  # nosec B405 - parsing our own output in tests
from datetime import date, datetime

import pytest

from periodica.issues import DAY, MONTH, NUMBER, IssueId
from periodica.metadata import DC_NS, OPF_NS, build_opf, render_title, validate_title_format
from periodica.numbering import decide, reference_month
from periodica.parser import parse_issue_filename
from periodica.scanner import UNCLEAR_REASON

from .helpers import write_torrent_files

DAY_SECONDS = 86400


# --- names --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("filename", "paper", "year", "number"),
    [
        ("Arvo.Paper.2026.09.pdf", "Arvo Paper", 2026, 9),
        ("Business Monthly 2026-11.pdf", "Business Monthly", 2026, 11),
        ("Business_Monthly_09_2026.pdf", "Business Monthly", 2026, 9),
        ("Duck Weekly 38-2026.pdf", "Duck Weekly", 2026, 38),
        ("Duck.Weekly.2026.052.pdf", "Duck Weekly", 2026, 52),
        ("Radio 2 2026.07.pdf", "Radio 2", 2026, 7),
    ],
)
def test_year_and_number_names(filename, paper, year, number):
    parsed = parse_issue_filename(filename)
    assert parsed is not None and not parsed.is_daily
    assert (parsed.paper, parsed.year, parsed.number) == (paper, year, number)


@pytest.mark.parametrize(
    "filename",
    [
        "Chronicle.2026.02.30.pdf",      # an invalid daily date must not turn into "February 2026, tag 30"
        "Chronicle.2026.09.SCANNED.pdf",  # no tags after a year + number
        "Chronicle.2026.0.pdf",          # issue 0 does not exist
        "Chronicle.1850.05.pdf",         # implausible year
        "Chronicle 2026.pdf",            # a year alone says nothing
        "Chronicle.2026.1234.pdf",       # four digits are a year, not a number
    ],
)
def test_names_that_stay_unrecognised(filename):
    assert parse_issue_filename(filename) is None


def test_daily_names_still_win():
    parsed = parse_issue_filename("Chronicle.2026.09.15.pdf")
    assert parsed is not None and parsed.issue_date == date(2026, 9, 15) and parsed.is_daily


# --- what an issue is ---------------------------------------------------------------------------
def test_issue_ids_name_and_order_each_kind():
    day = IssueId.for_day(date(2026, 9, 15))
    month = IssueId.for_month(2026, 9)
    number = IssueId.for_number(2026, 8)
    assert (day.key, day.label, day.series_index, day.opf_date) == (
        "2026-09-15", "2026-09-15", "20260915", "2026-09-15")
    assert (month.key, month.label, month.series_index, month.opf_date) == (
        "2026-09", "2026-09", "20260900", "2026-09-01")
    assert (number.key, number.label, number.series_index, number.opf_date) == (
        "2026#008", "2026 #08", "2026008", "2026")
    # A month sorts just before that month's daily issues.
    assert int(month.series_index) < int(IssueId.for_day(date(2026, 9, 1)).series_index)
    # Keys sort numbered issues numerically.
    assert IssueId.for_number(2026, 9).key < IssueId.for_number(2026, 38).key
    for issue in (day, month, number):
        assert IssueId.from_stored(issue.period, issue.nominal_date.isoformat(), issue.number or None) == issue
    with pytest.raises(ValueError):
        IssueId.for_month(2026, 13)


# --- the rules ----------------------------------------------------------------------------------
SEPT = (2026, 9)
FEB = (2026, 2)
JAN = (2026, 1)


@pytest.mark.parametrize(
    ("choice", "detected", "year", "number", "ref", "january", "period"),
    [
        ("number", None, 2026, 9, SEPT, (), NUMBER),        # the user's choice always wins
        ("month", "number", 2026, 9, SEPT, (), MONTH),
        (None, None, 2026, 38, SEPT, (), NUMBER),           # above 12 is never a month
        (None, "number", 2026, 9, SEPT, (), NUMBER),        # numbered stays numbered
        (None, None, 2026, 9, SEPT, (), MONTH),             # same month
        (None, None, 2026, 10, SEPT, (), MONTH),            # published a little early
        (None, None, 2026, 8, SEPT, (), MONTH),             # downloaded a little late
        (None, None, 2026, 8, FEB, (), NUMBER),             # issue 8 in February cannot be August
        (None, None, 2026, 1, JAN, (), None),               # January: could be either
        (None, None, 2025, 12, JAN, (), None),              # ... including December across the year end
        (None, None, 2026, 2, JAN, (1,), NUMBER),           # a second number in the same January
        (None, None, 2026, 1, JAN, (1,), None),             # the same number again proves nothing
        (None, "month", 2026, 1, JAN, (), MONTH),           # a known monthly is not unclear in January
        (None, None, 2023, 5, SEPT, (), None),              # a back issue from another year: no evidence
        (None, "month", 2023, 5, SEPT, (), MONTH),
        (None, None, 2026, 9, None, (), None),              # nothing to compare with
    ],
)
def test_month_or_number_rules(choice, detected, year, number, ref, january, period):
    assert decide(choice, detected, year, number, ref, january).period == period


def test_reference_month_prefers_the_daily_papers_in_the_same_pack():
    dailies = [date(2026, 9, 15), date(2026, 9, 15), date(2026, 8, 31)]
    october = datetime(2026, 10, 2, 12).timestamp()
    assert reference_month(dailies, october) == (2026, 9)
    assert reference_month([], october) == (2026, 10)
    assert reference_month([], None) is None


# --- titles and metadata ------------------------------------------------------------------------
def test_titles_for_each_kind():
    assert render_title("{paper} {iso}", "Arvo", IssueId.for_month(2026, 9), "en") == "Arvo 2026-09"
    assert render_title("{paper} {month_name} {year}", "Arvo", IssueId.for_month(2026, 9), "fi") == (
        "Arvo syyskuu 2026")
    assert render_title("{paper} {month}/{year}", "Arvo", IssueId.for_month(2026, 9), "en") == "Arvo 9/2026"
    assert render_title("{paper} {year} #{nn}", "Duck", IssueId.for_number(2026, 8), "en") == "Duck 2026 #08"
    with pytest.raises(ValueError, match="unknown field"):
        validate_title_format("{paper} {day}.{month}", MONTH)  # a month has no day
    with pytest.raises(ValueError, match="unknown field"):
        validate_title_format("{paper} {month}", NUMBER)       # a numbered issue has no month
    validate_title_format("{paper} {weekday} {day}", DAY)


def _opf(data: bytes) -> tuple[dict, dict]:
    root = ET.fromstring(data)
    ns = {"opf": OPF_NS, "dc": DC_NS}
    dc = {el.tag.split("}")[1]: el.text for el in root.find("opf:metadata", ns) if el.tag.startswith(f"{{{DC_NS}}}")}
    metas = {m.get("name"): m.get("content") for m in root.findall(".//opf:meta", ns)}
    return dc, metas


def test_opf_for_monthly_and_numbered_issues():
    dc, metas = _opf(build_opf("Arvo", IssueId.for_month(2026, 9), "Arvo 2026-09", "en"))
    assert (dc["date"], dc["description"]) == ("2026-09-01", "Arvo – September 2026")
    assert (metas["calibre:series_index"], metas["calibre:title_sort"]) == ("20260900", "Arvo 2026-09")

    dc, metas = _opf(build_opf("Duck", IssueId.for_number(2026, 38), "Duck 2026 #38", "en"))
    assert (dc["date"], dc["description"]) == ("2026", "Duck – 38/2026")
    assert (metas["calibre:series_index"], metas["calibre:title_sort"]) == ("2026038", "Duck 2026#038")


# --- scanner, end to end ------------------------------------------------------------------------
def add_pack(world, n, day: date, magazines=(), *, age_days: float = 0.0, dailies=("Chronicle",)):
    folder = f"Daily Newspapers {day:%d %m %Y}"
    files = []
    for paper in dailies:
        stem = f"{paper}.{day:%Y.%m.%d}"
        files.append(f"{folder}/{stem}/{stem}.pdf")
    for stem in magazines:
        files.append(f"{folder}/{stem}/{stem}.pdf")
    write_torrent_files(world.source, files)
    completed = datetime(day.year, day.month, day.day, 12).timestamp()
    if age_days:
        completed = time.time() - age_days * DAY_SECONDS
    return world.qbit.add(n, folder, "news", "/downloads/news", files, completion_on=completed,
                          local_save_path=world.source)


def library(world):
    return sorted(str(p.relative_to(world.dest)).replace("\\", "/") for p in world.dest.rglob("*.pdf"))


def test_monthly_issue_in_a_daily_pack(world):
    add_pack(world, 1, date(2026, 9, 16), ["Arvo.Paper.2026.09"])
    report = world.scanner.run()
    assert report.error is None and report.linked == 2 and report.unmatched == 0
    assert "Arvo Paper/Arvo Paper 2026-09/Arvo Paper 2026-09.pdf" in library(world)
    opf = (world.dest / "Arvo Paper" / "Arvo Paper 2026-09" / "metadata.opf").read_text(encoding="utf-8")
    assert "<dc:title>Arvo Paper 2026-09</dc:title>" in opf
    paper = world.repo.paper("Arvo Paper")
    assert paper is not None and paper.numbering_detected == MONTH
    issue = world.repo.issue_by_key("Arvo Paper", "2026-09")
    assert issue is not None and issue.period == MONTH and issue.issue_label == "2026-09"
    assert world.repo.unmatched() == []


def test_numbered_issue_gets_its_own_folder_and_title_format(world):
    world.store.save(world.settings(numbered_title_format="{paper} {number}/{year}"))
    add_pack(world, 1, date(2026, 9, 16), ["Duck.Weekly.2026.38"])
    world.scanner.run()
    assert "Duck Weekly/Duck Weekly 2026 #38/Duck Weekly 2026 #38.pdf" in library(world)
    opf = (world.dest / "Duck Weekly" / "Duck Weekly 2026 #38" / "metadata.opf").read_text(encoding="utf-8")
    assert "<dc:title>Duck Weekly 38/2026</dc:title>" in opf


def test_january_waits_for_a_decision_and_blocks_automatic_delete(world):
    world.store.save(world.settings(retention_enabled=True, retention_armed=True, retention_days=7, grace_hours=0))
    h = add_pack(world, 1, date(2026, 1, 15), ["Mag.2026.01"], age_days=30)
    report = world.scanner.run()
    reasons = {u["path"].rsplit("/", 1)[-1]: u["reason"] for u in world.repo.unmatched()}
    assert reasons["Mag.2026.01.pdf"].startswith(UNCLEAR_REASON)
    assert report.torrents_deleted == 0 and world.qbit.torrents[h]["hash"] == h, "undecided files protect the pack"

    world.repo.set_paper_numbering("Mag", MONTH)
    report = world.scanner.run()
    assert world.repo.unmatched() == []
    # Already past retention, so it is not linked any more - but it no longer protects its pack either.
    assert world.repo.issue_by_key("Mag", "2026-01") is None
    assert report.torrents_deleted == 1, "once decided, the pack expires like any other"


def test_a_decision_links_the_waiting_issue(world):
    add_pack(world, 1, date(2026, 1, 15), ["Mag.2026.01"])
    world.scanner.run()
    assert world.repo.unmatched()
    world.repo.set_paper_numbering("Mag", MONTH)
    world.scanner.run()
    assert world.repo.unmatched() == []
    assert "Mag/Mag 2026-01/Mag 2026-01.pdf" in library(world)


def test_a_second_number_in_january_means_numbered(world):
    add_pack(world, 1, date(2026, 1, 8), ["Mag.2026.01"])
    world.scanner.run()
    assert world.repo.unmatched(), "the first January number is unclear"
    add_pack(world, 2, date(2026, 1, 15), ["Mag.2026.02"])
    world.scanner.run()
    paper = world.repo.paper("Mag")
    assert paper is not None and paper.numbering_detected == NUMBER
    assert {"Mag/Mag 2026 #01/Mag 2026 #01.pdf", "Mag/Mag 2026 #02/Mag 2026 #02.pdf"} <= set(library(world))


def test_a_monthly_that_turns_out_numbered_is_relabelled(world):
    add_pack(world, 1, date(2026, 9, 16), ["Mag.2026.09"])
    world.scanner.run()
    assert "Mag/Mag 2026-09/Mag 2026-09.pdf" in library(world)

    add_pack(world, 2, date(2026, 9, 23), ["Mag.2026.05"])  # issue 5 in September: numbered after all
    report = world.scanner.run()
    assert report.error is None
    assert {"Mag/Mag 2026 #05/Mag 2026 #05.pdf", "Mag/Mag 2026 #09/Mag 2026 #09.pdf"} <= set(library(world))
    assert "Mag/Mag 2026-09/Mag 2026-09.pdf" not in library(world)
    relabelled = world.repo.issue_by_key("Mag", "2026#009")
    assert relabelled is not None and relabelled.period == NUMBER
    opf = (world.dest / "Mag" / "Mag 2026 #09" / "metadata.opf").read_text(encoding="utf-8")
    assert "<dc:title>Mag 2026 #09</dc:title>" in opf
    messages = [a["message"] for a in world.repo.activity(50)]
    assert any("looks like a numbered magazine" in m for m in messages)
    assert any("Relabelled Mag 2026-09 as 2026 #09" in m for m in messages)


def test_the_users_choice_relabels_on_the_next_scan(world):
    add_pack(world, 1, date(2026, 9, 16), ["Mag.2026.09"])
    world.scanner.run()
    world.repo.set_paper_numbering("Mag", NUMBER)
    world.scanner.run()
    assert "Mag/Mag 2026 #09/Mag 2026 #09.pdf" in library(world)
    world.repo.set_paper_numbering("Mag", MONTH)
    world.scanner.run()
    assert "Mag/Mag 2026-09/Mag 2026-09.pdf" in library(world)
    assert len([p for p in library(world) if p.startswith("Mag/")]) == 1


def test_a_monthly_choice_cannot_hold_numbers_above_12(world):
    world.repo.ensure_paper("Mag")
    world.repo.set_paper_numbering("Mag", MONTH)
    add_pack(world, 1, date(2026, 9, 16), ["Mag.2026.38"])
    world.scanner.run()
    reasons = [u["reason"] for u in world.repo.unmatched()]
    assert any("38 is not a month" in r for r in reasons)


def test_dry_run_decides_nothing(world):
    add_pack(world, 1, date(2026, 9, 16), ["Mag.2026.05"])
    report = world.scanner.run(dry_run=True)
    assert report.would_link == 2
    assert world.repo.paper("Mag") is None
    assert world.repo.january_numbers("Mag", 2026) == set()


# --- web ----------------------------------------------------------------------------------------
def test_undecided_magazine_is_warned_about_once(world):
    add_pack(world, 1, date(2026, 1, 15), ["Mag.2026.01"])
    world.scanner.run()
    world.scanner.run()
    warnings = [a["message"] for a in world.repo.activity(50, level="warning")]
    matching = [m for m in warnings if "Mag.2026.01.pdf is a monthly or a numbered issue" in m]
    assert len(matching) == 1, warnings
    assert not any("not recognised as issues" in m for m in warnings)


def test_unmatched_page_offers_the_choice_and_applies_it(client):
    from .test_web import csrf_of, setup_admin

    token = setup_admin(client)
    world = client.world
    add_pack(world, 1, date(2026, 1, 15), ["Mag.2026.01"])
    world.scanner.run()

    page = client.get("/unmatched").text
    assert 'action="/papers/1/Mag/numbering"' in page and 'value="month"' in page and 'value="number"' in page
    assert "needs a choice" in client.get("/papers").text

    r = client.post("/papers/1/Mag/numbering", data={"csrf_token": token, "numbering": "number", "back": "unmatched"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/unmatched")
    paper = world.repo.paper("Mag")
    assert paper is not None and paper.numbering == NUMBER
    assert client.app.state.scan_trigger, "choosing starts a scan"

    world.scanner.run()
    assert world.repo.unmatched() == []
    page = client.get("/papers/Mag").text
    assert "numbered (set by you)" in page and "2026 #01" in page

    # Back to automatic: the stored choice is cleared, detection takes over again.
    token = csrf_of(client.get("/papers/Mag").text)
    client.post("/papers/1/Mag/numbering", data={"csrf_token": token, "numbering": ""})
    paper = world.repo.paper("Mag")
    assert paper is not None and paper.numbering is None


def test_numbering_choice_rejects_unknown_papers_and_values(client):
    from .test_web import setup_admin

    token = setup_admin(client)
    assert client.post("/papers/1/Nope/numbering", data={"csrf_token": token, "numbering": "month"}).status_code == 404
    client.world.repo.ensure_paper("Mag")
    client.post("/papers/1/Mag/numbering", data={"csrf_token": token, "numbering": "weekly"})
    paper = client.world.repo.paper("Mag")
    assert paper is not None and paper.numbering is None


def test_title_previews_for_all_three_kinds(client):
    from .test_web import setup_admin

    token = setup_admin(client)
    data = client.post("/settings-test/title", headers={"X-CSRF-Token": token}, data={
        "title_format": "{paper} {iso}", "monthly_title_format": "{paper} {month_name} {year}",
        "numbered_title_format": "{paper} {day}", "language": "en",
    }).json()
    assert data["previews"]["day"]["ok"] and data["previews"]["month"]["ok"]
    assert "Business Monthly" in data["previews"]["month"]["message"]
    assert not data["previews"]["number"]["ok"] and "unknown field" in data["previews"]["number"]["message"]


def test_a_settings_page_from_before_the_upgrade_keeps_the_new_formats(client):
    from .test_web import setup_admin

    token = setup_admin(client)
    world = client.world
    world.store.save(world.settings(monthly_title_format="{paper} {month}/{year}"))
    from .test_web import library_form

    data = library_form(world, title_format="{paper} {iso}")
    del data["monthly_title_format"]   # a form from before the field existed
    r = client.post("/settings/libraries/1", follow_redirects=False, data={"csrf_token": token, **data})
    assert r.status_code == 303
    assert world.store.load().monthly_title_format == "{paper} {month}/{year}"


def test_publications_page_groups_by_kind(client):
    from .test_web import setup_admin

    setup_admin(client)
    world = client.world
    add_pack(world, 1, date(2026, 9, 16), ["Business.Monthly.2026.09", "Duck.Weekly.2026.38"])
    add_pack(world, 2, date(2026, 1, 15), ["Quarterly.2026.01"], dailies=())
    world.scanner.run()
    page = client.get("/papers").text
    order = [page.index(heading) for heading in ("Needs a choice", "Daily", "Monthly", "Numbered")]
    assert order == sorted(order), "sections appear in a fixed order"
    sections = page.split("<section")
    def section_with(heading):
        return next(s for s in sections if f"<h2>{heading} " in s)
    assert "Quarterly" in section_with("Needs a choice")
    assert "Chronicle" in section_with("Daily")
    assert "Business Monthly" in section_with("Monthly") and "Duck Weekly" in section_with("Numbered")
    assert "Publications" in page and "Newspapers" not in page


def test_keep_explains_itself(client):
    from .test_web import setup_admin

    setup_admin(client)
    world = client.world
    add_pack(world, 1, date(2026, 9, 16))
    world.scanner.run()
    page = client.get("/papers/Chronicle").text
    assert 'title="Never delete this issue automatically.' in page
    assert "with it the whole torrent it came" in page

"""Library health page, Fix buttons and the places that point at damaged issues."""

from __future__ import annotations

import shutil
from datetime import date

from .test_libraries import add_magazine_pack, add_magazines
from .test_scanner_retention import add_pack
from .test_web import setup_admin


def damaged(client, problem="missing"):
    world = client.world
    add_pack(world, 1, date(2026, 9, 15))
    world.scanner.run()
    issue = next(i for i in world.repo.linked_issues() if i.paper == "Evening Post")
    folder = world.dest / "Evening Post" / "Evening Post 2026-09-15"
    if problem == "missing":
        shutil.rmtree(folder)
    else:
        pdf = folder / "Evening Post 2026-09-15.pdf"
        data = pdf.read_bytes()
        pdf.unlink()
        pdf.write_bytes(data)
    world.scanner.run()
    assert world.repo.issue(issue.id).problem == problem
    return world, issue


def test_empty_page(client):
    setup_admin(client)
    page = client.get("/health").text
    assert "All linked issues are whole" in page and 'href="/health"' in page


def test_page_lists_problems_and_fix_marks_them(client):
    token = setup_admin(client)
    world, issue = damaged(client)
    page = client.get("/health").text
    assert "Evening Post 2026-09-15" in page and "issue file or folder missing" in page
    assert "Fix all (1)" in page and "data-confirm" not in page.split("Fix all")[0].rsplit("<form", 1)[-1]
    r = client.post("/health/fix", data={"csrf_token": token, "issue_id": str(issue.id)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/health?msg=fixing"
    assert world.repo.issue(issue.id).fix_requested
    assert client.app.state.scan_trigger == "Library health: fix requested by admin"
    assert "Fixing on the next scan" in client.get("/health").text
    assert world.scanner.run().fixed == 1
    assert "All linked issues are whole" in client.get("/health").text


def test_fix_all_skips_issues_whose_download_is_gone(client):
    token = setup_admin(client)
    world, issue = damaged(client)
    world.qbit.torrents.clear()
    world.scanner.run()
    page = client.get("/health").text
    assert "The download is gone" in page and "Fix all" not in page
    r = client.post("/health/fix", data={"csrf_token": token, "all": "1"}, follow_redirects=False)
    assert r.headers["location"] == "/health?msg=nothing_to_fix"
    r = client.post("/health/fix", data={"csrf_token": token, "issue_id": str(issue.id)}, follow_redirects=False)
    assert r.headers["location"] == "/health?msg=nothing_to_fix"
    assert not world.repo.issue(issue.id).fix_requested

    r = client.post(f"/issues/{issue.id}/remove", data={"csrf_token": token, "back": "health"},
                    follow_redirects=False)
    assert r.headers["location"] == "/health?msg=removed"
    assert world.repo.issue(issue.id).status == "excluded"


def test_a_copy_asks_for_confirmation(client):
    setup_admin(client)
    damaged(client, "copy")
    page = client.get("/health").text
    assert "not a link to the download" in page
    assert page.count("The file now in the library is deleted") == 1
    assert "the file in the library is deleted and replaced" in page


def test_fix_needs_csrf_and_valid_ids(client):
    token = setup_admin(client)
    world, issue = damaged(client)
    assert client.post("/health/fix", data={"csrf_token": "wrong", "issue_id": str(issue.id)}).status_code == 403
    assert client.post("/health/fix", data={"csrf_token": token, "issue_id": "x"}).status_code == 400
    assert not world.repo.issue(issue.id).fix_requested


def test_filter_by_library(client):
    token = setup_admin(client)
    world = client.world
    magazines = add_magazines(world)
    add_pack(world, 1, date(2026, 9, 15))
    add_magazine_pack(world, 2, date(2026, 9, 15))
    world.scanner.run()
    shutil.rmtree(world.dest / "Evening Post")
    shutil.rmtree(world.dest.parent / "magazines" / "Harbour Monthly")
    world.scanner.run()
    page = client.get(f"/health?library={magazines.id}").text
    assert "Harbour Monthly" in page and "Evening Post 2026" not in page
    client.post("/health/fix", data={"csrf_token": token, "all": "1", "library": str(magazines.id)})
    fixing = {p["label"] for p in world.repo.issue_problems() if p["fix_requested"]}
    assert fixing == {"Harbour Monthly"}
    dashboard = client.get("/").text
    assert f'href="/health?library={magazines.id}"' in dashboard and "2 issues are damaged" in dashboard


def test_publication_page_dashboard_and_status(client):
    token = setup_admin(client)
    world, issue = damaged(client)
    page = client.get("/papers/1/Evening%20Post").text
    assert "damaged" in page and f'action="/issues/{issue.id}/fix"' in page
    r = client.post(f"/issues/{issue.id}/fix", data={"csrf_token": token}, follow_redirects=False)
    assert r.headers["location"] == "/papers/1/Evening%20Post?msg=fixing"
    assert world.repo.issue(issue.id).fix_requested
    page = client.get("/papers/1/Evening%20Post").text
    assert "(fixing on the next scan)" in page and f'action="/issues/{issue.id}/fix"' not in page
    assert "1 issue is damaged" in client.get("/").text
    status = client.get("/system").text
    assert "Library health: linked issues are whole" in status and "1 damaged" in status

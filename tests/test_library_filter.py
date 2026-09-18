"""Pages with several libraries: filter, per-library publication links, dashboard card, Status checks."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from .test_libraries import add_magazine_pack, add_magazines, pdfs
from .test_scanner_retention import add_pack
from .test_web import csrf_of, setup_admin


def two_libraries(client):
    world = client.world
    magazines = add_magazines(world)
    add_pack(world, 1, date(2026, 9, 15))
    add_magazine_pack(world, 2, date(2026, 9, 15))
    world.scanner.run()
    return world, magazines


def test_single_library_pages_look_as_before(client):
    setup_admin(client)
    world = client.world
    add_pack(world, 1, date(2026, 9, 15))
    world.scanner.run()
    for path in ("/papers", "/deletions", "/unmatched", "/"):
        page = client.get(path).text
        assert "library-filter" not in page, path
    assert "<h2>Libraries</h2>" not in client.get("/").text
    assert 'href="/papers/1/Chronicle"' in client.get("/papers").text
    assert client.get("/papers/Chronicle", follow_redirects=False).headers["location"] == "/papers/1/Chronicle"


def test_filter_is_remembered(client):
    setup_admin(client)
    world, magazines = two_libraries(client)
    page = client.get("/papers").text
    assert "library-filter" in page
    assert f'href="/papers/{magazines.id}/Harbour%20Monthly"' in page and 'href="/papers/1/Evening%20Post"' in page
    page = client.get(f"/papers?library={magazines.id}").text
    assert f'href="/papers/{magazines.id}/Harbour%20Monthly"' in page and "Evening%20Post" not in page
    assert client.cookies.get("periodica_library") == str(magazines.id)
    page = client.get("/papers").text   # no parameter: the remembered choice
    assert "Evening%20Post" not in page
    page = client.get("/papers?library=all").text
    assert "Evening%20Post" in page and "Harbour%20Monthly" in page
    assert client.get("/papers?library=999").status_code == 200, "an unknown library shows everything"


def test_same_name_in_two_libraries(client):
    setup_admin(client)
    world, magazines = two_libraries(client)
    assert client.get("/papers/Chronicle", follow_redirects=False).status_code == 404, "ambiguous old link"
    page = client.get(f"/papers/{magazines.id}/Chronicle").text
    assert "Magazines" in page and f'action="/papers/{magazines.id}/Chronicle"' in page
    assert f'/papers/{magazines.id}/Chronicle/numbering' in page
    # Settings of one don't touch the other.
    token = csrf_of(page)
    client.post(f"/papers/{magazines.id}/Chronicle", data={"csrf_token": token, "display_name": "Chronicle Weekend",
                                                             "enabled": "on", "retention_days": ""})
    assert world.repo.paper("Chronicle", magazines.id).display_name == "Chronicle Weekend"
    assert world.repo.paper("Chronicle", 1).display_name is None


def test_issue_actions_use_the_issues_own_library(client):
    token = setup_admin(client)
    world, magazines = two_libraries(client)
    issue = next(i for i in world.repo.linked_issues({magazines.id}) if i.paper == "Chronicle")
    cover = Path(issue.dest_dir) / "cover.jpg"
    cover.write_bytes(b"\xff\xd8 fake jpeg")
    r = client.get(f"/issues/{issue.id}/cover")
    assert r.status_code == 200, "covers of other libraries are served too"
    r = client.post(f"/issues/{issue.id}/remove", data={"csrf_token": token}, follow_redirects=False)
    assert r.headers["location"].startswith(f"/papers/{magazines.id}/Chronicle")
    assert not Path(issue.dest_dir).exists()
    assert world.repo.issue(issue.id).status == "excluded"
    assert "Chronicle 2026-09-15.pdf" in pdfs(world.dest), "the same issue in the other library is untouched"


def test_unmatched_and_deletions_filter(client):
    setup_admin(client)
    world, magazines = two_libraries(client)
    add_magazine_pack(world, 3, date(2026, 1, 15), papers=("Chronicle", "Quarterly.Review"))
    world.qbit.files[world.qbit.add(4, "Other", "news", "/downloads/news", ["Other/Special edition.pdf"],
                                    local_save_path=world.source)] = [
        {"name": "Other/Special edition.pdf", "size": 1, "progress": 1}]
    (world.source / "Other").mkdir()
    (world.source / "Other" / "Special edition.pdf").write_bytes(b"%PDF-1.4")
    world.scanner.run()
    rows = {u["path"]: u for u in world.repo.unmatched()}
    assert rows["Other/Special edition.pdf"]["library_id"] == 1
    page = client.get(f"/unmatched?library={magazines.id}").text
    assert "Special edition" not in page
    page = client.get("/unmatched?library=1").text
    assert "Special edition" in page
    page = client.get("/deletions?library=1").text
    assert "Magazines 15 09 2026" not in page and "Daily Newspapers 15 09 2026" in page
    page = client.get("/deletions?library=all").text
    assert "Magazines 15 09 2026" in page and "Daily Newspapers 15 09 2026" in page


def test_pending_deletions_know_their_library(client):
    setup_admin(client)
    world = client.world
    magazines = add_magazines(world, retention_days=1)
    magazine_hash = add_magazine_pack(world, 2, date(2026, 9, 1), age_days=5)
    world.settings(retention_enabled=True, retention_armed=True, grace_hours=48)
    world.scanner.run()
    pending = {d["ref"]: d for d in world.repo.pending_deletions()}
    assert pending[magazine_hash]["library_id"] == magazines.id
    assert all(d["library_id"] == magazines.id for d in pending.values())
    def pending_table(page: str) -> str:
        return page.split("<h2>Pending", 1)[1].split("<h2>History", 1)[0]

    assert "Magazines 01 09 2026" not in pending_table(client.get("/deletions?library=1").text)
    assert "Magazines 01 09 2026" in pending_table(client.get(f"/deletions?library={magazines.id}").text)
    page = client.get("/deletions?library=1").text
    assert "Automatic delete covers all libraries" in page, "the preview and arming stay global"


def test_dashboard_libraries_card_and_stale_warning(client):
    setup_admin(client)
    world = client.world
    magazines = add_magazines(world)
    add_pack(world, 1, date(2026, 9, 15))
    add_magazine_pack(world, 2, date(2026, 9, 1), age_days=5)
    world.settings(stale_download_hours=48)
    world.scanner.run()
    page = client.get("/").text
    assert "<h2>Libraries</h2>" in page and f"/papers?library={magazines.id}" in page
    assert "No new downloads in Magazines" in page and "category <code>magazines</code>" in page
    assert "No new downloads in News" not in page


def test_status_checks_each_library(client):
    setup_admin(client)
    world, magazines = two_libraries(client)
    page = client.get("/system").text
    assert "News: Source folder exists" in page and "Magazines: Source folder exists" in page
    assert "qBittorrent RSS rule adds to category &#39;magazines&#39;" in page
    assert "qBittorrent RSS rule adds to category &#39;news&#39;" in page

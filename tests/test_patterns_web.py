"""Unmatched → "Set naming pattern…", the preview, saving and deleting patterns, and the extras field."""

from __future__ import annotations

import re

from .test_libraries import add_magazines
from .test_libraries_ui import form_of
from .test_patterns import DUCK, duck_pack
from .test_web import csrf_of, setup_admin


def unmatched_duck(client):
    world = client.world
    comics = add_magazines(world, name="Comics")
    h = duck_pack(world, comics)
    world.scanner.run()
    return world, comics, h


def test_unmatched_offers_a_pattern_only_for_unrecognised_pdfs(client):
    setup_admin(client)
    world, comics, h = unmatched_duck(client)
    page = client.get("/unmatched").text
    forms = re.findall(r'<form method="get" action="(/settings/libraries/\d+/patterns/new)">(.*?)</form>', page, re.S)
    assert len(forms) == 1 and forms[0][0] == f"/settings/libraries/{comics.id}/patterns/new"
    assert 'value="DW.2026.No.38/DW.2026.No.38.pdf"' in forms[0][1] and ">Fix</button>" in forms[0][1]
    assert f"/settings/libraries/{comics.id}#extras" in page, "unexpected types point at the extras field"
    assert "Allow jpg files in this library" in page


def test_form_is_prefilled_and_previews(client):
    token = setup_admin(client)
    world, comics, h = unmatched_duck(client)
    page = client.get(f"/settings/libraries/{comics.id}/patterns/new",
                      params={"torrent": h, "path": "DW.2026.No.38/DW.2026.No.38.pdf"}).text
    assert 'value="DW.{year}.No.{number}"' in page and 'name="publication" value="DW"' in page
    assert "DW.2026.No.38.pdf" in page
    url = f"/settings/libraries/{comics.id}/patterns/preview"
    r = client.post(url, headers={"X-CSRF-Token": token},
                    data={"pattern": DUCK, "publication": "Duck Weekly", "filename": "DW.2026.No.38.pdf"})
    assert r.json() == {"ok": True, "kind": "number", "matches": [],
                        "message": "DW.2026.No.38.pdf → Duck Weekly 2026 #38 (numbered)"}
    r = client.post(url, headers={"X-CSRF-Token": token},
                    data={"pattern": "DW.{year}", "publication": "Duck Weekly", "filename": "DW.2026.No.38.pdf"})
    assert r.json()["ok"] is False and "needs {number}" in r.json()["message"]
    r = client.post(url, headers={"X-CSRF-Token": token},
                    data={"pattern": "BB.{year}.No.{number}", "publication": "X", "filename": "DW.2026.No.38.pdf"})
    assert r.json()["ok"] is False and "does not match" in r.json()["message"]


def test_preview_lists_other_matching_files(client):
    token = setup_admin(client)
    world, comics, h = unmatched_duck(client)
    duck_pack(world, comics, n=6, files=("DW.2026.No.39.pdf",))
    world.scanner.run()
    r = client.post(f"/settings/libraries/{comics.id}/patterns/preview", headers={"X-CSRF-Token": token},
                    data={"pattern": DUCK, "publication": "Duck Weekly", "filename": "DW.2026.No.38.pdf"})
    assert r.json()["matches"] == ["DW.2026.No.39.pdf → Duck Weekly 2026 #39 (numbered)"]


def test_saving_a_pattern_links_the_file(client):
    token = setup_admin(client)
    world, comics, h = unmatched_duck(client)
    r = client.post(f"/settings/libraries/{comics.id}/patterns", follow_redirects=False, data={
        "csrf_token": token, "pattern": DUCK, "publication": "Duck Weekly", "filename": "DW.2026.No.38.pdf",
        "torrent": h})
    assert r.status_code == 303 and r.headers["location"] == "/unmatched?msg=pattern_added"
    assert [(p["pattern"], p["paper"]) for p in world.repo.name_patterns(comics.id)] == [(DUCK, "Duck Weekly")]
    assert world.repo.paper("Duck Weekly", comics.id).numbering == "number"
    assert client.app.state.scan_trigger == "File name pattern added by admin"
    assert world.scanner.run().linked == 1


def test_saving_validates(client):
    token = setup_admin(client)
    world, comics, h = unmatched_duck(client)
    url = f"/settings/libraries/{comics.id}/patterns"
    base = {"csrf_token": token, "pattern": DUCK, "publication": "Duck Weekly", "filename": "DW.2026.No.38.pdf"}
    for changes, message in (({"pattern": "{bad}"}, "unknown placeholder"),
                             ({"publication": ""}, "give the publication a name"),
                             ({"pattern": "BB.{year}.No.{number}"}, "does not match")):
        r = client.post(url, data={**base, **changes})
        assert r.status_code == 400 and message in r.text, changes
    assert world.repo.name_patterns(comics.id) == []
    assert client.post(url, data={**base, "csrf_token": "wrong"}).status_code == 403
    assert client.post("/settings/libraries/999/patterns", data=base).status_code == 404


def test_library_page_lists_and_deletes_patterns(client):
    token = setup_admin(client)
    world, comics, h = unmatched_duck(client)
    world.repo.add_name_pattern(comics.id, DUCK, "Duck Weekly")
    page = client.get(f"/settings/libraries/{comics.id}").text
    assert "DW.{year}.No.{number}" in page and "numbered" in page
    (row,) = world.repo.name_patterns(comics.id)
    r = client.post(f"/settings/libraries/{comics.id}/patterns/{row['id']}/delete",
                    data={"csrf_token": csrf_of(page)}, follow_redirects=False)
    assert "msg=pattern_deleted" in r.headers["location"] and world.repo.name_patterns(comics.id) == []
    assert client.post(f"/settings/libraries/1/patterns/{row['id']}/delete",
                       data={"csrf_token": token}).status_code == 404


def test_extras_field_is_saved_and_validated(client):
    token = setup_admin(client)
    world, comics, h = unmatched_duck(client)
    url = f"/settings/libraries/{comics.id}"
    r = client.post(url, data={"csrf_token": token, **form_of(comics, extra_extensions="pdf")})
    assert r.status_code == 400 and "pdf files are the issues" in r.text
    r = client.post(url, data={"csrf_token": token, **form_of(comics, extra_extensions="../x")})
    assert r.status_code == 400
    r = client.post(url, data={"csrf_token": token, **form_of(comics, extra_extensions=".JPG txt nfo")},
                    follow_redirects=False)
    assert r.status_code == 303
    assert world.repo.library(comics.id).extra_extensions == "jpg, txt, nfo"

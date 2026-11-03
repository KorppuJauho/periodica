"""Page redirects only ever lead to this app's own pages."""

from __future__ import annotations

import pytest

from periodica.web.deps import local_redirect


@pytest.mark.parametrize("target", ["/", "/settings/libraries/3?msg=saved", "/papers/1/Evening%20Post?msg=paper",
                                    "/settings/libraries/2?msg=pattern_deleted#patterns"])
def test_local_paths_are_kept(target):
    response = local_redirect(target)
    assert response.status_code == 303 and response.headers["location"] == target


@pytest.mark.parametrize("target", ["https://example.com/", "//example.com/x", r"/\example.com", r"\\example.com",
                                    "example.com", "", "/x\r\nSet-Cookie: a=b", r"/a\b"])
def test_anything_else_goes_to_the_dashboard(target):
    assert local_redirect(target).headers["location"] == "/"


def test_status_code_is_kept():
    assert local_redirect("/system/logs", status_code=307).status_code == 307

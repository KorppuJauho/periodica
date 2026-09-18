from __future__ import annotations

import os
from datetime import date

import pytest

from periodica import covers
from periodica.formats import FormatError, check_source
from periodica.linker import (
    MARKER_NAME,
    LinkConflictError,
    issue_paths,
    link_book,
    move_issue_dir,
    remove_issue_dir,
    write_marker,
)
from periodica.paths import UnsafePathError

from .helpers import can_symlink, make_pdf


@pytest.fixture
def roots(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    pdf = src / "Chronicle.2026.09.15.pdf"
    pdf.write_bytes(make_pdf())
    return src, dst, pdf


def test_prepare_library_root_creates_keep_file_only_inside_data_root(tmp_path):
    from periodica.linker import LIBRARY_KEEP_NAME, prepare_library_root

    data = tmp_path / "data"
    (data / "media" / "books").mkdir(parents=True)
    assert prepare_library_root(str(data / "media" / "books" / "news"), data) is True
    assert (data / "media" / "books" / "news" / LIBRARY_KEEP_NAME).is_file()
    assert prepare_library_root(str(tmp_path / "elsewhere" / "news"), data) is False  # outside data root
    assert prepare_library_root(str(data / "missing-parent" / "news"), data) is False
    assert not (data / "missing-parent").exists()


def test_link_creates_hardlink_and_is_idempotent(roots):
    src, dst, pdf = roots
    paths = issue_paths(dst, "Chronicle", date(2026, 9, 15))
    assert link_book(pdf, paths, dst) is True
    assert paths.book.name == "Chronicle 2026-09-15.pdf"
    assert paths.issue_dir.parent.name == "Chronicle"
    assert os.path.samefile(pdf, paths.book)
    assert os.stat(pdf).st_nlink == 2
    assert link_book(pdf, paths, dst) is False
    assert os.stat(pdf).st_nlink == 2


def test_link_refuses_to_overwrite_different_file(roots):
    src, dst, pdf = roots
    paths = issue_paths(dst, "Chronicle", date(2026, 9, 15))
    paths.issue_dir.mkdir(parents=True)
    paths.book.write_bytes(b"%PDF- something else")
    with pytest.raises(LinkConflictError):
        link_book(pdf, paths, dst)
    assert paths.book.read_bytes() == b"%PDF- something else"


def test_check_source_rejects_non_pdf(roots):
    src, dst, pdf = roots
    fake = src / "Fake.2026.09.15.pdf"
    fake.write_bytes(b"MZ\x90\x00 not a pdf")
    with pytest.raises(FormatError):
        check_source(fake, src)
    assert check_source(pdf, src) == ".pdf"


def test_remove_requires_marker(roots):
    src, dst, pdf = roots
    paths = issue_paths(dst, "Chronicle", date(2026, 9, 15))
    link_book(pdf, paths, dst)
    with pytest.raises(UnsafePathError):
        remove_issue_dir(paths.issue_dir, dst)
    assert paths.book.exists()
    write_marker(paths, "Chronicle", date(2026, 9, 15), "0" * 40)
    (paths.paper_dir / "folder.jpg").write_bytes(b"\xff\xd8\xff")
    assert remove_issue_dir(paths.issue_dir, dst) is True
    assert not paths.issue_dir.exists()
    assert not paths.paper_dir.exists()  # emptied paper folder cleaned up
    assert pdf.exists() and os.stat(pdf).st_nlink == 1  # source untouched


def test_remove_refuses_folders_outside_layout(roots, tmp_path):
    src, dst, pdf = roots
    other = dst / "Random"
    other.mkdir()
    (other / MARKER_NAME).write_text("{}")
    with pytest.raises(UnsafePathError):
        remove_issue_dir(other, dst)  # depth 1, not an issue folder
    with pytest.raises(UnsafePathError):
        remove_issue_dir(src, dst)


def test_move_issue_dir_on_rename(roots):
    src, dst, pdf = roots
    old = issue_paths(dst, "Chronicle", date(2026, 9, 15))
    link_book(pdf, old, dst)
    write_marker(old, "Chronicle", date(2026, 9, 15), "0" * 40)
    new = issue_paths(dst, "AL", date(2026, 9, 15))
    move_issue_dir(old.issue_dir, new, dst)
    assert new.book.exists() and os.path.samefile(pdf, new.book)
    assert not old.paper_dir.exists()


def test_symlink_in_destination_is_refused(roots, tmp_path):
    src, dst, pdf = roots
    if not can_symlink(tmp_path):
        pytest.skip("symlinks not available")
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, dst / "Chronicle", target_is_directory=True)
    with pytest.raises(UnsafePathError):
        paths = issue_paths(dst, "Chronicle", date(2026, 9, 15))
        link_book(pdf, paths, dst)
    assert not any(outside.iterdir())


def test_symlinked_source_is_refused(roots, tmp_path):
    src, dst, pdf = roots
    if not can_symlink(tmp_path):
        pytest.skip("symlinks not available")
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(make_pdf())
    link = src / "Evil.2026.09.15.pdf"
    os.symlink(secret, link)
    with pytest.raises(UnsafePathError):
        check_source(link, src)


@pytest.mark.skipif(not covers.pdftoppm_available(), reason="pdftoppm not installed")
def test_render_cover_and_folder_art(roots):
    src, dst, pdf = roots
    paths = issue_paths(dst, "Chronicle", date(2026, 9, 15))
    link_book(pdf, paths, dst)
    write_marker(paths, "Chronicle", date(2026, 9, 15), "0" * 40)
    covers.render_cover(paths.book, paths.cover, 300)
    assert paths.cover.read_bytes().startswith(covers.JPEG_MAGIC)
    assert covers.update_folder_art(paths.paper_dir, dst) is True
    assert (paths.paper_dir / "folder.jpg").read_bytes() == paths.cover.read_bytes()
    assert covers.update_folder_art(paths.paper_dir, dst) is False  # unchanged


@pytest.mark.skipif(not covers.pdftoppm_available(), reason="pdftoppm not installed")
def test_render_cover_rejects_garbage(roots):
    src, dst, pdf = roots
    bad = src / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4\ngarbage garbage")
    with pytest.raises(covers.CoverError):
        covers.render_cover(bad, dst / "cover.jpg", 300)
    assert not (dst / "cover.jpg").exists()

"""Settings → Libraries: list, add (wizard with suggestions), edit, enable/disable and delete libraries.

The wizard only reads from qBittorrent and Jellyfin (categories with their save paths, libraries with
their folders) to suggest values; it never creates or changes anything there.
"""

from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import ValidationError

from ..auth import Session
from ..jellyfin import JellyfinError
from ..libraries import (
    Library,
    jellyfin_path_hint,
    library_conflicts,
    slugify,
    suggest_destination,
    suggest_jellyfin_library,
)
from ..netsafe import HttpError, UnsafeUrlError
from ..paths import PathMapping, is_within, same_filesystem
from ..qbittorrent import QbitError
from ..scanner import all_problems, library_problems
from .deps import render, require_csrf, require_session

log = logging.getLogger(__name__)
router = APIRouter()

PENDING_TTL = 15 * 60
LIBRARY_FORM_FIELDS = ("name", "category", "source_dir", "dest_dir", "jellyfin_library_id", "jellyfin_library_name",
                       "title_format", "monthly_title_format", "numbered_title_format", "language", "cover_width",
                       "retention_days", "extra_extensions", "format_priority")
# Changing these changes which files the library links (and, through the category, may delete).
RISKY = {"category", "source_dir", "dest_dir"}
LABELS = {
    "name": "Name", "category": "Category", "source_dir": "Source folder", "dest_dir": "Destination folder",
    "jellyfin_library_id": "Jellyfin library", "jellyfin_library_name": "Jellyfin library name",
    "title_format": "Title format", "monthly_title_format": "Title format for monthly issues",
    "numbered_title_format": "Title format for numbered issues", "language": "Language",
    "cover_width": "Cover width", "retention_days": "Delete after (days)", "enabled": "Enabled",
    "extra_extensions": "Other files allowed in a download", "format_priority": "Format priority",
}


def _errors(exc: ValidationError) -> list[str]:
    messages = []
    for err in exc.errors():
        field = str(err["loc"][0]) if err.get("loc") else ""
        msg = err.get("msg", "invalid value").removeprefix("Value error, ")
        messages.append(f"{LABELS.get(field, field)}: {msg}")
    return messages


def _library_or_404(request: Request, library_id: int) -> Library | None:
    return request.app.state.repo.library(library_id)


def _from_form(form, base: Library) -> Library:
    data = base.model_dump()
    for field in LIBRARY_FORM_FIELDS:
        raw = form.get(field)
        if isinstance(raw, str):
            data[field] = raw
    return Library(**data)


def _page(request: Request, template: str, status_code: int = 200, **context):
    state = request.app.state
    settings = state.store.load()
    return render(request, template, status_code, nav="settings", section="libraries", settings=settings,
                  data_root=str(state.env.data_root), **context)


# --- list ---------------------------------------------------------------------------------------------
@router.get("/settings/libraries")
async def libraries_page(request: Request, session: Session = Depends(require_session)):
    state = request.app.state
    libraries = state.repo.libraries()
    settings = state.store.load()
    problems = {lib.id: library_problems(lib, settings, state.env, libraries) for lib in libraries if lib.enabled}
    return _page(request, "libraries.html", libraries=libraries, counts=state.repo.library_issue_counts(),
                 problems=problems, global_problems=all_problems(settings, state.env, libraries))


@router.post("/settings/libraries/{library_id}/enabled")
async def library_enabled(library_id: int, request: Request, session: Session = Depends(require_csrf)):
    state = request.app.state
    library = _library_or_404(request, library_id)
    if library is None:
        return PlainTextResponse("not found", status_code=404)
    form = await request.form()
    enabled = form.get("enabled") == "true"
    if not enabled and len([lib for lib in state.repo.libraries() if lib.enabled]) == 1 and library.enabled:
        return RedirectResponse("/settings/libraries?msg=last_enabled", status_code=303)
    state.repo.update_library(library.model_copy(update={"enabled": enabled}))
    word = "enabled" if enabled else "disabled"
    state.repo.log("info", f"Library '{library.name}' {word} by {session.username}")
    state.scan_event.set()
    return RedirectResponse(f"/settings/libraries?msg=library_{word}", status_code=303)


# --- add (wizard) -------------------------------------------------------------------------------------
@router.get("/settings/libraries/new")
async def library_new(request: Request, session: Session = Depends(require_session)):
    state = request.app.state
    existing = state.repo.libraries()
    template = existing[0] if existing else Library()
    draft = Library(name="New library", source_dir=template.source_dir, dest_dir=template.dest_dir,
                    title_format=template.title_format, monthly_title_format=template.monthly_title_format,
                    numbered_title_format=template.numbered_title_format, language=template.language,
                    cover_width=template.cover_width, retention_days=template.retention_days)
    # The folders stay empty in the form so the wizard can suggest them.
    return _page(request, "library_form.html", library=draft, new=True, blank_folders=True)


@router.post("/settings/libraries")
async def library_create(request: Request, session: Session = Depends(require_csrf)):
    state = request.app.state
    form = await request.form()
    existing = state.repo.libraries()
    try:
        library = _from_form(form, Library())
    except ValidationError as exc:
        return _page(request, "library_form.html", 400, library=_draft(form), new=True, errors=_errors(exc))
    conflicts = library_conflicts(library, existing)
    settings = state.store.load()
    if settings.uses_qbit and not library.category:
        conflicts.append("Category: required, because Periodica finds downloads by their qBittorrent category")
    if conflicts:
        return _page(request, "library_form.html", 400, library=library, new=True, errors=conflicts)
    library_id = state.repo.add_library(library)
    _disarm(request, settings, f"library '{library.name}' was added")
    state.repo.log("info", f"Library '{library.name}' added by {session.username} (category "
                           f"'{library.category or '-'}', {library.source_dir} -> {library.dest_dir})")
    log.info("library %r added by %s", library.name, session.username)
    state.scan_event.set()
    return RedirectResponse(f"/settings/libraries/{library_id}?msg=library_added", status_code=303)


def _draft(form) -> Library:
    """Whatever validates from a rejected form, so the page can show it again."""
    library = Library()
    for field in LIBRARY_FORM_FIELDS:
        raw = form.get(field)
        if isinstance(raw, str):
            try:
                setattr(library, field, raw)
            except ValidationError:
                continue
    return library


def _disarm(request: Request, settings, reason: str) -> None:
    if not settings.retention_armed:
        return
    settings.retention_armed = False
    request.app.state.store.save(settings)
    request.app.state.repo.log("warning", f"Automatic delete disarmed because {reason}; review the preview "
                                          "and re-arm")


# --- suggestions and preview (JSON, read-only) -------------------------------------------------------
@router.post("/settings/libraries/suggest")
async def library_suggest(request: Request, session: Session = Depends(require_csrf)):
    state = request.app.state
    form = await request.form()
    settings = state.store.load()
    library_id = _int(form.get("library_id"))
    name = str(form.get("name") or "")
    category = str(form.get("category") or "").strip()
    # Fields the page filled in itself (not typed by the user) keep following the name.
    auto = set(str(form.get("auto_fields") or "").split(","))
    typed_category = "" if "category" in auto else category
    source_dir = str(form.get("source_dir") or "").strip()
    dest_dir = str(form.get("dest_dir") or "").strip()
    existing = [lib for lib in state.repo.libraries() if lib.id != library_id]
    used = {lib.category: lib.name for lib in existing if lib.category}

    def gather() -> dict:
        result: dict = {"mode": "offline" if settings.offline else ("qbittorrent" if settings.uses_qbit else "folder")}
        categories = None
        if settings.uses_qbit:
            try:
                client = state.qbit_factory(settings)
                paths = client.category_paths()
                try:
                    default = client.default_save_path()
                except QbitError:
                    default = ""
                mapping = PathMapping(settings.path_map_remote, settings.path_map_local)
                categories = []
                for cat, path in sorted(paths.items()):
                    absolute = _absolute_save_path(cat, path, default)
                    categories.append({"name": cat, "save_path": absolute or path,
                                       "local_path": os.path.normpath(mapping.to_local(absolute)) if absolute else "",
                                       "used_by": used.get(cat, "")})
                result["categories"] = categories
            except (QbitError, HttpError, UnsafeUrlError) as exc:
                result["categories_error"] = str(exc)[:300]
        suggested_category = typed_category or slugify(name)
        suggested_source = ""
        for item in categories or []:
            if item["name"] == suggested_category and item["local_path"]:
                suggested_source = os.path.normpath(item["local_path"])
        if not suggested_source and existing:
            suggested_source = str(Path(existing[0].source_dir).parent / (suggested_category or "library"))
        result["suggested"] = {
            "category": suggested_category,
            "source_dir": suggested_source,
            "dest_dir": suggest_destination(name, existing),
        }
        dest_for_checks = dest_dir or result["suggested"]["dest_dir"]
        jellyfin: dict = {"active": settings.jellyfin_active}
        if settings.jellyfin_active:
            try:
                libraries = state.jellyfin_factory(settings).libraries()
                jellyfin["libraries"] = libraries
                jellyfin["suggested"] = suggest_jellyfin_library(libraries, dest_for_checks)
                jellyfin["path_hint"] = jellyfin_path_hint(dest_for_checks, libraries)
            except (JellyfinError, HttpError, UnsafeUrlError) as exc:
                jellyfin["error"] = str(exc)[:300]
        result["jellyfin"] = jellyfin
        result["checks"] = _checks(state, settings, existing, used, categories, category, source_dir, dest_dir,
                                   library_id, name)
        return result

    return JSONResponse(await asyncio.to_thread(gather))


def _absolute_save_path(category: str, save_path: str, default: str) -> str:
    """Where qBittorrent saves a category: its own path, relative paths and "" below the default path."""
    if save_path.startswith("/"):
        return posixpath.normpath(save_path)
    if not default.startswith("/"):
        return ""
    return posixpath.normpath(posixpath.join(default, save_path or category))


def _int(value) -> int:
    try:
        return int(str(value or "0"))
    except ValueError:
        return 0


def _checks(state, settings, existing, used, categories, category, source_dir, dest_dir, library_id,
            name) -> list[dict]:
    checks = []

    def add(label: str, ok: bool, detail: str = "", level: str = "") -> None:
        checks.append({"label": label, "ok": ok, "detail": detail, "level": level or ("ok" if ok else "bad")})

    if settings.uses_qbit and category:
        if category in used:
            add(f"Category '{category}' is already used", False, f"by library '{used[category]}'")
        elif categories is not None:
            if any(c["name"] == category for c in categories):
                add(f"Category '{category}' exists in qBittorrent", True)
            else:
                add(f"Category '{category}' does not exist in qBittorrent", False,
                    "create it in qBittorrent (right-click in the category list → Add category), then Reload")
    data_root = state.env.data_root
    if source_dir:
        src = Path(source_dir)
        if src.is_dir():
            add("Source folder exists", True, source_dir)
        elif settings.uses_qbit:
            add("Source folder does not exist yet", False,
                f"{source_dir}: qBittorrent creates it with the first download in this category; the library is "
                "skipped until then. Check the path if you expected it to exist.", level="warn")
        else:
            add("Source folder does not exist", False, source_dir)
        add(f"Source folder is inside {data_root}", is_within(src, data_root))
    if dest_dir:
        dst = Path(dest_dir)
        parent = dst if dst.exists() else dst.parent
        if dst.is_dir():
            add("Destination folder exists", True, dest_dir)
        elif parent.is_dir():
            add("Destination folder will be created", True, f"{dest_dir} (inside the existing {parent})")
        else:
            add("Destination folder cannot be created", False, f"its parent folder {parent} does not exist")
        if source_dir and Path(source_dir).is_dir() and parent.is_dir():
            try:
                same = same_filesystem(source_dir, parent)
            except OSError:
                same = False
            add("Source and destination are on the same filesystem (hardlinks possible)", same)
    if source_dir and dest_dir:
        try:
            candidate = Library(id=library_id or -1, name=name or "?", category=category, source_dir=source_dir,
                                dest_dir=dest_dir)
        except ValidationError as exc:
            add("Folders are valid", False, "; ".join(_errors(exc)))
        else:
            conflicts = [c for c in library_conflicts(candidate, existing) if "name" not in c
                         and "category" not in c]
            add("No overlap with other libraries", not conflicts, "; ".join(conflicts))
    return checks


@router.post("/settings/libraries/preview")
async def library_preview(request: Request, session: Session = Depends(require_csrf)):
    """Dry run of the library as filled in (saved or not): what would be linked."""
    state = request.app.state
    form = await request.form()
    library_id = _int(form.get("library_id"))
    base = state.repo.library(library_id) if library_id else None
    try:
        library = _from_form(form, base or Library())
    except ValidationError as exc:
        return JSONResponse({"ok": False, "message": "; ".join(_errors(exc))})
    report = await asyncio.to_thread(state.scanner.run, True, None, None, [library])
    if report.error:
        return JSONResponse({"ok": False, "message": f"The scan would not run: {report.error}"})
    noun = "torrent(s) in the category" if state.store.load().uses_qbit else "download(s) in the source folder"
    message = (f"{report.torrents} {noun}: {report.would_link} issue(s) would be linked, "
               f"{report.already_linked} already in the library, {report.unmatched} file(s) not recognised.")
    return JSONResponse({"ok": True, "message": message, "report": report.as_dict()})


# --- edit ---------------------------------------------------------------------------------------------
@router.get("/settings/libraries/{library_id}")
async def library_edit(library_id: int, request: Request, session: Session = Depends(require_session)):
    state = request.app.state
    library = _library_or_404(request, library_id)
    if library is None:
        return PlainTextResponse("not found", status_code=404)
    libraries = state.repo.libraries()
    problems = library_problems(library, state.store.load(), state.env, libraries) if library.enabled else []
    return _page(request, "library_form.html", library=library, new=False, problems=problems,
                 can_delete=len(libraries) > 1, patterns=_pattern_rows(state.repo, library_id))


def _pattern_rows(repo, library_id: int) -> list[dict]:
    from ..patterns import KIND_LABELS, compile_pattern

    rows = []
    for row in repo.name_patterns(library_id):
        try:
            kind = KIND_LABELS.get(compile_pattern(row["pattern"]).kind, "")
        except ValueError as exc:
            kind = f"invalid: {exc}"
        rows.append({**row, "kind": kind})
    return rows


@router.post("/settings/libraries/{library_id}")
async def library_save(library_id: int, request: Request, session: Session = Depends(require_csrf)):
    state = request.app.state
    current = _library_or_404(request, library_id)
    if current is None:
        return PlainTextResponse("not found", status_code=404)
    form = await request.form()
    try:
        new = _from_form(form, current)
    except ValidationError as exc:
        return _page(request, "library_form.html", 400, library=current, new=False, errors=_errors(exc))
    settings = state.store.load()
    conflicts = library_conflicts(new, state.repo.libraries())
    if settings.uses_qbit and not new.category:
        conflicts.append("Category: required, because Periodica finds downloads by their qBittorrent category")
    if conflicts:
        return _page(request, "library_form.html", 400, library=new, new=False, errors=conflicts)
    changed = {k for k in LIBRARY_FORM_FIELDS if getattr(current, k) != getattr(new, k)}
    if not changed:
        return RedirectResponse(f"/settings/libraries/{library_id}?msg=saved", status_code=303)
    if changed & RISKY:
        preview = await asyncio.to_thread(state.scanner.run, True, None, None, [new])
        token = secrets.token_urlsafe(24)
        state.repo.set_state(f"pending_library:{session.token_hash}", {
            "token": token, "expires": time.time() + PENDING_TTL, "library": new.model_dump(),
        })
        changes = [(LABELS.get(k, k), getattr(current, k), getattr(new, k)) for k in sorted(changed)]
        return render(request, "settings_confirm.html", nav="settings", section="libraries", preview=preview,
                      changes=changes, token=token, category_changed=bool(changed & RISKY),
                      folder_mode=not settings.uses_qbit, disarms=settings.retention_armed,
                      confirm_action=f"/settings/libraries/{library_id}/confirm")
    _commit(request, session, current, new, changed)
    return RedirectResponse(f"/settings/libraries/{library_id}?msg=saved", status_code=303)


@router.post("/settings/libraries/{library_id}/confirm")
async def library_confirm(library_id: int, request: Request, session: Session = Depends(require_csrf)):
    state = request.app.state
    form = await request.form()
    key = f"pending_library:{session.token_hash}"
    pending = state.repo.get_state(key)
    state.repo.delete_state(key)
    token = form.get("token")
    back = f"/settings/libraries/{library_id}"
    if (not pending or not isinstance(token, str) or not secrets.compare_digest(pending["token"], token)
            or pending["expires"] < time.time() or pending["library"].get("id") != library_id
            or form.get("action") != "confirm"):
        return RedirectResponse(f"{back}?msg=cancelled", status_code=303)
    current = _library_or_404(request, library_id)
    if current is None:
        return PlainTextResponse("not found", status_code=404)
    try:
        new = Library(**pending["library"])
    except ValidationError:
        return RedirectResponse(f"{back}?msg=cancelled", status_code=303)
    if library_conflicts(new, state.repo.libraries()):
        return RedirectResponse(f"{back}?msg=cancelled", status_code=303)
    changed = {k for k in LIBRARY_FORM_FIELDS if getattr(current, k) != getattr(new, k)}
    _commit(request, session, current, new, changed)
    return RedirectResponse(f"{back}?msg=saved", status_code=303)


def _commit(request: Request, session: Session, current: Library, new: Library, changed: set[str]) -> None:
    state = request.app.state
    if "dest_dir" in changed:
        from ..linker import prepare_library_root
        from ..paths import UnsafePathError

        try:
            prepare_library_root(new.dest_dir, state.env.data_root)
        except (OSError, UnsafePathError) as exc:
            state.repo.log("warning", f"Could not prepare the library folder {new.dest_dir}: {exc}")
    state.repo.update_library(new)
    settings = state.store.load()
    if changed & (RISKY | {"retention_days"}):
        _disarm(request, settings, f"library '{new.name}' changed")
    if changed & RISKY:
        cancelled = _cancel_pending_for_library(state.repo, current.id, "library category or folders changed")
        if cancelled:
            state.repo.log("warning", f"Cancelled {cancelled} pending deletion(s) of library '{new.name}'")
    labels = ", ".join(sorted(LABELS.get(k, k) for k in changed))
    state.repo.log("info", f"Library '{new.name}' changed by {session.username}: {labels}")
    log.info("library %r changed by %s: %s", new.name, session.username, labels)
    state.scan_event.set()


def _cancel_pending_for_library(repo, library_id: int, reason: str) -> int:
    torrents = {t["hash"] for t in repo.torrents() if t["library_id"] == library_id}
    issues = {str(i.id) for i in repo.linked_issues({library_id})}
    cancelled = 0
    for item in repo.pending_deletions():
        if (item["kind"] == "torrent" and item["ref"] in torrents) or (item["kind"] == "issue"
                                                                        and item["ref"] in issues):
            repo.cancel_pending_for(item["kind"], item["ref"], reason)
            cancelled += 1
    return cancelled


# --- delete -------------------------------------------------------------------------------------------
@router.get("/settings/libraries/{library_id}/delete")
async def library_delete_page(library_id: int, request: Request, session: Session = Depends(require_session)):
    state = request.app.state
    library = _library_or_404(request, library_id)
    if library is None:
        return PlainTextResponse("not found", status_code=404)
    return _page(request, "library_delete.html", library=library,
                 issues=state.repo.library_issue_counts().get(library_id, 0),
                 only_one=len(state.repo.libraries()) == 1)


@router.post("/settings/libraries/{library_id}/delete")
async def library_delete(library_id: int, request: Request, session: Session = Depends(require_csrf)):
    state = request.app.state
    library = _library_or_404(request, library_id)
    if library is None:
        return PlainTextResponse("not found", status_code=404)
    form = await request.form()
    if len(state.repo.libraries()) == 1:
        return PlainTextResponse("The last library cannot be deleted.", status_code=409)
    if form.get("confirm") != "yes":
        return RedirectResponse(f"/settings/libraries/{library_id}/delete", status_code=303)
    if not state.scanner.try_lock():
        return _page(request, "library_delete.html", 409, library=library,
                     issues=state.repo.library_issue_counts().get(library_id, 0), only_one=False,
                     flash_error="A scan is running right now; try again in a moment.")
    try:
        state.repo.delete_library(library_id, f"library '{library.name}' deleted by {session.username}")
    finally:
        state.scanner.unlock()
    state.repo.log("warning", f"Library '{library.name}' deleted by {session.username}; its files in "
                              f"{library.dest_dir} and its downloads were left alone")
    log.warning("library %r deleted by %s", library.name, session.username)
    return RedirectResponse("/settings/libraries?msg=library_deleted", status_code=303)


# --- Jellyfin -----------------------------------------------------------------------------------------
@router.post("/settings/libraries/{library_id}/jellyfin-scan")
async def library_jellyfin_scan(library_id: int, request: Request, session: Session = Depends(require_csrf)):
    state = request.app.state
    library = _library_or_404(request, library_id)
    if library is None:
        return PlainTextResponse("not found", status_code=404)
    settings = state.store.load()
    if settings.offline:
        from ..settings import OFFLINE_MESSAGE

        return PlainTextResponse(OFFLINE_MESSAGE, status_code=409)
    if not settings.jellyfin_active:
        return RedirectResponse(f"/settings/libraries/{library_id}?msg=jf_off", status_code=303)
    state.scanner.jellyfin.request(settings, f"manual library scan by {session.username}",
                                   targets=[library.jellyfin_target])
    return RedirectResponse(f"/settings/libraries/{library_id}?msg=jf_scan", status_code=303)

# --- file name patterns ------------------------------------------------------------------------------
def _unmatched_names(repo, library_id: int) -> list[dict]:
    from ..formats import book_suffix
    from ..scanner import UNRECOGNISED_REASON

    return [u for u in repo.unmatched()
            if u.get("library_id") == library_id and u["reason"] == UNRECOGNISED_REASON
            and book_suffix(u["path"])]


def _describe(parsed) -> str:
    from ..issues import IssueId
    from ..patterns import KIND_LABELS

    if parsed.issue_date is not None:
        issue = IssueId.for_day(parsed.issue_date)
    elif parsed.period == "month":
        issue = IssueId.for_month(parsed.year, parsed.number)
    else:
        issue = IssueId.for_number(parsed.year, parsed.number)
    return f"{parsed.paper} {issue.label} ({KIND_LABELS.get(issue.period, issue.period)})"


@router.get("/settings/libraries/{library_id:int}/patterns/new")
async def pattern_new(library_id: int, request: Request, torrent: str = "", path: str = "",
                      session: Session = Depends(require_session)):
    from ..patterns import suggest_pattern

    state = request.app.state
    library = _library_or_404(request, library_id)
    if library is None:
        return PlainTextResponse("not found", status_code=404)
    filename = Path(path).name if path else ""
    pattern, publication, _kind = suggest_pattern(filename) if filename else ("", "", "")
    return _page(request, "pattern_form.html", library=library, filename=filename, pattern=pattern,
                 publication=publication, torrent=torrent,
                 publications=sorted({p.label for p in state.repo.papers(library_id)}),
                 patterns=state.repo.name_patterns(library_id))


@router.post("/settings/libraries/{library_id:int}/patterns/preview")
async def pattern_preview(library_id: int, request: Request, session: Session = Depends(require_csrf)):
    from ..patterns import compile_pattern, validate_publication

    state = request.app.state
    if _library_or_404(request, library_id) is None:
        return PlainTextResponse("not found", status_code=404)
    form = await request.form()
    filename = str(form.get("filename") or "")
    try:
        pattern = compile_pattern(str(form.get("pattern") or ""))
        publication = validate_publication(pattern, str(form.get("publication") or ""))
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": str(exc)[:1].upper() + str(exc)[1:] + "."})
    result: dict = {"ok": True, "kind": pattern.kind, "matches": []}
    if filename:
        parsed = pattern.parse(filename, publication)
        if parsed is None:
            result.update(ok=False, message=f"{filename} does not match this pattern.")
        else:
            result["message"] = f"{filename} → {_describe(parsed)}"
    else:
        result["message"] = "The pattern is valid."
    for row in _unmatched_names(state.repo, library_id):
        name = Path(row["path"]).name
        if name == filename:
            continue
        parsed = pattern.parse(name, publication)
        if parsed is not None:
            result["matches"].append(f"{name} → {_describe(parsed)}")
    return JSONResponse(result)


@router.post("/settings/libraries/{library_id:int}/patterns")
async def pattern_save(library_id: int, request: Request, session: Session = Depends(require_csrf)):
    from ..patterns import MAX_PATTERNS_PER_LIBRARY, compile_pattern, validate_publication

    state = request.app.state
    library = _library_or_404(request, library_id)
    if library is None:
        return PlainTextResponse("not found", status_code=404)
    form = await request.form()
    filename = str(form.get("filename") or "")
    context = {"library": library, "filename": filename, "pattern": str(form.get("pattern") or ""),
               "publication": str(form.get("publication") or ""), "torrent": str(form.get("torrent") or ""),
               "publications": sorted({p.label for p in state.repo.papers(library_id)}),
               "patterns": state.repo.name_patterns(library_id)}
    try:
        pattern = compile_pattern(context["pattern"])
        publication = validate_publication(pattern, context["publication"])
        if filename and pattern.parse(filename, publication) is None:
            raise ValueError(f"{filename} does not match this pattern")
        existing = {row["pattern"] for row in context["patterns"]}
        if pattern.text not in existing and len(existing) >= MAX_PATTERNS_PER_LIBRARY:
            raise ValueError(f"a library can have at most {MAX_PATTERNS_PER_LIBRARY} patterns")
    except ValueError as exc:
        return _page(request, "pattern_form.html", 400, errors=[str(exc)], **context)
    state.repo.add_name_pattern(library_id, pattern.text, publication)
    if publication and pattern.kind in ("month", "number"):
        # The pattern says what the number is; the publication follows it (and keeps following it).
        state.repo.ensure_paper(publication, library_id)
        state.repo.set_paper_numbering(publication, pattern.kind, library_id)
    target = publication or "names read with {paper}"
    state.repo.log("info", f"{session.username} added the file name pattern '{pattern.text}' for {target} "
                           f"in library '{library.name}'")
    from ..app import request_scan

    request_scan(request.app, f"File name pattern added by {session.username}")
    return RedirectResponse("/unmatched?msg=pattern_added", status_code=303)


@router.post("/settings/libraries/{library_id:int}/patterns/{pattern_id:int}/delete")
async def pattern_delete(library_id: int, pattern_id: int, request: Request, session: Session = Depends(require_csrf)):
    state = request.app.state
    library = _library_or_404(request, library_id)
    if library is None or not state.repo.delete_name_pattern(library_id, pattern_id):
        return PlainTextResponse("not found", status_code=404)
    state.repo.log("info", f"{session.username} deleted a file name pattern of library '{library.name}'; "
                           "issues already linked stay in the library")
    return RedirectResponse(f"/settings/libraries/{library_id}?msg=pattern_deleted#patterns", status_code=303)

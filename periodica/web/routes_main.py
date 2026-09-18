"""Dashboard, publications, issues, deletions, unmatched files, activity and system pages."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse

from .. import __version__, covers
from ..auth import Session
from ..formats import MEDIA_TYPES, book_suffix, find_cover
from ..health import PROBLEMS
from ..linker import is_managed_issue_dir, remove_issue_dir
from ..paths import UnsafePathError, is_within, same_filesystem
from ..retention import APPROVAL_KEY, BLOCKED_KEY, PREVIEW_KEY
from ..scanner import LAST_SCAN_KEY, all_problems
from .connections import connections
from .deps import _ago, _datetime, render, require_csrf, require_session

log = logging.getLogger(__name__)
router = APIRouter()


def _back(path: str, msg: str) -> RedirectResponse:
    return RedirectResponse(f"{path}?msg={msg}", status_code=303)


LIBRARY_COOKIE = "periodica_library"


def _library_filter(request: Request, libraries) -> int | None:
    """The library chosen with ?library=<id> (or remembered in a cookie); None means all libraries."""
    if len(libraries) < 2:
        return None
    raw = request.query_params.get("library")
    if raw is None:
        raw = request.cookies.get(LIBRARY_COOKIE, "all")
    ids = {lib.id for lib in libraries}
    try:
        chosen = int(raw)
    except (TypeError, ValueError):
        return None
    return chosen if chosen in ids else None


def _filtered(request: Request, template: str, libraries, selected: int | None, **context):
    """Render a page with the library filter and remember an explicit choice."""
    response = render(request, template, libraries=libraries, selected_library=selected,
                      library_names={lib.id: lib.name for lib in libraries}, **context)
    if "library" in request.query_params:
        response.set_cookie(LIBRARY_COOKIE, str(selected or "all"), max_age=365 * 86400, httponly=True,
                            samesite="strict", secure=request.app.state.env.secure_cookies, path="/")
    return response


def _library_root(request: Request, library_id: int) -> Path | None:
    library = request.app.state.repo.library(library_id)
    return Path(library.dest_dir) if library else None


@router.get("/")
async def dashboard(request: Request, session: Session = Depends(require_session)):
    state = request.app.state
    settings = state.store.load()
    libraries = state.repo.libraries()
    overview = state.repo.library_overview()
    now = time.time()
    enabled = [lib for lib in libraries if lib.enabled]
    downloads = [overview.get(lib.id, {}).get("last_download") for lib in enabled]
    last_download = max((d for d in downloads if d), default=None)
    stale = []
    if settings.stale_download_hours:
        for lib in enabled:
            last = overview.get(lib.id, {}).get("last_download")
            if last and now - last > settings.stale_download_hours * 3600:
                stale.append({"library": lib, "last": last})
    return render(
        request, "dashboard.html",
        settings=settings,
        scan_queued=state.scan_trigger is not None,
        last_download=last_download,
        stale=stale,
        now=now,
        problems=all_problems(settings, state.env, libraries),
        last_scan=state.repo.get_state(LAST_SCAN_KEY),
        running=state.scanner.running,
        counts=state.repo.issue_counts(),
        papers=len(state.repo.papers()),
        pending=len(state.repo.pending_deletions()),
        unmatched=len(state.repo.unmatched()),
        blocked=state.repo.get_state(BLOCKED_KEY),
        errors=state.repo.activity(limit=8, level="error"),
        recent=state.repo.activity(limit=12),
        connections=connections(settings, state.repo),
        libraries=libraries,
        overview=overview,
        damaged=sum(o.get("problems") or 0 for o in overview.values()),
    )


@router.post("/scan")
async def scan_now(request: Request, session: Session = Depends(require_csrf)):
    from ..app import request_scan

    request_scan(request.app, f"Manual scan by {session.username}")
    if request.headers.get("x-csrf-token"):
        return JSONResponse({"status": "queued"})
    return _back("/", "scan")


@router.get("/scan/status")
async def scan_status(request: Request, session: Session = Depends(require_session)):
    state = request.app.state
    last = state.repo.get_state(LAST_SCAN_KEY) or {}
    return JSONResponse({
        "running": state.scanner.running,
        "queued": state.scan_trigger is not None,
        "requested_at": state.scan_requested_at,
        "finished": last.get("finished"),
    })


def _unmatched_with_choices(repo) -> list[dict]:
    """Unmatched rows, with the newspaper to ask about where the scanner could not tell month from number."""
    from ..parser import parse_issue_filename
    from ..scanner import UNCLEAR_REASON, UNRECOGNISED_REASON

    items = []
    for row in repo.unmatched():
        item = dict(row)
        parsed = parse_issue_filename(Path(row["path"]).name) if row["reason"].startswith(UNCLEAR_REASON) else None
        item["undecided_paper"] = parsed.paper if parsed and not parsed.is_daily else None
        item["library_id"] = row.get("library_id") or 1
        # Only an issue file whose name we cannot read may be ignored. Unsafe paths, files outside the source
        # folder and unexpected file types are refusals for safety, not naming gaps.
        item["ignorable"] = row["reason"] == UNRECOGNISED_REASON and book_suffix(row["path"]) is not None
        items.append(item)
    return items


# --- newspapers ---------------------------------------------------------------------------------
@router.get("/papers")
async def papers(request: Request, session: Session = Depends(require_session)):
    state = request.app.state
    repo = state.repo
    libraries = repo.libraries()
    selected = _library_filter(request, libraries)
    undecided = {(u["library_id"], u["undecided_paper"]) for u in _unmatched_with_choices(repo)
                 if u["undecided_paper"]}
    papers = repo.papers(selected)
    groups: list[dict] = [
        {"key": "undecided", "title": "Needs a choice", "papers": [],
         "note": "Could not tell whether the number in these names is a month or an issue number. "
                 "Open one to choose, or use the buttons on the Unmatched page."},
        {"key": "day", "title": "Daily", "papers": [], "note": ""},
        {"key": "month", "title": "Monthly", "papers": [], "note": ""},
        {"key": "number", "title": "Numbered", "papers": [], "note": ""},
    ]
    by_key = {group["key"]: group for group in groups}
    for paper in papers:
        kind = paper.effective_numbering or ("undecided" if (paper.library_id, paper.name) in undecided else "day")
        by_key[kind]["papers"].append(paper)
    return _filtered(request, "papers.html", libraries, selected, papers=papers, groups=groups,
                     stats=repo.paper_stats(), settings=state.store.load())


@router.get("/papers/{name}")
async def paper_legacy(name: str, request: Request, session: Session = Depends(require_session)):
    """Links from before libraries: open the publication if only one library has that name."""
    matches = [p for p in request.app.state.repo.papers() if p.name == name]
    if len(matches) != 1:
        return PlainTextResponse("not found", status_code=404)
    return RedirectResponse(f"/papers/{matches[0].library_id}/{quote(name)}", status_code=303)


def _paper_page(request: Request, paper, status_code: int = 200, **extra):
    repo = request.app.state.repo
    return render(request, "paper.html", status_code, nav="papers", paper=paper,
                  issues=repo.issues_for_paper(paper.name, paper.library_id),
                  library=repo.library(paper.library_id), show_library=len(repo.libraries()) > 1,
                  settings=request.app.state.store.load(), problem_texts=PROBLEMS, **extra)


def _paper_url(paper) -> str:
    return f"/papers/{paper.library_id}/{quote(paper.name)}"


@router.get("/papers/{library_id:int}/{name}")
async def paper_detail(library_id: int, name: str, request: Request, session: Session = Depends(require_session)):
    paper = request.app.state.repo.paper(name, library_id)
    if paper is None:
        return PlainTextResponse("not found", status_code=404)
    return _paper_page(request, paper)


@router.post("/papers/{library_id:int}/{name}")
async def paper_update(library_id: int, name: str, request: Request, display_name: str = Form(""),
                       enabled: str | None = Form(None), retention_days: str = Form(""),
                       session: Session = Depends(require_csrf)):
    repo = request.app.state.repo
    paper = repo.paper(name, library_id)
    if paper is None:
        return PlainTextResponse("not found", status_code=404)
    display = display_name.strip() or None
    days: int | None = None
    error = None
    if display is not None:
        from ..paths import sanitize_component

        try:
            if sanitize_component(display) != display:
                error = "Display name contains characters that are not allowed in folder names."
        except UnsafePathError:
            error = "Display name is not valid."
    if retention_days.strip():
        try:
            days = int(retention_days)
            if not 1 <= days <= 3650:
                raise ValueError
        except ValueError:
            error = "Retention override must be a whole number of days between 1 and 3650."
    if error:
        return _paper_page(request, paper, 400, flash_error=error)
    repo.update_paper(name, display, enabled is not None, days, library_id)
    repo.log("info", f"Publication {name!r} updated (display name={display!r}, enabled={enabled is not None}, "
                     f"retention override={days})")
    return RedirectResponse(f"{_paper_url(paper)}?msg=paper", status_code=303)


@router.post("/papers/{library_id:int}/{name}/numbering")
async def paper_numbering(library_id: int, name: str, request: Request, numbering: str = Form(""),
                          back: str = Form(""), session: Session = Depends(require_csrf)):
    """Monthly or numbered, chosen by the user; applied (and existing issues relabelled) by the next scan."""
    repo = request.app.state.repo
    paper = repo.paper(name, library_id)
    if paper is None:
        return PlainTextResponse("not found", status_code=404)
    choice = numbering if numbering in ("month", "number") else None
    repo.set_paper_numbering(name, choice, library_id)
    words = {"month": "monthly", "number": "numbered", None: "detected automatically"}
    repo.log("info", f"{name} is now counted as {words[choice]} (by {session.username})")
    from ..app import request_scan

    request_scan(request.app, f"Numbering of {name} changed by {session.username}")
    target = "/unmatched" if back == "unmatched" else _paper_url(paper)
    return RedirectResponse(f"{target}?msg=numbering", status_code=303)


# --- issues -------------------------------------------------------------------------------------
def _issue_or_404(request: Request, issue_id: int):
    return request.app.state.repo.issue(issue_id)


@router.get("/issues/{issue_id}/cover")
async def issue_cover(issue_id: int, request: Request, session: Session = Depends(require_session)):
    issue = _issue_or_404(request, issue_id)
    if issue is None or not issue.dest_dir:
        return PlainTextResponse("not found", status_code=404)
    cover = find_cover(Path(issue.dest_dir))
    dest_root = _library_root(request, issue.library_id)
    if (cover is None or dest_root is None or not is_within(cover, dest_root)
            or not is_managed_issue_dir(Path(issue.dest_dir), dest_root)):
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(cover, media_type=MEDIA_TYPES[cover.suffix])


@router.post("/issues/{issue_id}/{action}")
async def issue_action(issue_id: int, action: str, request: Request, session: Session = Depends(require_csrf),
                       back_to: str = Form("", alias="back")):
    repo = request.app.state.repo
    issue = _issue_or_404(request, issue_id)
    if issue is None:
        return PlainTextResponse("not found", status_code=404)
    back = "/health" if back_to == "health" else f"/papers/{issue.library_id}/{quote(issue.paper)}"
    dest_root = _library_root(request, issue.library_id)
    if dest_root is None and action in ("cover", "remove"):
        return PlainTextResponse("the issue's library no longer exists", status_code=409)
    label = f"{issue.paper} {issue.issue_label}"
    if action == "cover":
        if issue.dest_dir:
            cover = find_cover(Path(issue.dest_dir))
            if dest_root is not None and cover is not None and is_managed_issue_dir(Path(issue.dest_dir), dest_root):
                cover.unlink()
        repo.reset_cover(issue.id)
        request.app.state.scan_event.set()
        return RedirectResponse(f"{back}?msg=cover", status_code=303)
    if action == "keep":
        repo.set_issue_keep(issue.id, not issue.keep)
        repo.log("info", f"{label}: keep={'off' if issue.keep else 'on'}")
        return RedirectResponse(f"{back}?msg={'unkept' if issue.keep else 'kept'}", status_code=303)
    if action == "remove":
        try:
            if issue.dest_dir and dest_root is not None:
                remove_issue_dir(Path(issue.dest_dir), dest_root)
        except (UnsafePathError, OSError) as exc:
            repo.log("error", f"Could not remove {label}: {exc}")
            return RedirectResponse(back, status_code=303)
        repo.set_issue_status(issue.id, "excluded")
        repo.log("info", f"{label} removed from the library by {session.username} (will not be relinked)")
        return RedirectResponse(f"{back}?msg=removed", status_code=303)
    if action == "fix":
        from ..app import request_scan

        if repo.set_fix_requested([issue.id]):
            request_scan(request.app, f"Library health: fix requested by {session.username}")
        return RedirectResponse(f"{back}?msg=fixing", status_code=303)
    if action == "restore":
        if issue.status in {"excluded", "removed", "error"}:
            repo.set_issue_status(issue.id, "pending")
            request.app.state.scan_event.set()
        return RedirectResponse(f"{back}?msg=restored", status_code=303)
    return PlainTextResponse("unknown action", status_code=400)


# --- deletions ----------------------------------------------------------------------------------
def _needs_qbit(settings) -> PlainTextResponse | None:
    """Deleting (automatic or manual) is refused outright in folder mode."""
    from ..manual_delete import NEEDS_QBIT

    if settings.uses_qbit:
        return None
    return PlainTextResponse(NEEDS_QBIT[0].upper() + NEEDS_QBIT[1:] + ".", status_code=409)


@router.get("/deletions")
async def deletions(request: Request, session: Session = Depends(require_session)):
    state = request.app.state
    libraries = state.repo.libraries()
    selected = _library_filter(request, libraries)

    def mine(rows):
        return [r for r in rows if selected is None or r.get("library_id") == selected]

    return _filtered(
        request, "deletions.html", libraries, selected,
        settings=state.store.load(),
        pending=mine(state.repo.pending_deletions()),
        torrents=mine(state.repo.present_torrents()),
        history=mine(state.repo.deletion_history(100)),
        preview=state.repo.get_state(PREVIEW_KEY),
        blocked=state.repo.get_state(BLOCKED_KEY),
        now=time.time(),
    )


@router.post("/deletions/arm")
async def deletions_arm(request: Request, confirm: str | None = Form(None),
                        session: Session = Depends(require_csrf)):
    state = request.app.state
    settings = state.store.load()
    if refused := _needs_qbit(settings):
        return refused
    if not settings.retention_enabled or confirm != "yes":
        return RedirectResponse("/deletions", status_code=303)
    settings.retention_armed = True
    state.store.save(settings)
    log.warning("automatic delete armed by %s", session.username)
    days = ", ".join(f"{lib.name} {lib.retention_days} d" for lib in state.repo.libraries(enabled_only=True))
    state.repo.log("warning", f"Automatic delete armed by {session.username} "
                              f"({days}; grace {settings.grace_hours} h)")
    state.scan_event.set()
    return _back("/deletions", "armed")


@router.post("/deletions/disarm")
async def deletions_disarm(request: Request, session: Session = Depends(require_csrf)):
    state = request.app.state
    settings = state.store.load()
    settings.retention_armed = False
    state.store.save(settings)
    state.repo.cancel_pending("disarmed by user")
    log.info("automatic delete disarmed by %s", session.username)
    state.repo.log("info", f"Automatic delete disarmed by {session.username}")
    return _back("/deletions", "disarmed")


@router.post("/deletions/approve")
async def deletions_approve(request: Request, session: Session = Depends(require_csrf)):
    state = request.app.state
    if refused := _needs_qbit(state.store.load()):
        return refused
    blocked = state.repo.get_state(BLOCKED_KEY)
    if not blocked:
        return RedirectResponse("/deletions", status_code=303)
    state.repo.set_state(APPROVAL_KEY, {"torrents": blocked["torrents"], "issues": blocked["issues"],
                                        "expires": time.time() + 3600})
    state.repo.log("warning", f"{session.username} approved a large deletion batch "
                              f"({blocked['torrents']} torrents, {blocked['issues']} issues)")
    state.scan_event.set()
    return _back("/deletions", "approved")


@router.post("/deletions/{deletion_id}/{action}")
async def deletion_action(deletion_id: int, action: str, request: Request,
                          session: Session = Depends(require_csrf)):
    repo = request.app.state.repo
    item = repo.deletion(deletion_id)
    if item is None or item["status"] != "pending":
        return RedirectResponse("/deletions", status_code=303)
    if action == "keep":
        if item["kind"] == "torrent":
            repo.set_torrent_keep(item["ref"], True)
        else:
            repo.set_issue_keep(int(item["ref"]), True)
        repo.finish_deletion(deletion_id, "kept", f"kept by {session.username}")
        repo.log("info", f"{session.username} kept {item['label']}")
        return _back("/deletions", "kept")
    if action == "now":
        if refused := _needs_qbit(request.app.state.store.load()):
            return refused
        repo.make_due_now(deletion_id)
        repo.log("warning", f"{session.username} requested immediate deletion of {item['label']}")
        request.app.state.scan_event.set()
        return _back("/deletions", "due")
    return PlainTextResponse("unknown action", status_code=400)


@router.get("/torrents/{torrent_hash}/delete")
async def torrent_delete_confirm(torrent_hash: str, request: Request, session: Session = Depends(require_session)):
    from ..manual_delete import ManualDeleteError, check_deletable

    state = request.app.state
    settings = state.store.load()
    if refused := _needs_qbit(settings):
        return refused
    try:
        torrent = check_deletable(state.repo, settings, torrent_hash)
        error = None
    except ManualDeleteError as exc:
        torrent, error = state.repo.torrent(torrent_hash), str(exc)
    if torrent is None:
        return PlainTextResponse("not found", status_code=404)
    issues = [i for i in state.repo.issues_for_torrent(torrent_hash) if i.status == "linked"]
    return render(request, "torrent_delete.html", nav="deletions", torrent=torrent, issues=issues,
                  error=error, settings=settings)


@router.post("/torrents/{torrent_hash}/delete")
async def torrent_delete(torrent_hash: str, request: Request, confirm: str | None = Form(None),
                         session: Session = Depends(require_csrf)):
    from ..manual_delete import ManualDeleteError, delete_torrent_now

    state = request.app.state
    settings = state.store.load()
    if refused := _needs_qbit(settings):
        return refused
    if confirm != "yes":
        return RedirectResponse(f"/torrents/{quote(torrent_hash)}/delete", status_code=303)
    try:
        await asyncio.to_thread(delete_torrent_now, state.scanner, state.repo, settings, state.qbit_factory,
                                state.jellyfin_factory, torrent_hash, session.username)
    except ManualDeleteError as exc:
        torrent = state.repo.torrent(torrent_hash)
        if torrent is None:
            return PlainTextResponse(str(exc), status_code=409)
        issues = [i for i in state.repo.issues_for_torrent(torrent_hash) if i.status == "linked"]
        return render(request, "torrent_delete.html", 409, nav="deletions", torrent=torrent, issues=issues,
                      error=str(exc), settings=settings)
    return _back("/deletions", "deleted")


@router.post("/torrents/{torrent_hash}/unkeep")
async def torrent_unkeep(torrent_hash: str, request: Request, session: Session = Depends(require_csrf)):
    request.app.state.repo.set_torrent_keep(torrent_hash, False)
    return _back("/system", "unkept")


# --- library health -------------------------------------------------------------------------------
@router.get("/health")
async def library_health(request: Request, session: Session = Depends(require_session)):
    repo = request.app.state.repo
    libraries = repo.libraries()
    selected = _library_filter(request, libraries)
    items = repo.issue_problems(None if selected is None else {selected})
    for item in items:
        item["fixable"] = bool(item["download_present"])
        item["problem_text"] = PROBLEMS.get(item["problem"], item["problem"])
    return _filtered(request, "health.html", libraries, selected, items=items,
                     last_scan=repo.get_state(LAST_SCAN_KEY))


@router.post("/health/fix")
async def library_health_fix(request: Request, session: Session = Depends(require_csrf)):
    from ..app import request_scan

    repo = request.app.state.repo
    form = await request.form()
    wanted: set[int] = set()
    for raw in form.getlist("issue_id"):
        try:
            wanted.add(int(str(raw)))
        except ValueError:
            return PlainTextResponse("bad issue id", status_code=400)
    library = str(form.get("library") or "")
    if form.get("all"):
        scope = {int(library)} if library.isdigit() else None
        wanted = {p["id"] for p in repo.issue_problems(scope) if p["download_present"]}
    fixable = {p["id"] for p in repo.issue_problems() if p["download_present"]}
    count = repo.set_fix_requested(sorted(wanted & fixable))
    if count:
        repo.log("info", f"Library health: {session.username} asked to fix {count} issue(s)")
        request_scan(request.app, f"Library health: fix requested by {session.username}")
    return RedirectResponse(f"/health?msg={'fixing' if count else 'nothing_to_fix'}", status_code=303)


# --- other pages --------------------------------------------------------------------------------
@router.get("/unmatched")
async def unmatched(request: Request, session: Session = Depends(require_session)):
    repo = request.app.state.repo
    libraries = repo.libraries()
    selected = _library_filter(request, libraries)
    items = [u for u in _unmatched_with_choices(repo) if selected is None or u["library_id"] == selected]
    ignored = [i for i in repo.ignored_files() if selected is None or i["library_id"] == selected]
    return _filtered(request, "unmatched.html", libraries, selected, items=items, ignored=ignored,
                     settings=request.app.state.store.load())


@router.post("/unmatched/ignore")
async def unmatched_ignore(request: Request, torrent_hash: str = Form(...), path: str = Form(...),
                           session: Session = Depends(require_csrf)):
    """Stop an unrecognised file from protecting its torrent. The next scan re-evaluates the torrent."""
    repo = request.app.state.repo
    row = next((u for u in _unmatched_with_choices(repo)
                if u["torrent_hash"] == torrent_hash and u["path"] == path), None)
    if row is None or not row["ignorable"]:
        return PlainTextResponse("this file cannot be ignored", status_code=400)
    repo.ignore_file(torrent_hash, path, session.username)
    name = row.get("torrent_name") or torrent_hash
    log.info("file %s in %s ignored by %s", path, torrent_hash, session.username)
    repo.log("warning", f"{session.username} ignored {Path(path).name} in {name}: it no longer keeps the torrent "
                        "from being deleted automatically, and is deleted with it")
    from ..app import request_scan

    request_scan(request.app, f"File ignored by {session.username}")
    return _back("/unmatched", "ignored")


@router.post("/unmatched/unignore")
async def unmatched_unignore(request: Request, torrent_hash: str = Form(...), path: str = Form(...),
                             session: Session = Depends(require_csrf)):
    repo = request.app.state.repo
    if not repo.unignore_file(torrent_hash, path):
        return PlainTextResponse("not found", status_code=404)
    repo.log("info", f"{session.username} stopped ignoring {Path(path).name}; it protects its torrent again")
    from ..app import request_scan

    request_scan(request.app, f"File no longer ignored by {session.username}")
    return _back("/unmatched", "unignored")


@router.get("/activity")
async def activity(request: Request, level: str = "", session: Session = Depends(require_session)):
    level = level if level in {"info", "warning", "error"} else ""
    return render(request, "activity.html", items=request.app.state.repo.activity(500, level or None),
                  level=level)


def _log_query(level: str, source: str, search: str) -> tuple[str, bool, str]:
    return (level if level in {"info", "warning", "error"} else "", source == "app", search[:100])


@router.get("/activity/logs")
async def logs_moved(request: Request, session: Session = Depends(require_session)):
    """Logs moved under System; keep the old address working."""
    query = request.url.query
    return RedirectResponse("/system/logs" + (f"?{query}" if query else ""), status_code=307)


@router.get("/system/logs")
async def logs(request: Request, level: str = "", source: str = "", q: str = "",
               session: Session = Depends(require_session)):
    from .. import logbuffer

    logbuffer.enforce_level()
    level, app_only, search = _log_query(level, source, q)
    lines = logbuffer.BUFFER.lines(level, limit=1000, app_only=app_only, search=search)
    settings = request.app.state.store.load()
    return render(request, "logs.html", nav="logs", level=level, source="app" if app_only else "",
                  q=search, lines=lines, detail=logbuffer.current_level(), stored_level=settings.log_level,
                  temporary=logbuffer.debug_expires_at() > 0,
                  debug_until=logbuffer.debug_expires_at(), now=time.time(),
                  capacity=logbuffer.BUFFER.capacity, cursor=lines[0].seq if lines else 0,
                  file_size=settings.log_file_mb,
                  files=logbuffer.log_files(request.app.state.env.config_dir))


@router.get("/system/logs/tail")
async def logs_tail(request: Request, after: int = 0, level: str = "", source: str = "", q: str = "",
                    session: Session = Depends(require_session)):
    """New lines since ``after`` for the live view (polled by the Logs page)."""
    from .. import logbuffer

    logbuffer.enforce_level()
    level, app_only, search = _log_query(level, source, q)
    lines, newest, missed = logbuffer.BUFFER.since(max(0, after), level, limit=500,
                                                   app_only=app_only, search=search)
    return JSONResponse({
        "cursor": newest,
        "missed": missed,
        "detail": logbuffer.current_level(),
        "temporary": logbuffer.debug_expires_at() > 0,
        "debug_seconds_left": max(0, int(logbuffer.debug_expires_at() - time.time())),
        "lines": [{"seq": ln.seq, "ts": _datetime(ln.ts), "at": ln.ts, "level": ln.level,
                   "logger": ln.logger, "message": ln.message} for ln in lines],
    })


@router.post("/system/logs/level")
async def logs_level(request: Request, detail: str = Form("info"), session: Session = Depends(require_csrf)):
    """Switch between info and debug from the page; debug expires on its own."""
    from .. import logbuffer

    state = request.app.state
    stored = state.store.load().log_level
    if detail == "debug":
        logbuffer.set_level("debug", temporary=True)
        state.repo.log("info", f"Debug logging turned on by {session.username} for "
                               f"{logbuffer.DEBUG_SECONDS // 60} min")
    else:
        # Back to whatever Settings says, which may itself be debug.
        logbuffer.set_level(stored)
        state.repo.log("info", f"Debug logging turned off by {session.username}")
    keep = {k: v for k, v in request.query_params.items() if k in {"level", "source", "q"} and v}
    query = urlencode({"msg": "loglevel", **keep})
    return RedirectResponse(f"/system/logs?{query}", status_code=303)


@router.get("/system/logs/file")
async def logs_file(request: Request, name: str = "", session: Session = Depends(require_session)):
    """Download one rotated log file. Only our own files, by exact name, never a path."""
    from .. import logbuffer

    directory = logbuffer.log_dir(request.app.state.env.config_dir)
    known = {entry[0] for entry in logbuffer.log_files(request.app.state.env.config_dir)}
    if name not in known:
        return PlainTextResponse("not found", status_code=404)
    path = directory / name
    if not is_within(path, directory) or path.is_symlink() or not path.is_file():
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(path, media_type="text/plain", filename=name)


@router.get("/system/logs/download")
async def logs_download(request: Request, level: str = "", source: str = "", q: str = "",
                        session: Session = Depends(require_session)):
    from .. import logbuffer

    level, app_only, search = _log_query(level, source, q)
    lines = logbuffer.BUFFER.lines(level, limit=5000, app_only=app_only, search=search)
    body = "".join(
        f"{datetime.fromtimestamp(ln.ts).isoformat(timespec='seconds')} {ln.level.upper():<7} "
        f"{ln.logger}: {ln.message}\n"
        for ln in reversed(lines)
    )
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return PlainTextResponse(body, headers={
        "Content-Disposition": f'attachment; filename="periodica-{stamp}.log"',
    })


@router.get("/system")
async def system(request: Request, session: Session = Depends(require_session)):
    state = request.app.state
    settings = state.store.load()
    checks = []
    libraries = state.repo.libraries()
    enabled = [lib for lib in libraries if lib.enabled]
    several = len(libraries) > 1
    for lib in enabled:
        prefix = f"{lib.name}: " if several else ""
        src, dst = Path(lib.source_dir), Path(lib.dest_dir)
        checks.append((f"{prefix}Source folder exists", src.is_dir(), str(src)))
        dst_parent = dst if dst.exists() else dst.parent
        checks.append((f"{prefix}Destination folder (or parent) exists", dst_parent.is_dir(), str(dst)))
        try:
            same = src.is_dir() and dst_parent.is_dir() and same_filesystem(src, dst_parent)
        except OSError:
            same = False
        checks.append((f"{prefix}Source and destination on same filesystem (hardlinks possible)", same, ""))
        checks.append((f"{prefix}Destination writable", dst_parent.is_dir() and os.access(dst_parent, os.W_OK), ""))
        if settings.jellyfin_active:
            checks.append((f"{prefix}Jellyfin scans only this library's Jellyfin library (not all libraries)",
                           bool(lib.jellyfin_library_id), lib.jellyfin_library_name or "all libraries"))
    damaged = len(state.repo.issue_problems())
    checks.append(("Library health: linked issues are whole", not damaged,
                   f"{damaged} damaged, see Library health" if damaged else ""))
    checks.append(("pdftoppm available (PDF covers)", covers.pdftoppm_available(), ""))
    checks.append(("Running as non-root", hasattr(os, "geteuid") and os.geteuid() != 0, ""))
    if settings.offline:
        checks.append(("Offline mode: no outgoing connections", True, "OFFLINE_MODE=true"))
    from ..scanner import RSS_STATUS_KEY

    if not settings.uses_qbit and settings.api_enabled:
        categories = ", ".join(lib.category for lib in enabled if lib.category)
        checks.append(("Category set for finished-download calls", bool(categories),
                       categories or "set one in Settings → Libraries, or turn the API off"))
    statuses = state.repo.get_state(RSS_STATUS_KEY) or {}
    if "category" in statuses:   # stored before libraries existed
        statuses = {statuses["category"]: statuses}
    for lib in enabled if settings.uses_qbit else []:
        rss = statuses.get(lib.category)
        if not rss:
            continue
        checked = f"checked {_ago(rss.get('checked_at'))}"
        if rss.get("error"):
            checks.append((f"qBittorrent RSS rule adds to category '{lib.category}'", False,
                           f"could not check: {rss['error']} ({checked})"))
            continue
        names = ", ".join(r["name"] for r in rss.get("rules", [])) or "no enabled rule"
        ok = bool(rss.get("rules")) and rss.get("processing") and rss.get("auto_downloading")
        detail = names
        if not rss.get("processing"):
            detail += "; RSS fetching is off in qBittorrent"
        if not rss.get("auto_downloading"):
            detail += "; RSS auto-downloading is off in qBittorrent"
        if rss.get("disabled_rules"):
            detail += "; disabled: " + ", ".join(r["name"] for r in rss["disabled_rules"])
        checks.append((f"qBittorrent RSS rule adds to category '{lib.category}' (optional)", ok,
                       f"{detail} ({checked})"))
    kept = [t for t in state.repo.torrents() if t["keep"]]
    return render(request, "system.html", version=__version__, env=state.env, settings=settings,
                  checks=checks, kept=kept, problems=all_problems(settings, state.env, libraries),
                  connections=connections(settings, state.repo), libraries=libraries)


@router.post("/system/api")
async def toggle_api(request: Request, enabled: str = Form(...), session: Session = Depends(require_csrf)):
    store = request.app.state.store
    settings = store.load()
    if settings.offline:
        return _back("/system", "api_locked")
    settings.api_enabled = enabled == "true"
    store.save(settings)
    state = "on" if settings.api_enabled else "off"
    log.info("scan API turned %s by %s", state, session.username)
    request.app.state.repo.log("info", f"Scan API turned {state} by {session.username}")
    return _back("/system", f"api_{state}")


@router.post("/system/apikey")
async def regenerate_api_key(request: Request, session: Session = Depends(require_csrf)):
    store = request.app.state.store
    settings = store.load()
    settings.api_key = secrets.token_hex(24)
    store.save(settings)
    request.app.state.repo.log("info", f"API key regenerated by {session.username}")
    return _back("/system", "apikey")

"""First-run setup, login, logout, password change and API access."""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..auth import SESSION_COOKIE, AuthError, Session
from .deps import CsrfFailed, client_ip, render, require_csrf, same_origin

log = logging.getLogger(__name__)
router = APIRouter()


def _set_session_cookie(request: Request, response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="strict",
        secure=request.app.state.env.secure_cookies, max_age=30 * 86400, path="/",
    )


@router.get("/setup")
async def setup_page(request: Request):
    if request.app.state.auth.has_users():
        return RedirectResponse("/login", status_code=303)
    return render(request, "setup.html")


@router.post("/setup")
async def setup_submit(request: Request, username: str = Form(...), password: str = Form(...),
                       password2: str = Form(...)):
    auth = request.app.state.auth
    if auth.has_users():
        return RedirectResponse("/login", status_code=303)
    if not same_origin(request):
        raise CsrfFailed()
    if password != password2:
        return render(request, "setup.html", 400, error="Passwords do not match.", username=username)
    try:
        user_id = auth.create_first_user(username.strip(), password)
    except AuthError as exc:
        return render(request, "setup.html", 400, error=str(exc).capitalize() + ".", username=username)
    request.app.state.repo.log("info", f"Admin user {username.strip()!r} created")
    response = RedirectResponse("/settings?section=client", status_code=303)
    _set_session_cookie(request, response, auth.create_session(user_id))
    return response


@router.get("/login")
async def login_page(request: Request):
    if not request.app.state.auth.has_users():
        return RedirectResponse("/setup", status_code=303)
    return render(request, "login.html")


@router.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    auth = request.app.state.auth
    throttle = request.app.state.throttle
    if not same_origin(request):
        raise CsrfFailed()
    keys = [f"ip:{client_ip(request)}", f"user:{username.lower()[:64]}"]
    wait = throttle.retry_after(keys)
    if wait:
        return render(request, "login.html", 429, error=f"Too many failed attempts. Try again in {wait} s.",
                      username=username)
    user_id = auth.verify(username, password)
    if user_id is None:
        throttle.failure(keys)
        log.warning("failed login for %r from %s", username[:64], client_ip(request))
        request.app.state.repo.log("warning", f"Failed login for {username[:64]!r} from {client_ip(request)}")
        return render(request, "login.html", 401, error="Invalid username or password.", username=username)
    throttle.success(keys)
    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(request, response, auth.create_session(user_id))
    return response


@router.post("/logout")
async def logout(request: Request, session: Session = Depends(require_csrf)):
    request.app.state.auth.delete_session(request.cookies.get(SESSION_COOKIE))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.post("/account/password")
async def change_password(request: Request, current: str = Form(...), new: str = Form(...),
                          new2: str = Form(...), session: Session = Depends(require_csrf)):
    if new != new2:
        return RedirectResponse("/settings?section=account&pwerr=mismatch", status_code=303)
    try:
        request.app.state.auth.change_password(session.user_id, current, new, session.token_hash)
    except AuthError:
        return RedirectResponse("/settings?section=account&pwerr=invalid", status_code=303)
    request.app.state.repo.log("info", f"Password changed for {session.username!r}")
    return RedirectResponse("/settings?section=account&msg=password", status_code=303)


@router.post("/api/v1/scan")
async def api_scan(request: Request):
    """Trigger a scan from scripts (e.g. qBittorrent's "run on torrent finished").

    Optional ``category`` (query or form field, e.g. qBittorrent's %L): when it is not the configured
    category the call is acknowledged but ignored, so one command can be used for every torrent.
    """
    settings = request.app.state.store.load()
    if not settings.api_enabled:
        # Switched off: behave as if the endpoint did not exist, without looking at the key.
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    provided = request.headers.get("x-api-key", "")
    if not settings.api_key or not hmac.compare_digest(provided, settings.api_key):
        client = request.client.host if request.client else "unknown"
        log.warning("API scan rejected from %s: %s", client,
                    "no API key is set in Settings" if not settings.api_key else "invalid API key")
        return JSONResponse({"error": "invalid API key"}, status_code=401)
    category = request.query_params.get("category")
    path = request.query_params.get("path")
    if request.headers.get("content-type", "").startswith(
            ("application/x-www-form-urlencoded", "multipart/form-data")):
        form = await request.form()
        if category is None:
            value = form.get("category")
            category = value if isinstance(value, str) else None
        if path is None:
            value = form.get("path")
            path = value if isinstance(value, str) else None
    from ..app import request_scan

    libraries = [lib for lib in request.app.state.repo.libraries(enabled_only=True) if lib.category]
    if not settings.uses_qbit:
        return _folder_api_scan(request, settings, libraries, category, path)
    if category is not None and category not in {lib.category for lib in libraries}:
        log.info("API scan ignored: category %r is not one of the libraries' categories %s", category[:100],
                 sorted(lib.category for lib in libraries))
        return JSONResponse({"status": "ignored", "reason": "not the configured category"}, status_code=202)

    # qBittorrent mode ignores ``path``: qBittorrent itself reports which torrents are finished.
    name = (category or "")[:100]
    request_scan(request.app, f"API scan (torrent finished in '{name}')" if category else "API scan")
    return JSONResponse({"status": "scan queued"}, status_code=202)


def _folder_api_scan(request: Request, settings, libraries: list, category: str | None,
                     path: str | None) -> JSONResponse:
    """Folder mode: the call must name a library's category; a content path marks that download finished."""
    import time
    from pathlib import Path

    from ..app import request_scan
    from ..paths import PathMapping
    from ..sources import API_FINISHED_KEY, API_FINISHED_MAX, download_for_path

    if not libraries:
        log.warning("API scan refused: no category is configured for finished-download calls")
        return JSONResponse({"error": "no category configured (Settings → Download client)"}, status_code=409)
    library = next((lib for lib in libraries if lib.category == category), None)
    if library is None:
        log.info("API scan ignored: category %r is not one of the libraries' categories %s",
                 (category or "")[:100], sorted(lib.category for lib in libraries))
        return JSONResponse({"status": "ignored", "reason": "not the configured category"}, status_code=202)
    if not path:
        request_scan(request.app, f"API scan (download finished in '{category}')")
        return JSONResponse({"status": "scan queued"}, status_code=202)
    found = download_for_path(path, PathMapping(settings.path_map_remote, settings.path_map_local),
                              Path(library.source_dir), library.id)
    if found is None:
        log.warning("API scan ignored: path %r is not inside the source folder %s (check the path mapping)",
                    path[:300], library.source_dir)
        return JSONResponse({"status": "ignored", "reason": "path is not inside the source folder"},
                            status_code=202)
    ident, name = found
    repo = request.app.state.repo
    marks = repo.get_state(API_FINISHED_KEY) or {}
    marks[ident] = time.time()
    if len(marks) > API_FINISHED_MAX:
        marks = dict(sorted(marks.items(), key=lambda item: item[1])[-API_FINISHED_MAX:])
    repo.set_state(API_FINISHED_KEY, marks)
    request_scan(request.app, f"API scan (download finished in '{category}': {name[:100]})")
    return JSONResponse({"status": "scan queued"}, status_code=202)

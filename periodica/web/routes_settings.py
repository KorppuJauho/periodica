"""Settings pages, connection tests and the confirm-with-dry-run flow for risky changes."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import ValidationError

from .. import logbuffer
from ..auth import Session
from ..jellyfin import JellyfinError
from ..libraries import suggest_jellyfin_library
from ..netsafe import HttpError, UnsafeUrlError
from ..qbittorrent import QbitError
from ..scanner import all_problems
from ..settings import OFFLINE_MESSAGE, Settings
from .deps import render, require_csrf, require_session

log = logging.getLogger(__name__)
router = APIRouter()

SECTIONS = ["libraries", "client", "retention", "jellyfin", "logging", "account"]
PENDING_TTL = 15 * 60

# Changing these can make the tool touch different files, so they need a dry-run + confirmation.
RISKY_FIELDS = {"source_dir", "dest_dir", "download_client", "qbit_url", "qbit_category", "path_map_remote",
                "path_map_local"}
# Changing these re-requires arming automatic delete after reviewing the preview.
DISARM_FIELDS = RISKY_FIELDS | {"retention_enabled", "retention_days", "max_torrent_deletions_per_run",
                                "grace_hours"}

# Per-library settings (category, folders, Jellyfin library, titles, days to keep) are edited on the
# Libraries pages (routes_libraries.py); these sections hold what all libraries share.
SECTION_FIELDS: dict[str, dict[str, str]] = {
    "general": {"scan_interval_minutes": "int"},
    "client": {"download_client": "str", "settle_minutes": "int",
               "qbit_url": "str", "qbit_username": "str", "qbit_password": "secret",  # nosec B105
               "path_map_remote": "str", "path_map_local": "str", "allow_public_hosts": "bool",
               "stale_download_hours": "int"},
    "retention": {"retention_enabled": "bool", "grace_hours": "int", "max_torrent_deletions_per_run": "int"},
    "jellyfin": {"jellyfin_enabled": "bool", "jellyfin_url": "str", "jellyfin_api_key": "secret"},
    "logging": {"log_level": "str", "log_file_mb": "int", "log_files_kept": "int"},
}
# Where a saved section returns to.
SECTION_PAGE = {"general": "/settings/libraries"}

FIELD_LABELS = {
    "source_dir": "Source folder", "dest_dir": "Destination folder", "scan_interval_minutes": "Scan interval",
    "title_format": "Title format", "monthly_title_format": "Title format for monthly issues",
    "numbered_title_format": "Title format for numbered issues", "language": "Language",
    "cover_width": "Cover width",
    "download_client": "Download client", "settle_minutes": "Settle time",
    "qbit_url": "qBittorrent URL", "qbit_username": "Username", "qbit_password": "Password",  # nosec B105
    "qbit_category": "Category", "path_map_remote": "Remote path", "path_map_local": "Local path",
    "allow_public_hosts": "Allow non-LAN addresses", "stale_download_hours": "Warn when no new download for",
    "retention_enabled": "Automatic delete",
    "retention_days": "Delete after (days)", "grace_hours": "Grace period (hours)",
    "max_torrent_deletions_per_run": "Safety limit per run", "jellyfin_enabled": "Use Jellyfin",
    "jellyfin_url": "Jellyfin URL",
    "jellyfin_api_key": "Jellyfin API key", "jellyfin_library_id": "Jellyfin library",
    "jellyfin_library_name": "Jellyfin library name",
    "log_level": "Log level", "log_file_mb": "Size of one log file (MB)", "log_files_kept": "Log files kept",
}


def _page(request: Request, section: str, settings: Settings, status_code: int = 200, **extra):
    state = request.app.state
    pwerr = {"mismatch": "New passwords do not match.",
             "invalid": "Current password is wrong or the new one is too short (min 10 characters)."}
    libraries = state.repo.libraries()
    return render(
        request, "settings.html", status_code,
        section=section, sections=SECTIONS, settings=settings, libraries=libraries,
        problems=all_problems(state.store.load(), state.env, libraries),
        data_root=str(state.env.data_root),
        pw_error=pwerr.get(request.query_params.get("pwerr", "")),
        **extra,
    )


@router.get("/settings")
async def settings_page(request: Request, section: str = "libraries", session: Session = Depends(require_session)):
    if section == "library":   # the page from before libraries existed: the first library
        libraries = request.app.state.repo.libraries()
        target = f"/settings/libraries/{libraries[0].id}" if libraries else "/settings/libraries"
        return RedirectResponse(target, status_code=303)
    if section not in SECTIONS or section == "libraries":
        return RedirectResponse("/settings/libraries", status_code=303)
    return _page(request, section, request.app.state.store.load())


def _back(section: str, msg: str) -> RedirectResponse:
    return RedirectResponse(f"{SECTION_PAGE.get(section, f'/settings?section={section}')}"
                            f"{'&' if section not in SECTION_PAGE else '?'}msg={msg}", status_code=303)


def _apply_form(current: Settings, section: str, form) -> Settings:
    data = current.model_dump()
    for field, kind in SECTION_FIELDS[section].items():
        raw = form.get(field)
        if kind == "bool":
            data[field] = raw is not None
        elif kind == "secret":
            if isinstance(raw, str) and raw != "":
                data[field] = raw
            if form.get(f"clear_{field}") is not None:
                data[field] = ""
        else:
            # A field that is not in the form at all keeps its value: that is a page from before an
            # upgrade added the field, not a request to clear it. (Checkboxes cannot tell the two apart.)
            data[field] = raw if isinstance(raw, str) else data[field]
    return Settings(**data)


def _errors(exc: ValidationError) -> list[str]:
    messages = []
    for err in exc.errors():
        field = str(err["loc"][0]) if err.get("loc") else ""
        msg = err.get("msg", "invalid value").removeprefix("Value error, ")
        messages.append(f"{FIELD_LABELS.get(field, field)}: {msg}")
    return messages


def _mode_problem(settings: Settings, libraries) -> str | None:
    """Rules that span fields (kept out of the model so a stored combination can always be loaded)."""
    if not settings.uses_qbit and settings.api_enabled and not any(lib.category for lib in libraries):
        return ("Category: required while the scan API is on. Finished-download calls must name a library's "
                "category, e.g. news: set one in Settings → Libraries, or turn the API off under System → Status.")
    if settings.uses_qbit and any(not lib.category for lib in libraries if lib.enabled):
        return ("Every enabled library needs a qBittorrent category. Set them in Settings → Libraries first.")
    return None


def _changed(old: Settings, new: Settings) -> set[str]:
    a, b = old.model_dump(), new.model_dump()
    return {k for k in b if a.get(k) != b.get(k)}


@router.post("/settings/{section}")
async def settings_save(section: str, request: Request, session: Session = Depends(require_csrf)):
    if section not in SECTION_FIELDS:
        return PlainTextResponse("unknown section", status_code=400)
    state = request.app.state
    current = state.store.load()
    form = await request.form()
    try:
        new = state.store.effective(_apply_form(current, section, form))
    except ValidationError as exc:
        return _page(request, section, current, 400, errors=_errors(exc))

    changed = _changed(current, new)
    rule = _mode_problem(new, state.repo.libraries()) if "download_client" in changed or section == "client" else None
    if rule:
        return _page(request, section, current, 400, errors=[rule])
    if not changed:
        return _back(section, "saved")
    if changed & DISARM_FIELDS:
        new.retention_armed = False

    if changed & RISKY_FIELDS:
        preview = await asyncio.to_thread(state.scanner.run, True, new)
        token = secrets.token_urlsafe(24)
        state.repo.set_state(f"pending_settings:{session.token_hash}", {
            "token": token, "expires": time.time() + PENDING_TTL, "section": section,
            "settings": new.model_dump(),
        })
        changes = [(FIELD_LABELS.get(k, k), "••••" if "password" in k or "key" in k else getattr(current, k),
                    "••••" if "password" in k or "key" in k else getattr(new, k)) for k in sorted(changed)]
        return render(request, "settings_confirm.html", nav="settings", section=section, preview=preview,
                      changes=changes, token=token,
                      category_changed=bool(changed & {"qbit_category", "download_client"}),
                      folder_mode=not new.uses_qbit,
                      disarms=current.retention_armed)

    _commit(request, session, current, new, changed)
    return _back(section, "saved")


@router.post("/settings-confirm")
async def settings_confirm(request: Request, session: Session = Depends(require_csrf)):
    state = request.app.state
    form = await request.form()
    key = f"pending_settings:{session.token_hash}"
    pending = state.repo.get_state(key)
    state.repo.delete_state(key)
    token = form.get("token")
    if (not pending or not isinstance(token, str) or not secrets.compare_digest(pending["token"], token)
            or pending["expires"] < time.time()):
        return RedirectResponse("/settings?msg=cancelled", status_code=303)
    if form.get("action") != "confirm":
        return RedirectResponse(f"/settings?section={pending['section']}&msg=cancelled", status_code=303)
    current = state.store.load()
    try:
        new = Settings(**pending["settings"])
    except ValidationError:
        return RedirectResponse("/settings?msg=cancelled", status_code=303)
    new.api_key = current.api_key
    new = state.store.effective(new)
    changed = _changed(current, new)
    _commit(request, session, current, new, changed)
    return RedirectResponse(f"/settings?section={pending['section']}&msg=saved", status_code=303)


def _commit(request: Request, session: Session, current: Settings, new: Settings, changed: set[str]) -> None:
    state = request.app.state
    if "dest_dir" in changed:
        from ..linker import prepare_library_root
        from ..paths import UnsafePathError

        try:
            prepare_library_root(new.dest_dir, state.env.data_root)
        except (OSError, UnsafePathError) as exc:
            state.repo.log("warning", f"Could not prepare the library folder {new.dest_dir}: {exc}")
    if changed & DISARM_FIELDS:
        new.retention_armed = False
    state.store.save(new)
    if changed & {"qbit_category", "qbit_url", "download_client"}:
        cancelled = state.repo.cancel_pending("download client or category changed")
        if cancelled:
            state.repo.log("warning", f"Cancelled {cancelled} pending deletion(s) because the download client "
                                      "or category changed")
    labels = ", ".join(sorted(FIELD_LABELS.get(k, k) for k in changed))
    logbuffer.apply_settings(new, state.env)
    log.info("settings changed by %s: %s", session.username, labels)
    state.repo.log("info", f"Settings changed by {session.username}: {labels}")
    if current.retention_armed and not new.retention_armed:
        state.repo.log("warning", "Automatic delete disarmed by a settings change; review the preview and re-arm")
    state.scan_event.set()


@router.post("/jellyfin/scan")
async def jellyfin_scan(request: Request, session: Session = Depends(require_csrf)):
    """Ask Jellyfin for a library scan now, with the saved settings (never the unsaved form)."""
    state = request.app.state
    settings = state.store.load()
    if settings.offline:
        return PlainTextResponse(OFFLINE_MESSAGE, status_code=409)
    if not settings.jellyfin_active:
        return RedirectResponse("/settings?section=jellyfin&msg=jf_off", status_code=303)
    log.info("manual Jellyfin library scan requested by %s", session.username)
    targets = [lib.jellyfin_target for lib in state.repo.libraries(enabled_only=True)]
    state.scanner.jellyfin.request(settings, f"manual library scan by {session.username}", targets=targets)
    return RedirectResponse("/settings?section=jellyfin&msg=jf_scan", status_code=303)


# --- connection tests (JSON, used by app.js) ----------------------------------------------------
async def _form_settings(request: Request, section: str) -> Settings:
    store = request.app.state.store
    return store.effective(_apply_form(store.load(), section, await request.form()))


def _offline_refusal(request: Request) -> JSONResponse | None:
    if request.app.state.store.offline:
        return JSONResponse({"ok": False, "message": OFFLINE_MESSAGE}, status_code=409)
    return None


@router.post("/settings-test/qbittorrent")
async def test_qbittorrent(request: Request, session: Session = Depends(require_csrf)):
    if refused := _offline_refusal(request):
        return refused
    try:
        s = await _form_settings(request, "client")
    except ValidationError as exc:
        return JSONResponse({"ok": False, "message": "; ".join(_errors(exc))})

    def check() -> dict:
        client = request.app.state.qbit_factory(s)
        version = client.version()
        categories = client.categories()
        result = {"ok": True, "version": version, "categories": categories}
        wanted = [lib for lib in request.app.state.repo.libraries(enabled_only=True) if lib.category]
        if not wanted:
            result["message"] = f"Connected to qBittorrent {version}. Give each library a category."
            return result
        parts = []
        for lib in wanted:
            count = len(client.torrents(lib.category))
            parts.append(f"{count} torrent(s) in '{lib.category}' ({lib.name})")
        result["message"] = f"Connected to qBittorrent {version}. " + ", ".join(parts) + "."
        missing = [lib.category for lib in wanted if lib.category not in categories]
        if missing:
            result["message"] += " Warning: missing in qBittorrent: " + ", ".join(missing) + "."
        return result

    try:
        return JSONResponse(await asyncio.to_thread(check))
    except (QbitError, HttpError, UnsafeUrlError) as exc:
        return JSONResponse({"ok": False, "message": str(exc)})


@router.post("/settings-test/jellyfin")
async def test_jellyfin(request: Request, session: Session = Depends(require_csrf)):
    if refused := _offline_refusal(request):
        return refused
    try:
        s = await _form_settings(request, "jellyfin")
    except ValidationError as exc:
        return JSONResponse({"ok": False, "message": "; ".join(_errors(exc))})

    def check() -> dict:
        client = request.app.state.jellyfin_factory(s)
        info = client.system_info()
        name = str(info.get("ServerName", "Jellyfin"))[:100]
        version = str(info.get("Version", "?"))[:30]
        libraries = client.libraries()
        # Suggest the library whose folder ends like the destination folder, e.g. .../books/news
        suggested = suggest_jellyfin_library(libraries, s.dest_dir)
        message = f"Connected to {name} (Jellyfin {version}). {len(libraries)} libraries found."
        return {"ok": True, "message": message, "libraries": libraries, "suggested": suggested}

    try:
        return JSONResponse(await asyncio.to_thread(check))
    except (JellyfinError, HttpError, UnsafeUrlError) as exc:
        return JSONResponse({"ok": False, "message": str(exc)})


@router.post("/settings-test/title")
async def test_title(request: Request, session: Session = Depends(require_csrf)):
    """Example titles for whichever of the three formats the form contains."""
    from datetime import date

    from ..issues import IssueId
    from ..metadata import render_title

    form = await request.form()
    lang = str(form.get("language") or "en")
    today = date.today()
    examples = {
        "day": ("title_format", "Evening Post", IssueId.for_day(today)),
        "month": ("monthly_title_format", "Business Monthly", IssueId.for_month(today.year, today.month)),
        "number": ("numbered_title_format", "Duck Weekly", IssueId.for_number(today.year, 38)),
    }
    previews: dict[str, dict] = {}
    for period, (field, paper, issue) in examples.items():
        fmt = form.get(field)
        if fmt is None:
            continue
        try:
            previews[period] = {"ok": True, "message": f"Example: {render_title(str(fmt), paper, issue, lang)}"}
        except ValueError as exc:
            previews[period] = {"ok": False, "message": str(exc)}
    first = previews.get("day") or {"ok": True, "message": ""}
    return JSONResponse({**first, "previews": previews})

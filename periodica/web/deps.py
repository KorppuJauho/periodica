"""Shared web plumbing: templates, auth dependencies, CSRF, security headers."""

from __future__ import annotations

import datetime as dt
import hashlib
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from ..auth import SESSION_COOKIE, Session, csrf_matches

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # "same-origin" (not "no-referrer"): with no-referrer browsers send "Origin: null" on form posts,
    # which breaks the same-origin check. Referrers still never leave this site.
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def _datetime(ts: float | None) -> str:
    if not ts:
        return "—"
    return dt.datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


def _ago(ts: float | None) -> str:
    if not ts:
        return "never"
    seconds = int(dt.datetime.now().timestamp() - ts)
    future = seconds < 0
    seconds = abs(seconds)
    if seconds < 60:
        text = f"{seconds} s"
    elif seconds < 3600:
        text = f"{seconds // 60} min"
    elif seconds < 86400:
        text = f"{seconds // 3600} h"
    else:
        text = f"{seconds // 86400} d"
    return f"in {text}" if future else f"{text} ago"


def _size(value: int | None) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


from .. import build_label  # noqa: E402

TEMPLATES.env.globals["build"] = build_label()
# Cache-busting token for /static URLs so browsers pick up new CSS/JS right after an update.
TEMPLATES.env.globals["asset_v"] = hashlib.sha256(f"{build_label()}-{time.time()}".encode()).hexdigest()[:10]
TEMPLATES.env.filters["datetime"] = _datetime
TEMPLATES.env.filters["ago"] = _ago
TEMPLATES.env.filters["size"] = _size


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        if request.url.path.startswith(("/static/", "/issues/")):
            response.headers.setdefault("Cache-Control", "private, max-age=3600")
        else:
            response.headers.setdefault("Cache-Control", "no-store")
        return response


class LoginRequired(Exception):
    pass


class SetupRequired(Exception):
    pass


class CsrfFailed(Exception):
    pass


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def same_origin(request: Request) -> bool:
    """Reject cross-site form posts.

    Prefer Sec-Fetch-Site (set by the browser, cannot be forged by page scripts and survives
    reverse proxies rewriting Host). Fall back to comparing Origin with Host. Requests without
    either header are not from a modern browser form and are allowed (CSRF tokens still apply).
    """
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site == "same-origin"
    origin = request.headers.get("origin")
    if origin is None:
        return True
    if origin == "null":
        return False
    return urlsplit(origin).netloc == request.headers.get("host", "")


def current_session(request: Request) -> Session | None:
    return request.app.state.auth.get_session(request.cookies.get(SESSION_COOKIE))


async def require_session(request: Request) -> Session:
    if not request.app.state.auth.has_users():
        raise SetupRequired()
    session = current_session(request)
    if session is None:
        raise LoginRequired()
    request.state.session = session
    return session


async def require_csrf(request: Request) -> Session:
    session = await require_session(request)
    if not same_origin(request):
        raise CsrfFailed()
    provided = request.headers.get("x-csrf-token")
    if provided is None:
        form = await request.form()
        value = form.get("csrf_token")
        provided = value if isinstance(value, str) else None
    if not csrf_matches(session.csrf_token, provided):
        raise CsrfFailed()
    return session


# Redirect messages are looked up by key so nobody can inject arbitrary text via a crafted link.
MESSAGES = {
    "saved": "Settings saved.",
    "scan": "Scan started. This page refreshes when it has finished.",
    "armed": "Automatic delete is armed. Items will be queued with the grace period.",
    "disarmed": "Automatic delete disarmed.",
    "approved": "Batch approved; it will run on the next scan.",
    "kept": "Item will be kept and excluded from automatic delete.",
    "unkept": "Item is no longer protected from automatic delete.",
    "due": "Item will be deleted on the next scan.",
    "cover": "Cover will be regenerated on the next scan.",
    "removed": "Issue removed from the library.",
    "restored": "Issue will be relinked on the next scan.",
    "fixing": "Fixing on the next scan, which is starting now.",
    "nothing_to_fix": "Nothing to fix: the issues are whole, or their download is gone.",
    "paper": "Publication updated.",
    "ignored": "File ignored. A scan has started to apply it.",
    "unignored": "The file is no longer ignored and protects its torrent again.",
    "numbering": "Saved. A scan has started to link the waiting files and relabel existing issues.",
    "password": "Password changed. Other sessions were signed out.",  # nosec B105
    "apikey": "API key regenerated.",
    "api_on": "Scan API turned on.",
    "pattern_added": "Naming pattern saved. A scan has started to link the matching files.",
    "pattern_deleted": "Naming pattern deleted.",
    "library_added": "Library added. The next scan links it; a scan has started.",
    "library_enabled": "Library enabled. A scan has started.",
    "library_disabled": "Library disabled. It is no longer scanned; nothing was removed.",
    "library_deleted": "Library deleted from Periodica. Its files and downloads were left alone.",
    "last_enabled": "The last enabled library cannot be disabled.",
    "api_locked": "The scan API stays off while OFFLINE_MODE is set in the container's environment.",
    "jf_scan": "Jellyfin library scan requested. The result appears in Activity and on the dashboard.",
    "jf_off": "Jellyfin is not in use: turn on Use Jellyfin and save a URL and key first.",
    "api_off": "Scan API turned off. Calls to it now get 'not found'.",
    "cancelled": "Changes discarded.",
    "deleted": "Torrent and its files were deleted, and its issues were removed from the library. Details in History.",
}


def render(request: Request, template: str, status_code: int = 200, **context):
    session = getattr(request.state, "session", None)
    context.setdefault("session", session)
    context.setdefault("csrf_token", session.csrf_token if session else "")
    context.setdefault("flash", MESSAGES.get(request.query_params.get("msg", "")))
    context.setdefault("flash_error", None)
    context.setdefault("nav", template.split(".")[0])
    context.setdefault("offline_mode", request.app.state.store.offline)
    return TEMPLATES.TemplateResponse(request, template, context, status_code=status_code)

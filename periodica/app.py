"""FastAPI application factory and background scheduler."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import build_label, logbuffer, netsafe
from .auth import AuthService, LoginThrottle
from .config import Env, load_env
from .db import Database
from .linker import prepare_library_root
from .paths import UnsafePathError
from .repo import Repo
from .scanner import Scanner, default_jellyfin_factory, default_qbit_factory
from .settings import SettingsStore
from .web.deps import CsrfFailed, LoginRequired, SecurityHeadersMiddleware, SetupRequired

log = logging.getLogger(__name__)

STARTUP_SCAN_DELAY = 15


async def _scheduler(app: FastAPI) -> None:
    scanner: Scanner = app.state.scanner
    event: asyncio.Event = app.state.scan_event
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(event.wait(), timeout=STARTUP_SCAN_DELAY)
    while True:
        event.clear()
        trigger, request_id = app.state.scan_trigger, app.state.scan_request_id
        await asyncio.to_thread(scanner.run, False, None, trigger)
        # Clear only if no new request arrived meanwhile. Keeping it set until the scan has finished
        # lets the dashboard tell "queued" from "done" without a race.
        if app.state.scan_request_id == request_id:
            app.state.scan_trigger = None
        interval = app.state.store.load().scan_interval_minutes * 60
        if interval <= 0:
            # Scheduled scans off: wait for a trigger (download finished, Scan now, API, settings change).
            await event.wait()
        else:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(event.wait(), timeout=interval)


def request_scan(app: FastAPI, trigger: str | None = None) -> None:
    """Wake the scheduler. ``trigger`` (e.g. "Manual scan by jauho") makes the result always show in Activity."""
    if trigger:
        log.info("scan requested: %s", trigger)
        app.state.scan_trigger = trigger
        app.state.scan_request_id += 1
        app.state.scan_requested_at = time.time()
    app.state.scan_event.set()


def create_app(
    env: Env | None = None,
    qbit_factory: Callable | None = None,
    jellyfin_factory: Callable | None = None,
    run_scheduler: bool = True,
) -> FastAPI:
    env = env or load_env()
    netsafe.OFFLINE = env.offline
    db = Database(env.db_path)
    db.migrate()
    store = SettingsStore(db, offline=env.offline)
    store.ensure_api_key()
    repo = Repo(db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.scan_event = asyncio.Event()
        logbuffer.install(env.log_level)  # after uvicorn configured logging, so our handler isn't replaced
        logbuffer.apply_settings(store.load(), env)
        log.info("Periodica %s started%s", build_label(),
                 " in offline mode (OFFLINE_MODE): no qBittorrent, Jellyfin or scan API" if env.offline else "")
        try:
            prepare_library_root(store.load().dest_dir, env.data_root)
        except (OSError, UnsafePathError) as exc:
            log.warning("Could not prepare the library folder: %s", exc)
        task = asyncio.create_task(_scheduler(app)) if run_scheduler else None
        try:
            yield
        finally:
            log.info("Periodica stopping (shutdown requested by the container runtime)")
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.env = env
    app.state.db = db
    app.state.store = store
    app.state.repo = repo
    app.state.auth = AuthService(db)
    app.state.throttle = LoginThrottle()
    app.state.qbit_factory = qbit_factory or default_qbit_factory
    app.state.jellyfin_factory = jellyfin_factory or default_jellyfin_factory
    app.state.scanner = Scanner(env, store, repo, app.state.qbit_factory, app.state.jellyfin_factory)
    app.state.scan_event = asyncio.Event()
    app.state.scan_trigger = None
    app.state.scan_request_id = 0
    app.state.scan_requested_at = 0.0

    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "web" / "static")), name="static")

    @app.exception_handler(SetupRequired)
    async def _setup(request: Request, exc: SetupRequired):
        return RedirectResponse("/setup", status_code=303)

    @app.exception_handler(LoginRequired)
    async def _login(request: Request, exc: LoginRequired):
        if request.method == "GET":
            return RedirectResponse("/login", status_code=303)
        return PlainTextResponse("login required", status_code=401)

    @app.exception_handler(CsrfFailed)
    async def _csrf(request: Request, exc: CsrfFailed):
        return PlainTextResponse("CSRF check failed. Reload the page and try again.", status_code=403)

    from .web import routes_auth, routes_libraries, routes_main, routes_settings

    app.include_router(routes_auth.router)
    app.include_router(routes_main.router)
    app.include_router(routes_libraries.router)   # before routes_settings: /settings/{section} is generic
    app.include_router(routes_settings.router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return PlainTextResponse("ok")

    return app

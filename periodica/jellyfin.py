"""Optional Jellyfin integration: trigger a library scan after changes."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable

from .netsafe import HttpError, SafeHttpClient, UnsafeUrlError

log = logging.getLogger(__name__)

# Jellyfin's POST /Library/Refresh only returns after the scan of *all* libraries has finished.
REFRESH_TIMEOUT_S = 3600

_KEY_RE = re.compile(r"^[A-Za-z0-9]{16,128}$")
_ID_RE = re.compile(r"[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class JellyfinError(Exception):
    pass


class JellyfinClient:
    def __init__(self, url: str, api_key: str, allow_public: bool = False,
                 http: SafeHttpClient | None = None):
        if not _KEY_RE.fullmatch(api_key or ""):
            raise JellyfinError("API key looks invalid (expected letters and digits)")
        self.http = http or SafeHttpClient(url, allow_public=allow_public)
        self._headers = {"Authorization": f'MediaBrowser Token="{api_key}"'}

    def system_info(self) -> dict:
        try:
            resp = self.http.request("GET", "/System/Info", headers=self._headers)
        except HttpError as exc:
            raise JellyfinError(str(exc)) from exc
        if resp.status in (401, 403):
            raise JellyfinError("Jellyfin rejected the API key")
        if resp.status != 200:
            raise JellyfinError(f"Jellyfin returned HTTP {resp.status}")
        try:
            data = json.loads(resp.body)
        except ValueError as exc:
            raise JellyfinError("invalid response from Jellyfin") from exc
        return data if isinstance(data, dict) else {}

    def libraries(self) -> list[dict]:
        """Libraries ("virtual folders") with id, name, type and folder locations."""
        try:
            resp = self.http.request("GET", "/Library/VirtualFolders", headers=self._headers)
        except HttpError as exc:
            raise JellyfinError(str(exc)) from exc
        if resp.status in (401, 403):
            raise JellyfinError("Jellyfin rejected the API key")
        if resp.status != 200:
            raise JellyfinError(f"Jellyfin returned HTTP {resp.status} for the library list")
        try:
            data = json.loads(resp.body)
        except ValueError as exc:
            raise JellyfinError("invalid library list from Jellyfin") from exc
        result = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict) or not _ID_RE.fullmatch(str(item.get("ItemId", "")).lower()):
                continue
            result.append({
                "id": str(item["ItemId"]).lower(),
                "name": str(item.get("Name", ""))[:100],
                "type": str(item.get("CollectionType") or "")[:30],
                "locations": [str(p)[:500] for p in item.get("Locations") or [] if isinstance(p, str)][:20],
            })
        return result

    def refresh_library_folder(self, library_id: str) -> None:
        """Queue a scan of one library (like 'Scan library' in Jellyfin). Returns immediately."""
        library_id = (library_id or "").lower()
        if not _ID_RE.fullmatch(library_id):
            raise JellyfinError("invalid library id")
        try:
            resp = self.http.request(
                "POST", f"/Items/{library_id}/Refresh",
                params={"metadataRefreshMode": "Default", "imageRefreshMode": "Default",
                        "replaceAllMetadata": "false", "replaceAllImages": "false"},
                headers=self._headers, form={},
            )
        except HttpError as exc:
            raise JellyfinError(str(exc)) from exc
        if resp.status == 404:
            raise JellyfinError("the selected Jellyfin library no longer exists; choose it again in Settings")
        if resp.status in (401, 403):
            raise JellyfinError("Jellyfin rejected the API key")
        if resp.status not in (200, 204):
            raise JellyfinError(f"library refresh failed: HTTP {resp.status}")

    def refresh_library(self, timeout: float = REFRESH_TIMEOUT_S) -> None:
        """Scan *all* libraries. Jellyfin only answers after the whole scan has finished."""
        previous = self.http.timeout
        self.http.timeout = timeout
        try:
            resp = self.http.request("POST", "/Library/Refresh", headers=self._headers, form={})
        except HttpError as exc:
            raise JellyfinError(str(exc)) from exc
        finally:
            self.http.timeout = previous
        if resp.status not in (200, 204):
            raise JellyfinError(f"library refresh failed: HTTP {resp.status}")


JELLYFIN_STATUS_KEY = "jellyfin_status"


class RefreshCoordinator:
    """Runs Jellyfin library refreshes in the background, one at a time.

    Requests arriving while a refresh runs are merged into one follow-up refresh, so a change made
    during a Jellyfin scan (e.g. a delete right after linking) is always picked up by a later scan.
    A request names the Jellyfin libraries to scan, as (library id, name); an empty id means all
    libraries, which then replaces the individual scans.
    """

    def __init__(self, repo, factory: Callable):
        self.repo = repo
        self.factory = factory
        self._lock = threading.Lock()
        self._pending: tuple | None = None   # (settings, reason, {(library id, name)})
        self._running = False

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._running

    def request(self, settings, reason: str, wait: bool = False,
                targets: list[tuple[str, str]] | None = None) -> None:
        if not settings.jellyfin_active:
            return
        wanted = set(targets) if targets is not None else {(settings.jellyfin_library_id,
                                                            settings.jellyfin_library_name)}
        if not wanted:
            return
        with self._lock:
            merged = wanted | (self._pending[2] if self._pending else set())
            self._pending = (settings, reason, merged)
            if self._running:
                log.info("refresh already running, merged request (%s)", reason)
                return
            self._running = True
        thread = threading.Thread(target=self._worker, name="jellyfin-refresh", daemon=True)
        thread.start()
        if wait:
            thread.join()

    def _worker(self) -> None:
        while True:
            with self._lock:
                item, self._pending = self._pending, None
                if item is None:
                    self._running = False
                    return
            settings, reason, targets = item
            if any(not library for library, _name in targets):
                targets = {("", "")}   # one scan of everything covers the individual libraries
            for library, name in sorted(targets):
                self._refresh(settings, reason, library, name)

    def _refresh(self, settings, reason: str, library: str, name: str) -> None:
        started = time.monotonic()
        label = f"library '{name or library}'" if library else "all libraries"
        log.info("asking for a scan of %s (%s)", label, reason)
        try:
            client = self.factory(settings)
            if library:
                client.refresh_library_folder(library)
            else:
                client.refresh_library()
        except (JellyfinError, HttpError, UnsafeUrlError) as exc:
            log.warning("scan of %s failed after %.1f s: %s", label, time.monotonic() - started, exc)
            self.repo.log("warning", f"Jellyfin scan of {label} failed ({reason}): {exc}")
            self._status(settings, str(exc))
        except Exception as exc:  # never kill the worker silently
            log.exception("Jellyfin refresh crashed")
            self.repo.log("warning", f"Jellyfin scan of {label} failed ({reason}): {type(exc).__name__}")
            self._status(settings, type(exc).__name__)
        else:
            self._status(settings, None)
            log.info("scan of %s accepted in %.1f s", label, time.monotonic() - started)
            if library:
                self.repo.log("info", f"Jellyfin: scan of {label} requested ({reason})")
            else:
                seconds = int(time.monotonic() - started)
                self.repo.log("info", f"Jellyfin: scan of all libraries finished in {seconds} s ({reason})")

    def _status(self, settings, error: str | None) -> None:
        """Last contact, for the connection rows on the dashboard and Status page."""
        self.repo.set_state(JELLYFIN_STATUS_KEY, {"ok": error is None, "error": (error or "")[:300],
                                                  "at": time.time(), "url": settings.jellyfin_url})

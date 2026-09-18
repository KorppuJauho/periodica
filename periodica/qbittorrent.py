"""qBittorrent Web API v2 client, scoped to a single category.

The configured category is the only thing this application may touch. Every call that
returns torrents is filtered again client-side, and deletes re-fetch the torrent by hash
and re-check its category immediately before deleting.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from .netsafe import HttpError, SafeHttpClient

COMPLETE_STATES = {"uploading", "stalledUP", "pausedUP", "stoppedUP", "queuedUP", "forcedUP"}
_HASH_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


log = logging.getLogger(__name__)


class QbitError(Exception):
    pass


def _detail(resp) -> str:
    """HTTP status plus qBittorrent's short response text, for diagnosing auth/CSRF/host-header rejections."""
    text = " ".join(resp.body.decode("utf-8", "replace").split())[:120]
    return f"HTTP {resp.status}" + (f": {text}" if text else "")


def validate_hash(value: str) -> str:
    # Strict: the API treats hashes=all as "every torrent".
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise QbitError(f"invalid torrent hash: {value!r}")
    return value


def validate_category(category: str) -> str:
    if not isinstance(category, str) or not category.strip() or category != category.strip():
        raise QbitError("a non-empty category is required")
    return category


@dataclass(frozen=True)
class Torrent:
    hash: str
    name: str
    category: str
    save_path: str
    progress: float
    state: str
    completion_on: float | None
    size: int

    @property
    def complete(self) -> bool:
        return self.progress >= 1.0 and self.state in COMPLETE_STATES

    @classmethod
    def from_api(cls, data: dict) -> Torrent:
        completion = data.get("completion_on")
        return cls(
            hash=validate_hash(str(data.get("hash", ""))),
            name=str(data.get("name", "")),
            category=str(data.get("category", "")),
            save_path=str(data.get("save_path", "")),
            progress=float(data.get("progress", 0) or 0),
            state=str(data.get("state", "")),
            completion_on=float(completion) if isinstance(completion, (int, float)) and completion > 0 else None,
            size=int(data.get("size", 0) or 0),
        )


@dataclass(frozen=True)
class TorrentFile:
    name: str
    size: int
    progress: float


class QbitClient:
    def __init__(self, url: str, username: str, password: str, allow_public: bool = False,
                 http: SafeHttpClient | None = None):
        self.http = http or SafeHttpClient(url, allow_public=allow_public)
        self.username = username
        self.password = password
        self._logged_in = False

    @property
    def _headers(self) -> dict[str, str]:
        # qBittorrent's CSRF protection checks Referer/Origin against its own host.
        return {"Referer": self.http.base_url, "Origin": self.http.base_url}

    def login(self) -> None:
        if not self.username:
            self._logged_in = True  # relies on qBittorrent's auth bypass for whitelisted subnets
            return
        try:
            resp = self.http.request(
                "POST", "/api/v2/auth/login",
                form={"username": self.username, "password": self.password},
                headers=self._headers,
            )
        except HttpError as exc:
            raise QbitError(str(exc)) from exc
        body = resp.body.strip()
        if resp.status == 403:
            raise QbitError("qBittorrent refused the login: this IP is temporarily banned after too many failed "
                            "attempts (wait, or restart qBittorrent)")
        # qBittorrent 5.x: 200 or 204 with an empty body on success, 401 on bad credentials.
        # Older versions: 200 with "Ok." on success, 200 with "Fails." on bad credentials.
        if resp.status == 401 or body == b"Fails." or resp.status not in (200, 204) or body not in (b"", b"Ok."):
            log.warning("login failed at %s (%s)", self.http.base_url, _detail(resp))
            raise QbitError(f"qBittorrent login failed ({_detail(resp)}). Check username and password; if they are "
                            "right, see Troubleshooting in the README (CSRF / Host header validation)")
        log.info("logged in at %s as %r", self.http.base_url, self.username)
        self._logged_in = True

    def _call(self, method: str, path: str, params: dict[str, str] | None = None,
              form: dict[str, str] | None = None) -> bytes:
        if not self._logged_in:
            self.login()
        try:
            resp = self.http.request(method, path, params=params, form=form, headers=self._headers)
            if resp.status in (401, 403) and self.username:
                # Session expired (401 on qBittorrent 5.x, 403 on older versions): log in again once.
                self._logged_in = False
                self.login()
                resp = self.http.request(method, path, params=params, form=form, headers=self._headers)
        except HttpError as exc:
            raise QbitError(str(exc)) from exc
        if resp.status in (401, 403):
            raise QbitError(f"qBittorrent denied access ({_detail(resp)}). Enter the Web UI username and password, "
                            "or check the auth bypass whitelist and Host header validation in qBittorrent")
        if resp.status != 200:
            raise QbitError(f"qBittorrent returned {_detail(resp)} for {path}")
        return resp.body

    def _json(self, path: str, params: dict[str, str] | None = None):
        body = self._call("GET", path, params=params)
        try:
            return json.loads(body)
        except ValueError as exc:
            raise QbitError(f"invalid JSON from qBittorrent for {path}") from exc

    def default_save_path(self) -> str:
        return self._call("GET", "/api/v2/app/defaultSavePath").decode("utf-8", "replace").strip()

    def version(self) -> str:
        return self._call("GET", "/api/v2/app/version").decode("utf-8", "replace").strip()

    def categories(self) -> list[str]:
        data = self._json("/api/v2/torrents/categories")
        return sorted(data.keys()) if isinstance(data, dict) else []

    def category_paths(self) -> dict[str, str]:
        """Category name -> save path as qBittorrent sees it ("" when the category uses the default folder)."""
        data = self._json("/api/v2/torrents/categories")
        if not isinstance(data, dict):
            return {}
        return {str(name): str((info or {}).get("savePath") or "") if isinstance(info, dict) else ""
                for name, info in data.items()}

    def rss_status(self, category: str) -> dict:
        """Read-only check that qBittorrent's RSS auto-downloader feeds the category.

        Only the few needed fields are kept; nothing else from the preferences is stored.
        """
        category = validate_category(category)
        prefs = self._json("/api/v2/app/preferences")
        rules = self._json("/api/v2/rss/rules")
        processing = bool(prefs.get("rss_processing_enabled")) if isinstance(prefs, dict) else False
        auto = bool(prefs.get("rss_auto_downloading_enabled")) if isinstance(prefs, dict) else False
        matching: list[dict] = []
        disabled: list[dict] = []
        for name, rule in (rules.items() if isinstance(rules, dict) else []):
            if not isinstance(rule, dict):
                continue
            raw_params = rule.get("torrentParams")
            params: dict = raw_params if isinstance(raw_params, dict) else {}
            rule_category = params.get("category") or rule.get("assignedCategory") or ""
            if rule_category != category:
                continue
            entry = {"name": str(name)[:100], "feeds": len(rule.get("affectedFeeds") or [])}
            (matching if rule.get("enabled") else disabled).append(entry)
        return {"processing": processing, "auto_downloading": auto, "rules": matching, "disabled_rules": disabled}

    def torrents(self, category: str) -> list[Torrent]:
        category = validate_category(category)
        data = self._json("/api/v2/torrents/info", {"category": category})
        if not isinstance(data, list):
            raise QbitError("unexpected torrent list response")
        result = []
        for item in data:
            if isinstance(item, dict) and item.get("category") == category:
                result.append(Torrent.from_api(item))
        return result

    def torrent(self, torrent_hash: str) -> Torrent | None:
        data = self._json("/api/v2/torrents/info", {"hashes": validate_hash(torrent_hash)})
        if not isinstance(data, list):
            raise QbitError("unexpected torrent list response")
        for item in data:
            if isinstance(item, dict) and item.get("hash") == torrent_hash:
                return Torrent.from_api(item)
        return None

    def files(self, torrent_hash: str) -> list[TorrentFile]:
        data = self._json("/api/v2/torrents/files", {"hash": validate_hash(torrent_hash)})
        if not isinstance(data, list):
            raise QbitError("unexpected file list response")
        return [
            TorrentFile(name=str(f.get("name", "")), size=int(f.get("size", 0) or 0),
                        progress=float(f.get("progress", 0) or 0))
            for f in data if isinstance(f, dict)
        ]

    def delete_with_files(self, torrent_hash: str, category: str) -> None:
        """Delete one torrent and its data, only if it is still complete and in the category."""
        category = validate_category(category)
        torrent_hash = validate_hash(torrent_hash)
        current = self.torrent(torrent_hash)
        if current is None:
            raise QbitError("torrent no longer exists in qBittorrent")
        if current.category != category:
            raise QbitError(f"torrent category is now {current.category!r}, not {category!r}; not deleting")
        if not current.complete:
            raise QbitError(f"torrent is not complete (state {current.state}); not deleting")
        log.warning("deleting torrent %s (%s) in category %r with its files",
                    torrent_hash, current.name, category)
        self._call("POST", "/api/v2/torrents/delete", form={"hashes": torrent_hash, "deleteFiles": "true"})

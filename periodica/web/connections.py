"""Connection rows for the dashboard and Status page: in use and working, failing, or not used (grey)."""

from __future__ import annotations

from dataclasses import dataclass

from ..jellyfin import JELLYFIN_STATUS_KEY
from ..scanner import QBIT_STATUS_KEY
from ..settings import Settings

OK, BAD, IDLE, OFF = "ok", "bad", "idle", "off"


@dataclass(frozen=True)
class Connection:
    name: str
    state: str          # ok | bad | idle (in use, no contact yet) | off (not used, shown grey)
    summary: str
    detail: str = ""
    at: float | None = None
    link: str = ""

    @property
    def used(self) -> bool:
        return self.state != OFF


def _last_contact(status: dict | None, url: str) -> dict | None:
    # A result for another address says nothing about the one configured now.
    if not status or status.get("url") != url:
        return None
    return status


def connections(settings: Settings, repo) -> list[Connection]:
    if settings.offline:
        detail = "Locked by OFFLINE_MODE in the container's environment"
        return [Connection(name, OFF, "Off: offline mode", detail) for name in ("qBittorrent", "Jellyfin", "Scan API")]
    rows = []

    if not settings.uses_qbit:
        rows.append(Connection("qBittorrent", OFF, "Not used: watching the source folder",
                               "Everything in the libraries' source folders counts as a download; nothing is deleted",
                               link="/settings?section=client"))
    elif not settings.qbit_configured:
        rows.append(Connection("qBittorrent", BAD, "Not configured", "URL and category are required",
                               link="/settings?section=client"))
    else:
        status = _last_contact(repo.get_state(QBIT_STATUS_KEY), settings.qbit_url)
        if status is None:
            rows.append(Connection("qBittorrent", IDLE, "No contact yet", settings.qbit_url,
                                   link="/settings?section=client"))
        elif status["ok"]:
            rows.append(Connection("qBittorrent", OK, "Connected", settings.qbit_url, status["at"],
                                   "/settings?section=client"))
        else:
            rows.append(Connection("qBittorrent", BAD, "Unreachable", status["error"], status["at"],
                                   "/settings?section=client"))

    if not settings.jellyfin_configured:
        rows.append(Connection("Jellyfin", OFF, "Not used",
                               "Jellyfin finds new issues on its own library scan", link="/settings?section=jellyfin"))
    elif not settings.jellyfin_enabled:
        rows.append(Connection("Jellyfin", OFF, "Switched off",
                               "Settings are kept; Jellyfin finds new issues on its own library scan",
                               link="/settings?section=jellyfin"))
    else:
        status = _last_contact(repo.get_state(JELLYFIN_STATUS_KEY), settings.jellyfin_url)
        if status is None:
            rows.append(Connection("Jellyfin", IDLE, "No library scan requested yet", settings.jellyfin_url,
                                   link="/settings?section=jellyfin"))
        elif status["ok"]:
            rows.append(Connection("Jellyfin", OK, "Library scan accepted", settings.jellyfin_url, status["at"],
                                   "/settings?section=jellyfin"))
        else:
            rows.append(Connection("Jellyfin", BAD, "Library scan failed", status["error"], status["at"],
                                   "/settings?section=jellyfin"))

    if settings.api_enabled:
        rows.append(Connection("Scan API", OK, "On", "Scripts can start a scan with the API key", link="/system#api"))
    else:
        rows.append(Connection("Scan API", OFF, "Off", "Scans run on schedule and from Scan now only",
                               link="/system#api"))
    return rows

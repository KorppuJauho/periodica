"""Minimal, locked-down HTTP client for talking to qBittorrent and Jellyfin on the LAN.

- http/https only, no credentials in URLs
- hostnames must resolve to private/LAN addresses unless explicitly allowed
- redirects are never followed, environment proxies are ignored
- timeouts and response size caps on every request
"""

from __future__ import annotations

import http.cookiejar
import ipaddress
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
# Set from OFFLINE_MODE at start-up. No client can be created while it is on.
OFFLINE = False
_CGNAT = ipaddress.ip_network("100.64.0.0/10")  # e.g. Tailscale


log = logging.getLogger(__name__)


class UnsafeUrlError(ValueError):
    pass


class HttpError(Exception):
    pass


def _is_lan_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_multicast or ip.is_unspecified:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or (ip.version == 4 and ip in _CGNAT)


def validate_url(url: str, allow_public: bool = False, resolve: bool = True) -> str:
    """Validate and normalise a base URL. Returns it without a trailing slash."""
    url = (url or "").strip()
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UnsafeUrlError(f"invalid URL: {exc}") from exc
    if parts.scheme not in {"http", "https"}:
        raise UnsafeUrlError("URL must start with http:// or https://")
    if not parts.hostname:
        raise UnsafeUrlError("URL has no host")
    if parts.username or parts.password:
        raise UnsafeUrlError("do not put credentials in the URL")
    if parts.query or parts.fragment:
        raise UnsafeUrlError("URL must not contain a query or fragment")
    if resolve:
        try:
            infos = socket.getaddrinfo(parts.hostname, port or (443 if parts.scheme == "https" else 80),
                                       type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise UnsafeUrlError(f"cannot resolve host {parts.hostname!r}") from exc
        for info in infos:
            ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
            if ip.is_multicast or ip.is_unspecified:
                raise UnsafeUrlError(f"{parts.hostname} resolves to a disallowed address ({ip})")
            if not allow_public and not _is_lan_address(ip):
                raise UnsafeUrlError(
                    f"{parts.hostname} resolves to a public address ({ip}); only LAN addresses are allowed"
                )
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HttpError(f"server tried to redirect to {newurl!r}; redirects are not followed")


@dataclass
class Response:
    status: int
    body: bytes

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class SafeHttpClient:
    def __init__(self, base_url: str, allow_public: bool = False, timeout: float = 10.0):
        if OFFLINE:
            raise UnsafeUrlError("offline mode: network connections are disabled (OFFLINE_MODE)")
        self.allow_public = allow_public
        self.base_url = validate_url(base_url, allow_public)
        self.timeout = timeout
        self.cookies = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.cookies),
            _NoRedirect(),
        )

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        # Re-validate on every call so a DNS change can't point us somewhere else later.
        validate_url(self.base_url, self.allow_public)
        if not path.startswith("/"):
            raise ValueError("path must start with /")
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})  # noqa: S310
        if data is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        started = time.monotonic()

        def elapsed_ms() -> float:
            return (time.monotonic() - started) * 1000

        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                answer = Response(resp.status, self._read(resp))
        except urllib.error.HTTPError as exc:
            answer = Response(exc.code, self._read(exc))
        except urllib.error.URLError as exc:
            log.debug("%s %s failed after %d ms", method, path, elapsed_ms())
            reason = exc.reason if isinstance(exc.reason, str) else type(exc.reason).__name__
            if isinstance(exc.reason, HttpError):
                raise exc.reason from exc
            raise HttpError(f"connection to {self.base_url} failed: {reason}") from exc
        except (TimeoutError, OSError) as exc:
            log.debug("%s %s failed after %d ms", method, path, elapsed_ms())
            raise HttpError(f"connection to {self.base_url} failed: {type(exc).__name__}") from exc
        # Path only: query strings, forms and headers can carry keys and passwords.
        log.debug("%s %s -> %s in %d ms", method, path, answer.status, elapsed_ms())
        return answer

    @staticmethod
    def _read(resp) -> bytes:
        body = resp.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise HttpError("response too large")
        return body

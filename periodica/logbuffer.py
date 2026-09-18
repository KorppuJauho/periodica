"""In-memory ring buffer of application log lines, shown on Activity → Logs.

Lines carry a monotonic sequence number so the Logs page can ask for "everything after N" and
notice when it fell behind (the buffer keeps only the newest ``MAX_LINES``).
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

MAX_LINES = max(200, min(50_000, int(os.environ.get("LOG_BUFFER_LINES", "2000") or 2000)))
MAX_MESSAGE_CHARS = 2000
APP_LOGGER = "periodica"
LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}
# How long a debug session lasts before it falls back to info on its own.
DEBUG_SECONDS = 30 * 60


@dataclass(frozen=True)
class LogLine:
    seq: int
    ts: float
    level: str
    logger: str
    message: str


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = MAX_LINES):
        super().__init__(level=logging.DEBUG)
        # Two windows, because they fill at wildly different rates: a few minutes of browsing would
        # otherwise evict the scan that failed an hour ago, which is exactly what you come here to read.
        self._app: deque[LogLine] = deque(maxlen=capacity)
        self._other: deque[LogLine] = deque(maxlen=capacity)
        self.capacity = capacity
        self._lines_lock = threading.Lock()
        self._seq = itertools.count(1)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + logging.Formatter().formatException(record.exc_info)
            line = LogLine(next(self._seq), record.created, record.levelname.lower(), record.name,
                           message[:MAX_MESSAGE_CHARS])
        except Exception:  # never let logging break the app
            self.handleError(record)
            return
        with self._lines_lock:
            (self._app if record.name.startswith(APP_LOGGER) else self._other).append(line)

    def _snapshot(self, app_only: bool) -> list[LogLine]:
        """Both windows merged back into one timeline (they share a sequence counter)."""
        with self._lines_lock:
            app = list(self._app)
            other = [] if app_only else list(self._other)
        return app if not other else sorted(app + other, key=lambda line: line.seq)

    def _select(self, min_level: str, app_only: bool, search: str) -> list[LogLine]:
        threshold = LEVELS.get(min_level, logging.DEBUG)
        needle = search.lower()
        return [
            line for line in self._snapshot(app_only)
            if LEVELS.get(line.level, logging.CRITICAL) >= threshold
            and (not needle or needle in line.message.lower() or needle in line.logger.lower())
        ]

    def lines(self, min_level: str = "", limit: int = 1000, app_only: bool = False,
              search: str = "") -> list[LogLine]:
        """Newest first. ``app_only`` drops the web server's own lines (mostly access logs)."""
        return list(reversed(self._select(min_level, app_only, search)))[:limit]

    def since(self, after: int, min_level: str = "", limit: int = 500, app_only: bool = False,
              search: str = "") -> tuple[list[LogLine], int, int]:
        """Lines newer than ``after`` (oldest first), the newest sequence number, and how many were missed.

        ``missed`` is non-zero when the buffer has already dropped lines the caller never saw.
        """
        with self._lines_lock:
            firsts = [q[0].seq for q in ((self._app, self._other) if not app_only else (self._app,)) if q]
            lasts = [q[-1].seq for q in ((self._app, self._other) if not app_only else (self._app,)) if q]
        oldest, newest = (min(firsts) if firsts else 0), (max(lasts) if lasts else 0)
        missed = max(0, oldest - after - 1) if after and oldest else 0
        selected = [line for line in self._select(min_level, app_only, search) if line.seq > after]
        return selected[-limit:], newest, missed


# Endpoints that something polls on a timer. Their access lines would drown the log they are
# polling for: the container hits /healthz every minute, and an open Logs page asks for new
# lines every 2 seconds, which alone would push real lines out of the buffer.
QUIET_PATHS = ("/healthz", "/system/logs/tail", "/scan/status")


class AccessNoiseFilter(logging.Filter):
    """Drop access-log lines for polled endpoints (see QUIET_PATHS)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        return not (isinstance(args, tuple) and len(args) >= 3 and str(args[2]).startswith(QUIET_PATHS))


BUFFER = RingBufferHandler()
_installed = False
_install_lock = threading.Lock()
_debug_until = 0.0


def install(level: str = "info") -> RingBufferHandler:
    """Attach the buffer to the root and uvicorn loggers. Call after uvicorn has configured logging."""
    global _installed
    with _install_lock:
        for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
            logger = logging.getLogger(name)
            if BUFFER not in logger.handlers and (name in ("", "uvicorn.access") or not logger.propagate):
                logger.addHandler(BUFFER)
        access = logging.getLogger("uvicorn.access")
        if not any(isinstance(f, AccessNoiseFilter) for f in access.filters):
            access.addFilter(AccessNoiseFilter())
        if not _installed:
            set_level(level)
        _installed = True
    return BUFFER


def current_level() -> str:
    return logging.getLevelName(logging.getLogger(APP_LOGGER).level).lower()


def set_level(level: str, temporary: bool = False) -> float:
    """Set the application log level. ``temporary`` (debug from the UI) expires after DEBUG_SECONDS."""
    global _debug_until
    logging.getLogger(APP_LOGGER).setLevel(LEVELS.get(level, logging.INFO))
    _debug_until = time.time() + DEBUG_SECONDS if temporary and level == "debug" else 0.0
    return _debug_until


def debug_expires_at() -> float:
    """When a temporary debug session ends (0 when the level is not temporary)."""
    return _debug_until


def enforce_level(now: float | None = None) -> None:
    """Fall back to info once a temporary debug session has run out. Cheap; call it often."""
    global _debug_until
    if _debug_until and (now or time.time()) >= _debug_until:
        _debug_until = 0.0
        logging.getLogger(APP_LOGGER).setLevel(logging.INFO)
        logging.getLogger(__name__).info("debug logging expired, back to info")


class _SecureRotatingFileHandler(RotatingFileHandler):
    """Rotating file handler that keeps every file readable only by the account we run as."""

    def _open(self):
        stream = super()._open()
        with contextlib.suppress(OSError):  # e.g. a filesystem without POSIX permissions
            os.chmod(self.baseFilename, 0o600)
        return stream


LOG_DIR_NAME = "logs"
LOG_FILE_NAME = "periodica.log"
_file_handler: _SecureRotatingFileHandler | None = None
_file_lock = threading.Lock()


def log_dir(config_dir: Path) -> Path:
    return Path(config_dir) / LOG_DIR_NAME


def log_files(config_dir: Path) -> list[tuple[str, int, float]]:
    """(name, size, modified) for the current log file and its rotated copies, newest first."""
    directory = log_dir(config_dir)
    if not directory.is_dir():
        return []
    files = []
    for path in directory.iterdir():
        if path.is_file() and not path.is_symlink() and path.name.startswith(LOG_FILE_NAME):
            stat = path.stat()
            files.append((path.name, stat.st_size, stat.st_mtime))
    return sorted(files, key=lambda item: item[2], reverse=True)


def configure_file_logging(config_dir: Path, enabled: bool, max_mb: int, keep: int) -> Path | None:
    """Attach (or detach, or resize) the rotating log file. Safe to call again with new settings."""
    global _file_handler
    logger = logging.getLogger(APP_LOGGER)
    with _file_lock:
        if _file_handler is not None:
            logger.removeHandler(_file_handler)
            _file_handler.close()
            _file_handler = None
        if not enabled:
            return None
        directory = log_dir(config_dir)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            handler = _SecureRotatingFileHandler(
                directory / LOG_FILE_NAME, maxBytes=max_mb * 1024 * 1024, backupCount=max(0, keep - 1),
                encoding="utf-8", delay=False,
            )
        except OSError as exc:
            logging.getLogger(__name__).warning("could not open the log file in %s: %s", directory, exc)
            return None
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        logger.addHandler(handler)
        _file_handler = handler
        return Path(handler.baseFilename)


def apply_settings(settings, env) -> None:
    """Put the stored logging settings into effect (level and log file; size 0 means no file at all)."""
    set_level(settings.log_level)
    configure_file_logging(env.config_dir, settings.log_file_mb > 0, settings.log_file_mb,
                           settings.log_files_kept)

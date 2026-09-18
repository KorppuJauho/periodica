"""Authentication: Argon2id password hashes, server-side sessions, CSRF tokens, login throttling."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .db import Database

SESSION_COOKIE = "nl_session"
SESSION_IDLE_SECONDS = 7 * 86400
SESSION_ABSOLUTE_SECONDS = 30 * 86400
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PASSWORD = 10
MAX_PASSWORD = 256

_hasher = PasswordHasher()  # argon2id with library defaults
_DUMMY_HASH = _hasher.hash(secrets.token_hex(16))


class AuthError(ValueError):
    pass


@dataclass(frozen=True)
class Session:
    user_id: int
    username: str
    csrf_token: str
    token_hash: str


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def validate_new_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"password must be at least {MIN_PASSWORD} characters")
    if len(password) > MAX_PASSWORD:
        raise AuthError("password is too long")


class AuthService:
    def __init__(self, db: Database):
        self.db = db

    def has_users(self) -> bool:
        with self.db.connect() as conn:
            return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def create_first_user(self, username: str, password: str) -> int:
        if not USERNAME_RE.fullmatch(username):
            raise AuthError("username must be 3-32 letters, digits, dot, dash or underscore")
        validate_new_password(password)
        password_hash = _hasher.hash(password)
        with self.db.connect() as conn:
            # Checked inside the same write transaction so two setup requests can't both win.
            if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                raise AuthError("setup has already been completed")
            cur = conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                               (username, password_hash, time.time()))
            return int(cur.lastrowid or 0)

    def verify(self, username: str, password: str) -> int | None:
        if len(password) > MAX_PASSWORD:
            return None
        with self.db.connect() as conn:
            row = conn.execute("SELECT id, password_hash FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            with contextlib.suppress(VerificationError):
                _hasher.verify(_DUMMY_HASH, password)  # equalise timing
            return None
        try:
            _hasher.verify(row["password_hash"], password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return None
        if _hasher.check_needs_rehash(row["password_hash"]):
            with self.db.connect() as conn:
                conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (_hasher.hash(password), row["id"]))
        return int(row["id"])

    def change_password(self, user_id: int, current: str, new: str, keep_token_hash: str) -> None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None or self.verify(row["username"], current) is None:
            raise AuthError("current password is incorrect")
        validate_new_password(new)
        with self.db.connect() as conn:
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (_hasher.hash(new), user_id))
            conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, keep_token_hash))

    def create_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.db.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE last_seen < ? OR created_at < ?",
                         (now - SESSION_IDLE_SECONDS, now - SESSION_ABSOLUTE_SECONDS))
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, csrf_token, created_at, last_seen) VALUES (?, ?, ?, ?, ?)",
                (_token_hash(token), user_id, secrets.token_urlsafe(32), now, now),
            )
        return token

    def get_session(self, token: str | None) -> Session | None:
        if not token or len(token) > 128:
            return None
        token_hash = _token_hash(token)
        now = time.time()
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT s.user_id, s.csrf_token, s.created_at, s.last_seen, u.username FROM sessions s "
                "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?", (token_hash,)).fetchone()
            if row is None:
                return None
            if now - row["last_seen"] > SESSION_IDLE_SECONDS or now - row["created_at"] > SESSION_ABSOLUTE_SECONDS:
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
                return None
            if now - row["last_seen"] > 60:
                conn.execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now, token_hash))
        return Session(row["user_id"], row["username"], row["csrf_token"], token_hash)

    def delete_session(self, token: str | None) -> None:
        if token:
            with self.db.connect() as conn:
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def csrf_matches(expected: str, provided: str | None) -> bool:
    return bool(provided) and hmac.compare_digest(expected, provided or "")


class LoginThrottle:
    """Per-IP and per-username failure tracking with exponential lockout."""

    WINDOW = 15 * 60
    FREE_ATTEMPTS = 5
    MAX_LOCK = 15 * 60

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._failures[key]
        while q and now - q[0] > self.WINDOW:
            q.popleft()
        return q

    def retry_after(self, keys: list[str], now: float | None = None) -> int:
        now = now or time.time()
        wait = 0.0
        with self._lock:
            for key in keys:
                q = self._prune(key, now)
                if len(q) >= self.FREE_ATTEMPTS:
                    lock = min(30 * 2 ** (len(q) - self.FREE_ATTEMPTS), self.MAX_LOCK)
                    wait = max(wait, q[-1] + lock - now)
        return max(0, int(wait + 0.999))

    def failure(self, keys: list[str], now: float | None = None) -> None:
        now = now or time.time()
        with self._lock:
            for key in keys:
                self._prune(key, now).append(now)

    def success(self, keys: list[str]) -> None:
        with self._lock:
            for key in keys:
                self._failures.pop(key, None)

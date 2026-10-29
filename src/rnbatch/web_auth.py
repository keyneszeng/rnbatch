"""SQLite user accounts and authenticated sessions for the isolated Web App.

stdlib-only. A public reverse proxy must terminate TLS; do not expose this
module directly on a network interface. No user self-registration is provided.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

SESSION_SECONDS = 12 * 60 * 60
USERNAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{2,31}$")
_SCRYPT_N = 2 ** 14
_MAX_FAILED = 5
_BLOCK_SECONDS = 120


@dataclass(frozen=True)
class Session:
    user_id: str
    username: str
    csrf: str
    expires_at: int


class AuthStore:
    def __init__(self, database: str | Path):
        self.path = Path(database).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.path.exists():
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        self._lock = threading.RLock()
        self._attempts: dict[str, tuple[int, float]] = {}
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                  id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                  salt BLOB NOT NULL, password_hash BLOB NOT NULL,
                  created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                  csrf TEXT NOT NULL, expires_at INTEGER NOT NULL,
                  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_expiry
                  ON sessions(expires_at);
            """)

    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _hash_password(password: str, salt: bytes) -> bytes:
        return hashlib.scrypt(password.encode("utf-8"), salt=salt,
                              n=_SCRYPT_N, r=8, p=1, dklen=32)

    def add_user(self, username: str, password: str) -> str:
        if not isinstance(username, str) or not USERNAME_PATTERN.fullmatch(username):
            raise ValueError("用户名需为 3-32 位，以英文字母开头，可包含数字、._-")
        if not isinstance(password, str) or not (12 <= len(password) <= 128):
            raise ValueError("密码长度须为 12-128 个字符")
        salt = os.urandom(16)
        hashed = self._hash_password(password, salt)
        ident = uuid.uuid4().hex
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO users VALUES (?,?,?,?,?)",
                       (ident, username, salt, hashed, int(time.time())))
        return ident

    def has_users(self) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def sign_in(self, username: str, password: str, remote: str) -> tuple[str, Session] | None:
        # Throttle per remote address; do not trust forwarded-for headers.
        # A reverse proxy should also provide durable request-rate limiting.
        key = remote or "unknown"
        now = time.monotonic()
        with self._lock:
            attempts, last = self._attempts.get(key, (0, 0))
            if attempts >= _MAX_FAILED and now - last < _BLOCK_SECONDS:
                raise ValueError("登录失败次数过多，请稍后再试")
            if now - last >= _BLOCK_SECONDS:
                attempts = 0
            valid_input = isinstance(username, str) and isinstance(password, str)
            if not valid_input or len(username) > 32 or len(password) > 128:
                self._attempts[key] = (attempts + 1, now)
                return None
            with self._connect() as db:
                row = db.execute(
                    "SELECT id, salt, password_hash FROM users WHERE username=?",
                    (username,)
                ).fetchone()
                # Always compute expensive hash, including nonexistent users.
                salt = row[1] if row else bytes(16)
                computed = self._hash_password(password, salt)
                if not row or not hmac.compare_digest(computed, row[2]):
                    self._attempts[key] = (attempts + 1, now)
                    return None
                self._attempts.pop(key, None)
                token = secrets.token_urlsafe(32)
                csrf = secrets.token_urlsafe(32)
                expiry = int(time.time()) + SESSION_SECONDS
                db.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(time.time()),))
                db.execute(
                    "INSERT INTO sessions VALUES(?,?,?,?)",
                    (self._token_hash(token), row[0], csrf, expiry))
            return token, Session(row[0], username, csrf, expiry)

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def verify(self, token: str) -> Session | None:
        if not isinstance(token, str) or len(token) < 30 or len(token) > 128:
            return None
        try:
            token_hash = self._token_hash(token)
        except UnicodeEncodeError:
            return None
        with self._lock, self._connect() as db:
            result = db.execute(
                """SELECT users.id, users.username, sessions.csrf, sessions.expires_at
                   FROM sessions JOIN users ON users.id = sessions.user_id
                   WHERE sessions.token_hash=? AND sessions.expires_at > ?""",
                (token_hash, int(time.time()))
            ).fetchone()
        return Session(*result) if result else None

    def sign_out(self, token: str):
        if not isinstance(token, str) or len(token) > 128:
            return
        try:
            token_hash = self._token_hash(token)
        except UnicodeEncodeError:
            return
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

"""Password hashing and server-side sessions.

Stdlib only — PBKDF2-HMAC-SHA256 with a per-user salt. Sessions are opaque
random tokens stored in SQLite, so a logout or a password change can revoke
them immediately.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from fastapi import Cookie, Depends, HTTPException, Request, status

from . import config
from .db import get_conn

PBKDF2_ROUNDS = 240_000
TOKEN_BYTES = 32


# --------------------------------------------------------------------------
# passwords
# --------------------------------------------------------------------------


def hash_password(password: str, *, rounds: int = PBKDF2_ROUNDS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return f"pbkdf2_sha256${rounds}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected, actual)


# --------------------------------------------------------------------------
# login throttling (in-process; a single-node app on the VPN)
# --------------------------------------------------------------------------

_attempts: dict[str, list[float]] = {}


def _prune(key: str, now: float) -> list[float]:
    window = [t for t in _attempts.get(key, []) if now - t < config.LOGIN_LOCKOUT_SECONDS]
    if window:
        _attempts[key] = window
    else:
        _attempts.pop(key, None)
    return window


def is_locked_out(username: str) -> int:
    """Return seconds remaining in a lockout, or 0 when the user may try."""
    key = username.strip().lower()
    now = time.time()
    window = _prune(key, now)
    if len(window) < config.LOGIN_MAX_ATTEMPTS:
        return 0
    return int(config.LOGIN_LOCKOUT_SECONDS - (now - min(window))) + 1


def record_failure(username: str) -> None:
    key = username.strip().lower()
    now = time.time()
    _prune(key, now)
    _attempts.setdefault(key, []).append(now)


def clear_failures(username: str) -> None:
    _attempts.pop(username.strip().lower(), None)


# --------------------------------------------------------------------------
# users and sessions
# --------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_user(conn: sqlite3.Connection, username: str, password: str) -> int:
    username = username.strip()
    if not username:
        raise ValueError("username is required")
    if len(password) < 10:
        raise ValueError("password must be at least 10 characters")
    cursor = conn.execute(
        "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
        (username, hash_password(password), _now().isoformat()),
    )
    return int(cursor.lastrowid)


def set_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    if len(password) < 10:
        raise ValueError("password must be at least 10 characters")
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (hash_password(password), user_id),
    )
    # Force every other device to log in again.
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username.strip(),)
    ).fetchone()
    if row is None:
        # Equalise timing between "no such user" and "wrong password".
        hash_password(secrets.token_hex(8), rounds=PBKDF2_ROUNDS)
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return row


def start_session(conn: sqlite3.Connection, user_id: int) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(TOKEN_BYTES)
    now = _now()
    expires = now + timedelta(days=config.SESSION_DAYS)
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, user_id, now.isoformat(), expires.isoformat()),
    )
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(),))
    return token, expires


def end_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def lookup_session(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    row = conn.execute(
        """
        SELECT u.id, u.username, s.token, s.expires_at
          FROM sessions s JOIN users u ON u.id = s.user_id
         WHERE s.token = ?
        """,
        (token,),
    ).fetchone()
    if row is None:
        return None
    try:
        expires = datetime.fromisoformat(row["expires_at"])
    except ValueError:
        return None
    if expires <= _now():
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        return None
    return row


# --------------------------------------------------------------------------
# FastAPI dependencies
# --------------------------------------------------------------------------


class CurrentUser:
    def __init__(self, user_id: int, username: str, token: str):
        self.id = user_id
        self.username = username
        self.token = token


async def current_user(
    session: str | None = Cookie(default=None, alias=config.COOKIE_NAME),
) -> CurrentUser:
    if not session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    with get_conn() as conn:
        row = lookup_session(conn, session)
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")
    return CurrentUser(row["id"], row["username"], row["token"])


async def require_csrf(request: Request) -> None:
    """Reject cross-site writes.

    The session cookie is SameSite=Lax, so a cross-site POST never carries it;
    this header requirement closes the remaining same-site-form gap. A browser
    cannot set a custom header cross-origin without a preflight, and no CORS
    headers are served, so the preflight fails.
    """
    if request.headers.get("x-winelog-app") != "1":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing app header")


AuthedUser = Depends(current_user)
CsrfGuard = Depends(require_csrf)

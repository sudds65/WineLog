"""Runtime configuration, all overridable by environment variable."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


DATA_DIR = _env_path("WINELOG_DATA_DIR", PROJECT_DIR / "data")
DB_PATH = _env_path("WINELOG_DB", DATA_DIR / "winelog.db")
UPLOAD_DIR = _env_path("WINELOG_UPLOAD_DIR", DATA_DIR / "receipts")
STATIC_DIR = BASE_DIR / "static"

# Cookie is marked Secure only when the app is served over HTTPS. A plain-HTTP
# deployment on the VPN needs this off or the browser drops the session cookie.
COOKIE_SECURE = _env_bool("WINELOG_COOKIE_SECURE", False)
COOKIE_NAME = os.environ.get("WINELOG_COOKIE_NAME", "winelog_session")
SESSION_DAYS = int(os.environ.get("WINELOG_SESSION_DAYS", "30"))

MAX_UPLOAD_BYTES = int(os.environ.get("WINELOG_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))

# Failed logins per username before a temporary lockout.
LOGIN_MAX_ATTEMPTS = int(os.environ.get("WINELOG_LOGIN_MAX_ATTEMPTS", "8"))
LOGIN_LOCKOUT_SECONDS = int(os.environ.get("WINELOG_LOGIN_LOCKOUT_SECONDS", "300"))

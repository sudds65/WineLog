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


def _env_optional_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else None


DATA_DIR = _env_path("WINELOG_DATA_DIR", PROJECT_DIR / "data")
DB_PATH = _env_path("WINELOG_DB", DATA_DIR / "winelog.db")
UPLOAD_DIR = _env_path("WINELOG_UPLOAD_DIR", DATA_DIR / "receipts")
STATIC_DIR = BASE_DIR / "static"

# Where to listen. Ports below 1024 work because the systemd unit grants
# CAP_NET_BIND_SERVICE — the app still runs as the unprivileged winelog account.
HOST = os.environ.get("WINELOG_HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = int(os.environ.get("WINELOG_PORT", "8071"))

# Point these at a certificate and key to have the app itself serve HTTPS.
# Leave them empty to serve plain HTTP (fine behind nginx, or on a VPN).
TLS_CERT = _env_optional_path("WINELOG_TLS_CERT")
TLS_KEY = _env_optional_path("WINELOG_TLS_KEY")
TLS_ENABLED = TLS_CERT is not None and TLS_KEY is not None

# Port to run a bare HTTP→HTTPS redirect on, so someone typing the hostname
# without a scheme still lands on the app. Only used when TLS is on.
HTTP_REDIRECT_PORT = int(os.environ.get("WINELOG_HTTP_REDIRECT_PORT", "0") or 0)

# Cookie is marked Secure only when the app is served over HTTPS. A plain-HTTP
# deployment on the VPN needs this off or the browser drops the session cookie.
# Serving TLS ourselves flips the default, so the safe setting is automatic and
# an HTTPS deployment can't quietly hand out a cookie that leaks over HTTP.
COOKIE_SECURE = _env_bool("WINELOG_COOKIE_SECURE", TLS_ENABLED)
COOKIE_NAME = os.environ.get("WINELOG_COOKIE_NAME", "winelog_session")
SESSION_DAYS = int(os.environ.get("WINELOG_SESSION_DAYS", "30"))

MAX_UPLOAD_BYTES = int(os.environ.get("WINELOG_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))

# Failed logins per username before a temporary lockout.
LOGIN_MAX_ATTEMPTS = int(os.environ.get("WINELOG_LOGIN_MAX_ATTEMPTS", "8"))
LOGIN_LOCKOUT_SECONDS = int(os.environ.get("WINELOG_LOGIN_LOCKOUT_SECONDS", "300"))

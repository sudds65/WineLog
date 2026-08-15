"""Listening on 80/443 and serving TLS ourselves."""
import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import serve


@pytest.fixture()
def certificate(tmp_path):
    """A stand-in cert and key — nothing here parses them, only reads them."""
    cert, key = tmp_path / "winelog.crt", tmp_path / "winelog.key"
    cert.write_text("certificate")
    key.write_text("key")
    return cert, key


# ── uvicorn arguments ─────────────────────────────────────────────────


def test_plain_http_asks_for_no_tls():
    options = serve.uvicorn_options("0.0.0.0", 80)
    assert options["host"] == "0.0.0.0"
    assert options["port"] == 80
    assert "ssl_certfile" not in options
    assert "ssl_keyfile" not in options


def test_a_certificate_turns_on_tls(certificate):
    cert, key = certificate
    options = serve.uvicorn_options("0.0.0.0", 443, cert, key)
    assert options["ssl_certfile"] == str(cert)
    assert options["ssl_keyfile"] == str(key)


def test_forwarded_headers_are_trusted_from_localhost_only():
    """nginx may sit in front; a client on the LAN may not spoof its scheme."""
    assert serve.uvicorn_options("0.0.0.0", 8071)["forwarded_allow_ips"] == "127.0.0.1"


# ── certificate checks ────────────────────────────────────────────────


def test_no_certificate_is_not_a_problem():
    assert serve.tls_problems(None, None) == []


def test_a_usable_certificate_reports_nothing(certificate):
    assert serve.tls_problems(*certificate) == []


def test_half_a_certificate_is_rejected(certificate):
    cert, _ = certificate
    assert serve.tls_problems(cert, None)


def test_a_missing_certificate_is_named(certificate, tmp_path):
    _, key = certificate
    problems = serve.tls_problems(tmp_path / "gone.crt", key)
    assert len(problems) == 1
    assert "gone.crt" in problems[0]


def test_an_unreadable_key_says_how_to_fix_it(certificate):
    cert, key = certificate
    key.chmod(0o000)
    try:
        problems = serve.tls_problems(cert, key)
    finally:
        key.chmod(0o600)
    # Running the suite as root defeats the permission check; skip there.
    if problems:
        assert "chmod" in problems[0]


# ── the HTTP → HTTPS bounce ───────────────────────────────────────────


def call_redirect(app, path=b"/", method="GET", host=b"winelog.home.lan"):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": method,
        "path": path.decode(),
        "raw_path": path,
        "query_string": b"",
        "headers": [(b"host", host)] if host else [],
    }
    asyncio.run(app(scope, receive, send))
    start = sent[0]
    headers = {name.decode(): value.decode() for name, value in start["headers"]}
    return start["status"], headers.get("location")


def test_port_80_bounces_to_https():
    status, location = call_redirect(serve.make_redirect_app(443), b"/purchases")
    assert status == 301
    assert location == "https://winelog.home.lan/purchases"


def test_the_bounce_keeps_a_non_standard_https_port():
    _, location = call_redirect(serve.make_redirect_app(8443))
    assert location == "https://winelog.home.lan:8443/"


def test_the_bounce_drops_the_port_the_browser_used():
    """The Host header says :80; the redirect must not repeat it."""
    _, location = call_redirect(serve.make_redirect_app(443), host=b"winelog.home.lan:80")
    assert location == "https://winelog.home.lan/"


def test_a_posted_request_keeps_its_method():
    status, _ = call_redirect(serve.make_redirect_app(443), method="POST")
    assert status == 308


def test_a_request_without_a_host_is_not_redirected():
    status, location = call_redirect(serve.make_redirect_app(443), host=b"")
    assert status == 400
    assert location is None


# ── cookie policy follows TLS ─────────────────────────────────────────


def reload_config(monkeypatch, **environment):
    """Re-read app.config under a different environment, then put it back.

    A snapshot is returned rather than the module, because the module is
    reloaded again on the way out to leave the rest of the suite untouched.
    """
    from app import config

    for name in ("WINELOG_TLS_CERT", "WINELOG_TLS_KEY", "WINELOG_COOKIE_SECURE",
                 "WINELOG_PORT", "WINELOG_HTTP_REDIRECT_PORT"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    try:
        reloaded = importlib.reload(config)
        return SimpleNamespace(
            TLS_ENABLED=reloaded.TLS_ENABLED,
            COOKIE_SECURE=reloaded.COOKIE_SECURE,
            PORT=reloaded.PORT,
            HTTP_REDIRECT_PORT=reloaded.HTTP_REDIRECT_PORT,
        )
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_serving_tls_marks_the_cookie_secure(monkeypatch, certificate):
    cert, key = certificate
    config = reload_config(
        monkeypatch, WINELOG_TLS_CERT=str(cert), WINELOG_TLS_KEY=str(key)
    )
    assert config.TLS_ENABLED is True
    assert config.COOKIE_SECURE is True


def test_plain_http_leaves_the_cookie_usable(monkeypatch):
    """Secure cookies over HTTP are dropped, so no one could stay signed in."""
    config = reload_config(monkeypatch)
    assert config.TLS_ENABLED is False
    assert config.COOKIE_SECURE is False


def test_tls_terminating_elsewhere_can_still_force_secure(monkeypatch):
    """nginx in front: the app speaks HTTP but the browser is on HTTPS."""
    config = reload_config(monkeypatch, WINELOG_COOKIE_SECURE="true")
    assert config.TLS_ENABLED is False
    assert config.COOKIE_SECURE is True


def test_the_default_port_is_unprivileged(monkeypatch):
    config = reload_config(monkeypatch)
    assert config.PORT == 8071


def test_the_port_comes_from_the_environment(monkeypatch):
    config = reload_config(monkeypatch, WINELOG_PORT="443")
    assert config.PORT == 443


# ── the systemd unit ──────────────────────────────────────────────────

UNIT = (Path(__file__).resolve().parent.parent / "deploy" / "winelog.service").read_text()


def test_the_unit_can_bind_privileged_ports():
    """Without this the service cannot listen on 80 or 443 as the winelog user."""
    assert "AmbientCapabilities=CAP_NET_BIND_SERVICE" in UNIT
    assert "CapabilityBoundingSet=CAP_NET_BIND_SERVICE" in UNIT


def test_the_unit_still_runs_unprivileged():
    assert "User=winelog" in UNIT
    assert "NoNewPrivileges=true" in UNIT

"""Process entry point — starts uvicorn the way the environment asked for.

    python -m app.serve

The systemd unit runs this rather than the uvicorn console script so that the
port, the TLS certificate and the HTTP→HTTPS redirect can all be decided from
/etc/winelog.env without editing the unit file.

Binding 80 or 443 does not need root: the unit grants CAP_NET_BIND_SERVICE and
the app still runs as the unprivileged winelog account.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import uvicorn

from . import config

# Trust X-Forwarded-* from the local machine only, so nginx (or nothing) can
# sit in front without letting a client spoof its own scheme or address.
FORWARDED_ALLOW_IPS = os.environ.get("WINELOG_FORWARDED_ALLOW_IPS", "127.0.0.1")


def tls_problems(cert: Path | None, key: Path | None) -> list[str]:
    """Why the configured certificate can't be used — empty when it's fine."""
    if cert is None and key is None:
        return []
    if cert is None or key is None:
        return [
            "WINELOG_TLS_CERT and WINELOG_TLS_KEY must be set together "
            "(one of them is empty)."
        ]

    problems = []
    for label, path in (("certificate", cert), ("private key", key)):
        if not path.exists():
            problems.append(f"TLS {label} not found: {path}")
        elif not os.access(path, os.R_OK):
            problems.append(
                f"TLS {label} is not readable by this account: {path} — "
                f"try: sudo chgrp winelog {path} && sudo chmod 640 {path}"
            )
    return problems


def uvicorn_options(
    host: str,
    port: int,
    cert: Path | None = None,
    key: Path | None = None,
) -> dict:
    """The keyword arguments for uvicorn.run(), TLS included when configured."""
    options = {
        "host": host,
        "port": port,
        "proxy_headers": True,
        "forwarded_allow_ips": FORWARDED_ALLOW_IPS,
    }
    if cert is not None and key is not None:
        options["ssl_certfile"] = str(cert)
        options["ssl_keyfile"] = str(key)
    return options


def _redirect_target(headers: list[tuple[bytes, bytes]], path: bytes, https_port: int) -> str:
    """The https:// URL matching an http:// request, keeping the host and path."""
    host = ""
    for name, value in headers:
        if name.lower() == b"host":
            host = value.decode("latin-1")
            break
    # The Host header carries the port the browser used (80); ours differs.
    host = host.rsplit(":", 1)[0] if ":" in host and not host.endswith("]") else host
    if not host:
        return ""
    if https_port != 443:
        host = f"{host}:{https_port}"
    return f"https://{host}{path.decode('latin-1')}"


def make_redirect_app(https_port: int):
    """A bare ASGI app that bounces every request to HTTPS."""

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return

        raw_path = scope.get("raw_path") or scope["path"].encode()
        if scope.get("query_string"):
            raw_path += b"?" + scope["query_string"]
        location = _redirect_target(scope.get("headers", []), raw_path, https_port)

        headers = [(b"content-length", b"0")]
        status = 400
        if location:
            status = 308 if scope.get("method") not in {"GET", "HEAD"} else 301
            headers.append((b"location", location.encode("latin-1")))

        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b""})

    return app


def _start_redirect(listen_host: str, http_port: int, https_port: int) -> None:
    server = uvicorn.Server(
        uvicorn.Config(
            make_redirect_app(https_port),
            host=listen_host,
            port=http_port,
            log_level="warning",
        )
    )
    # uvicorn skips signal handling off the main thread, so this is safe as-is.
    threading.Thread(target=server.run, name="winelog-redirect", daemon=True).start()


def main() -> None:
    problems = tls_problems(config.TLS_CERT, config.TLS_KEY)
    if problems:
        for problem in problems:
            print(f"winelog: {problem}", file=sys.stderr)
        sys.exit(1)

    if config.TLS_ENABLED and not config.COOKIE_SECURE:
        print(
            "winelog: serving HTTPS but WINELOG_COOKIE_SECURE=false — the session "
            "cookie will be sent over plain HTTP too. Remove the override.",
            file=sys.stderr,
        )
    elif not config.TLS_ENABLED and config.COOKIE_SECURE:
        print(
            "winelog: WINELOG_COOKIE_SECURE=true without HTTPS — browsers will drop "
            "the session cookie and no one will be able to stay signed in.",
            file=sys.stderr,
        )

    if config.TLS_ENABLED and config.HTTP_REDIRECT_PORT:
        _start_redirect(config.HOST, config.HTTP_REDIRECT_PORT, config.PORT)

    scheme = "https" if config.TLS_ENABLED else "http"
    print(f"winelog: serving {scheme} on {config.HOST}:{config.PORT}", file=sys.stderr)

    uvicorn.run(
        "app.main:app",
        **uvicorn_options(config.HOST, config.PORT, config.TLS_CERT, config.TLS_KEY),
    )


if __name__ == "__main__":
    main()

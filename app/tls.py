"""The certificate the app serves HTTPS with — inspect, validate, swap.

A certificate uploaded through the web app lands in the data directory, which
is the one place the service account can write. It takes precedence over
anything named in /etc/winelog.env, so uploading always wins over the
bootstrap certificate the installer generated.

Swapping is live. uvicorn hands one ssl.SSLContext to the event loop and asks
it for a certificate on every new connection, so calling load_cert_chain() on
that context re-binds the certificate without dropping a request. serve.py
registers the context here at startup; without it (a shell import, or nginx
terminating TLS in front) an upload is stored and takes effect on restart.
"""
from __future__ import annotations

import ipaddress
import ssl
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

from . import config

CERT_NAME = "server.crt"
KEY_NAME = "server.key"

# Certificates and keys are a few KB; a megabyte is already absurd.
MAX_PEM_BYTES = 1024 * 1024


class CertificateError(Exception):
    """An uploaded certificate that cannot be served, with the reason why."""


# ── the live context ──────────────────────────────────────────────────

_live_context: ssl.SSLContext | None = None


def register_live_context(context: ssl.SSLContext | None) -> None:
    """Called by serve.py with the context uvicorn is serving from."""
    global _live_context
    _live_context = context


def live_context() -> ssl.SSLContext | None:
    return _live_context


# ── where the certificate lives ───────────────────────────────────────


def uploaded_pair() -> tuple[Path, Path]:
    return config.UPLOADED_TLS_CERT, config.UPLOADED_TLS_KEY


def active_pair() -> tuple[Path | None, Path | None, str]:
    """The certificate in use, and where it came from."""
    pair = config.active_tls_pair()
    if pair is None:
        return None, None, "none"
    source = "uploaded" if pair == uploaded_pair() else "configured"
    return pair[0], pair[1], source


# ── reading a certificate ─────────────────────────────────────────────


def _expiry(certificate: x509.Certificate) -> tuple[datetime, datetime]:
    """Validity window as aware UTC, across cryptography versions."""
    try:  # cryptography >= 42
        return certificate.not_valid_before_utc, certificate.not_valid_after_utc
    except AttributeError:  # pragma: no cover - older cryptography
        return (
            certificate.not_valid_before.replace(tzinfo=timezone.utc),
            certificate.not_valid_after.replace(tzinfo=timezone.utc),
        )


def _common_name(name: x509.Name) -> str:
    values = name.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
    if values:
        return str(values[0].value)
    return name.rfc4514_string()


def subject_names(certificate: x509.Certificate) -> list[str]:
    """Every name the certificate is valid for: the SANs, or the CN if none."""
    try:
        san = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
    except x509.ExtensionNotFound:
        return [_common_name(certificate.subject)]

    names = [str(entry) for entry in san.get_values_for_type(x509.DNSName)]
    names += [str(entry) for entry in san.get_values_for_type(x509.IPAddress)]
    return names


def describe(certificate: x509.Certificate) -> dict:
    """Everything the settings screen shows about a certificate."""
    not_before, not_after = _expiry(certificate)
    now = datetime.now(timezone.utc)
    fingerprint = certificate.fingerprint(hashes.SHA256()).hex()

    public_key = certificate.public_key()
    key_size = getattr(public_key, "key_size", None)
    key_type = type(public_key).__name__.replace("PublicKey", "").lstrip("_")

    return {
        "subject": _common_name(certificate.subject),
        "issuer": _common_name(certificate.issuer),
        "self_signed": certificate.issuer == certificate.subject,
        "names": subject_names(certificate),
        "not_before": not_before.date().isoformat(),
        "not_after": not_after.date().isoformat(),
        "days_remaining": (not_after - now).days,
        "expired": now > not_after,
        "not_yet_valid": now < not_before,
        "key": f"{key_type} {key_size}" if key_size else key_type,
        "fingerprint_sha256": ":".join(
            fingerprint[i : i + 2] for i in range(0, len(fingerprint), 2)
        ).upper(),
    }


def load(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def describe_active() -> dict | None:
    cert, _, _ = active_pair()
    if cert is None or not cert.exists():
        return None
    try:
        return describe(load(cert))
    except Exception:  # a certificate we cannot parse is one we cannot describe
        return None


# ── hostname matching ─────────────────────────────────────────────────


def covers(certificate: x509.Certificate, hostname: str) -> bool:
    """Whether a browser asking for this hostname would accept the certificate.

    Only used to warn before installing something that would lock the browser
    out — the real check is the browser's, and it is stricter.
    """
    hostname = hostname.strip().rstrip(".").lower()
    if not hostname:
        return True
    hostname = hostname.rsplit(":", 1)[0] if _has_port(hostname) else hostname

    try:
        address = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        address = None

    for name in subject_names(certificate):
        name = name.lower().rstrip(".")
        if address is not None:
            try:
                if ipaddress.ip_address(name) == address:
                    return True
            except ValueError:
                continue
            continue
        if name == hostname:
            return True
        # One wildcard, leftmost label only: *.home.lan matches a.home.lan.
        if name.startswith("*.") and hostname.count(".") == name.count("."):
            if hostname.split(".", 1)[1:] == name.split(".", 1)[1:]:
                return True
    return False


def _has_port(hostname: str) -> bool:
    if hostname.startswith("["):  # [::1]:8443
        return "]:" in hostname
    return hostname.count(":") == 1


# ── validating an upload ──────────────────────────────────────────────


def _first_certificate(pem: bytes) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(pem)
    except Exception as exc:
        raise CertificateError(
            "That is not a PEM certificate. It should start with "
            "-----BEGIN CERTIFICATE-----."
        ) from exc


def _private_key(pem: bytes):
    try:
        return serialization.load_pem_private_key(pem, password=None)
    except TypeError as exc:
        raise CertificateError(
            "That private key is passphrase-protected. The service starts "
            "unattended and cannot be prompted — decrypt it first with: "
            "openssl rsa -in key.pem -out decrypted.pem"
        ) from exc
    except Exception as exc:
        raise CertificateError(
            "That is not a PEM private key. It should start with "
            "-----BEGIN PRIVATE KEY----- or -----BEGIN RSA PRIVATE KEY-----."
        ) from exc


def validate(cert_pem: bytes, key_pem: bytes) -> x509.Certificate:
    """Check a pair is servable, or explain what is wrong with it."""
    if len(cert_pem) > MAX_PEM_BYTES or len(key_pem) > MAX_PEM_BYTES:
        raise CertificateError("That file is far too large to be a certificate.")

    certificate = _first_certificate(cert_pem)
    key = _private_key(key_pem)

    if key.public_key().public_numbers() != certificate.public_key().public_numbers():
        raise CertificateError(
            "That private key does not go with that certificate. Check you "
            "picked the pair your CA issued together."
        )

    described = describe(certificate)
    if described["expired"]:
        raise CertificateError(
            f"That certificate expired on {described['not_after']}. "
            "Issue a new one rather than installing this."
        )
    if described["not_yet_valid"]:
        raise CertificateError(
            f"That certificate is not valid until {described['not_before']}. "
            "Check the server's clock if that looks wrong."
        )

    # The last word belongs to the machinery that will actually serve it: if
    # OpenSSL accepts the pair here, it will accept it on the live context.
    with tempfile.TemporaryDirectory() as scratch:
        cert_path = Path(scratch) / CERT_NAME
        key_path = Path(scratch) / KEY_NAME
        cert_path.write_bytes(cert_pem)
        key_path.write_bytes(key_pem)
        try:
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert_path, key_path)
        except ssl.SSLError as exc:
            raise CertificateError(f"OpenSSL would not load that pair: {exc}") from exc

    return certificate


# ── installing it ─────────────────────────────────────────────────────


def install(cert_pem: bytes, key_pem: bytes) -> dict:
    """Store a validated pair and, if we are serving TLS, bind it live.

    Returns the new certificate's details plus whether it is already in use.
    On failure the previous certificate is put back, still serving.
    """
    certificate = validate(cert_pem, key_pem)
    cert_path, key_path = uploaded_pair()
    cert_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    previous = None
    if cert_path.exists() and key_path.exists():
        previous = (cert_path.read_bytes(), key_path.read_bytes())

    _write(cert_path, cert_pem, 0o644)
    _write(key_path, key_pem, 0o600)

    applied = False
    context = live_context()
    if context is not None:
        try:
            context.load_cert_chain(cert_path, key_path)
            applied = True
        except ssl.SSLError as exc:
            if previous is not None:
                _write(cert_path, previous[0], 0o644)
                _write(key_path, previous[1], 0o600)
                context.load_cert_chain(cert_path, key_path)
            else:
                cert_path.unlink(missing_ok=True)
                key_path.unlink(missing_ok=True)
            raise CertificateError(
                f"The certificate could not be bound, so the old one is still "
                f"in use: {exc}"
            ) from exc

    return {"certificate": describe(certificate), "applied": applied}


def _write(path: Path, payload: bytes, mode: int) -> None:
    """Write a file whole or not at all, and never widen its permissions."""
    staged = path.with_suffix(path.suffix + ".new")
    staged.write_bytes(payload)
    staged.chmod(mode)
    staged.replace(path)

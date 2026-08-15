"""Uploading a certificate and binding it to the running server."""
import socket
import ssl
import threading
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

from app import config, tls

# The TestClient sends this as the Host header.
TEST_HOST = "testserver"


def make_cert(
    common_name="testserver",
    names=("testserver",),
    valid_from_days=-1,
    valid_to_days=365,
    issuer=None,
    rsa_key=False,
):
    """A certificate and its key, both PEM. Signed by `issuer` if given."""
    key = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        if rsa_key
        else ec.generate_private_key(ec.SECP256R1())
    )
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    issuer_name, signing_key = (issuer if issuer else (subject, key))
    now = datetime.now(timezone.utc)

    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + timedelta(days=valid_from_days))
        .not_valid_after(now + timedelta(days=valid_to_days))
    )
    if names:
        entries = []
        for name in names:
            try:
                entries.append(x509.IPAddress(__import__("ipaddress").ip_address(name)))
            except ValueError:
                entries.append(x509.DNSName(name))
        builder = builder.add_extension(
            x509.SubjectAlternativeName(entries), critical=False
        )

    certificate = builder.sign(signing_key, hashes.SHA256())
    return (
        certificate.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


@pytest.fixture(autouse=True)
def clean_tls_dir():
    """No certificate installed at the start of each test, or the end."""

    def wipe():
        for path in tls.uploaded_pair():
            path.unlink(missing_ok=True)
        tls.register_live_context(None)

    wipe()
    yield
    wipe()


# ── validation ────────────────────────────────────────────────────────


def test_a_good_pair_validates():
    cert_pem, key_pem = make_cert()
    assert tls.validate(cert_pem, key_pem).subject is not None


def test_a_key_from_another_certificate_is_caught():
    cert_pem, _ = make_cert()
    _, other_key = make_cert()
    with pytest.raises(tls.CertificateError, match="does not go with"):
        tls.validate(cert_pem, other_key)


def test_an_expired_certificate_is_refused():
    cert_pem, key_pem = make_cert(valid_from_days=-400, valid_to_days=-30)
    with pytest.raises(tls.CertificateError, match="expired"):
        tls.validate(cert_pem, key_pem)


def test_a_certificate_from_the_future_is_refused():
    """Usually a wrong clock on the server rather than a bad certificate."""
    cert_pem, key_pem = make_cert(valid_from_days=5, valid_to_days=400)
    with pytest.raises(tls.CertificateError, match="not valid until"):
        tls.validate(cert_pem, key_pem)


def test_something_that_is_not_a_certificate_is_refused():
    _, key_pem = make_cert()
    with pytest.raises(tls.CertificateError, match="not a PEM certificate"):
        tls.validate(b"hello, I am a receipt", key_pem)


def test_something_that_is_not_a_key_is_refused():
    cert_pem, _ = make_cert()
    with pytest.raises(tls.CertificateError, match="not a PEM private key"):
        tls.validate(cert_pem, b"-----BEGIN PRIVATE KEY-----\nnope\n")


def test_a_passphrase_protected_key_explains_how_to_decrypt_it():
    cert_pem, _ = make_cert(rsa_key=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    encrypted = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"secret"),
    )
    with pytest.raises(tls.CertificateError, match="passphrase"):
        tls.validate(cert_pem, encrypted)


def test_an_enormous_file_is_refused():
    cert_pem, key_pem = make_cert()
    with pytest.raises(tls.CertificateError, match="too large"):
        tls.validate(b"x" * (tls.MAX_PEM_BYTES + 1), key_pem)


# ── reading one ───────────────────────────────────────────────────────


def test_describe_reports_what_the_settings_screen_shows():
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Home CA")])
    cert_pem, _ = make_cert(
        common_name="winelog.home.lan",
        names=("winelog.home.lan", "192.168.1.50"),
        issuer=(ca_name, ca_key),
    )
    described = tls.describe(x509.load_pem_x509_certificate(cert_pem))

    assert described["subject"] == "winelog.home.lan"
    assert described["issuer"] == "Home CA"
    assert described["self_signed"] is False
    assert described["names"] == ["winelog.home.lan", "192.168.1.50"]
    assert 360 <= described["days_remaining"] <= 365
    assert described["expired"] is False
    assert len(described["fingerprint_sha256"].split(":")) == 32


def test_a_self_signed_certificate_says_so():
    cert_pem, _ = make_cert()
    assert tls.describe(x509.load_pem_x509_certificate(cert_pem))["self_signed"] is True


# ── hostname matching ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "names,host,expected",
    [
        (("winelog.home.lan",), "winelog.home.lan", True),
        (("winelog.home.lan",), "winelog.home.lan:8443", True),
        (("winelog.home.lan",), "other.home.lan", False),
        (("*.home.lan",), "winelog.home.lan", True),
        (("*.home.lan",), "a.b.home.lan", False),
        (("192.168.1.50",), "192.168.1.50", True),
        (("192.168.1.50",), "192.168.1.51", False),
        (("winelog.home.lan",), "WINELOG.HOME.LAN", True),
    ],
)
def test_covers_matches_the_way_a_browser_would(names, host, expected):
    cert_pem, _ = make_cert(names=names)
    certificate = x509.load_pem_x509_certificate(cert_pem)
    assert tls.covers(certificate, host) is expected


# ── installing ────────────────────────────────────────────────────────


def test_installing_writes_the_pair_and_locks_the_key_down():
    cert_pem, key_pem = make_cert()
    result = tls.install(cert_pem, key_pem)

    cert_path, key_path = tls.uploaded_pair()
    assert cert_path.read_bytes() == cert_pem
    assert key_path.read_bytes() == key_pem
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert result["applied"] is False  # nothing serving in this process


def test_an_uploaded_certificate_beats_the_configured_one():
    cert_pem, key_pem = make_cert()
    tls.install(cert_pem, key_pem)
    path, _, source = tls.active_pair()
    assert source == "uploaded"
    assert path == config.UPLOADED_TLS_CERT


def test_installing_rebinds_a_live_context():
    """The point of the whole thing: a real handshake serves the new
    certificate, on the same context, with nothing restarted."""
    tls.install(*make_cert(common_name="first", names=("localhost",)))

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(*tls.uploaded_pair())
    tls.register_live_context(context)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    port = listener.getsockname()[1]

    def answer_one_connection():
        raw, _ = listener.accept()
        try:
            with context.wrap_socket(raw, server_side=True) as tunnel:
                tunnel.recv(1)
        except OSError:  # the client hanging up mid-handshake is fine here
            pass

    def subject_on_the_wire():
        server = threading.Thread(target=answer_one_connection, daemon=True)
        server.start()
        client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client.check_hostname = False
        client.verify_mode = ssl.CERT_NONE
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with client.wrap_socket(raw) as tunnel:
                der = tunnel.getpeercert(binary_form=True)
        server.join(timeout=5)
        return x509.load_der_x509_certificate(der).subject.rfc4514_string()

    try:
        assert subject_on_the_wire() == "CN=first"

        result = tls.install(*make_cert(common_name="second", names=("localhost",)))
        assert result["applied"] is True
        assert result["certificate"]["subject"] == "second"

        assert subject_on_the_wire() == "CN=second"
    finally:
        listener.close()


def test_a_failed_rebind_puts_the_old_certificate_back(monkeypatch):
    good_cert, good_key = make_cert(common_name="working")
    tls.install(good_cert, good_key)

    class Sulky:
        def load_cert_chain(self, cert, key):
            if tls.load(cert).subject.rfc4514_string() != "CN=working":
                raise ssl.SSLError("nope")

    tls.register_live_context(Sulky())
    new_cert, new_key = make_cert(common_name="replacement")

    with pytest.raises(tls.CertificateError, match="old one is still in use"):
        tls.install(new_cert, new_key)

    # On disk and in the context, the working certificate survived.
    assert tls.load(tls.uploaded_pair()[0]).subject.rfc4514_string() == "CN=working"


# ── requesting one from a CA ──────────────────────────────────────────


def test_a_request_carries_every_name_the_server_answers_to():
    """AD CS only reads SANs from inside the request, so they must be there."""
    csr_pem, key_pem = tls.make_request(
        "winelog.suddarth.local", ["winelog", "winelog.local", "192.168.86.150"]
    )
    request = x509.load_pem_x509_csr(csr_pem)
    assert request.is_signature_valid
    assert tls._common_name(request.subject) == "winelog.suddarth.local"

    san = request.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == [
        "winelog.suddarth.local", "winelog", "winelog.local",
    ]
    assert [str(ip) for ip in san.get_values_for_type(x509.IPAddress)] == ["192.168.86.150"]

    # The key is usable and unencrypted, so the service can start unattended.
    assert serialization.load_pem_private_key(key_pem, password=None)


def test_a_request_does_not_repeat_the_common_name():
    csr_pem, _ = tls.make_request("winelog", ["winelog"])
    san = x509.load_pem_x509_csr(csr_pem).extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    assert san.get_values_for_type(x509.DNSName) == ["winelog"]


def test_a_signed_request_comes_back_installable():
    """The whole round trip: request, sign it as a CA would, install it."""
    csr_pem, key_pem = tls.make_request("winelog.suddarth.local", ["192.168.86.150"])
    request = x509.load_pem_x509_csr(csr_pem)

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Windows Issuing CA")])
    now = datetime.now(timezone.utc)
    issued = (
        x509.CertificateBuilder()
        .subject_name(request.subject)
        .issuer_name(ca_name)
        .public_key(request.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=397))
        .add_extension(
            request.extensions.get_extension_for_class(x509.SubjectAlternativeName).value,
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    cert_pem = issued.public_bytes(serialization.Encoding.PEM)

    result = tls.install(cert_pem, key_pem)
    assert result["certificate"]["issuer"] == "Windows Issuing CA"
    assert "192.168.86.150" in result["certificate"]["names"]


def test_a_chain_led_by_the_ca_certificate_is_named_as_such():
    """Some CA exports put the issuer first, which OpenSSL would reject."""
    ca_cert, ca_key = make_cert(common_name="Issuing CA")
    leaf_cert, leaf_key = make_cert(common_name="winelog")
    with pytest.raises(tls.CertificateError, match="starts with the CA"):
        tls.validate(ca_cert + leaf_cert, leaf_key)


# ── the API ───────────────────────────────────────────────────────────


def test_reading_the_certificate_needs_a_session(client):
    assert client.get("/api/tls").status_code == 401


def test_with_no_certificate_it_says_so(auth_client):
    state = auth_client.get("/api/tls").json()
    assert state["certificate"] is None
    assert state["source"] == "none"
    assert state["serving_https"] is False


def test_uploading_a_certificate_stores_and_reports_it(auth_client):
    cert_pem, key_pem = make_cert(common_name="winelog", names=(TEST_HOST,))
    response = auth_client.post(
        "/api/tls",
        files={"certificate": ("winelog.crt", cert_pem), "private_key": ("winelog.key", key_pem)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["certificate"]["subject"] == "winelog"
    assert body["source"] == "uploaded"

    assert auth_client.get("/api/tls").json()["certificate"]["subject"] == "winelog"


def test_one_file_holding_both_the_certificate_and_the_key_works(auth_client):
    cert_pem, key_pem = make_cert(names=(TEST_HOST,))
    response = auth_client.post(
        "/api/tls", files={"certificate": ("bundle.pem", cert_pem + key_pem)}
    )
    assert response.status_code == 200


def test_a_certificate_for_another_hostname_is_held_back(auth_client):
    cert_pem, key_pem = make_cert(common_name="elsewhere", names=("elsewhere.example.com",))
    response = auth_client.post(
        "/api/tls",
        files={"certificate": ("x.crt", cert_pem), "private_key": ("x.key", key_pem)},
    )
    assert response.status_code == 409
    assert "elsewhere.example.com" in response.json()["detail"]
    assert not tls.uploaded_pair()[0].exists()


def test_the_hostname_guard_can_be_overridden(auth_client):
    cert_pem, key_pem = make_cert(common_name="elsewhere", names=("elsewhere.example.com",))
    response = auth_client.post(
        "/api/tls",
        files={"certificate": ("x.crt", cert_pem), "private_key": ("x.key", key_pem)},
        data={"force": "true"},
    )
    assert response.status_code == 200
    assert tls.uploaded_pair()[0].exists()


def test_a_broken_upload_is_rejected_with_a_reason(auth_client):
    _, key_pem = make_cert()
    response = auth_client.post(
        "/api/tls",
        files={"certificate": ("notes.txt", b"just some text"), "private_key": ("x.key", key_pem)},
    )
    assert response.status_code == 400
    assert "PEM certificate" in response.json()["detail"]


def test_installing_a_certificate_needs_the_app_header(auth_client):
    cert_pem, key_pem = make_cert(names=(TEST_HOST,))
    response = auth_client.post(
        "/api/tls",
        files={"certificate": ("x.crt", cert_pem), "private_key": ("x.key", key_pem)},
        headers={"X-WineLog-App": ""},
    )
    assert response.status_code == 403

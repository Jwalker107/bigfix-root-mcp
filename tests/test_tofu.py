"""Trust on first use for session relevance, against a real local TLS server.

The library skips pinning whenever a transport is injected, so these tests
cannot use httpx.MockTransport: a threaded HTTPS server on loopback answers
/api/query with a self-signed certificate it can swap mid-test. Pins land in
the per-test HOME from conftest.isolated_home, never the real ~/.bigfix.
"""

import datetime
import http.server
import ipaddress
import json
import ssl
import threading
import tomllib

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from bigfix_root_mcp import connection, sessionrelevance


def self_signed(tmp_path, name):
    """A self-signed cert for 127.0.0.1, like a stock BigFix root server's."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / f"{name}.pem", tmp_path / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.server.authorizations.append(self.headers.get("Authorization"))
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.dumps({"result": [7], "plural": False, "type": "integer"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep test output quiet
        pass


class _TLSServer(http.server.ThreadingHTTPServer):
    """Wraps each connection with whatever certificate is current."""

    def get_request(self):
        sock, addr = super().get_request()
        return self.tls.wrap_socket(sock, server_side=True), addr


@pytest.fixture
def tls_server(tmp_path, monkeypatch):
    server = _TLSServer(("127.0.0.1", 0), _Handler)
    server.tls = self_signed(tmp_path, "first")
    server.authorizations = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    monkeypatch.setenv("BES_ROOT_SERVER", f"https://127.0.0.1:{port}")
    monkeypatch.setenv("BES_USER_NAME", "testoperator")
    monkeypatch.setenv("BES_PASSWORD", "<PASSWORD>")
    monkeypatch.delenv("BES_SSL_VERIFY", raising=False)
    connection.reset_connection()
    yield server
    connection.reset_connection()
    server.shutdown()
    server.server_close()


def known_certs(home):
    path = home / ".bigfix" / ".remote_session_known_certs.toml"
    return tomllib.loads(path.read_text())["certs"] if path.is_file() else {}


def host_port(server):
    return f"127.0.0.1:{server.server_address[1]}"


def test_self_signed_cert_is_pinned_on_first_use(tls_server, isolated_home):
    assert sessionrelevance.evaluate("7")["result"] == [7]
    assert host_port(tls_server) in known_certs(isolated_home)


def test_pinned_cert_keeps_working_after_restart(tls_server, isolated_home):
    sessionrelevance.evaluate("7")
    pinned = known_certs(isolated_home)[host_port(tls_server)]["sha256"]
    connection.reset_connection()  # a fresh client, as after a server restart
    assert sessionrelevance.evaluate("7")["result"] == [7]
    assert known_certs(isolated_home)[host_port(tls_server)]["sha256"] == pinned


def test_changed_cert_is_refused_before_credentials_are_sent(tls_server, isolated_home, tmp_path):
    sessionrelevance.evaluate("7")
    sent_before = len(tls_server.authorizations)
    tls_server.tls = self_signed(tmp_path, "impostor")
    connection.reset_connection()
    with pytest.raises(sessionrelevance.SessionRelevanceUnavailableError) as excinfo:
        sessionrelevance.evaluate("7")
    message = str(excinfo.value)
    assert "changed" in message
    # says where the pin lives, so an operator who verified the new cert can remove it
    assert ".remote_session_known_certs.toml" in message
    assert len(tls_server.authorizations) == sent_before


def test_explicit_false_still_skips_verification_and_pins_nothing(
    tls_server, isolated_home, monkeypatch
):
    monkeypatch.setenv("BES_SSL_VERIFY", "false")
    assert sessionrelevance.evaluate("7")["result"] == [7]
    assert known_certs(isolated_home) == {}


def test_verify_true_rejects_self_signed_and_pins_nothing(tls_server, isolated_home, monkeypatch):
    monkeypatch.setenv("BES_SSL_VERIFY", "true")
    with pytest.raises(sessionrelevance.SessionRelevanceUnavailableError, match="tls"):
        sessionrelevance.evaluate("7")
    assert known_certs(isolated_home) == {}
    assert tls_server.authorizations == []

"""Trust on first use, against a real TLS server with a real certificate.

Stubbing SSL would test the stubs. These tests generate a self-signed
certificate, serve HTTPS on a loopback port with it, and drive the client
against it -- which is the only way to be sure that "trusted" and "refused"
mean what they are supposed to, since every one of them is a property of
the handshake rather than of our own code.

The certificates come from openssl, and each test skips rather than fails
if it is not installed, so the suite still runs on a machine without it.
"""

import http.server
import socket
import ssl
import subprocess
import threading

import pytest

from proxima.api import certs
from proxima.api.client import (
    CertificateMismatch,
    CertificateUntrusted,
    ProxmoxAPI,
    ProxmoxError,
)
from proxima.config import Config

OPENSSL = "openssl"


def _make_certificate(directory, common_name):
    """A self-signed certificate, the way a Proxmox node's own CA looks."""
    key = directory / f"{common_name}.key"
    crt = directory / f"{common_name}.crt"
    result = subprocess.run(
        [
            OPENSSL,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(crt),
            "-days",
            "1",
            "-nodes",
            "-subj",
            f"/CN={common_name}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not crt.exists():
        pytest.skip(f"no openssl to build a test certificate: {result.stderr[:200]}")
    return key, crt


class Server:
    """An HTTPS server on a loopback port, with a certificate of its own."""

    def __init__(self, key, crt):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(crt), str(key))
        self.httpd = http.server.HTTPServer(
            ("127.0.0.1", 0), http.server.BaseHTTPRequestHandler
        )
        self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.socket.getsockname()[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        # Idempotent: one test stops the server early to put another in its
        # place, and the fixture still tears it down afterwards.
        if self.httpd is None:
            return
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.httpd = None


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    directory = tmp_path_factory.mktemp("certs")
    return {
        "first": _make_certificate(directory, "proxmox-one.invalid"),
        "second": _make_certificate(directory, "proxmox-two.invalid"),
    }


@pytest.fixture
def server(certificates):
    running = Server(*certificates["first"])
    yield running
    running.stop()


def api_for(server, **kwargs):
    return ProxmoxAPI("127.0.0.1", port=server.port, timeout=10, **kwargs)


def reach(api):
    """Make the smallest possible request. TLS is what is under test."""
    return api._request("GET", "/", authed=False)


# -- fingerprints -------------------------------------------------------


def test_the_fingerprint_is_the_one_openssl_prints(certificates):
    """It has to be, or nobody can check it against the server."""
    _key, crt = certificates["first"]
    result = subprocess.run(
        [OPENSSL, "x509", "-in", str(crt), "-noout", "-fingerprint", "-sha256"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("openssl cannot read the certificate back")
    expected = result.stdout.strip().split("=", 1)[1]

    der = ssl.PEM_cert_to_DER_cert(crt.read_text())
    assert certs.fingerprint(der) == expected


def test_fetch_reads_the_certificate_without_trusting_it(server):
    info = certs.fetch("127.0.0.1", server.port, timeout=10)
    assert info["sha256"].count(":") == 31, "not a SHA-256 fingerprint"
    # Details are best effort, but a self-signed cert names itself twice.
    assert "proxmox-one.invalid" in info.get("subject", "")
    assert info.get("subject") == info.get("issuer")


def test_fetch_of_something_that_is_not_there_returns_none():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    assert certs.fetch("127.0.0.1", closed, timeout=2) is None


# -- the flow -----------------------------------------------------------


def test_an_unknown_certificate_is_a_question_not_a_failure(server):
    """What an ordinary Proxmox looks like on the first connection."""
    api = api_for(server)
    with pytest.raises(CertificateUntrusted) as raised:
        reach(api)
    refusal = raised.value
    assert refusal.host == "127.0.0.1"
    assert refusal.port == server.port
    assert refusal.info["sha256"], "the user was asked without being shown anything"


def test_trusting_it_once_is_enough(server):
    config = Config()
    api = api_for(server)
    with pytest.raises(CertificateUntrusted) as raised:
        reach(api)

    certs.trust(config, "127.0.0.1", server.port, raised.value.info)
    api.trust(certs.pinned(config, "127.0.0.1", server.port))

    # Reaches the server: the bare handler answers 501, which is an HTTP
    # reply and therefore proof the handshake was accepted.
    with pytest.raises(ProxmoxError) as after:
        reach(api)
    assert "HTTP" in str(after.value), f"did not get through TLS: {after.value}"

    # And a second client built from the stored pin needs no prompt at all.
    fresh = api_for(server, fingerprint=certs.pinned(config, "127.0.0.1", server.port))
    with pytest.raises(ProxmoxError) as again:
        reach(fresh)
    assert "HTTP" in str(again.value)


def test_a_changed_certificate_stops_everything(server, certificates):
    """The case the whole arrangement exists to catch.

    A different server answering on the same address must not be able to
    collect a credential, however plausible its certificate looks.
    """
    config = Config()
    info = certs.fetch("127.0.0.1", server.port, timeout=10)
    certs.trust(config, "127.0.0.1", server.port, info)
    pin = certs.pinned(config, "127.0.0.1", server.port)

    server.stop()
    impostor = Server(*certificates["second"])
    # Same port is not available, so pin the first cert against the second
    # server: identical in shape to the certificate being swapped.
    try:
        api = ProxmoxAPI("127.0.0.1", port=impostor.port, fingerprint=pin)
        with pytest.raises(CertificateMismatch) as raised:
            reach(api)
        assert raised.value.expected == pin
        assert "changed" in str(raised.value)
    finally:
        impostor.stop()


def test_there_is_no_way_to_switch_checking_off(server):
    """Deliberately: the prompt is the answer to a self-signed server.

    An option to skip the check is an option people set once and forget,
    and it is set on exactly the connection that carries root credentials.
    """
    import inspect

    assert "verify_ssl" not in inspect.signature(ProxmoxAPI).parameters
    assert "verify_ssl" not in Config()


def test_a_pin_beats_ca_verification_rather_than_adding_to_it(server):
    """A pinned certificate is one the user already approved.

    It will not chain to a public CA and its name will not match the
    address, and neither is a reason to refuse it a second time.
    """
    info = certs.fetch("127.0.0.1", server.port, timeout=10)
    api = api_for(server, fingerprint=info["sha256"])
    with pytest.raises(ProxmoxError) as raised:
        reach(api)
    assert "HTTP" in str(raised.value), f"the pin was not honoured: {raised.value}"


# -- the store ----------------------------------------------------------


def test_pins_are_kept_per_host_and_port():
    config = Config()
    certs.trust(config, "one.invalid", 8006, {"sha256": "AA:BB"})
    certs.trust(config, "one.invalid", 9006, {"sha256": "CC:DD"})
    assert certs.pinned(config, "one.invalid", 8006) == "AA:BB"
    assert certs.pinned(config, "one.invalid", 9006) == "CC:DD"
    assert certs.pinned(config, "two.invalid", 8006) is None


def test_forgetting_a_pin_asks_again_next_time():
    config = Config()
    certs.trust(config, "one.invalid", 8006, {"sha256": "AA:BB"})
    assert certs.forget(config, "one.invalid", 8006) is True
    assert certs.pinned(config, "one.invalid", 8006) is None
    assert certs.forget(config, "one.invalid", 8006) is False


def test_a_pin_survives_being_saved_and_read_back(tmp_path, monkeypatch):
    monkeypatch.setenv("PROXIMA_CONFIG_DIR", str(tmp_path))
    config = Config()
    certs.trust(config, "one.invalid", 8006, {"sha256": "AA:BB", "subject": "CN=one"})
    assert config.save()
    assert certs.pinned(Config.load(), "one.invalid", 8006) == "AA:BB"

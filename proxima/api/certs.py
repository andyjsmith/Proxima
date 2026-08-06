"""Trust on first use for a server's TLS certificate.

Proxmox installs with a certificate signed by the cluster's own CA, which no
public root knows about. Strict verification therefore fails on a healthy,
correctly configured server, and the usual answer -- turn verification off
-- means the client will talk to anything that answers on port 8006, which
is a poor trade for a program that carries root credentials.

So this is the trade browsers and ssh settled on instead. The first time a
server is seen, its certificate is shown with its fingerprint and the user
decides once. That fingerprint is then pinned, and from then on the
identity is checked against it rather than against a CA. A server whose
certificate changes stops the client and says so, which is the case the
whole arrangement exists to catch.

Two things follow from pinning that are worth stating:

  * The hostname is not checked once a pin is in force. A self-signed or
    CA-issued Proxmox certificate frequently has a CN that does not match
    the address you reach it on, and under pinning the fingerprint *is* the
    identity -- checking the name as well would reject the exact
    certificate the user already approved.
  * A pin only replaces CA verification. If a certificate does chain to a
    public root, nothing is pinned and ordinary verification keeps working,
    so a proper certificate keeps behaving properly when it is renewed.
"""

import contextlib
import hashlib
import logging
import os
import socket
import ssl
import tempfile
import time

log = logging.getLogger(__name__)

# Where the pins live in the config.
STORE = "trusted_certs"


def store_key(host, port):
    return f"{host}:{int(port)}"


def fingerprint(der):
    """The SHA-256 fingerprint of a DER certificate, as it is usually shown.

    Colon-separated uppercase hex, which is what `openssl x509 -fingerprint
    -sha256` prints and what the Proxmox web UI shows under Certificates --
    so the two can be compared by eye without transcribing anything.
    """
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[index : index + 2] for index in range(0, len(digest), 2))


def _name(pairs):
    """Flatten the nested tuples getpeercert() uses for a distinguished name."""
    parts = []
    for group in pairs or ():
        for entry in group:
            with contextlib.suppress(ValueError):
                key, value = entry
                parts.append(f"{key}={value}")
    return ", ".join(parts)


def _details(der):
    """Subject, issuer and validity, best effort.

    getpeercert() returns an empty dict for a connection that did not
    verify, which is exactly the connection this is called about, so the
    certificate is decoded from its DER instead. The decoder is private API;
    it is worth using because the alternative is parsing X.509 by hand, and
    it is wrapped because nothing here is important enough to fail over --
    the fingerprint is the part that matters and it never comes from here.
    """
    handle, path = tempfile.mkstemp(suffix=".pem", prefix="proxima-cert-")
    try:
        with os.fdopen(handle, "w") as pem:
            pem.write(ssl.DER_cert_to_PEM_cert(der))
        decoded = ssl._ssl._test_decode_cert(path)
    except Exception as exc:
        log.info("could not decode the certificate for display: %s", exc)
        return {}
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)

    return {
        "subject": _name(decoded.get("subject")),
        "issuer": _name(decoded.get("issuer")),
        "not_before": decoded.get("notBefore", ""),
        "not_after": decoded.get("notAfter", ""),
    }


def fetch(host, port, timeout=10):
    """The certificate a server presents, without judging it.

    Returns a dict with at least 'sha256', or None if the server could not
    be reached at all.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with (
            socket.create_connection((host, int(port)), timeout) as raw,
            context.wrap_socket(raw, server_hostname=host) as tls,
        ):
            der = tls.getpeercert(binary_form=True)
    except OSError as exc:
        log.warning("could not fetch the certificate for %s:%s: %s", host, port, exc)
        return None
    if not der:
        return None

    info = {"host": host, "port": int(port), "sha256": fingerprint(der)}
    info.update(_details(der))
    return info


# -- the pin store -----------------------------------------------------
#
# Plain functions over the config dict, like the rest of the settings: the
# store is small, it is read on every connect, and it has to survive being
# written by one window and read by another.


def pinned(config, host, port):
    """The fingerprint trusted for a server, or None."""
    entry = (config.get(STORE) or {}).get(store_key(host, port))
    if isinstance(entry, dict):
        return entry.get("sha256") or None
    # Tolerate a bare string, which is what a hand-edited config would have.
    return entry or None


def trust(config, host, port, info):
    """Pin a certificate. Returns the fingerprint that was stored."""
    known = dict(config.get(STORE) or {})
    known[store_key(host, port)] = {
        "sha256": info["sha256"],
        "subject": info.get("subject", ""),
        "issuer": info.get("issuer", ""),
        "not_after": info.get("not_after", ""),
        "trusted": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    config[STORE] = known
    log.info("pinned the certificate for %s:%s (%s)", host, port, info["sha256"])
    return info["sha256"]


def forget(config, host, port):
    """Drop a pin, so the next connection asks again."""
    known = dict(config.get(STORE) or {})
    if known.pop(store_key(host, port), None) is None:
        return False
    config[STORE] = known
    log.info("forgot the pinned certificate for %s:%s", host, port)
    return True

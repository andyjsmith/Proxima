"""Proxmox VE API client.

Uses only the standard library so the app has no pip dependencies beyond
PyGObject itself -- which on MSYS2 has to come from pacman anyway, and mixing
a pip virtualenv with an MSYS2 PyGObject reliably breaks.

Authentication model, which is not obvious from the docs:

  * POST /access/ticket returns a ticket that goes in the PVEAuthCookie
    cookie. Read requests need only that.
  * Every write (POST/PUT/DELETE) additionally needs the CSRFPreventionToken
    header from the same response.
  * Tickets are valid for two hours and can be renewed by posting the ticket
    back as the password, with no need to re-prompt.
  * With TFA enabled the first response carries NeedTFA=1 and a partial
    ticket beginning with "PVE2FA!". That partial ticket is then posted back
    as the password together with the OTP code.
"""

import http.client
import json
import logging
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import certs
from .models import Guest, Node, parse_spice_clients

log = logging.getLogger(__name__)

DEFAULT_PORT = 8006
TICKET_LIFETIME = 7200  # what PVE grants
RENEW_MARGIN = 900  # renew with 15 minutes to spare


class ProxmoxError(Exception):
    """Any API level failure."""

    def __init__(self, message, status=None, errors=None):
        super().__init__(message)
        self.status = status
        self.errors = errors or {}


class AuthError(ProxmoxError):
    """Credentials were rejected, or the ticket expired and could not renew."""


class TwoFactorRequired(ProxmoxError):
    """First factor accepted; a second factor is needed to finish."""

    def __init__(self, partial_ticket, csrf, message="Two-factor code required"):
        super().__init__(message)
        self.partial_ticket = partial_ticket
        self.csrf = csrf


class CertificateUntrusted(ProxmoxError):
    """No pin for this server, and its certificate has no public CA behind it.

    Carries what the user needs to decide: the fingerprint, and whatever
    else could be read off the certificate. Not a failure so much as a
    question -- see api/certs.py.
    """

    def __init__(self, host, port, info, reason=""):
        super().__init__(
            f"the certificate for {host}:{port} is not trusted yet"
            + (f" ({reason})" if reason else "")
        )
        self.host = host
        self.port = port
        self.info = info or {}
        self.reason = reason


class CertificateMismatch(ProxmoxError):
    """A pin exists and the server presented something else.

    Either the certificate was replaced, or something is between the client
    and the server. Never resolved without asking.
    """

    def __init__(self, host, port, expected, info):
        super().__init__(
            f"the certificate for {host}:{port} has changed since it was trusted"
        )
        self.host = host
        self.port = port
        self.expected = expected
        self.info = info or {}


def _permissive_context():
    """A context that checks nothing itself.

    Used only where something else does the checking: with a pin, where the
    fingerprint is the identity, or where the user has explicitly turned
    verification off.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _pinned_opener(context, host, port, expected):
    """An opener that hangs up on anything but the pinned certificate.

    The check has to happen on the socket, after the handshake and before a
    single byte of request goes out -- which is why this reaches into the
    connection rather than using a verify callback: Python's ssl module has
    no hook for "accept this one certificate and no other".
    """

    class PinnedConnection(http.client.HTTPSConnection):
        def connect(self):
            super().connect()
            der = self.sock.getpeercert(binary_form=True)
            got = certs.fingerprint(der) if der else ""
            if got != expected:
                self.close()
                raise CertificateMismatch(
                    host, port, expected, {"sha256": got} if got else {}
                )

    class PinnedHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(PinnedConnection, req, context=context)

    return urllib.request.build_opener(PinnedHandler)


class ProxmoxAPI:
    """Thread-safe-enough Proxmox client.

    Requests happen on worker threads; the lock only guards the ticket, which
    is the sole piece of mutable shared state.
    """

    def __init__(self, host, port=DEFAULT_PORT, timeout=20, fingerprint=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        # The pinned SHA-256, if this server has been trusted before. It
        # takes precedence over CA verification: a pinned certificate is one
        # the user has already looked at and approved.
        self.fingerprint = fingerprint or None
        self._lock = threading.RLock()
        self._ticket = None
        self._csrf = None
        self._issued = 0.0
        self._username = None
        self._password = None  # kept only to renew after expiry
        self._build_opener()
        # Whether this login may use the QEMU monitor. Starts optimistic and
        # is switched off for good the first time the server says the
        # privilege is missing, so a token without VM.Monitor costs one
        # refused call rather than one before every console.
        self.monitor_available = True

    # -- TLS -----------------------------------------------------------

    def _build_opener(self):
        """Decide how this connection's certificate will be judged.

        Two ways and no third: against a fingerprint the user has already
        approved, or against the public CAs. There is deliberately no way to
        skip the question -- an unknown certificate is something to look at
        once, not something to turn checking off for.
        """
        if self.fingerprint:
            self._context = _permissive_context()
            self._opener = _pinned_opener(
                self._context, self.host, self.port, self.fingerprint
            )
        else:
            self._context = ssl.create_default_context()
            self._opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=self._context)
            )

    def trust(self, fingerprint):
        """Pin a fingerprint and use it from the next request onwards."""
        self.fingerprint = fingerprint or None
        self._build_opener()

    def certificate(self):
        """What this server is presenting, for the user to look at."""
        return certs.fetch(self.host, self.port, timeout=self.timeout)

    def _untrusted(self, reason):
        """Turn a CA verification failure into a question about the cert."""
        return CertificateUntrusted(self.host, self.port, self.certificate(), reason)

    # -- plumbing ------------------------------------------------------

    @property
    def base_url(self):
        return f"https://{self.host}:{self.port}/api2/json"

    @property
    def username(self):
        return self._username

    @property
    def ticket(self):
        with self._lock:
            return self._ticket

    def _request(self, method, path, data=None, params=None, authed=True):
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)

        body = None
        headers = {"Accept": "application/json", "User-Agent": "proxima/0.1"}

        if data is not None:
            body = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        if authed:
            with self._lock:
                if not self._ticket:
                    raise AuthError("not logged in")
                headers["Cookie"] = f"PVEAuthCookie={self._ticket}"
                if method != "GET" and self._csrf:
                    headers["CSRFPreventionToken"] = self._csrf

        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        # Every call, with how long it took. This is the record that says
        # whether a console that never opened was waiting on the server or
        # never asked it anything -- worth the two lines it costs.
        started = time.monotonic()
        log.debug("%s %s", method, path)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                payload = response.read()
                log.debug(
                    "%s %s -> %s in %.2fs (%d bytes)",
                    method,
                    path,
                    response.status,
                    time.monotonic() - started,
                    len(payload),
                )
        except urllib.error.HTTPError as exc:
            error = self._http_error(exc)
            log.warning(
                "%s %s -> HTTP %s in %.2fs: %s",
                method,
                path,
                exc.code,
                time.monotonic() - started,
                error,
            )
            raise error from None
        except CertificateMismatch:
            # Raised from inside the connection, so it arrives here rather
            # than at the caller. It is a refusal, not a network error, and
            # must never be reworded into one.
            log.error(
                "%s:%s presented a certificate that does not match its pin",
                self.host,
                self.port,
            )
            raise
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLCertVerificationError) and not (
                self.fingerprint
            ):
                # No pin yet and no public CA behind it -- which is what an
                # ordinary Proxmox install looks like. A question for the
                # user rather than a failure.
                log.info(
                    "%s:%s is not trusted yet: %s", self.host, self.port, exc.reason
                )
                raise self._untrusted(str(exc.reason)) from None
            log.warning(
                "%s %s failed after %.2fs: %s",
                method,
                path,
                time.monotonic() - started,
                exc.reason,
            )
            raise ProxmoxError(f"cannot reach {self.host}: {exc.reason}") from None
        except TimeoutError:
            log.warning(
                "%s %s timed out after %.2fs", method, path, time.monotonic() - started
            )
            raise ProxmoxError(f"timed out talking to {self.host}") from None

        if not payload:
            return None
        try:
            return json.loads(payload.decode("utf-8"))
        except ValueError:
            raise ProxmoxError("server returned malformed JSON") from None

    @staticmethod
    def _http_error(exc):
        detail = ""
        errors = {}
        try:
            parsed = json.loads(exc.read().decode("utf-8"))
            errors = parsed.get("errors") or {}
            detail = parsed.get("message") or ""
            if errors:
                detail += " " + "; ".join(f"{k}: {v}" for k, v in errors.items())
        except Exception:
            pass
        detail = (detail or exc.reason or "").strip()

        if exc.code in (401, 403):
            return AuthError(
                detail or "authentication failed", status=exc.code, errors=errors
            )
        return ProxmoxError(
            f"HTTP {exc.code}: {detail}".rstrip(": "), status=exc.code, errors=errors
        )

    def _data(self, result):
        if result is None:
            return None
        if isinstance(result, dict) and "data" in result:
            return result["data"]
        return result

    def get(self, path, params=None):
        self._ensure_ticket()
        return self._data(self._request("GET", path, params=params))

    def post(self, path, data=None):
        self._ensure_ticket()
        return self._data(self._request("POST", path, data=data or {}))

    def put(self, path, data=None):
        self._ensure_ticket()
        return self._data(self._request("PUT", path, data=data or {}))

    def delete(self, path):
        self._ensure_ticket()
        return self._data(self._request("DELETE", path))

    # -- authentication ------------------------------------------------

    def login(self, username, password, realm=None, otp=None):
        """Obtain a ticket. Raises TwoFactorRequired if a code is needed."""
        if realm and "@" not in username:
            username = f"{username}@{realm}"

        data = {"username": username, "password": password}
        if otp:
            data["otp"] = otp

        result = self._data(
            self._request("POST", "/access/ticket", data=data, authed=False)
        )
        if not result or not result.get("ticket"):
            raise AuthError("server accepted the request but returned no ticket")

        ticket = result["ticket"]
        csrf = result.get("CSRFPreventionToken")

        # A partial ticket means the first factor passed and PVE now wants
        # the second. It is posted back as the password, not as the ticket.
        if result.get("NeedTFA") or ticket.startswith("PVE2FA!"):
            raise TwoFactorRequired(ticket, csrf)

        with self._lock:
            self._ticket = ticket
            self._csrf = csrf
            self._issued = time.monotonic()
            self._username = result.get("username", username)
            self._password = password
        return self._username

    def login_tfa(self, username, partial_ticket, code, realm=None):
        """Finish a two-factor login using the partial ticket."""
        if realm and "@" not in username:
            username = f"{username}@{realm}"
        return self.login(username, partial_ticket, otp=code)

    def restore_ticket(self, username, ticket, csrf):
        """Adopt a previously saved ticket without a password round trip."""
        with self._lock:
            self._username = username
            self._ticket = ticket
            self._csrf = csrf
            self._issued = time.monotonic()
            self._password = None
        # Cheapest authenticated call there is; proves the ticket still lives.
        self.get("/version")
        return username

    def _ensure_ticket(self):
        with self._lock:
            if not self._ticket:
                raise AuthError("not logged in")
            age = time.monotonic() - self._issued
            if age < TICKET_LIFETIME - RENEW_MARGIN:
                return
            username, ticket, password = self._username, self._ticket, self._password

        # Renewing with the ticket itself is the documented path and avoids
        # storing the password; fall back to the password only if we have it.
        for secret in (ticket, password):
            if not secret:
                continue
            try:
                result = self._data(
                    self._request(
                        "POST",
                        "/access/ticket",
                        authed=False,
                        data={"username": username, "password": secret},
                    )
                )
            except ProxmoxError:
                continue
            if result and result.get("ticket") and not result.get("NeedTFA"):
                with self._lock:
                    self._ticket = result["ticket"]
                    self._csrf = result.get("CSRFPreventionToken")
                    self._issued = time.monotonic()
                return
        raise AuthError("session expired; please log in again")

    def logout(self):
        with self._lock:
            self._ticket = None
            self._csrf = None
            self._password = None

    # -- inventory -----------------------------------------------------

    def version(self):
        return self.get("/version") or {}

    def nodes(self):
        return [Node.from_api(row) for row in (self.get("/nodes") or [])]

    def guests(self):
        """Every VM and container in the cluster, in one call.

        /cluster/resources is the only endpoint that returns live status for
        the whole cluster at once, so it is what the sidebar polls.
        """
        rows = self.get("/cluster/resources", {"type": "vm"}) or []
        return [
            Guest.from_api(row) for row in rows if row.get("type") in ("qemu", "lxc")
        ]

    def guest_config(self, node, vmid, kind="qemu"):
        return self.get(f"/nodes/{node}/{kind}/{vmid}/config") or {}

    def rename_guest(self, node, vmid, name, kind="qemu"):
        """Rename a guest.

        The two guest types spell it differently: a VM has a 'name', a
        container has a 'hostname'. PUT .../config merges, so nothing else
        in the config is touched.
        """
        field = "hostname" if kind == "lxc" else "name"
        return self.put(f"/nodes/{node}/{kind}/{vmid}/config", {field: name})

    def guest_status(self, node, vmid, kind="qemu"):
        return self.get(f"/nodes/{node}/{kind}/{vmid}/status/current") or {}

    def guest_agent_info(self, node, vmid):
        """Guest agent ping; returns None when the agent is not answering."""
        try:
            return self.get(f"/nodes/{node}/qemu/{vmid}/agent/get-osinfo")
        except ProxmoxError:
            return None

    def guest_interfaces(self, node, vmid):
        try:
            result = self.get(f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces")
        except ProxmoxError:
            return []
        return (result or {}).get("result", []) if isinstance(result, dict) else []

    # -- cloning -------------------------------------------------------

    def next_vmid(self):
        """The lowest free VMID, as the web UI's clone dialog offers."""
        value = self.get("/cluster/nextid")
        return int(value) if value is not None else None

    def node_storages(self, node, content=None):
        """Storages on a node, optionally only those holding a content type.

        A clone needs somewhere that takes guest images -- 'images' for a VM,
        'rootdir' for a container -- so anything else (backups, ISOs) would
        only be offered to be rejected.
        """
        params = {"content": content} if content else None
        rows = self.get(f"/nodes/{node}/storage", params) or []
        return [row for row in rows if row.get("active", 1)]

    def clone_guest(
        self,
        node,
        vmid,
        newid,
        name=None,
        target=None,
        full=False,
        storage=None,
        kind="qemu",
    ):
        """Clone a guest or template. Returns the UPID of the clone task.

        A linked clone (full=False) keeps its disks as references into the
        template's, so it is instant and costs almost no space, but it can
        only be made from a template and cannot move to another storage.
        Proxmox rejects 'storage' outright unless the clone is a full one.
        """
        data = {"newid": int(newid)}
        if name:
            data["hostname" if kind == "lxc" else "name"] = name
        if target:
            data["target"] = target
        if full:
            data["full"] = 1
            if storage:
                data["storage"] = storage
        return self.post(f"/nodes/{node}/{kind}/{vmid}/clone", data)

    def delete_guest(
        self, node, vmid, kind="qemu", purge=True, destroy_unreferenced=False
    ):
        """Destroy a guest and its disks. Returns the UPID of the task.

        'purge' also strips the guest out of backup jobs and HA resources,
        which is what the web UI's "Purge from job configurations" does --
        without it a deleted guest leaves a backup job failing nightly on a
        VMID that no longer exists.
        """
        params = {}
        if purge:
            params["purge"] = 1
        if destroy_unreferenced:
            params["destroy-unreferenced-disks"] = 1
        path = f"/nodes/{node}/{kind}/{vmid}"
        if params:
            path += "?" + urllib.parse.urlencode(params)
        return self.delete(path)

    # -- power ---------------------------------------------------------

    POWER_ACTIONS = {
        "start": ("qemu", "lxc"),
        "shutdown": ("qemu", "lxc"),
        "stop": ("qemu", "lxc"),
        "reboot": ("qemu", "lxc"),
        "reset": ("qemu",),
        "suspend": ("qemu", "lxc"),
        "resume": ("qemu", "lxc"),
    }

    def power(self, node, vmid, action, kind="qemu"):
        if action not in self.POWER_ACTIONS:
            raise ValueError(f"unknown power action {action!r}")
        if kind not in self.POWER_ACTIONS[action]:
            raise ProxmoxError(f"{action} is not supported for {kind} guests")
        return self.post(f"/nodes/{node}/{kind}/{vmid}/status/{action}")

    def task_status(self, node, upid):
        return self.get(f"/nodes/{node}/tasks/{urllib.parse.quote(upid)}/status")

    def cluster_tasks(self, limit=50):
        """Recent cluster-wide tasks, newest first."""
        rows = self.get("/cluster/tasks") or []
        return rows[:limit]

    def task_log(self, node, upid, limit=200):
        rows = (
            self.get(
                f"/nodes/{node}/tasks/{urllib.parse.quote(upid)}/log", {"limit": limit}
            )
            or []
        )
        return [row.get("t", "") for row in rows]

    # -- snapshots -----------------------------------------------------

    def snapshots(self, node, vmid, kind="qemu", include_current=False):
        """Snapshots for a guest, newest first.

        Proxmox includes a synthetic entry named 'current' representing the
        running state; it is not a snapshot and must never be offered as a
        rollback target. It is worth asking for anyway when drawing the
        snapshot tree, because its 'parent' is the only thing that says
        which branch the guest is actually sitting on.
        """
        rows = self.get(f"/nodes/{node}/{kind}/{vmid}/snapshot") or []
        if not include_current:
            rows = [row for row in rows if row.get("name") != "current"]
        rows.sort(key=lambda row: row.get("snaptime") or 0, reverse=True)
        return rows

    def create_snapshot(
        self, node, vmid, name, description="", vmstate=False, kind="qemu"
    ):
        data = {"snapname": name}
        if description:
            data["description"] = description
        # vmstate saves guest RAM, which only means anything for a running
        # QEMU guest; containers reject the parameter outright.
        if vmstate and kind == "qemu":
            data["vmstate"] = 1
        return self.post(f"/nodes/{node}/{kind}/{vmid}/snapshot", data)

    def rollback_snapshot(self, node, vmid, name, kind="qemu"):
        return self.post(
            f"/nodes/{node}/{kind}/{vmid}/snapshot/"
            f"{urllib.parse.quote(str(name))}/rollback"
        )

    def delete_snapshot(self, node, vmid, name, kind="qemu"):
        return self.delete(
            f"/nodes/{node}/{kind}/{vmid}/snapshot/{urllib.parse.quote(str(name))}"
        )

    # -- notes ---------------------------------------------------------

    def guest_notes(self, node, vmid, kind="qemu"):
        config = self.guest_config(node, vmid, kind)
        return config.get("description", "") or ""

    def set_guest_notes(self, node, vmid, text, kind="qemu"):
        """Replace a guest's description.

        PUT .../config merges rather than replaces the whole config, so this
        touches nothing but the description.
        """
        return self.put(f"/nodes/{node}/{kind}/{vmid}/config", {"description": text})

    # -- configuration -------------------------------------------------

    def set_guest_config(
        self, node, vmid, changes=None, delete=None, kind="qemu", digest=None
    ):
        """Change guest configuration keys, and only those keys.

        'digest' is the checksum that came with the config being edited.
        Proxmox refuses the write if the guest has changed since, which
        turns "two people editing at once" from a silent overwrite into an
        error somebody can act on.
        """
        data = {key: value for key, value in (changes or {}).items()}
        if delete:
            data["delete"] = delete if isinstance(delete, str) else ",".join(delete)
        if digest:
            data["digest"] = digest
        if not data:
            return None
        return self.put(f"/nodes/{node}/{kind}/{vmid}/config", data)

    # Interface types a guest NIC can actually be attached to. SDN vnets
    # belong here as much as plain and OVS bridges do -- from the guest's
    # side they are the same thing, and leaving them out means an SDN
    # network cannot be chosen at all.
    BRIDGE_TYPES = ("bridge", "OVSBridge", "vnet")

    def node_bridges(self, node):
        """Everything a guest NIC can be attached to on a node.

        Filtered here rather than by the API's own 'type' parameter. The
        value meaning "anything a NIC can use" has changed across releases
        -- bridge, then any_bridge, then any_local_bridge once SDN vnets
        arrived -- and asking an older server for a type it does not know is
        an error rather than an empty list. Asking for everything and
        picking works on all of them.
        """
        names = set()
        for row in self.get(f"/nodes/{node}/network") or []:
            if row.get("type") in self.BRIDGE_TYPES and row.get("iface"):
                names.add(row["iface"])

        # And the cluster's own view of SDN, for releases that do not list
        # vnets per node. Best effort: SDN is optional, and a login without
        # the privilege for it should not cost the bridges we already have.
        try:
            for row in self.get("/cluster/sdn/vnets") or []:
                if row.get("vnet"):
                    names.add(row["vnet"])
        except ProxmoxError as exc:
            log.debug("no SDN vnets listed: %s", exc)

        return sorted(names)

    # -- guest agent ---------------------------------------------------

    def agent_ping(self, node, vmid):
        """True when the QEMU guest agent answers."""
        try:
            self.post(f"/nodes/{node}/qemu/{vmid}/agent/ping")
            return True
        except ProxmoxError:
            return False

    def agent_exec(self, node, vmid, command, input_data=None):
        """Start a command via the guest agent. Returns its pid."""
        data = {"command": command}
        if input_data:
            data["input-data"] = input_data
        result = self.post(f"/nodes/{node}/qemu/{vmid}/agent/exec", data)
        return (result or {}).get("pid")

    def agent_exec_status(self, node, vmid, pid):
        return (
            self.get(f"/nodes/{node}/qemu/{vmid}/agent/exec-status", {"pid": pid}) or {}
        )

    def agent_exec_wait(self, node, vmid, command, timeout=30, interval=0.5):
        """Run a command and block until it exits or the timeout passes."""
        pid = self.agent_exec(node, vmid, command)
        if pid is None:
            raise ProxmoxError("guest agent did not return a pid")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.agent_exec_status(node, vmid, pid)
            if status.get("exited"):
                return status
            time.sleep(interval)
        raise ProxmoxError(f"command did not finish within {timeout}s")

    # -- consoles ------------------------------------------------------

    def spice_config(self, node, vmid, kind="qemu"):
        """SPICE connection parameters, i.e. what a .vv file would contain.

        The 'proxy' request parameter is an *address*, not a URL: Proxmox
        builds "http://<proxy>:3128" from it itself, and its format check
        rejects anything with a scheme attached. Sending it at all is worth
        the trouble because the default is the node running the VM, which
        often is not the host you can actually reach -- the web UI passes
        window.location.hostname here for the same reason.

        In the response, 'proxy' is the real network target and 'host' is an
        opaque Proxmox proxy ticket. The 'ca' value arrives with literal
        backslash-n sequences that have to be unescaped before the PEM will
        parse.
        """
        path = f"/nodes/{node}/{kind}/{vmid}/spiceproxy"
        try:
            data = self.post(path, {"proxy": self.host})
        except ProxmoxError as exc:
            # Some addresses the client is perfectly happy with still fail
            # PVE's format check. Falling back to no proxy lets the server
            # pick its own node, which beats refusing to connect.
            if exc.status != 400:
                raise
            print(
                f"[api] spiceproxy rejected proxy={self.host!r} ({exc}); "
                "retrying without it"
            )
            data = self.post(path, {})

        if not data:
            raise ProxmoxError("spiceproxy returned nothing")
        settings = dict(data)
        if settings.get("ca"):
            settings["ca"] = settings["ca"].replace("\\n", "\n")
        return settings

    # -- who else is watching ------------------------------------------

    def qemu_monitor(self, node, vmid, command):
        """Run one HMP command against a running VM. Returns its text.

        Needs the VM.Monitor privilege, which a restricted API token often
        does not have. Callers are expected to treat a failure as "cannot
        tell" rather than as an answer.
        """
        try:
            result = self.post(
                f"/nodes/{node}/qemu/{vmid}/monitor", {"command": command}
            )
        except ProxmoxError as exc:
            # Missing privilege or an endpoint that is not there at all --
            # neither will fix itself, so stop asking. Any other failure
            # (the VM is not running, a momentary error) is about this call
            # and leaves the capability alone.
            if exc.status in (401, 403, 404, 501):
                self.monitor_available = False
            raise
        if isinstance(result, dict):
            # Older builds wrap it; newer ones return the bare string.
            result = result.get("data")
        if isinstance(result, str) and result.strip():
            return result
        # Not a refusal, but not an answer either. Raising keeps the caller
        # from reading an empty or unexpected payload as a real result.
        raise ProxmoxError(
            f"the monitor returned nothing usable for {command!r} "
            f"({type(result).__name__})"
        )

    def spice_clients(self, node, vmid):
        """How many SPICE clients QEMU currently has on a VM.

        Returns (count, addresses). Raises ProxmoxError if the question
        could not be asked, or was answered with something unrecognisable --
        neither of which is the same as zero, and the caller has to keep
        them apart or it will happily displace somebody every time the
        monitor is unavailable.
        """
        text = self.qemu_monitor(node, vmid, "info spice")
        parsed = parse_spice_clients(text)
        if parsed is None:
            # Printed in full because the only way this gets fixed is by
            # seeing what the server actually said.
            log.info("unrecognised 'info spice' output:")
            for line in text.splitlines()[:20]:
                log.info("  %s", line)
            raise ProxmoxError("could not read the monitor's SPICE report")
        return parsed

    def vnc_ticket(self, node, vmid, kind="qemu"):
        """A websocket-capable VNC proxy session.

        websocket=1 is what makes PVE expose the session through the 8006
        HTTPS listener rather than a raw port on the node, which is the only
        variant that survives a typical firewall.
        """
        data = self.post(f"/nodes/{node}/{kind}/{vmid}/vncproxy", {"websocket": 1})
        if not data or "port" not in data or "ticket" not in data:
            raise ProxmoxError("vncproxy returned an unusable response")
        return data

    def vnc_websocket_url(self, node, vmid, port, vncticket, kind="qemu"):
        query = urllib.parse.urlencode({"port": port, "vncticket": vncticket})
        return (
            f"wss://{self.host}:{self.port}/api2/json"
            f"/nodes/{node}/{kind}/{vmid}/vncwebsocket?{query}"
        )

    def auth_cookie_header(self):
        # PVE uri-unescapes the cookie server side, but a ticket is only
        # base64 and punctuation, so it round-trips either way. Sent raw to
        # match what the REST calls above do.
        with self._lock:
            return f"PVEAuthCookie={self._ticket or ''}"

    def basic_ws_headers(self):
        """Headers a websocket handshake to this server needs."""
        return {
            "Cookie": self.auth_cookie_header(),
            "Origin": f"https://{self.host}:{self.port}",
        }

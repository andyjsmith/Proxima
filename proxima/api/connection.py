"""One Proxmox server, and the set of them.

Everything that used to assume a single API client goes through here instead.
A connection owns its own ProxmoxAPI, its slice of the inventory, and its
state -- so a server that is down shows as failed in the tree while the
others keep working, rather than taking the whole window with it.
"""

import contextlib
import threading

from .client import DEFAULT_PORT, AuthError, ProxmoxAPI, ProxmoxError

# Connection states
DISCONNECTED = "disconnected"
CONNECTING = "connecting"
CONNECTED = "connected"
FAILED = "failed"


def split_host(value):
    """Accept 'host', 'host:port' and a pasted 'https://host:8006/...'."""
    value = (value or "").strip()
    for prefix in ("https://", "http://"):
        value = value.removeprefix(prefix)
    value = value.split("/")[0]
    if value.startswith("["):  # bracketed IPv6
        host, _, rest = value.partition("]")
        port = rest.lstrip(":")
        return host[1:], int(port) if port.isdigit() else DEFAULT_PORT
    if value.count(":") == 1:
        host, _, port = value.partition(":")
        return host, int(port) if port.isdigit() else DEFAULT_PORT
    return value, DEFAULT_PORT


class Connection:
    """A single server: credentials, client, guests, and current state."""

    def __init__(
        self,
        host,
        port=DEFAULT_PORT,
        username="root",
        realm="pam",
        verify_ssl=False,
        password="",
        save=False,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.realm = realm
        self.verify_ssl = verify_ssl
        self.password = password
        self.save = save

        self.api = ProxmoxAPI(host, port=port, verify_ssl=verify_ssl)
        self.state = DISCONNECTED
        self.error = ""
        self.guests = {}  # key -> Guest
        # False until the first poll returns. The tree shows "Loading..."
        # rather than a half-populated server whose guests would briefly
        # appear with the wrong actions enabled.
        self.loaded = False
        self.lock = threading.RLock()

    # -- identity ------------------------------------------------------

    @property
    def id(self):
        """Stable identifier, also what the tree shows as the root label."""
        return self.host if self.port == DEFAULT_PORT else f"{self.host}:{self.port}"

    @property
    def label(self):
        return self.id

    @property
    def connected(self):
        return self.state == CONNECTED

    def __repr__(self):
        return f"<Connection {self.id} {self.state}>"

    # -- lifecycle -----------------------------------------------------

    def connect(self, password=None, otp=None):
        """Log in. Raises on failure and records the reason."""
        self.state = CONNECTING
        self.error = ""
        secret = password if password is not None else self.password
        try:
            self.api.login(self.username, secret, realm=self.realm, otp=otp)
        except (AuthError, ProxmoxError) as exc:
            self.state = FAILED
            self.error = str(exc)
            raise
        except Exception as exc:
            self.state = FAILED
            self.error = f"{type(exc).__name__}: {exc}"
            raise
        self.state = CONNECTED
        if password is not None:
            self.password = password
        return self

    def disconnect(self):
        with contextlib.suppress(Exception):
            self.api.logout()
        self.state = DISCONNECTED
        self.loaded = False
        self.guests.clear()

    # -- inventory -----------------------------------------------------

    def poll(self):
        """Refresh this connection's guests. Returns them, tagged with it."""
        guests = self.api.guests()
        for guest in guests:
            guest.connection = self.id
        with self.lock:
            merged = {}
            for guest in guests:
                existing = self.guests.get(guest.key)
                if existing is not None:
                    existing.merge_live(guest)
                    merged[guest.key] = existing
                else:
                    merged[guest.key] = guest
            self.guests = merged
            self.loaded = True
            return list(self.guests.values())

    def cluster_tasks(self, limit=50):
        return self.api.cluster_tasks(limit)

    # -- persistence ---------------------------------------------------

    def to_config(self, encode_password):
        entry = {
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "realm": self.realm,
            "verify_ssl": self.verify_ssl,
            "save": True,
        }
        if self.password:
            entry["password"] = encode_password(self.password)
        return entry

    @classmethod
    def from_config(cls, entry, decode_password):
        return cls(
            host=entry.get("host", ""),
            port=int(entry.get("port") or DEFAULT_PORT),
            username=entry.get("username", "root"),
            realm=entry.get("realm", "pam"),
            verify_ssl=bool(entry.get("verify_ssl", False)),
            password=decode_password(entry.get("password", "")),
            save=True,
        )


class ConnectionManager:
    """Every server the window is talking to, in the order they were added."""

    def __init__(self):
        self._connections = []

    def __iter__(self):
        return iter(list(self._connections))

    def __len__(self):
        return len(self._connections)

    def __bool__(self):
        return bool(self._connections)

    @property
    def all(self):
        return list(self._connections)

    @property
    def connected(self):
        return [c for c in self._connections if c.connected]

    def get(self, connection_id):
        for connection in self._connections:
            if connection.id == connection_id:
                return connection
        return None

    def add(self, connection):
        existing = self.get(connection.id)
        if existing is not None:
            # Reconnecting to the same server replaces the old entry rather
            # than showing it twice.
            self._connections[self._connections.index(existing)] = connection
        else:
            self._connections.append(connection)
        return connection

    def remove(self, connection_id):
        connection = self.get(connection_id)
        if connection is None:
            return None
        connection.disconnect()
        self._connections.remove(connection)
        return connection

    def guests(self):
        """Every guest across every connected server."""
        result = []
        for connection in self._connections:
            result.extend(connection.guests.values())
        return result

    def guest(self, key):
        for connection in self._connections:
            found = connection.guests.get(key)
            if found is not None:
                return found
        return None

    def api_for(self, key_or_guest):
        """The client that owns a guest, by key or by Guest."""
        connection_id = getattr(key_or_guest, "connection", None)
        if connection_id is None and isinstance(key_or_guest, str):
            connection_id = key_or_guest.split("/", 1)[0]
        connection = self.get(connection_id)
        return connection.api if connection is not None else None

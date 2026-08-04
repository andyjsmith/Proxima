"""Fixtures for driving the real UI against a fake Proxmox.

The suite builds actual GTK widgets and pumps the main loop, which is what
catches the errors that only appear once the widgets are realised -- wrong
signal signatures, bad CSS, missing icons, TreeStore column mismatches --
without needing a server.

Windows are expensive to build and the polling behaviour is half of what is
under test, so a window is module-scoped and the tests in a module run in
file order against it, each putting back whatever it changed.
"""

import copy
import os
import sys
import time

# Never touch the real user settings: this suite opens the preferences
# dialog, which saves on close.
os.environ.setdefault(
    "PROXIMA_CONFIG_DIR",
    os.path.join(os.environ.get("TEMP", "/tmp"), "proxima-tests"),
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi

gi.require_version("Gtk", "3.0")
import pytest
from gi.repository import Gtk

from proxima.api.connection import CONNECTED, Connection
from proxima.api.models import Guest
from proxima.config import Config
from proxima.theme import apply as apply_theme
from proxima.ui.main_window import MainWindow

CONN_ID = "pve.example.invalid"

SAMPLE_DEFAULTS = [
    {
        "vmid": 100,
        "name": "web01",
        "node": "pve-node-01",
        "type": "qemu",
        "status": "running",
        "uptime": 1043400,
        "cpu": 0.07,
        "maxcpu": 4,
        "mem": 4294967296,
        "maxmem": 8589934592,
        "maxdisk": 68719476736,
    },
    {
        "vmid": 101,
        "name": "db01",
        "node": "pve-node-01",
        "type": "qemu",
        "status": "running",
        "uptime": 1043400,
        "cpu": 0.31,
        "maxcpu": 8,
        "mem": 12884901888,
        "maxmem": 17179869184,
        "maxdisk": 549755813888,
    },
    {
        "vmid": 102,
        "name": "build-runner",
        "node": "pve-node-01",
        "type": "qemu",
        "status": "stopped",
        "maxcpu": 16,
        "maxmem": 34359738368,
        "maxdisk": 274877906944,
    },
    {
        "vmid": 200,
        "name": "win11-test",
        "node": "pve-node-02",
        "type": "qemu",
        "status": "running",
        "uptime": 259200,
        "cpu": 0.12,
        "maxcpu": 4,
        "mem": 6442450944,
        "maxmem": 8589934592,
        "maxdisk": 137438953472,
    },
    {
        "vmid": 201,
        "name": "pfsense",
        "node": "pve-node-02",
        "type": "qemu",
        "status": "running",
        "uptime": 3542400,
        "cpu": 0.02,
        "maxcpu": 2,
        "mem": 1073741824,
        "maxmem": 2147483648,
        "maxdisk": 34359738368,
    },
    {
        "vmid": 202,
        "name": "backup-tgt",
        "node": "pve-node-02",
        "type": "lxc",
        "status": "paused",
        "uptime": 259200,
        "maxcpu": 2,
        "maxmem": 2147483648,
        "maxdisk": 8589934592,
    },
    {
        "vmid": 900,
        "name": "debian12-tmpl",
        "node": "pve-node-02",
        "type": "qemu",
        "status": "stopped",
        "template": 1,
        "maxcpu": 2,
        "maxmem": 2147483648,
        "maxdisk": 8589934592,
    },
]

# The inventory the fake serves, mutated in place by tests that need a guest
# to change state under the window. reset_fakes() puts it back.
SAMPLE = copy.deepcopy(SAMPLE_DEFAULTS)


def sample_row(vmid):
    """The raw inventory row for a VMID, to change status or name in place."""
    for row in SAMPLE:
        if row["vmid"] == vmid:
            return row
    raise KeyError(vmid)


def key_for(vmid, node="pve-node-01", kind="qemu", host=CONN_ID):
    return f"{host}/{node}/{kind}/{vmid}"


class FakeAPI:
    host = "pve.example.invalid"
    port = 8006
    username = "root@pam"
    verify_ssl = False

    def __init__(self):
        self.calls = []

    # A rename the server has accepted but not yet reports back, which is
    # the window the tree's spinner exists to cover.
    RENAME_DELAY = False
    _deferred_rename = None

    def guests(self):
        self.calls.append("guests")
        if FakeAPI._deferred_rename is not None:
            vmid, name, polls = FakeAPI._deferred_rename
            if polls <= 1:
                FakeAPI._deferred_rename = None
                for row in SAMPLE:
                    if row["vmid"] == vmid:
                        row["name"] = name
            else:
                FakeAPI._deferred_rename = (vmid, name, polls - 1)
        guests = [Guest.from_api(row) for row in SAMPLE]
        for guest in guests:
            guest.connection = CONN_ID
        return guests

    def guest_notes(self, node, vmid, kind="qemu"):
        return self.NOTES.get(vmid, "")

    def set_guest_notes(self, node, vmid, text, kind="qemu"):
        self.calls.append(("set-notes", vmid))
        self.NOTES[vmid] = text

    # Display adapter per VM, covering each branch of the SPICE decision.
    VGA = {
        100: "qxl,memory=32",  # classic SPICE
        101: "std",  # VNC only
        200: "virtio",  # VirtIO-GPU -- SPICE, and easy to miss
        201: "virtio-gl",  # VirGL -- also SPICE
        202: "some-future-gpu",  # unknown -- must try SPICE, not assume VNC
    }

    # Guests with Proxmox's delete guard set.
    PROTECTED = {900}

    # Hardware, per vmid, merged over the defaults below.
    HARDWARE = {}

    def guest_config(self, node, vmid, kind="qemu"):
        self.calls.append(("config", vmid))
        config = {
            "vga": self.VGA.get(vmid, "std"),
            # A real config carries the notes, which is where a guest's own
            # settings live -- so a re-read must not look like "no settings".
            "description": self.NOTES.get(vmid, ""),
            "agent": "1",
            "cores": 2,
            "sockets": 1,
            "memory": 2048,
            "net0": "virtio=BC:24:11:00:00:01,bridge=vmbr0,firewall=1,rate=10",
            "digest": f"digest-{vmid}",
        }
        config.update(self.HARDWARE.get(vmid, {}))
        if vmid in self.PROTECTED:
            config["protection"] = 1
        return config

    def set_guest_config(
        self, node, vmid, changes=None, delete=None, kind="qemu", digest=None
    ):
        self.calls.append(
            ("set-config", vmid, dict(changes or {}), tuple(delete or ()), digest)
        )
        stored = dict(self.HARDWARE.get(vmid, {}))
        stored.update(changes or {})
        for key in delete or ():
            stored.pop(key, None)
            stored[key] = None  # remembers that it was deleted
        self.HARDWARE[vmid] = {k: v for k, v in stored.items() if v is not None}

    def node_bridges(self, node):
        self.calls.append(("bridges", node))
        return ["vmbr0", "vmbr1"]

    def guest_agent_info(self, node, vmid):
        return {"result": {"pretty-name": "Debian GNU/Linux 12 (bookworm)"}}

    def guest_interfaces(self, node, vmid):
        return [
            {
                "name": "eth0",
                "ip-addresses": [
                    {"ip-address-type": "ipv4", "ip-address": "10.20.30.41"}
                ],
            }
        ]

    # -- who else is connected -----------------------------------------

    # What 'info spice' comes back with, per vmid. None means the monitor
    # itself refuses, which must never be read as "nobody is connected".
    SPICE_CLIENTS = {}
    monitor_available = True

    def qemu_monitor(self, node, vmid, command):
        from proxima.api import ProxmoxError

        self.calls.append(("monitor", vmid, command))
        if not self.monitor_available:
            self.monitor_available = False
            raise ProxmoxError("Permission check failed (VM.Monitor)", status=403)
        count = self.SPICE_CLIENTS.get(vmid, 0)
        if count is None:
            raise ProxmoxError("VM is not running", status=500)
        header = (
            "Server:\n     address: 127.0.0.1:61000 [tls]\n"
            "    migrated: false\n        auth: spice\n"
            "    compiled: 0.15.2\n  mouse-mode: client\n"
        )
        if not count:
            return header + "Channels: none"
        # The shape a real Proxmox host returns, captured from a live
        # server: a singular "Channel:" block repeated per channel, one
        # shared "session" per viewer, and a [tls] flag glued to the
        # address. Inventing this format instead of capturing it is exactly
        # what let the feature ship broken with its tests passing.
        names = ("main", "display", "inputs", "cursor")
        blocks = ""
        for client in range(count):
            for index, name in enumerate(names):
                port = 42220 + client * 100 + index
                blocks += (
                    f"Channel:\n     address: 127.0.0.1:{port} [tls]\n"
                    f"     session: {1938609120 + client}\n"
                    f"     channel: {index + 1}:0\n"
                    f"     channel name: {name}\n"
                )
        return header + blocks

    def spice_clients(self, node, vmid):
        from proxima.api.models import parse_spice_clients

        return parse_spice_clients(self.qemu_monitor(node, vmid, "info spice"))

    def spice_config(self, node, vmid, kind="qemu"):
        self.calls.append(("spice", vmid))
        return {
            "type": "spice",
            "host": "pvespiceproxy:fake",
            "proxy": "https://pve.example.invalid",
            "tls-port": "61000",
            "password": "fake",
            "host-subject": "OU=PVE Cluster Node",
        }

    def power(self, node, vmid, action, kind="qemu"):
        self.calls.append(("power", vmid, action))
        return "UPID:fake"

    # -- tasks ---------------------------------------------------------

    def cluster_tasks(self, limit=50):
        self.calls.append("tasks")
        return [
            {
                "upid": "UPID:n1:0001",
                "node": "pve-node-01",
                "type": "qmstart",
                "id": "100",
                "user": "root@pam",
                "starttime": 1750000000,
                "endtime": 1750000005,
                "status": "OK",
            },
            {
                "upid": "UPID:n2:0002",
                "node": "pve-node-02",
                "type": "vzdump",
                "id": "200",
                "user": "root@pam",
                "starttime": 1750000100,
            },
            {
                "upid": "UPID:n1:0003",
                "node": "pve-node-01",
                "type": "qmsnapshot",
                "id": "100",
                "user": "root@pam",
                "starttime": 1750000200,
                "endtime": 1750000210,
                "status": "unable to create snapshot",
            },
        ][:limit]

    def task_log(self, node, upid, limit=200):
        return [f"log line {i} for {upid}" for i in range(3)]

    # -- snapshots -----------------------------------------------------

    # A forked history, which is the shape a flat list cannot show:
    # clean-install has two children, and the live state hangs off one of
    # them.
    SNAPSHOTS = [
        {
            "name": "before-upgrade",
            "snaptime": 1749000000,
            "description": "pre 24.04",
            "parent": "clean-install",
        },
        {
            "name": "experiment",
            "snaptime": 1748500000,
            "description": "",
            "parent": "clean-install",
        },
        {"name": "clean-install", "snaptime": 1748000000, "description": ""},
        {"name": "current", "description": "You are here", "parent": "before-upgrade"},
    ]

    def snapshots(self, node, vmid, kind="qemu", include_current=False):
        self.calls.append(("snapshots", vmid))
        # Mirrors the client: newest first, 'current' filtered unless asked
        # for.
        rows = list(self.SNAPSHOTS)
        if not include_current:
            rows = [r for r in rows if r["name"] != "current"]
        return sorted(rows, key=lambda r: r.get("snaptime") or 0, reverse=True)

    def create_snapshot(
        self, node, vmid, name, description="", vmstate=False, kind="qemu"
    ):
        self.calls.append(("snap-create", vmid, name, vmstate))
        return "UPID:fake"

    def rollback_snapshot(self, node, vmid, name, kind="qemu"):
        self.calls.append(("snap-rollback", vmid, name))
        return "UPID:fake"

    def delete_snapshot(self, node, vmid, name, kind="qemu"):
        self.calls.append(("snap-delete", vmid, name))
        return "UPID:fake"

    # -- renaming and cloning ------------------------------------------

    def rename_guest(self, node, vmid, name, kind="qemu"):
        self.calls.append(("rename", vmid, name, kind))
        # A real server reports the new name on the next poll, and the poll
        # overwrites what the window set locally -- so the fake has to as
        # well, or the test would pass on the local update alone.
        if self.RENAME_DELAY:
            # ...and it takes a couple of polls to get there, which is the
            # gap the tree has to cover without flicking back and forth.
            FakeAPI._deferred_rename = (vmid, name, 2)
            return
        for row in SAMPLE:
            if row["vmid"] == vmid:
                row["name"] = name
        return

    def next_vmid(self):
        return 903

    def nodes(self):
        from proxima.api.models import Node

        return [
            Node(name="pve-node-01", status="online"),
            Node(name="pve-node-02", status="online"),
            Node(name="pve-node-03", status="offline"),
        ]

    def node_storages(self, node, content=None):
        self.calls.append(("storages", node, content))
        return [
            {"storage": "local-lvm", "active": 1},
            {"storage": "ceph-pool", "active": 1},
        ]

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
        self.calls.append(("clone", vmid, newid, name, target, full, storage))
        return "UPID:fake"

    def delete_guest(
        self, node, vmid, kind="qemu", purge=True, destroy_unreferenced=False
    ):
        self.calls.append(("delete", vmid, kind, purge))
        return "UPID:fake"

    # -- notes ---------------------------------------------------------

    NOTES = {}

    # -- guest agent ---------------------------------------------------

    agent_available = True

    def agent_ping(self, node, vmid):
        self.calls.append(("agent-ping", vmid))
        return self.agent_available

    def agent_exec_wait(self, node, vmid, command, timeout=30, interval=0.5):
        self.calls.append(("agent-exec", vmid, tuple(command)))
        return {"exited": 1, "exitcode": 0, "out-data": "hello\n"}

    def logout(self):
        pass


class SlowAPI(FakeAPI):
    """A server whose per-guest detail calls take a while.

    Mirrors a VM with no guest agent, where the agent ping is what stalls.
    """

    def __init__(self, delay=1.2):
        super().__init__()
        self.delay = delay
        self.config_calls = 0

    def guest_config(self, node, vmid, kind="qemu"):
        self.config_calls += 1
        time.sleep(self.delay)
        return super().guest_config(node, vmid, kind)


class SlowConsoleAPI(FakeAPI):
    """Console tickets take a while, as a busy Proxmox host does."""

    def __init__(self, delay=1.5):
        super().__init__()
        self.delay = delay

    def guest_config(self, node, vmid, kind="qemu"):
        time.sleep(self.delay)
        return super().guest_config(node, vmid, kind)

    def spice_config(self, node, vmid, kind="qemu"):
        time.sleep(self.delay)
        return super().spice_config(node, vmid, kind)


class FailingConsoleAPI(FakeAPI):
    """Every console path refuses, to exercise the error state."""

    def spice_config(self, node, vmid, kind="qemu"):
        from proxima.api import ProxmoxError

        raise ProxmoxError("spiceproxy refused: no such VM")

    def vnc_ticket(self, node, vmid, kind="qemu"):
        from proxima.api import ProxmoxError

        raise ProxmoxError("vncproxy refused: no such VM")


class FakeEditable:
    """Stands in for the entry a network row's handlers read from."""

    def __init__(self, text):
        self._text = text

    def get_text(self):
        return self._text

    def set_text(self, text):
        self._text = text


class FakeConsole(Gtk.Box):
    """A console-shaped widget, so fullscreen can be tested without a guest."""

    protocol = "spice"
    supports = {
        "auto_resize": True,
        "scaling": True,
        "codec": True,
        "compression": True,
        "refresh": False,
        "ctrl_alt_del": True,
        "clipboard": True,
        "audio": True,
    }
    agent_connected = True

    def __init__(self, title="fake-guest"):
        super().__init__()
        self.title = title
        self.guest_key = "fake"
        self.auto_resize = True
        self.scaling = False
        self.codec_index = 0
        self.compression_index = 0
        self.ctrl_alt_del_sent = 0
        self.share_clipboard = True
        self.play_audio = True
        self.last_status = ""
        self.pack_start(Gtk.Label(label="console"), True, True, 0)

    def set_auto_resize(self, value):
        self.auto_resize = value

    # Clipboard applies live; audio cannot, exactly as SpiceConsole reports.
    def set_clipboard_enabled(self, value):
        self.share_clipboard = value
        return True

    def set_audio_enabled(self, value):
        self.play_audio = value
        return False

    def set_scaling(self, value):
        self.scaling = value

    def set_codec_index(self, index):
        self.codec_index = index

    def set_compression_index(self, index):
        self.compression_index = index

    def send_ctrl_alt_del(self):
        self.ctrl_alt_del_sent += 1

    def show_guest_state(self, status):
        self.last_status = status

    def telemetry(self):
        return {"rate": 2.5 * 1024 * 1024, "fps": 30.0, "size": "1920x1080"}

    def grab_focus_display(self):
        pass

    def release_input(self):
        pass

    def shutdown(self):
        pass


def pump(seconds=0.4):
    """Run the main loop for a while.

    The inner drain is bounded by the same deadline, not just by the queue
    emptying: an animation timer can refill it as fast as it is consumed,
    and an unbounded drain would then never come back.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        while Gtk.events_pending() and time.time() < deadline:
            Gtk.main_iteration_do(False)
        time.sleep(0.01)


def pump_until(predicate, seconds=8, step=0.2):
    """Pump until predicate() is true; returns whether it became true."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        pump(step)
        if predicate():
            return True
    return predicate()


def make_connection(api):
    """A Connection backed by a fake API, already 'logged in'."""
    connection = Connection(host=CONN_ID, username="root@pam")
    connection.api = api
    connection.state = CONNECTED
    return connection


def wait_for_guests(window, seconds=10):
    """Block until the first poll has populated the tree."""
    return pump_until(lambda: bool(window.sidebar.guests), seconds)


def build_window(api, config):
    """A MainWindow with one fake connection already attached."""

    window = MainWindow(config)
    connection = window.connections.add(make_connection(api))
    window.show_all()
    # Same path the real app takes once a login succeeds.
    window._connection_ready(connection)
    return window


def close_window(window):
    window.shutdown()
    window.destroy()
    pump(0.3)


def plan_protocol(window, key):
    """The protocol a console would open with, without touching the network.

    The fake API has no vnc_ticket, so an AttributeError naming it proves
    planning reached the VNC branch.
    """
    try:
        return window._plan_console(window.sidebar.guests[key])["protocol"]
    except AttributeError as exc:
        if "vnc_ticket" in str(exc):
            return "vnc"
        raise


def make_config(**overrides):
    config = Config()
    config["host"] = CONN_ID
    for key, value in overrides.items():
        config[key] = value
    return config


def reset_fakes():
    """Put the module-level fake state back to its defaults."""
    global SAMPLE
    SAMPLE[:] = copy.deepcopy(SAMPLE_DEFAULTS)
    FakeAPI.NOTES = {}
    FakeAPI.HARDWARE = {}
    FakeAPI.SPICE_CLIENTS = {}
    FakeAPI.SNAPSHOTS = [
        {
            "name": "before-upgrade",
            "snaptime": 1749000000,
            "description": "pre 24.04",
            "parent": "clean-install",
        },
        {
            "name": "experiment",
            "snaptime": 1748500000,
            "description": "",
            "parent": "clean-install",
        },
        {"name": "clean-install", "snaptime": 1748000000, "description": ""},
        {"name": "current", "description": "You are here", "parent": "before-upgrade"},
    ]
    FakeAPI.RENAME_DELAY = False
    FakeAPI._deferred_rename = None
    FakeAPI.monitor_available = True
    FakeAPI.agent_available = True


@pytest.fixture(scope="session", autouse=True)
def _theme():
    """The CSS the windows are built against, loaded once."""
    apply_theme(make_config())


@pytest.fixture(scope="module")
def config():
    return make_config()


@pytest.fixture(scope="module")
def api():
    reset_fakes()
    yield FakeAPI()
    reset_fakes()


@pytest.fixture(scope="module")
def window(api, config):
    """A main window, shared by every test in the module, in file order."""
    window = build_window(api, config)
    pump(2.0)
    yield window
    close_window(window)

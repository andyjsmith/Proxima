#!/usr/bin/env python3
"""Build the real UI against a fake API and pump the main loop.

Catches the class of error that only shows up once GTK actually instantiates
and realises the widgets -- wrong signal signatures, bad CSS, missing icons,
TreeStore column mismatches -- without needing a Proxmox server.

    python3 tools/smoke_test.py
"""

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
from gi.repository import Gdk, Gtk

from proxima.api import notes as notes_mod
from proxima.api.connection import (
    CONNECTED,
    Connection,
)
from proxima.api.models import Guest, vga_is_spice
from proxima.config import Config
from proxima.theme import apply as apply_theme
from proxima.ui.clone import CloneDialog
from proxima.ui.login_dialog import LoginDialog
from proxima.ui.main_window import MainWindow
from proxima.ui.settings_dialog import SettingsDialog
from proxima.ui.snapshots import SnapshotManager

SAMPLE = [
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


CONN_ID = "pve.example.invalid"


def make_connection(api):
    """A Connection backed by a fake API, already 'logged in'."""
    connection = Connection(host=CONN_ID, username="root@pam")
    connection.api = api
    connection.state = CONNECTED
    return connection


def wait_for_guests(window, seconds=10):
    """Block until the first poll has populated the tree."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        pump(0.2)
        if window.sidebar.guests:
            return True
    return False


def build_window(api, config):
    """A MainWindow with one fake connection already attached."""

    window = MainWindow(config)
    connection = window.connections.add(make_connection(api))
    window.show_all()
    # Same path the real app takes once a login succeeds.
    window._connection_ready(connection)
    return window


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


class _FakeEditable:
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

    agent_connected = True

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


def main():
    failures = []

    config = Config()
    config["host"] = "pve.example.invalid"
    apply_theme(config)
    print("[smoke] theme applied")

    # -- login dialog --------------------------------------------------
    login = LoginDialog(None, config)
    pump(0.2)
    assert login._split_host("https://10.0.0.5:8006/#v1") == ("10.0.0.5", 8006)
    assert login._split_host("pve.local") == ("pve.local", 8006)
    assert login._split_host("[fd00::1]:8007") == ("fd00::1", 8007)
    login.destroy()
    print("[smoke] login dialog built; host parsing ok")

    # -- main window ---------------------------------------------------
    api = FakeAPI()
    FakeAPI.NOTES = {}
    window = build_window(api, config)
    pump(2.0)

    guests = window.sidebar.guests
    if len(guests) != len(SAMPLE):
        failures.append(f"sidebar has {len(guests)} guests, expected {len(SAMPLE)}")
    print(f"[smoke] sidebar populated with {len(guests)} guests")

    # Selection drives the toolbar and the summary page.
    window.sidebar.select_key("pve.example.invalid/pve-node-01/qemu/100")
    pump(0.4)
    selected = window.sidebar.selected_guest()
    if selected is None or selected.vmid != 100:
        failures.append("selecting a guest by key did not take")
    else:
        print(
            f"[smoke] selected {selected.label}; "
            f"summary console field = "
            f"{window.summary.values['console'].get_text()!r}"
        )

    start_enabled = [w.get_sensitive() for w in window._action_items["start"]]
    stop_enabled = [w.get_sensitive() for w in window._action_items["stop"]]
    if any(start_enabled):
        failures.append("Power On is enabled for an already running guest")
    if not all(stop_enabled):
        failures.append("Power Off is disabled for a running guest")

    window.sidebar.select_key("pve.example.invalid/pve-node-01/qemu/102")
    pump(0.3)
    if not all(w.get_sensitive() for w in window._action_items["start"]):
        failures.append("Power On is disabled for a stopped guest")
    if any(w.get_sensitive() for w in window._action_items["reset"]):
        failures.append("Reset is enabled for a stopped guest")
    print("[smoke] toolbar sensitivity tracks guest state")

    # A template must offer nothing.
    window.sidebar.select_key("pve.example.invalid/pve-node-02/qemu/900")
    pump(0.3)
    if any(
        any(w.get_sensitive() for w in widgets)
        for widgets in window._action_items.values()
    ):
        failures.append("power actions are enabled for a template")
    if window.console_tool_item.get_sensitive():
        failures.append("Console is enabled for a template")

    # A container must not offer Reset.
    window.sidebar.select_key("pve.example.invalid/pve-node-02/lxc/202")
    pump(0.3)
    if any(w.get_sensitive() for w in window._action_items["reset"]):
        failures.append("Reset is offered for an LXC container")
    print("[smoke] template and container restrictions hold")

    # The vga -> SPICE rule itself. VirtIO-GPU is plain 'virtio' in the
    # config, which is exactly the value that must not be mistaken for a
    # VNC-only adapter.
    for value, expected, note in [
        ("qxl", True, "QXL"),
        ("qxl2,memory=32", True, "QXL with options"),
        ("virtio", True, "VirtIO-GPU"),
        ("virtio-gl", True, "VirGL"),
        ("VirtIO", True, "case insensitive"),
        (" virtio , memory=64 ", True, "whitespace tolerated"),
        ("std", False, "std"),
        ("cirrus", False, "cirrus"),
        ("vmware", False, "vmware"),
        ("serial0", False, "serial console"),
        ("none", False, "no display"),
        ("", False, "unset (Proxmox defaults to std)"),
        (None, False, "missing"),
        ("some-future-gpu", None, "unknown type -> ask the server"),
    ]:
        actual = vga_is_spice(value)
        if actual is not expected:
            failures.append(
                f"vga_is_spice({value!r}) returned {actual!r}, "
                f"expected {expected!r} ({note})"
            )
    print("[smoke] vga -> SPICE rule correct for all display types")

    # Console planning picks the right protocol without touching the network.
    # The fake API has no vnc_ticket, so an AttributeError naming it proves
    # planning reached the VNC branch.
    def plan_protocol(key):
        try:
            return window._plan_console(window.sidebar.guests[key])["protocol"]
        except AttributeError as exc:
            if "vnc_ticket" in str(exc):
                return "vnc"
            raise

    for key, expected in [
        ("pve.example.invalid/pve-node-01/qemu/100", "spice"),  # qxl
        ("pve.example.invalid/pve-node-01/qemu/101", "vnc"),  # std
        ("pve.example.invalid/pve-node-02/qemu/200", "spice"),  # virtio
        ("pve.example.invalid/pve-node-02/qemu/201", "spice"),
    ]:  # virtio-gl
        actual = plan_protocol(key)
        vmid = key.rsplit("/", 1)[-1]
        vga = FakeAPI.VGA.get(int(vmid))
        if actual != expected:
            failures.append(
                f"vmid {vmid} (vga={vga}) planned a {actual} console, "
                f"expected {expected}"
            )
        else:
            print(f"[smoke] vmid {vmid} (vga={vga}) -> {actual}")

    # An unknown adapter must attempt SPICE rather than assume VNC.
    unknown = window.sidebar.guests["pve.example.invalid/pve-node-02/lxc/202"]
    unknown.kind = "qemu"  # containers never get SPICE; test the VM path
    before = len([c for c in api.calls if c[0] == "spice"])
    plan_protocol("pve.example.invalid/pve-node-02/lxc/202")
    after = len([c for c in api.calls if c[0] == "spice"])
    if after == before:
        failures.append("unknown display type did not attempt SPICE")
    else:
        print("[smoke] unknown display type -> SPICE attempted")
    unknown.kind = "lxc"

    # The summary's console field, per display type. 'checking...' must not
    # survive a completed lookup -- an unrecognised adapter reports None for
    # spice_capable, which used to be indistinguishable from "not looked up".
    for key, expect in [
        ("pve.example.invalid/pve-node-02/qemu/200", "SPICE"),  # virtio
        ("pve.example.invalid/pve-node-01/qemu/101", "VNC"),  # std
        ("pve.example.invalid/pve-node-02/qemu/201", "SPICE"),
    ]:  # virtio-gl
        window.sidebar.select_key(key)
        pump(0.6)
        text = window.summary.values["console"].get_text()
        display = window.summary.values["display"].get_text()
        if "checking" in text:
            failures.append(f"{key}: summary stuck on 'checking...'")
        elif not text.startswith(expect):
            failures.append(
                f"{key}: display {display!r} reported console {text!r}, "
                f"expected {expect}"
            )
        else:
            print(f"[smoke] summary: {display} -> {text}")

    # A slow detail fetch must survive the polls that land on top of it.
    # This is the "checking... forever" regression: the poll re-renders the
    # summary every few seconds, and that used to cancel the in-flight reply
    # while the cache still claimed the request had been made.
    slow = SlowAPI(delay=1.2)
    slow_config = Config(dict(config))
    slow_config["refresh_seconds"] = 1
    slow_window = build_window(slow, slow_config)
    pump(1.0)
    slow_window.sidebar.select_key("pve.example.invalid/pve-node-02/qemu/200")
    deadline = time.time() + 8
    console_text = ""
    while time.time() < deadline:
        pump(0.2)
        console_text = slow_window.summary.values["console"].get_text()
        if "checking" not in console_text:
            break
    if "checking" in console_text:
        failures.append(
            f"summary never left 'checking...' under polling "
            f"(after {slow.config_calls} config calls)"
        )
    else:
        print(
            f"[smoke] slow detail fetch resolved to {console_text!r} "
            f"despite polling ({slow.config_calls} config call(s))"
        )
    slow_window.shutdown()
    slow_window.destroy()

    # -- sidebar columns and icon colouring ----------------------------
    columns = window.sidebar.view.get_columns()
    if len(columns) != 1:
        failures.append(f"sidebar has {len(columns)} columns, expected 1")
    elif window.sidebar.view.get_headers_visible():
        failures.append("sidebar headers are still visible")
    else:
        print("[smoke] sidebar reduced to a single unheaded column")

    from proxima.ui import sidebar as sidebar_mod

    store = window.sidebar.store
    row = window.sidebar._find_row(
        "pve.example.invalid/pve-node-01/qemu/100"
    )  # running
    stopped_row = window.sidebar._find_row(
        "pve.example.invalid/pve-node-01/qemu/102"
    )  # stopped
    running_icon = store.get_value(row, sidebar_mod.COL_ICON)
    stopped_icon = store.get_value(stopped_row, sidebar_mod.COL_ICON)

    if running_icon is None:
        failures.append("running guest has no icon pixbuf")
    else:
        # Sample the recoloured pixels and confirm green dominates.
        data = running_icon.get_pixels()
        stride, channels = running_icon.get_rowstride(), running_icon.get_n_channels()
        greenest = (0, 0, 0)
        for y in range(running_icon.get_height()):
            for x in range(running_icon.get_width()):
                offset = y * stride + x * channels
                r, g, b = data[offset], data[offset + 1], data[offset + 2]
                alpha = data[offset + 3] if channels == 4 else 255
                if alpha > 200 and g > greenest[1]:
                    greenest = (r, g, b)
        red, green, blue = greenest
        if not (green > red + 30 and green > blue + 30):
            failures.append(f"running icon is not green, strongest pixel = {greenest}")
        else:
            print(f"[smoke] running icon is green, strongest pixel = {greenest}")

    if stopped_icon is not None and running_icon is not None:
        if stopped_icon.get_pixels() == running_icon.get_pixels():
            failures.append("stopped and running icons are identical")

    # Switching palettes must repaint the existing rows. Toggle away from
    # whichever palette is live -- it follows the system theme, so it is
    # already dark on a dark desktop.
    was_dark = window.sidebar._palette is sidebar_mod.PALETTES[True]
    window.sidebar.set_dark(not was_dark)
    pump(0.2)
    swapped_icon = store.get_value(
        window.sidebar._find_row("pve.example.invalid/pve-node-01/qemu/100"),
        sidebar_mod.COL_ICON,
    )
    if swapped_icon is None or (
        running_icon is not None
        and swapped_icon.get_pixels() == running_icon.get_pixels()
    ):
        failures.append(
            f"icons did not change when the palette flipped (was_dark={was_dark})"
        )
    else:
        print(
            f"[smoke] icon palette follows the theme "
            f"(started {'dark' if was_dark else 'light'})"
        )
    window.sidebar.set_dark(was_dark)

    # -- fullscreen console --------------------------------------------
    window.sidebar.select_key("pve.example.invalid/pve-node-01/qemu/100")
    pump(0.3)
    if window.fullscreen_item.get_sensitive():
        failures.append("Full Screen is enabled with no console open")

    fake_console = FakeConsole()
    window.consoles["fake"] = fake_console
    page = window.panes.append(fake_console, Gtk.Label(label="fake"))
    pump(0.4)

    if not window.fullscreen_item.get_sensitive():
        failures.append("Full Screen is disabled with a console open")

    window.fullscreen_control.enter()
    pump(0.4)
    if not window.fullscreen_control.active:
        failures.append("enter_fullscreen did not take")
    for name, widget in (
        ("menubar", window.menubar),
        ("toolbar", window.toolbar),
        ("statusbar", window.statusbar_box),
        ("sidebar", window.sidebar),
    ):
        if widget.get_visible():
            failures.append(f"{name} still visible in fullscreen")
    if window.notebook.get_show_tabs():
        failures.append("notebook tabs still visible in fullscreen")
    if not window.fullscreen_control.revealer.get_reveal_child():
        failures.append("fullscreen bar not shown on entry")
    if window.fullscreen_control.title_label.get_text() != fake_console.title:
        failures.append("fullscreen bar shows the wrong title")
    print("[smoke] fullscreen hides all chrome and shows the bar")

    # Pinning holds the bar open; unpinning lets the poll hide it again.
    # The reveal is driven by the real pointer, so park it deliberately --
    # otherwise this passes or fails on where the mouse happens to be.
    def warp_pointer(dx, dy):
        gdk_window = window.get_window()
        if gdk_window is None:
            return False
        seat = Gdk.Display.get_default().get_default_seat()
        pointer = seat.get_pointer() if seat else None
        if pointer is None:
            return False
        origin = gdk_window.get_origin()
        pointer.warp(Gdk.Screen.get_default(), origin[1] + dx, origin[2] + dy)
        return True

    can_warp = warp_pointer(400, 500)  # away from the top edge
    pump(0.3)

    window.fullscreen_control.pin.set_active(True)
    pump(0.3)
    if not window.fullscreen_control.revealer.get_reveal_child():
        failures.append("pinned bar was hidden")

    # The poll reads the real pointer, so keep nudging it away from the top
    # edge rather than failing on a warp the window manager ignored.
    window.fullscreen_control.pin.set_active(False)
    hidden = False
    for _attempt in range(4):
        warp_pointer(400, 500)
        window.fullscreen_control._hide_at = time.monotonic() - 1
        pump(0.5)
        if not window.fullscreen_control.revealer.get_reveal_child():
            hidden = True
            break
    if not hidden:
        failures.append("unpinned bar did not auto-hide")
    else:
        print("[smoke] fullscreen bar pins and auto-hides")

    if can_warp:
        revealed = False
        for _attempt in range(4):
            warp_pointer(400, 0)  # touch the top edge
            pump(0.5)
            if window.fullscreen_control.revealer.get_reveal_child():
                revealed = True
                break
        if not revealed:
            failures.append("bar did not reveal at the top edge")
        else:
            print("[smoke] bar reveals when the pointer hits the top edge")
        left_edge = False
        for _attempt in range(4):
            warp_pointer(400, 500)
            pump(0.5)
            if not window.fullscreen_control.revealer.get_reveal_child():
                left_edge = True
                break
        if not left_edge:
            failures.append("bar did not hide again after leaving the edge")
    else:
        print("[smoke] pointer warp unavailable; edge reveal not checked")

    window.fullscreen_control.enter()
    pump(0.5)
    window.fullscreen_control.leave()
    pump(0.4)
    if window.fullscreen_control.active:
        failures.append("leave_fullscreen did not take")
    for name, widget in (
        ("menubar", window.menubar),
        ("toolbar", window.toolbar),
        ("statusbar", window.statusbar_box),
        ("sidebar", window.sidebar),
    ):
        if not widget.get_visible():
            failures.append(f"{name} was not restored after fullscreen")
    if not window.notebook.get_show_tabs():
        failures.append("notebook tabs were not restored")
    if window.fullscreen_control._poll_source is not None:
        failures.append("fullscreen poll timer still running")
    print("[smoke] leaving fullscreen restores the chrome")

    # Closing the fullscreen console must not strand the window.
    window.notebook.set_current_page(window.notebook.page_num(fake_console))
    pump(0.3)
    window.fullscreen_control.enter()
    pump(0.3)
    window.close_console("fake")
    pump(0.3)
    if window.fullscreen_control.active:
        failures.append("closing the console left the window fullscreen")
    else:
        print("[smoke] closing a fullscreen console exits fullscreen")

    # -- Start and Resume share one control -----------------------------
    from proxima.ui import actions as action_defs

    if "resume" in action_defs.TOOLBAR_ACTIONS:
        failures.append("Resume still has its own toolbar button")
    if window._action_items.get("resume"):
        failures.append("Resume still has its own menu entry")

    def start_button_for(key):
        window.notebook.set_current_page(0)
        window.sidebar.select_key(key)
        pump(0.6)
        widget = window._action_items["start"][0]
        return widget.get_label(), widget.get_sensitive()

    label, enabled = start_button_for("pve.example.invalid/pve-node-01/qemu/102")
    if label != "Start" or not enabled:
        failures.append(f"stopped guest: button is {label!r}, enabled={enabled}")

    label, enabled = start_button_for("pve.example.invalid/pve-node-02/lxc/202")
    if label != "Resume" or not enabled:
        failures.append(f"paused guest: button is {label!r}, enabled={enabled}")

    label, enabled = start_button_for("pve.example.invalid/pve-node-01/qemu/100")
    if label != "Start" or enabled:
        failures.append(f"running guest: button is {label!r}, enabled={enabled}")
    else:
        print("[smoke] Start/Resume share one button and relabel by state")

    # Clicking it must call the API that applies, not always "start".
    paused = window.sidebar.guests["pve.example.invalid/pve-node-02/lxc/202"]
    window._run_action(paused.key, "start", confirm=False)
    pump(0.8)
    powered = [c for c in api.calls if c[0] == "power" and c[1] == 202]
    if not powered:
        failures.append("the combined button issued no power action")
    elif powered[-1][2] != "resume":
        failures.append(f"paused guest got '{powered[-1][2]}', expected 'resume'")
    else:
        print("[smoke] the combined button resumes a paused guest")

    # -- a requested action shows before Proxmox reports it ---------------
    running_key = "pve.example.invalid/pve-node-01/qemu/100"
    window.open_console(running_key)
    pump(1.2)
    live = window.consoles.get(running_key)
    if live is None:
        failures.append("no console opened for the running guest")
    else:
        window._run_action(running_key, "stop", confirm=False)
        pump(1.0)
        if running_key not in window._pending_actions:
            failures.append("no pending state recorded for the action")
        elif not live.status_panel.get_visible():
            failures.append("nothing shown while the action was in flight")
        elif "Stopping" not in live.status_panel.title.get_text():
            failures.append(
                f"panel reads {live.status_panel.title.get_text()!r}, expected Stopping"
            )
        elif not live.pending:
            failures.append("console not marked pending, so it is not greyed")
        else:
            print("[smoke] a requested action shows immediately on the console")

        # Once the guest really moves, the pending state must give way.
        SAMPLE[0]["status"] = "stopped"
        try:
            window.refresh()
            deadline = time.time() + 6
            while time.time() < deadline:
                pump(0.3)
                if running_key not in window._pending_actions:
                    break
            if running_key in window._pending_actions:
                failures.append("pending state outlived the status change")
            elif "stopped" not in live.status_panel.title.get_text():
                failures.append(
                    f"after stopping, panel reads "
                    f"{live.status_panel.title.get_text()!r}"
                )
            else:
                print("[smoke] pending gives way to the real guest state")
        finally:
            SAMPLE[0]["status"] = "running"
        window.close_console(running_key)
        pump(0.5)

    # -- polling intervals are configurable ------------------------------
    for key in ("refresh_seconds", "task_refresh_seconds", "burst_seconds"):
        if key not in config:
            failures.append(f"{key} missing from the config")
    settings_pages = SettingsDialog(window, config)
    pump(0.3)
    page_titles = [
        settings_pages.get_children()[0].get_children()[0].get_tab_label_text(child)
        for child in settings_pages.get_children()[0].get_children()[0].get_children()
    ]
    settings_pages.destroy()
    if "Polling" not in page_titles:
        failures.append(f"no Polling page in preferences: {page_titles}")
    else:
        print(f"[smoke] preferences pages: {', '.join(page_titles)}")

    # -- the tab opens before the ticket arrives -------------------------
    slow_console = SlowConsoleAPI(delay=1.5)
    slow_console_window = build_window(slow_console, Config(dict(config)))
    slow_key = "pve.example.invalid/pve-node-01/qemu/100"
    if not wait_for_guests(slow_console_window):
        failures.append("slow-console window never listed any guests")
    tabs_before = slow_console_window.notebook.get_n_pages()

    started = time.time()
    slow_console_window.open_console(slow_key)
    pump(0.3)
    elapsed = time.time() - started
    opening = slow_console_window.consoles.get(slow_key)

    if slow_console_window.notebook.get_n_pages() != tabs_before + 1:
        failures.append("no tab appeared while the console was connecting")
    elif elapsed > 1.0:
        failures.append(f"tab took {elapsed:.1f}s to appear")
    elif opening is None or opening.protocol != "offline":
        failures.append("connecting tab is not a placeholder")
    elif "Connecting" not in opening.status_panel.title.get_text():
        failures.append(
            f"connecting tab reads {opening.status_panel.title.get_text()!r}"
        )
    else:
        print(f"[smoke] console tab opens in {elapsed:.2f}s and says Connecting")

    deadline = time.time() + 15
    while time.time() < deadline:
        pump(0.3)
        if slow_console_window.consoles.get(slow_key) is not opening:
            break
    swapped = slow_console_window.consoles.get(slow_key)
    if swapped is opening:
        failures.append("the real console never replaced the placeholder")
    elif slow_console_window.notebook.get_n_pages() != tabs_before + 1:
        failures.append("swapping the real console in changed the tab count")
    else:
        print(f"[smoke] real console ({swapped.protocol}) swapped into the same tab")
    slow_console_window.shutdown()
    slow_console_window.destroy()
    pump(0.3)

    # A console that cannot open keeps its tab and explains itself.
    failing_window = build_window(FailingConsoleAPI(), Config(dict(config)))
    if not wait_for_guests(failing_window):
        failures.append("failing-console window never listed any guests")
    failing_window.open_console(slow_key)
    deadline = time.time() + 8
    while time.time() < deadline:
        pump(0.3)
        console = failing_window.consoles.get(slow_key)
        if console is not None and console.last_status == "error":
            break
    console = failing_window.consoles.get(slow_key)
    if console is None:
        failures.append("a failed console lost its tab")
    elif console.last_status != "error":
        failures.append(
            f"failed console shows {console.status_panel.title.get_text()!r}"
        )
    elif not console.status_panel.reconnect_button.get_visible():
        failures.append("a failed console offers no way to retry")
    else:
        print("[smoke] a failed console keeps its tab and offers Reconnect")
    failing_window.shutdown()
    failing_window.destroy()
    pump(0.3)

    # -- the summary must not flicker across polls -----------------------
    # Rebuilding the tree empties the selection for an instant. If that
    # reaches the summary it discards its detail and re-fetches, which reads
    # as agent/IP/OS blinking to "-" every refresh.
    window.notebook.set_current_page(0)
    window.sidebar.select_key("pve.example.invalid/pve-node-01/qemu/100")
    deadline = time.time() + 6
    while time.time() < deadline:
        pump(0.2)
        if window.summary.values["agent"].get_text() not in ("-", "checking..."):
            break

    watched = ("agent", "address", "os")
    populated = {f: window.summary.values[f].get_text() for f in watched}
    if any(v in ("-", "") for v in populated.values()):
        failures.append(f"summary never populated: {populated}")
    else:
        spurious = []
        window.sidebar.connect("guest-selected", lambda _s, key: spurious.append(key))
        blanks = {f: 0 for f in watched}
        end = time.time() + 5
        while time.time() < end:
            pump(0.1)
            for field in watched:
                if window.summary.values[field].get_text() == "-":
                    blanks[field] += 1
        if any(blanks.values()):
            failures.append(f"summary blanked during polling: {blanks}")
        elif spurious:
            failures.append(f"tree rebuild emitted selection changes: {spurious}")
        else:
            print("[smoke] summary holds its detail across polls")

    # -- task feed ------------------------------------------------------
    if window.task_feed.get_visible():
        failures.append("task feed is visible by default")
    if "tasks" in api.calls:
        failures.append("task feed polled while collapsed")
    else:
        print("[smoke] task feed starts hidden and does not poll")

    window.task_feed.open()
    pump(0.8)
    rows = len(window.task_feed.store)
    if rows != 3:
        failures.append(f"task feed shows {rows} tasks, expected 3")
    else:
        from proxima.ui import task_feed as tf_mod

        statuses = [window.task_feed.store[i][tf_mod.COL_STATUS] for i in range(rows)]
        starts = [window.task_feed.store[i][tf_mod.COL_START] for i in range(rows)]
        if not all("-" in v for v in starts):
            failures.append(f"task start times lack dates: {starts}")
        if "running" not in statuses or "OK" not in statuses:
            failures.append(f"task feed statuses wrong: {statuses}")
        else:
            print(f"[smoke] task feed populated: {statuses}")
    if "running" not in window.task_feed.title_label.get_text():
        failures.append(
            f"task feed title lacks the running count: "
            f"{window.task_feed.title_label.get_text()!r}"
        )

    columns = [c.get_title() for c in window.task_feed.view.get_columns()]
    if columns != ["Start", "End", "Server", "Node", "User", "Description", "Status"]:
        failures.append(f"task feed columns are {columns}")
    elif not window.task_feed.view.get_columns()[5].get_expand():
        failures.append("the Description column does not expand")
    else:
        print(f"[smoke] task columns: {', '.join(columns)}")

    window.task_feed.close()
    pump(0.2)
    if window.task_feed.get_visible():
        failures.append("task feed still visible after close")
    if window.task_feed._source is not None:
        failures.append("task feed kept polling after closing")

    # -- snapshots ------------------------------------------------------
    window.sidebar.select_key("pve.example.invalid/pve-node-02/qemu/900")  # template
    pump(0.3)
    if any(i.get_sensitive() for i in window.snapshot_items.values()):
        failures.append("snapshot buttons are enabled for a template")

    window.sidebar.select_key("pve.example.invalid/pve-node-01/qemu/102")  # stopped VM
    pump(0.3)
    if not all(i.get_sensitive() for i in window.snapshot_items.values()):
        failures.append("snapshot buttons are disabled for a stopped guest")
    else:
        print("[smoke] snapshot buttons enabled for guests, not templates")

    # Revert must target the newest snapshot, not an arbitrary one.
    guest = window.sidebar.selected_guest()
    window._confirm = lambda *a, **k: True  # auto-approve
    window._snapshot_action("revert")
    pump(0.8)
    rollbacks = [c for c in api.calls if c[0] == "snap-rollback"]
    if not rollbacks:
        failures.append("revert did not issue a rollback")
    elif rollbacks[-1][2] != "before-upgrade":
        failures.append(
            f"revert rolled back to {rollbacks[-1][2]!r}, expected the "
            "newest snapshot 'before-upgrade'"
        )
    else:
        print("[smoke] revert targets the newest snapshot")

    # Revert is only offered when a snapshot exists, and says which one.
    window.sidebar.select_key("pve.example.invalid/pve-node-01/qemu/100")
    pump(0.8)
    revert = window.snapshot_items["revert"]
    if not revert.get_sensitive():
        failures.append("revert disabled despite an existing snapshot")
    # GtkToolItem stores its tooltip apart from the widget, so the tool
    # button's getter always returns None; read the menu item, which carries
    # the same text.
    tooltip = window.snapshot_menu_items["revert"].get_tooltip_text() or ""
    if "before-upgrade" not in tooltip or "ago" not in tooltip:
        failures.append(f"revert tooltip reads {tooltip!r}")
    else:
        print(f"[smoke] revert tooltip: {tooltip}")

    saved = FakeAPI.SNAPSHOTS
    try:
        FakeAPI.SNAPSHOTS = []
        window.sidebar.select_key("pve.example.invalid/pve-node-01/qemu/101")
        pump(0.8)
        if window.snapshot_items["revert"].get_sensitive():
            failures.append("revert enabled for a guest with no snapshots")
        elif not window.snapshot_items["take"].get_sensitive():
            failures.append("take disabled for a guest with no snapshots")
        else:
            print(
                f"[smoke] no snapshots -> revert disabled, tooltip: "
                f"{window.snapshot_menu_items['revert'].get_tooltip_text()!r}"
            )
    finally:
        FakeAPI.SNAPSHOTS = saved
    window.sidebar.select_key("pve.example.invalid/pve-node-01/qemu/102")
    pump(0.8)

    # Pop out and back again.
    popout_console = FakeConsole("popme")
    popout_console.guest_key = "pve.example.invalid/pve-node-01/qemu/102"
    window.consoles[popout_console.guest_key] = popout_console
    ppage = window.panes.append(popout_console, Gtk.Label(label="p"))
    pump(0.4)

    tabs_before = window.notebook.get_n_pages()
    window.popout_console()
    pump(0.6)
    popout = window._popouts.get(popout_console.guest_key)
    if popout is None:
        failures.append("pop out did not create a window")
    elif window.notebook.get_n_pages() != tabs_before - 1:
        failures.append("pop out did not remove the tab")
    elif popout_console.get_parent() is None:
        failures.append("popped-out console lost its parent")
    else:
        print("[smoke] console popped out into its own window")
        if not popout._action_items["start"].get_sensitive():
            failures.append("pop-out toolbar did not follow guest state")
        popout.return_to_tabs()
        pump(0.6)
        if window.notebook.get_n_pages() != tabs_before:
            failures.append("returning from pop out did not restore the tab")
        elif window._popouts:
            failures.append("pop-out window was not forgotten")
        else:
            print("[smoke] pop-out returned to a tab")
    window.close_console(popout_console.guest_key)
    pump(0.2)

    manager = SnapshotManager(window, api, guest)
    pump(0.6)

    def snapshot_rows(store, parent=None, depth=0):
        rows = []
        row = store.iter_children(parent)
        while row is not None:
            rows.append((depth, store.get_value(row, 0)))
            rows.extend(snapshot_rows(store, row, depth + 1))
            row = store.iter_next(row)
        return rows

    shape = snapshot_rows(manager.store)
    expected = [
        (0, "clean-install"),
        (1, "experiment"),
        (1, "before-upgrade"),
        (2, "NOW"),
    ]
    if shape != expected:
        failures.append(f"snapshot tree is {shape}, expected {expected}")
    else:
        print("[smoke] snapshot manager draws the branching history as a tree")

    # NOW is not a snapshot and must never be a rollback or delete target.
    manager.view.expand_all()
    manager.view.get_selection().select_path(Gtk.TreePath.new_from_string("0:1:0"))
    pump(0.2)
    if manager.selected() is not None:
        failures.append("the NOW row is selectable as a snapshot")
    elif manager.rollback_button.get_sensitive():
        failures.append("roll back is enabled on the NOW row")
    else:
        print("[smoke] NOW row cannot be rolled back to or deleted")
    manager.destroy()

    # -- guest agent indicators -----------------------------------------
    window.sidebar.select_key("pve.example.invalid/pve-node-01/qemu/100")  # running VM
    pump(0.8)
    if not window._agent_ok:
        failures.append("guest agent indicator did not go green for a running guest")
    if not all(i.get_sensitive() for i in window.agent_items.values()):
        failures.append("guest agent menu is disabled despite a live agent")
    else:
        print("[smoke] guest agent menu enabled when the agent answers")

    api.agent_available = False
    window.sidebar.select_key("pve.example.invalid/pve-node-01/qemu/101")
    pump(0.8)
    if window._agent_ok:
        failures.append("agent indicator stayed on with no agent")
    if any(i.get_sensitive() for i in window.agent_items.values()):
        failures.append("guest agent menu enabled with no agent")
    else:
        print("[smoke] guest agent menu disabled when the agent is absent")
    api.agent_available = True

    # A container has no QEMU guest agent at all.
    window.sidebar.select_key("pve.example.invalid/pve-node-02/lxc/202")
    pump(0.5)
    if window.qga_icon.get_opacity() > 0.3:
        failures.append("agent indicator is not dimmed for a container")

    # -- telemetry -------------------------------------------------------
    window.sidebar.select_key("pve.example.invalid/pve-node-01/qemu/100")
    pump(0.3)
    telemetry_console = FakeConsole()
    telemetry_console.guest_key = "telemetry"
    window.consoles["telemetry"] = telemetry_console
    tpage = window.panes.append(telemetry_console, Gtk.Label(label="t"))
    pump(0.3)
    window._sample_telemetry()
    text = window.telemetry_label.get_text()
    if "1920x1080" not in text or "MB/s" not in text or "fps" not in text:
        failures.append(f"telemetry label reads {text!r}")
    else:
        print(f"[smoke] telemetry label: {text}")
    if window.vdagent_icon.get_opacity() < 0.9:
        failures.append("vdagent indicator dim despite a connected agent")
    window.close_console("telemetry")
    pump(0.2)

    # -- context follows the front tab, not the tree --------------------
    window.notebook.set_current_page(0)
    window.sidebar.select_key("pve.example.invalid/pve-node-01/qemu/100")  # running
    pump(0.5)
    if window.context_guest() is None or window.context_guest().vmid != 100:
        failures.append("summary page context is not the tree selection")

    ctx_console = FakeConsole("ctx")
    ctx_console.guest_key = (
        "pve.example.invalid/pve-node-01/qemu/102"  # a stopped guest
    )
    window.consoles[ctx_console.guest_key] = ctx_console
    cpage = window.panes.append(ctx_console, Gtk.Label(label="c"))
    pump(0.5)

    if window.context_guest() is None or window.context_guest().vmid != 102:
        failures.append("console tab did not take over the context")
    # Clicking around the tree must not re-aim the toolbar.
    window.sidebar.select_key("pve.example.invalid/pve-node-01/qemu/100")
    pump(0.5)
    if window.context_guest().vmid != 102:
        failures.append(
            "tree selection overrode the console tab's context "
            f"(got {window.context_guest().vmid})"
        )
    elif any(w.get_sensitive() for w in window._action_items["stop"]):
        failures.append("Stop enabled for the stopped guest in the front tab")
    else:
        print("[smoke] toolbar context follows the front tab")
    window.close_console(ctx_console.guest_key)
    window.notebook.set_current_page(0)
    pump(0.4)

    # -- a stopped guest still gets a tab -------------------------------
    tabs_before = window.notebook.get_n_pages()
    window.open_console("pve.example.invalid/pve-node-01/qemu/102")
    pump(0.6)
    stopped_console = window.consoles.get("pve.example.invalid/pve-node-01/qemu/102")
    if stopped_console is None:
        failures.append("no tab opened for a stopped guest")
    elif window.notebook.get_n_pages() != tabs_before + 1:
        failures.append("stopped guest did not add a tab")
    elif stopped_console.protocol != "offline":
        failures.append(f"stopped guest got a {stopped_console.protocol} console")
    elif not stopped_console.status_panel.get_visible():
        failures.append("stopped guest tab shows no explanation")
    else:
        print("[smoke] stopped guest opens a placeholder tab")
    window.close_console("pve.example.invalid/pve-node-01/qemu/102")
    pump(0.3)

    # -- search filters the tree ----------------------------------------
    window.sidebar.search_entry.set_text("pfsense")
    pump(0.8)
    visible = set(window.sidebar.visible_keys())
    if any("201" in k for k in visible) and len(visible) == 1:
        print(f"[smoke] search narrowed the tree to {len(visible)} guest")
    else:
        failures.append(f"search left {len(visible)} guests: {sorted(visible)}")

    window.sidebar.search_entry.set_text("running qemu")
    pump(0.8)
    shown = window.sidebar.visible_guests()
    if not shown:
        failures.append("multi-term search matched nothing")
    elif any(not g.running for g in shown):
        failures.append("multi-term search kept a stopped guest")
    else:
        print(f"[smoke] multi-term search kept {len(shown)} running VMs")
    window.sidebar.search_entry.set_text("")
    pump(0.8)

    # -- per-guest console preferences ----------------------------------
    window.consoles["pref"] = FakeConsole("pref")
    window.consoles["pref"].guest_key = "pve.example.invalid/pve-node-01/qemu/100"
    prefpage = window.panes.append(window.consoles["pref"], Gtk.Label(label="pref"))
    pump(0.4)
    window.scaling_item.set_active(True)
    pump(0.3)
    stored = (config.get("guest_prefs") or {}).get(
        "pve.example.invalid/pve-node-01/qemu/100", {}
    )
    if not stored.get("scale_to_fit"):
        failures.append(f"scale-to-fit not saved per guest (got {stored})")
    elif (
        window.guest_prefs("pve.example.invalid/pve-node-01/qemu/100")["scaling"]
        is not True
    ):
        failures.append("stored guest preference does not read back")
    else:
        print("[smoke] console preferences saved per guest")
    window.close_console("pref")
    window.notebook.set_current_page(0)
    pump(0.3)

    # -- multiple connections -------------------------------------------
    from proxima.api.connection import FAILED
    from proxima.ui import sidebar as sb

    second = FakeAPI()
    second_conn = make_connection(second)
    second_conn.host = "pve2.example.invalid"
    window.connections.add(second_conn)
    window._connection_ready(second_conn)
    pump(1.5)

    roots = []
    row = window.sidebar.store.iter_children(None)
    while row is not None:
        roots.append(window.sidebar.store.get_value(row, sb.COL_ID))
        row = window.sidebar.store.iter_next(row)
    if len(roots) != 2:
        failures.append(f"tree shows {len(roots)} servers, expected 2: {roots}")
    else:
        print(f"[smoke] two servers in the tree: {', '.join(roots)}")

    # Keys are namespaced, so the same VMID on both servers stays distinct.
    keys = window.sidebar.visible_keys()
    first_100 = "pve.example.invalid/pve-node-01/qemu/100"
    second_100 = "pve2.example.invalid/pve-node-01/qemu/100"
    if first_100 not in keys or second_100 not in keys:
        failures.append("identical VMIDs on two servers collided")
    else:
        print("[smoke] identical VMIDs stay distinct across servers")

    # Each guest resolves to its own server's client.
    if window.api_for(window.sidebar.guests[second_100]) is not second:
        failures.append("guest resolved to the wrong server's API")
    else:
        print("[smoke] guests resolve to their own server")

    # A failed server stays listed and does not take the others with it.
    second_conn.state = FAILED
    second_conn.error = "connection refused"
    window.sidebar.update(window.connections)
    pump(0.4)
    labels = []
    row = window.sidebar.store.iter_children(None)
    while row is not None:
        labels.append(window.sidebar.store.get_value(row, sb.COL_LABEL))
        row = window.sidebar.store.iter_next(row)
    if not any("failed" in label for label in labels):
        failures.append(f"failed server not marked: {labels}")
    elif len(window.sidebar.visible_keys()) == 0:
        failures.append("a failed server emptied the whole tree")
    else:
        print("[smoke] failed server marked, others unaffected")

    window.disconnect_connection("pve2.example.invalid")
    pump(0.6)
    if window.connections.get("pve2.example.invalid") is not None:
        failures.append("disconnect did not remove the server")
    else:
        print("[smoke] disconnect removes one server, keeps the rest")

    # -- folders ---------------------------------------------------------
    FakeAPI.NOTES = {}
    window.sidebar.folder_view = True
    window.sidebar._update_view_button()

    window.move_guest_to_folder(first_100, "Production/Customer A")
    deadline = time.time() + 5
    while time.time() < deadline:
        pump(0.2)
        if FakeAPI.NOTES.get(100):
            break

    stored = FakeAPI.NOTES.get(100, "")
    if notes_mod.folder_of(stored) != ("Production", "Customer A"):
        failures.append(f"folder not written to notes: {stored!r}")
    else:
        print("[smoke] folder written into the guest notes block")

    # User text in the notes must survive a folder change.
    FakeAPI.NOTES[101] = "Important: do not delete."
    window.move_guest_to_folder("pve.example.invalid/pve-node-01/qemu/101", "Staging")
    deadline = time.time() + 5
    while time.time() < deadline:
        pump(0.2)
        if "PROXIMA" in FakeAPI.NOTES.get(101, ""):
            break
    kept = notes_mod.parse(FakeAPI.NOTES.get(101, ""))[1]
    if kept != "Important: do not delete.":
        failures.append(f"existing notes were damaged: {kept!r}")
    elif notes_mod.folder_of(FakeAPI.NOTES[101]) != ("Staging",):
        failures.append("folder not applied alongside existing notes")
    else:
        print("[smoke] existing notes preserved when setting a folder")

    window.sidebar.rebuild()
    pump(0.3)
    folder_labels = []

    def collect(parent):
        row = window.sidebar.store.iter_children(parent)
        while row is not None:
            if window.sidebar.store.get_value(row, sb.COL_KIND) == "folder":
                folder_labels.append(
                    window.sidebar.store.get_value(row, sb.COL_TOOLTIP)
                )
            collect(row)
            row = window.sidebar.store.iter_next(row)

    collect(None)
    if (
        "Production" not in folder_labels
        or "Production/Customer A" not in folder_labels
    ):
        failures.append(f"folder tree wrong: {folder_labels}")
    else:
        print(f"[smoke] folder view built: {sorted(set(folder_labels))}")

    # Moving back to the root clears the folder.
    window.move_guest_to_folder(first_100, "")
    deadline = time.time() + 5
    while time.time() < deadline:
        pump(0.2)
        if not notes_mod.folder_of(FakeAPI.NOTES.get(100, "")):
            break
    if notes_mod.folder_of(FakeAPI.NOTES.get(100, "")):
        failures.append("moving to the root did not clear the folder")
    else:
        print("[smoke] moving to the root clears the folder")

    window.sidebar.folder_view = False
    window.sidebar._update_view_button()
    window.sidebar.rebuild()
    pump(0.3)

    # -- a reconnect keeps the tab ---------------------------------------
    # Starting a stopped guest must not make its console vanish and come
    # back. The swap itself is what regressed, so it is exercised directly:
    # building a real console here would need a live SPICE or VNC server.
    window.notebook.set_current_page(0)
    pump(0.3)
    stopped_key = "pve.example.invalid/pve-node-01/qemu/102"
    window.open_console(stopped_key)
    pump(0.8)

    placeholder = window.consoles.get(stopped_key)
    if placeholder is None or placeholder.protocol != "offline":
        failures.append("stopped guest did not get a placeholder console")
    else:
        # A tab after it, so a rebuild that appended instead of replacing
        # would show up as a move to the end.
        trailing = FakeConsole("trailing")
        trailing.guest_key = "trailing"
        window.consoles["trailing"] = trailing
        window.panes.append(trailing, Gtk.Label(label="trailing"))
        pump(0.3)

        pages_before = window.notebook.get_n_pages()
        position_before = window.notebook.page_num(placeholder)

        guest = window.sidebar.guests[stopped_key]
        rebuilt = FakeConsole(guest.name)
        window._install_console(guest, rebuilt)
        pump(0.4)

        if window.consoles.get(stopped_key) is not rebuilt:
            failures.append("install did not take over the guest's console")
        elif window.notebook.get_n_pages() != pages_before:
            failures.append(
                f"tab count changed on reconnect: {pages_before} -> "
                f"{window.notebook.get_n_pages()}"
            )
        elif window.notebook.page_num(rebuilt) != position_before:
            failures.append(
                f"console moved from tab {position_before} to "
                f"{window.notebook.page_num(rebuilt)} on reconnect"
            )
        elif window.notebook.page_num(placeholder) >= 0:
            failures.append("the replaced console is still in the notebook")
        else:
            print("[smoke] reconnect replaces the console, tab stays put")

        window.close_console_widget(trailing)
        window.close_console(stopped_key)
        pump(0.3)

    # -- folder view holds guests until it knows where they go ------------
    FakeAPI.NOTES = {100: notes_mod.with_folder("", ["Production"])}
    for guest in window.sidebar.guests.values():
        guest.notes_loaded = False
        guest.folder = ()
    window.sidebar.folder_view = True
    window.sidebar._update_view_button()
    window.sidebar.rebuild()
    # Checked without pumping: the poll loop would kick off a folder scan
    # and, against a fake API, finish it before a pump returned.
    if window.sidebar.visible_keys():
        failures.append("folder view showed guests before their notes were read")
    else:
        print("[smoke] folder view holds guests until folders are known")

    window._load_folders()
    deadline = time.time() + 8
    while time.time() < deadline:
        pump(0.3)
        if window.sidebar.visible_keys():
            break
    shown = window.sidebar.visible_keys()
    unloaded = [g.key for g in window.sidebar.guests.values() if not g.notes_loaded]
    if not shown:
        failures.append("folder view never showed any guests")
    elif unloaded:
        failures.append(f"guests shown while {len(unloaded)} still unread")
    else:
        print(f"[smoke] folder scan completed in one pass ({len(shown)} guests)")

    window.sidebar.folder_view = False
    window.sidebar._update_view_button()
    window.sidebar.rebuild()
    pump(0.3)

    # -- confirmation settings -------------------------------------------
    from proxima.ui import actions as action_defs

    running = window.sidebar.guests[f"{CONN_ID}/pve-node-01/qemu/100"]
    asks = {}
    for name in ("stop", "shutdown", "reset", "suspend"):
        action = action_defs.ACTIONS_BY_NAME[name]
        asks[name] = bool(action_defs.confirmation_text(action, running, config))
    if asks != {"stop": True, "shutdown": True, "reset": True, "suspend": False}:
        failures.append(f"default confirmations are wrong: {asks}")
    else:
        print("[smoke] stop/shutdown/reset ask by default, pause does not")

    config["confirm_stop"] = False
    config["confirm_pause"] = True
    flipped = {
        name: bool(
            action_defs.confirmation_text(
                action_defs.ACTIONS_BY_NAME[name], running, config
            )
        )
        for name in ("stop", "suspend")
    }
    if flipped != {"stop": False, "suspend": True}:
        failures.append(f"confirmation settings ignored: {flipped}")
    else:
        print("[smoke] confirmation toggles are honoured")
    config["confirm_stop"] = True
    config["confirm_pause"] = False

    # -- name formats -----------------------------------------------------
    def label_for(vmid):
        store = window.sidebar.store
        found = []

        def walk(parent):
            row = store.iter_children(parent)
            while row is not None:
                if store.get_value(row, 0).endswith(f"/{vmid}"):
                    found.append(store.get_value(row, 1))
                walk(row)
                row = store.iter_next(row)

        walk(None)
        return found[0] if found else ""

    if label_for(100) != "web01 (100)":
        failures.append(f"tree name format wrong: {label_for(100)!r}")
    else:
        print("[smoke] tree shows 'name (id)' by default")

    config["tree_name_format"] = "id"
    window._apply_name_formats()
    pump(0.3)
    if label_for(100) != "100 (web01)":
        failures.append(f"ID-first tree format wrong: {label_for(100)!r}")
    else:
        print("[smoke] ID-first tree format applies live")

    # Guests sort by whichever half of the label leads.
    def guests_under(node_name):
        store = window.sidebar.store
        labels = []

        def walk(parent):
            row = store.iter_children(parent)
            while row is not None:
                if store.get_value(row, 4) == "node" and store.get_value(
                    row, 5
                ).endswith(f"/{node_name}"):
                    child = store.iter_children(row)
                    while child is not None:
                        labels.append(store.get_value(child, 1))
                        child = store.iter_next(child)
                walk(row)
                row = store.iter_next(row)

        walk(None)
        return labels

    if guests_under("pve-node-01") != [
        "100 (web01)",
        "101 (db01)",
        "102 (build-runner)",
    ]:
        failures.append(
            f"ID-first tree is not sorted by ID: {guests_under('pve-node-01')}"
        )
    else:
        print("[smoke] ID-first tree sorts by VMID")

    config["tree_name_format"] = "name"
    window._apply_name_formats()
    pump(0.3)

    if guests_under("pve-node-01") != [
        "build-runner (102)",
        "db01 (101)",
        "web01 (100)",
    ]:
        failures.append(
            f"name-first tree is not sorted by name: {guests_under('pve-node-01')}"
        )
    else:
        print("[smoke] name-first tree sorts by name")

    # Wait for the *real* console, not the placeholder that open_console puts
    # up first: the placeholder is swapped out when the session connects, and
    # a reference captured before that goes stale mid-test.
    console_key = f"{CONN_ID}/pve-node-01/qemu/100"
    window.open_console(console_key)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        pump(0.1)
        console = window.consoles.get(console_key)
        if (
            console is not None
            and type(console).__name__ != "PlaceholderConsole"
            and window.notebook.get_tab_label(console) is not None
        ):
            break
    console = window.consoles.get(console_key)
    if console is None or window.notebook.get_tab_label(console) is None:
        failures.append("no console tab appeared for a running guest")
        console = None
    else:
        print(f"[smoke] console tab is a {type(console).__name__}")
    titles = {}
    for style in ("name", "id", "both"):
        config["tab_title_format"] = style
        window._apply_name_formats()
        pump(0.1)
        # Re-read: a reconnect can swap the widget behind the tab.
        current = window.consoles.get(console_key)
        label = window.notebook.get_tab_label(current) if current else None
        if label is not None:
            titles[style] = label.label.get_text()
    if console is None:
        pass  # already reported above
    elif titles != {"name": "web01", "id": "100", "both": "web01 (100)"}:
        failures.append(f"tab title formats wrong: {titles}")
    else:
        print("[smoke] tab titles follow the name/ID/both setting")
    config["tab_title_format"] = "name"

    # -- reopening a console on the other protocol -------------------------
    # The console itself is not rebuilt here: this fake has no vnc_ticket, on
    # purpose, so planning is what gets checked -- it is where the decision
    # is actually made.
    key = f"{CONN_ID}/pve-node-01/qemu/100"
    if console is None:
        print("[smoke] (no console; protocol switch not exercised)")
    elif console.protocol != "spice":
        print(
            f"[smoke] (spice unavailable; protocol switch tested from "
            f"{console.protocol})"
        )
    else:
        window._sync_view_menu()
        if window.switch_protocol_item.get_label() != "Reopen Console with VNC":
            failures.append("the VM menu does not offer VNC on a SPICE tab")
        elif not window.switch_protocol_item.get_sensitive():
            failures.append("the VNC entry is disabled on a SPICE console")

        window._force_vnc.add(key)
        if plan_protocol(key) != "vnc":
            failures.append("a forced console still planned SPICE")
        else:
            print("[smoke] a SPICE-capable guest plans VNC once switched")

        # A guest that only ever plans VNC must still be able to go back:
        # forcing VNC is not allowed to look like evidence about the display.
        if window.sidebar.guests[key].spice_capable is not True:
            failures.append("forcing VNC lost the guest's SPICE capability")

        class _VncTab:
            protocol = "vnc"
            guest_key = key

        window._sync_protocol_switch(_VncTab())
        if window.switch_protocol_item.get_label() != "Reopen Console with SPICE":
            failures.append("no way back to SPICE from a switched console")
        elif not window.switch_protocol_item.get_sensitive():
            failures.append("the way back to SPICE is disabled")
        else:
            print("[smoke] a VNC console offers SPICE back")

    window.close_console(key)
    pump(0.3)
    if key in window._force_vnc:
        failures.append("closing the tab did not forget the VNC choice")
    else:
        print("[smoke] closing the tab returns the guest to preferring SPICE")

    # -- session restore ---------------------------------------------------
    window.open_console(key)
    pump(1.0)
    config["restore_session"] = True
    window._save_session()
    if config["session_consoles"] != [key]:
        failures.append(f"open console not saved: {config['session_consoles']}")
    elif not config["session_expanded"]:
        failures.append("tree expansion was not saved")
    else:
        print(
            f"[smoke] session saved: 1 console, "
            f"{len(config['session_expanded'])} expanded rows"
        )
    window.close_console(key)
    pump(0.3)

    window._restore_keys = [key]
    window._restore_until = time.monotonic() + 30
    window._resume_session()
    pump(1.0)
    if key not in window.consoles:
        failures.append("the saved console did not reopen")
    else:
        print("[smoke] a saved console reopens once its guest appears")
    window.close_console(key)
    pump(0.3)

    # A guest that never turns up must not leave the restore running.
    window._restore_keys = ["gone.example.invalid/pve-node-01/qemu/999"]
    window._restore_until = time.monotonic() - 1
    window._resume_session()
    if window._restore_keys:
        failures.append("session restore never gives up on a missing guest")
    else:
        print("[smoke] restore gives up on guests that never appear")

    # -- renaming ----------------------------------------------------------
    from proxima.api.models import valid_guest_name

    bad = [
        name
        for name in ("web 01", "web/01", "-web01", "web01-", "")
        if valid_guest_name(name)
    ]
    good = [
        name for name in ("web01", "web-01", "a.b.c", "1") if not valid_guest_name(name)
    ]
    if bad or good:
        failures.append(f"name validation wrong: accepted {bad}, rejected {good}")
    else:
        print("[smoke] guest name validation matches the Proxmox rules")

    before = len(api.calls)
    window.rename_guest(key, "web01-renamed")
    pump(0.8)
    renames = [
        c for c in api.calls[before:] if isinstance(c, tuple) and c[0] == "rename"
    ]
    if not renames:
        failures.append("rename never reached the API")
    elif renames[0][1:] != (100, "web01-renamed", "qemu"):
        failures.append(f"rename sent the wrong parameters: {renames[0]}")
    elif window.sidebar.guests[key].name != "web01-renamed":
        failures.append("the tree did not take the new name")
    else:
        print("[smoke] rename reaches the API and updates the tree")
    window.sidebar.guests[key].name = "web01"
    for row in SAMPLE:
        if row["vmid"] == 100:
            row["name"] = "web01"

    # An inline edit is not allowed to be wiped out by the poll.
    window.sidebar._editing_key = key
    window.sidebar.store.set_value(window.sidebar._find_row(key), 1, "being-edited")
    window.sidebar.rebuild()
    if label_for(100) != "being-edited":
        failures.append("a poll rebuild destroyed an open inline rename")
    else:
        print("[smoke] the tree holds still while a rename is open")
    window.sidebar._end_editing()
    pump(0.2)

    # -- cloning a template ------------------------------------------------
    template_key = f"{CONN_ID}/pve-node-02/qemu/900"
    template = window.sidebar.guests[template_key]
    clone = CloneDialog(window, api, template)
    pump(0.6)
    name, vmid, target, full, storage = clone.values()
    if vmid != 903:
        failures.append(f"clone dialog did not take the next VMID: {vmid}")
    elif full or storage is not None:
        failures.append("clone dialog does not default to a linked clone")
    elif target != "pve-node-02":
        failures.append(f"clone dialog targets the wrong node: {target}")
    else:
        print(f"[smoke] clone dialog defaults: {name} ({vmid}) linked on {target}")

    clone.mode_combo.set_active_id("full")
    pump(0.2)
    if not clone.storage_combo.get_sensitive():
        failures.append("storage stays disabled for a full clone")
    clone.storage_combo.set_active_id("ceph-pool")
    clone.name_entry.set_text("bad name")
    pump(0.1)
    if clone.ok_button.get_sensitive():
        failures.append("the clone dialog accepts an invalid name")
    clone.name_entry.set_text("debian12-clone")
    pump(0.1)
    if not clone.ok_button.get_sensitive():
        failures.append("the clone dialog rejects a valid name")
    values = clone.values()
    clone.destroy()
    if values != ("debian12-clone", 903, "pve-node-02", True, "ceph-pool"):
        failures.append(f"clone dialog returned {values}")
    else:
        print("[smoke] clone dialog collects a full clone onto a storage")

    # The template's context menu must not offer things a template cannot do.
    menu = Gtk.Menu()
    window.sidebar._build_single_menu(menu, template)
    entries = [
        c.get_label()
        for c in menu.get_children()
        if isinstance(c, Gtk.MenuItem) and c.get_label()
    ]
    unwanted = [
        e
        for e in entries
        if any(
            word in e
            for word in (
                "Console",
                "Start",
                "Stop",
                "Shutdown",
                "Reset",
                "Snapshot",
                "Suspend",
                "Reboot",
            )
        )
    ]
    if unwanted:
        failures.append(f"template menu offers {unwanted}")
    elif "Clone..." not in entries or "Rename..." not in entries:
        failures.append(f"template menu is missing entries: {entries}")
    else:
        print(f"[smoke] template context menu: {entries}")

    menu = Gtk.Menu()
    window.sidebar._build_single_menu(menu, running)
    entries = [
        c.get_label()
        for c in menu.get_children()
        if isinstance(c, Gtk.MenuItem) and c.get_label()
    ]
    if any("Snapshot" in e for e in entries):
        failures.append(f"snapshots are still in the tree menu: {entries}")
    elif "Rename..." not in entries or "Open Console" not in entries:
        failures.append(f"guest menu is missing entries: {entries}")
    else:
        print(f"[smoke] guest context menu: {entries}")

    window.sidebar.folder_view = True
    menu = Gtk.Menu()
    window.sidebar._build_single_menu(menu, running)
    entries = [
        c.get_label()
        for c in menu.get_children()
        if isinstance(c, Gtk.MenuItem) and c.get_label()
    ]
    folder_entries = [e for e in entries if "Folder" in e or "folder" in e]
    if folder_entries != ["Move to New Subfolder..."]:
        failures.append(f"folder menu entries are {folder_entries}")
    else:
        print("[smoke] folder view offers only the new-subfolder entry")
    window.sidebar.folder_view = False

    # Folders sort by name regardless of case, and independently of however
    # the guests below them are sorted.
    paths = [("Zebra",), ("apps",), ("Apps", "beta"), ("Apps", "Alpha")]
    for style in ("name", "id"):
        window.sidebar.name_format = style
        ordered = [
            "/".join(p) for p in sorted(paths, key=window.sidebar._folder_sort_key)
        ]
        if ordered != ["apps", "Apps/Alpha", "Apps/beta", "Zebra"]:
            failures.append(f"folders sorted {ordered} with {style} names")
            break
    else:
        print("[smoke] folders sort case-insensitively by name in both modes")
    window.sidebar.name_format = "name"

    # -- clipboard and audio switches --------------------------------------
    switch_key = f"{CONN_ID}/pve-node-01/qemu/100"
    # Let anything already in flight for this guest land first, or it will
    # replace the stand-in console halfway through and the assertions below
    # would be reading a widget that is no longer on screen.
    pump(1.0)
    window._console_offline.pop(switch_key, None)
    switch_console = FakeConsole()
    switch_console.guest_key = switch_key
    window.consoles[switch_key] = switch_console
    spage = window.panes.append(switch_console, Gtk.Label(label="s"))
    pump(0.4)

    # The switches act on whichever console is installed now, and a
    # reconnect may legitimately have swapped it, so read it back rather
    # than holding on to the one that was put there.
    def live_console():
        return window.consoles.get(switch_key)

    switch_guest = window.sidebar.guests[switch_key]
    if window._guest_switch(switch_guest, "clipboard") is not True:
        failures.append("clipboard does not default to on")

    window._toggle_clipboard()
    pump(0.3)
    stored = (config.get("guest_prefs") or {}).get(switch_key, {})
    if "clipboard" in stored:
        failures.append(f"the clipboard button wrote a saved preference: {stored}")
    elif getattr(live_console(), "share_clipboard", None) is not False:
        failures.append("clipboard switch did not reach the console")
    elif not window.vdagent_icon.struck:
        failures.append("clipboard icon is not struck through when off")
    else:
        print("[smoke] clipboard toggles live and strikes the icon, saving nothing")

    window._toggle_clipboard()
    pump(0.3)
    if window.vdagent_icon.struck or not getattr(
        live_console(), "share_clipboard", False
    ):
        failures.append("clipboard did not toggle back on")
    else:
        print("[smoke] clipboard toggles back on")

    # Audio cannot be changed on a live SPICE session, so the toggle has to
    # rebuild the console rather than claim success.
    # Checked before pumping: the rebuilt console's own status messages
    # overwrite the status bar a moment later.
    window._reconnecting = None
    window._toggle_audio()
    status = window.status_label_main.get_text()
    reconnecting = getattr(window, "_reconnecting", None) == switch_key
    stored = (config.get("guest_prefs") or {}).get(switch_key, {})
    if "audio" in stored:
        failures.append(f"the audio button wrote a saved preference: {stored}")
    elif switch_console.play_audio is not False:
        # Deliberately the console the toggle acted on, not live_console():
        # audio needs a rebuild, so by now a placeholder is already standing
        # in while the replacement connects.
        failures.append("audio switch did not reach the console")
    elif not reconnecting:
        failures.append(f"audio toggle did not reconnect, said {status!r}")
    else:
        print(f"[smoke] audio toggle reconnects without saving: {status!r}")
    pump(0.5)

    # The session switch has to reach a console built afterwards, or the
    # reconnect would bring the sound straight back.
    if window._guest_switch(switch_guest, "audio") is not False:
        failures.append("a new console would not see the audio switch")
    else:
        print("[smoke] a rebuilt console is told audio is off")

    # ...and closing the tab has to forget it, because the button is only
    # ever about the console in front of you.
    window.close_console(switch_key)
    pump(0.3)
    if window._guest_switch(switch_guest, "audio") is not True:
        failures.append("the audio switch outlived its console")
    else:
        print("[smoke] closing the tab forgets the status bar switches")
    window.consoles[switch_key] = switch_console
    spage = window.panes.append(switch_console, Gtk.Label(label="s"))
    pump(0.3)
    config["guest_prefs"] = {}

    # The reconnect above replaced the fake console with a real one, so close
    # by key rather than by the stale widget reference.
    window.close_console(switch_key)
    pump(0.3)

    # Neither switch means anything on a VNC console, so neither is clickable.
    class _VncConsoleStub(Gtk.Box):
        protocol = "vnc"
        supports = {
            "auto_resize": False,
            "scaling": True,
            "codec": False,
            "compression": False,
            "refresh": True,
            "ctrl_alt_del": True,
            "clipboard": False,
            "audio": False,
        }

        def __init__(self):
            super().__init__()
            self.title = "vnc-stub"
            self.guest_key = switch_key
            self.pack_start(Gtk.Label(label="vnc"), True, True, 0)

        def shutdown(self):
            pass

    vnc_stub = _VncConsoleStub()
    window.consoles[switch_key] = vnc_stub
    vpage = window.panes.append(vnc_stub, Gtk.Label(label="v"))
    pump(0.4)
    if window.audio_icon.can_toggle:
        failures.append("audio is toggleable on a VNC console")
    elif window.vdagent_icon.can_toggle:
        failures.append("clipboard is toggleable on a VNC console")
    else:
        print(
            f"[smoke] VNC console: neither switch is clickable "
            f"({window.audio_icon.get_tooltip_text()!r})"
        )
    window.close_console(switch_key)
    pump(0.3)

    # An indicator with nothing behind it is dimmed, not struck.
    window.notebook.set_current_page(0)
    pump(0.3)
    if window.vdagent_icon.struck:
        failures.append("clipboard icon struck with no console open")
    elif window.vdagent_icon.can_toggle or window.audio_icon.can_toggle:
        failures.append("switches are clickable with no console open")
    else:
        print("[smoke] no console leaves the clipboard icon merely dimmed")

    # -- deleting a guest --------------------------------------------------
    def delete_entry(guest):
        menu = Gtk.Menu()
        window.sidebar._build_single_menu(menu, guest)
        for child in menu.get_children():
            if isinstance(child, Gtk.MenuItem) and child.get_label() == "Delete...":
                return child
        return None

    stopped = window.sidebar.guests[f"{CONN_ID}/pve-node-01/qemu/102"]
    running_guest = window.sidebar.guests[f"{CONN_ID}/pve-node-01/qemu/100"]
    template_guest = window.sidebar.guests[f"{CONN_ID}/pve-node-02/qemu/900"]

    entry = delete_entry(stopped)
    if entry is None:
        failures.append("no Delete entry for a stopped guest")
    elif not entry.get_sensitive():
        failures.append(
            f"Delete disabled for a stopped guest: {entry.get_tooltip_text()}"
        )
    else:
        print("[smoke] Delete offered for a stopped guest")

    entry = delete_entry(running_guest)
    if entry is not None and entry.get_sensitive():
        failures.append("Delete offered for a running guest")
    else:
        print(f"[smoke] running guest: Delete disabled - {entry.get_tooltip_text()!r}")

    # Protection is read from the config, so make sure it has been.
    window.sidebar.select_key(template_guest.key)
    pump(0.8)
    if template_guest.protected is not True:
        failures.append("the protection flag was not read from the config")
    entry = delete_entry(template_guest)
    if entry is None:
        failures.append("no Delete entry for a template")
    elif entry.get_sensitive():
        failures.append("Delete offered for a protected template")
    elif "rotect" not in (entry.get_tooltip_text() or ""):
        failures.append(f"protection not explained: {entry.get_tooltip_text()!r}")
    else:
        print(
            f"[smoke] protected template: Delete disabled - "
            f"{entry.get_tooltip_text()!r}"
        )

    # A protected guest must be refused even if the menu is bypassed.
    before = len(api.calls)
    window.delete_guest(template_guest.key)
    pump(0.6)
    if any(c[0] == "delete" for c in api.calls[before:] if isinstance(c, tuple)):
        failures.append("a protected guest was deleted anyway")
    else:
        print("[smoke] deleting a protected guest is refused")

    # -- which rows get which context menu --------------------------------
    # "Connect..." belongs to the server row and the empty space below the
    # tree; a node or a folder has nothing to do with adding a server.
    sidebar = window.sidebar
    sidebar.rebuild()
    sidebar.view.expand_all()
    pump(0.3)

    class _RightClick:
        type = Gdk.EventType.BUTTON_PRESS
        button = 3

        def __init__(self, x, y):
            self.x, self.y = x, y

    popped = []
    real_popup = sidebar._popup
    sidebar._popup = lambda menu, event: popped.append(
        [
            c.get_label()
            for c in menu.get_children()
            if isinstance(c, Gtk.MenuItem) and c.get_label()
        ]
    )

    def menu_for(kind):
        """Right-click the first row of a given kind; return its menu."""
        store = sidebar.store
        found = []

        def walk(parent):
            row = store.iter_children(parent)
            while row is not None and not found:
                if store.get_value(row, 4) == kind:
                    found.append(store.get_path(row))
                walk(row)
                row = store.iter_next(row)

        walk(None)
        if not found:
            return None
        area = sidebar.view.get_cell_area(found[0], sidebar.name_column)
        popped.clear()
        sidebar._on_button_press(
            sidebar.view, _RightClick(area.x + 4, area.y + area.height / 2)
        )
        return popped[0] if popped else []

    connection_menu = menu_for("connection")
    node_menu = menu_for("node")
    if connection_menu is None or node_menu is None:
        failures.append("could not find a connection and a node row to click")
    elif not any("Connect" in e for e in connection_menu):
        failures.append(f"the server row lost Connect...: {connection_menu}")
    elif node_menu:
        failures.append(f"a node row opened a menu: {node_menu}")
    else:
        print("[smoke] Connect... is on the server row, not on nodes")

    sidebar.folder_view = True
    sidebar.rebuild()
    sidebar.view.expand_all()
    pump(0.3)
    folder_menu = menu_for("folder")
    if folder_menu is None:
        print("[smoke] (no folder row to right-click)")
    elif folder_menu:
        failures.append(f"a folder row opened a menu: {folder_menu}")
    else:
        print("[smoke] a folder row opens no menu")
    sidebar.folder_view = False
    sidebar._popup = real_popup
    sidebar.rebuild()
    pump(0.3)

    # -- busy rows ------------------------------------------------------
    # A change that has been asked for spins until the cluster confirms it.
    # The one unacceptable outcome is a row that spins for ever, so every
    # way out is checked.
    busy_key = f"{CONN_ID}/pve-node-01/qemu/102"  # stopped
    busy_guest = window.sidebar.guests[busy_key]

    def busy_names():
        return {k: v[1] for k, v in window.sidebar.busy.items()}

    # Renaming shows the new name at once, with a spinner, and does not
    # flick back to the old one while the server catches up.
    FakeAPI.RENAME_DELAY = True
    window.rename_guest(busy_key, "renamed-vm")
    pump(0.2)
    if busy_key not in window.sidebar.busy:
        failures.append("a rename did not start a spinner")
    elif busy_names().get(busy_key) != "renamed-vm":
        failures.append(f"the row does not show the new name: {busy_names()}")
    else:
        row = window.sidebar._find_row(busy_key)
        label = window.sidebar.store.get_value(row, sidebar_mod.COL_LABEL)
        if "renamed-vm" not in label:
            failures.append(f"the tree still shows the old name: {label!r}")
        else:
            print(f"[smoke] a rename shows its new name at once: {label!r}")

    # The poll that finally reports the new name ends the wait.
    deadline = time.time() + 10
    while time.time() < deadline and busy_key in window.sidebar.busy:
        pump(0.3)
    if busy_key in window.sidebar.busy:
        failures.append("the rename spinner outlived the server confirming it")
    elif window.sidebar._pulse_source is not None:
        failures.append("the pulse timer kept running with nothing spinning")
    else:
        print("[smoke] the rename spinner stops when the cluster agrees")

    # Renaming to the name it already has must not start a wait that
    # nothing could ever finish.
    window._mark_busy(
        busy_key,
        "name",
        "renamed-vm",
        "renamed-vm",
        "Renaming...",
        30,
        name="renamed-vm",
    )
    if busy_key in window._busy:
        failures.append("renaming to the current name started a spinner")
    else:
        print("[smoke] renaming to the current name starts no spinner")

    # A change that never arrives -- undone in the web UI before we saw it,
    # or a task that failed silently -- gives up on its deadline.
    window._mark_busy(
        busy_key,
        "name",
        "renamed-vm",
        "never-lands",
        "Renaming...",
        0.4,
        name="never-lands",
    )
    if busy_key not in window._busy:
        failures.append("a pending rename did not register")
    deadline = time.time() + 8
    while time.time() < deadline and busy_key in window._busy:
        pump(0.3)
    if busy_key in window._busy:
        failures.append("a change that never arrived spun for ever")
    else:
        print("[smoke] a change that never arrives gives up on its deadline")

    # Somebody else moving the guest resolves it too, even though the value
    # is not the one we asked for.
    window._mark_busy(busy_key, "status", "stopped", "paused", "Suspending...", 30)
    for row in SAMPLE:
        if row["vmid"] == 102:
            row["status"] = "running"  # not what we asked for
    try:
        deadline = time.time() + 8
        while time.time() < deadline and busy_key in window._busy:
            pump(0.3)
        if busy_key in window._busy:
            failures.append("an unexpected status change left the row spinning")
        else:
            print("[smoke] someone else changing the guest ends the wait")
    finally:
        for row in SAMPLE:
            if row["vmid"] == 102:
                row["status"] = "stopped"
        window.refresh()
        pump(0.5)

    # A failed action clears its own spinner rather than waiting it out.
    # _action_failed also raises an error dialog, which would sit there
    # modal with nobody to dismiss it, so it is stubbed for this check.
    window._mark_busy(busy_key, "status", "stopped", "running", "Starting...", 30)
    real_error_dialog = window._error_dialog
    window._error_dialog = lambda *a, **k: None
    try:
        window._action_failed(action_defs.ACTIONS_BY_NAME["start"], busy_guest, "nope")
    finally:
        window._error_dialog = real_error_dialog
    if busy_key in window._busy:
        failures.append("a failed action left the row spinning")
    else:
        print("[smoke] a failed action clears its spinner immediately")

    # Rebooting has no status to wait for, so it must not wait for one.
    window._action_done(action_defs.ACTIONS_BY_NAME["reboot"], busy_guest)
    change = window._busy.get(busy_key)
    if change is None:
        failures.append("a reboot showed nothing at all")
    elif change.deadline - time.monotonic() > window.REBOOT_ACK + 1:
        failures.append("a reboot waits for a status change that never comes")
    else:
        print("[smoke] a reboot acknowledges briefly rather than waiting")
    window._clear_busy(busy_key)
    FakeAPI.RENAME_DELAY = False
    for row in SAMPLE:
        if row["vmid"] == 102:
            row["name"] = "build-runner"
    window.refresh()
    pump(0.6)

    # -- 'info spice', as a real server actually writes it -----------------
    # Verbatim from Proxmox with QEMU/SPICE 0.15.2. This exact text was
    # once read as "nobody is connected", which silently threw people off
    # their consoles, so it is pinned here rather than paraphrased.
    from proxima.api.models import parse_spice_clients as _parse

    REAL_BUSY = (
        "Server:\n"
        "     address: 127.0.0.1:61000 [tls]\n"
        "    migrated: false\n"
        "        auth: spice\n"
        "    compiled: 0.15.2\n"
        "  mouse-mode: client\n"
        "Channel:\n"
        "     address: 127.0.0.1:42220 [tls]\n"
        "     session: 1938609120\n"
        "     channel: 1:0\n"
        "     channel name: main\n"
        "Channel:\n"
        "     address: 127.0.0.1:42234 [tls]\n"
        "     session: 1938609120\n"
        "     channel: 3:0\n"
        "     channel name: inputs\n"
        "Channel:\n"
        "     address: 127.0.0.1:42242 [tls]\n"
        "     session: 1938609120\n"
        "     channel: 2:0\n"
        "     channel name: display\n"
        "Channel:\n"
        "     address: 127.0.0.1:42232 [tls]\n"
        "     session: 1938609120\n"
        "     channel: 4:0\n"
        "     channel name: cursor\n"
    )
    REAL_IDLE = (
        "Server:\n"
        "     address: 127.0.0.1:61002 [tls]\n"
        "    migrated: false\n"
        "        auth: spice\n"
        "    compiled: 0.15.2\n"
        "  mouse-mode: client\n"
        "Channels: none"
    )

    for label, text, expected in (
        ("one viewer, four channels", REAL_BUSY, (1, 4)),
        ("idle server", REAL_IDLE, (0, 0)),
        # Two viewers differ only by their session number.
        ("two viewers", REAL_BUSY + REAL_BUSY.replace("1938609120", "999999"), (2, 8)),
    ):
        parsed = _parse(text)
        if parsed is None:
            failures.append(f"real 'info spice' ({label}) was not understood")
        elif parsed[0] != expected[0] or len(parsed[1]) != expected[1]:
            failures.append(
                f"real 'info spice' ({label}) parsed as {parsed[0]} "
                f"client(s) from {len(parsed[1])} channel(s), expected "
                f"{expected[0]} from {expected[1]}"
            )
        else:
            print(f"[smoke] real 'info spice' {label} -> {parsed[0]} client(s)")

    if any(a.endswith("[tls]") for a in (_parse(REAL_BUSY) or (0, []))[1]):
        failures.append("the [tls] flag was kept as part of an address")

    # And the safety property, on the shapes that are not output at all.
    for label, text in (
        ("empty", ""),
        ("junk", "who knows"),
        ("an error message", "Permission denied"),
    ):
        if _parse(text) is not None:
            failures.append(f"{label} was read as an answer, not as unknown")
    print("[smoke] unrecognised replies mean 'cannot tell', never 'empty'")

    # -- another client on the SPICE console -----------------------------
    # Nobody may be thrown off a console by accident, and "the monitor
    # would not answer" must never be mistaken for "nobody is there".
    occupied_key = f"{CONN_ID}/pve-node-01/qemu/100"
    occupied_guest = window.sidebar.guests[occupied_key]
    window.close_console(occupied_key)
    window._recent_spice.pop(occupied_key, None)
    pump(0.3)

    FakeAPI.SPICE_CLIENTS = {100: 0}
    if window._spice_occupancy(occupied_guest) != (0, []):
        failures.append("an idle console was reported as occupied")

    FakeAPI.SPICE_CLIENTS = {100: 1}
    occupancy = window._spice_occupancy(occupied_guest)
    if occupancy is None or occupancy[0] != 1:
        failures.append(f"an occupied console was not detected: {occupancy}")
    elif window._plan_console(occupied_guest)["protocol"] != "occupied":
        failures.append("planning ignored the other client")
    elif window._plan_console(occupied_guest, takeover=True)["protocol"] != "spice":
        failures.append("take over did not skip the occupancy check")
    else:
        print("[smoke] another client on the console is detected and overridable")

    # Our own session must not count as somebody else, or every reconnect
    # would stop to ask.
    class _LiveSpice(FakeConsole):
        protocol = "spice"
        connected = True

    mine = _LiveSpice()
    mine.guest_key = occupied_key
    window.consoles[occupied_key] = mine
    if window._spice_occupancy(occupied_guest)[0] != 0:
        failures.append("our own SPICE session counted as another client")
    else:
        print("[smoke] our own session is not mistaken for another client")

    # ...nor may one we tore down a moment ago and QEMU has not yet
    # forgotten, which is what a reconnect looks like from here.
    window.consoles.pop(occupied_key, None)
    window._recent_spice[occupied_key] = time.monotonic()
    if window._spice_occupancy(occupied_guest)[0] != 0:
        failures.append("a session we just closed counted as another client")
    else:
        print("[smoke] a just-closed session is not mistaken for another")

    # A guest going down voids that claim: anything there afterwards is
    # somebody else's.
    window.consoles[occupied_key] = PlaceholderStub = FakeConsole()
    PlaceholderStub.guest_key = occupied_key
    SAMPLE[0]["status"] = "stopped"
    try:
        window.refresh()
        deadline = time.time() + 6
        while time.time() < deadline and occupied_key in window._recent_spice:
            pump(0.2)
        if occupied_key in window._recent_spice:
            failures.append("a stopped guest kept our stale session claim")
        else:
            print("[smoke] a stopped guest voids our claim on its session")
    finally:
        SAMPLE[0]["status"] = "running"
        window.refresh()
        pump(0.5)
    window.close_console(occupied_key)
    window._recent_spice.pop(occupied_key, None)
    pump(0.3)

    # A monitor that refuses is not evidence of an empty console.
    FakeAPI.monitor_available = False
    api.monitor_available = False
    if window._spice_occupancy(occupied_guest) is not None:
        failures.append("a refused monitor call was read as an answer")
    elif window._plan_console(occupied_guest)["protocol"] != "spice":
        failures.append("a refused monitor call blocked the console")
    else:
        print("[smoke] a refused monitor call means unknown, not empty")
    FakeAPI.monitor_available = True
    api.monitor_available = True

    # And the check can be switched off entirely.
    config["spice_session_check"] = False
    FakeAPI.SPICE_CLIENTS = {100: 2}
    if window._spice_occupancy(occupied_guest) is not None:
        failures.append("the occupancy check ran while switched off")
    else:
        print("[smoke] the occupancy check honours its preference")
    config["spice_session_check"] = True

    # The choice has to reach the tab when nothing was clicked.
    window.open_console(occupied_key, automatic=True)
    deadline = time.time() + 8
    while time.time() < deadline:
        pump(0.3)
        held = window.consoles.get(occupied_key)
        if getattr(held, "last_status", "") == "choice":
            break
    held = window.consoles.get(occupied_key)
    if getattr(held, "last_status", "") != "choice":
        failures.append(
            f"an automatic open did not put the choice on the tab "
            f"(tab shows {getattr(held, 'last_status', None)!r})"
        )
    elif len(held.status_panel._extra) != 2:
        failures.append("the occupied tab does not offer both ways out")
    elif held.status_panel.reconnect_button.get_visible():
        failures.append("the occupied tab still offers a plain Reconnect")
    else:
        print("[smoke] an automatic open puts Take Over / VNC on the tab")

    # Choosing VNC leaves the other client alone. This fake has no
    # vnc_ticket, so the open fails after planning -- which is fine, the
    # point is that planning never went near spiceproxy.
    spice_calls = len([c for c in api.calls if c[0] == "spice"])
    held.status_panel._extra[1].clicked()
    deadline = time.time() + 6
    while time.time() < deadline:
        pump(0.3)
        if occupied_key in window._force_vnc and not window._poll_busy:
            break
    if len([c for c in api.calls if c[0] == "spice"]) != spice_calls:
        failures.append("choosing VNC still asked for a SPICE ticket")
    else:
        print("[smoke] choosing VNC does not touch the other client's session")
    window.close_console(occupied_key)
    FakeAPI.SPICE_CLIENTS = {}
    pump(0.3)

    # -- pane toggles ---------------------------------------------------
    # Both toolbar buttons have to close what they opened, and stay in step
    # with the pane when it is closed some other way.
    if not window.tree_tool_item.get_active() or not window.sidebar.get_visible():
        failures.append("the tree starts hidden")
    window.tree_tool_item.set_active(False)
    pump(0.3)
    if window.sidebar.get_visible():
        failures.append("the tree toggle did not hide the sidebar")
    elif config.get("sidebar_visible") is not False:
        failures.append("hiding the tree was not remembered")
    else:
        window.tree_tool_item.set_active(True)
        pump(0.3)
        if not window.sidebar.get_visible():
            failures.append("the tree toggle did not bring the sidebar back")
        else:
            print("[smoke] the tree toggle opens and closes the sidebar")

    window.tasks_tool_item.set_active(True)
    pump(0.5)
    if not window.task_feed.get_visible():
        failures.append("the tasks toggle did not open the pane")
    else:
        window.tasks_tool_item.set_active(False)
        pump(0.3)
        if window.task_feed.get_visible():
            failures.append("the tasks toggle did not close the pane")
        else:
            print("[smoke] the tasks toggle opens and closes the task pane")

    window.tasks_tool_item.set_active(True)
    pump(0.4)
    window.task_feed.close()  # the pane's own X
    pump(0.3)
    if window.tasks_tool_item.get_active():
        failures.append("closing the pane left its toolbar button pressed in")
    else:
        print("[smoke] closing the task pane releases its toolbar button")

    # -- drag and drop switch -------------------------------------------
    if not window.sidebar.dnd_enabled:
        failures.append("drag and drop starts disabled")
    window._toggle_dnd()
    pump(0.2)
    if window.sidebar.dnd_enabled:
        failures.append("the drag and drop switch did not reach the sidebar")
    elif config.get("enable_dnd") is not False:
        failures.append("the drag and drop switch was not saved")
    elif not window.dnd_icon.struck:
        failures.append("the drag and drop icon is not struck through when off")
    else:
        # Unset rather than refused later: with no drag source the tree
        # cannot start a drag at all, which is the point of the switch.
        targets = window.sidebar.view.drag_dest_get_target_list()
        if (
            targets is not None
            and targets.find(Gdk.Atom.intern("proxima/guest", False))[0]
        ):
            failures.append("the tree still accepts guest drops with dnd off")
        else:
            print("[smoke] drag and drop switch disarms the tree and strikes its icon")
    window._toggle_dnd()
    pump(0.2)
    if not window.sidebar.dnd_enabled or window.dnd_icon.struck:
        failures.append("drag and drop did not switch back on")

    # -- per-VM settings -------------------------------------------------
    from proxima.ui.vm_settings import RESPONSE_APPLY, VMSettingsDialog

    settings_key = f"{CONN_ID}/pve-node-01/qemu/100"
    settings_guest = window.sidebar.guests[settings_key]
    FakeAPI.NOTES = {100: "Handwritten notes about this VM."}
    settings_guest.settings_loaded = False
    settings_guest.config_loaded = False

    entry = None
    menu = Gtk.Menu()
    window.sidebar._build_single_menu(menu, settings_guest)
    labels = [
        c.get_label()
        for c in menu.get_children()
        if isinstance(c, Gtk.MenuItem) and c.get_label()
    ]
    if not labels or labels[-1] != "Settings":
        failures.append(f"Settings is not the last context menu entry: {labels}")
    else:
        print("[smoke] Settings sits at the bottom of a VM's context menu")

    opened = []
    real_show = window._show_guest_settings
    window._show_guest_settings = lambda g, a: opened.append((g, a))
    window.open_guest_settings(settings_key)
    deadline = time.time() + 6
    while time.time() < deadline and not opened:
        pump(0.2)
    window._show_guest_settings = real_show
    if not opened:
        failures.append("Settings never opened for a guest with no config read")
    else:
        dialog = VMSettingsDialog(
            window,
            api,
            settings_guest,
            on_saved=lambda s: window._guest_settings_saved(settings_key, s),
        )
        pump(0.3)
        tabs = [
            dialog.get_content_area().get_children()[0].get_tab_label_text(c)
            for c in dialog.get_content_area().get_children()[0].get_children()
        ]
        if tabs != ["Hardware", "Options", "Proxmox Manager"]:
            failures.append(f"settings tabs are {tabs}")
        elif dialog.apply_button.get_sensitive():
            failures.append("Apply is offered before anything has changed")
        else:
            print(f"[smoke] VM settings tabs: {', '.join(tabs)}")

        dialog.values["protocol"] = "vnc"
        dialog.values["audio"] = "disabled"
        dialog._sync_buttons()
        if not dialog.apply_button.get_sensitive():
            failures.append("Apply stayed disabled after a change")
        dialog.emit("response", RESPONSE_APPLY)
        deadline = time.time() + 6
        while time.time() < deadline and dialog._saving:
            pump(0.2)
        pump(0.3)

        written = FakeAPI.NOTES.get(100, "")
        if "Handwritten notes" not in written:
            failures.append(f"saving settings damaged the notes: {written!r}")
        elif notes_mod.settings_of(written)["protocol"] != "vnc":
            failures.append(f"protocol not stored: {notes_mod.settings_of(written)}")
        elif settings_guest.settings.get("audio") != "disabled":
            failures.append("the guest did not take the saved settings")
        elif dialog.apply_button.get_sensitive():
            failures.append("Apply stayed live after a successful save")
        else:
            print("[smoke] VM settings saved into the notes, user text intact")
        dialog.destroy()
        pump(0.2)

    # The stored protocol has to steer the next console, and the switches
    # have to follow the stored clipboard and audio values.
    window.notebook.set_current_page(0)
    pump(0.3)
    if plan_protocol(settings_key) != "vnc":
        failures.append("a VM set to VNC only still planned SPICE")
    elif window._guest_switch(settings_guest, "audio") is not False:
        failures.append("the stored audio setting did not reach the switch")
    else:
        print("[smoke] stored settings steer the protocol and the switches")

    # Reopen with SPICE is a temporary helper and must overrule the setting.
    window._force_spice.add(settings_key)
    if plan_protocol(settings_key) != "spice":
        failures.append("Reopen with SPICE could not overrule the VM setting")
    else:
        print("[smoke] Reopen with SPICE overrules the stored protocol")
    window._clear_session_choices(settings_key)

    # -- hardware and options -------------------------------------------
    from proxima.api import devices as dev_mod

    FakeAPI.HARDWARE = {}
    hw_guest = window.sidebar.guests[f"{CONN_ID}/pve-node-01/qemu/102"]
    hw_guest.config = api.guest_config(hw_guest.node, hw_guest.vmid)
    hw = VMSettingsDialog(window, api, hw_guest)
    pump(0.4)

    if hw.running:
        failures.append("the stopped guest was treated as running")
    elif not hw.nets or hw.nets[0]["slot"] != "net0":
        failures.append(f"network devices were not read: {hw.nets}")
    elif hw.dirty:
        failures.append("a freshly opened settings dialog is already dirty")
    else:
        print("[smoke] hardware page read the config with no phantom edits")

    # Editing the parts of a NIC the dialog shows must not disturb the
    # parts it does not -- the rate limit here.
    hw._on_net_bridge(_FakeEditable("vmbr1"), hw.nets[0])
    hw._on_net_vlan(_FakeEditable("42"), hw.nets[0])
    hw.nets[0]["pairs"] = dev_mod.set_pair(hw.nets[0]["pairs"], "firewall", "0")
    changes, deletes = hw._config_edits()
    rendered = changes.get("net0", "")
    if "rate=10" not in rendered:
        failures.append(f"editing a NIC dropped its other settings: {rendered}")
    elif "bridge=vmbr1" not in rendered or "tag=42" not in rendered:
        failures.append(f"NIC edits did not take: {rendered}")
    elif "BC:24:11:00:00:01" not in rendered:
        failures.append(f"editing a NIC lost its MAC: {rendered}")
    else:
        print(f"[smoke] NIC edit keeps everything else: {rendered}")

    # Adding and removing devices.
    hw._add_net()
    added = [e for e in hw.nets if e["new"]]
    if len(added) != 1 or added[0]["slot"] != "net1":
        failures.append(f"adding a NIC picked the wrong slot: {hw.nets}")
    changes, deletes = hw._config_edits()
    if "net1" not in changes:
        failures.append("the added NIC was not in the changes")
    hw._remove_net(added[0])
    hw._remove_net(hw.nets[0])
    changes, deletes = hw._config_edits()
    if deletes != ["net0"]:
        failures.append(f"removing a NIC did not delete it: {deletes}")
    elif "net0" in changes:
        failures.append("a removed NIC was both written and deleted")
    else:
        print("[smoke] NICs can be added and removed")

    # Saving sends the digest, so a VM changed underneath is refused rather
    # than silently overwritten.
    hw.emit("response", RESPONSE_APPLY)
    deadline = time.time() + 6
    while time.time() < deadline and hw._saving:
        pump(0.2)
    pump(0.3)
    written = [c for c in api.calls if c[0] == "set-config"]
    if not written:
        failures.append("applying hardware changes wrote nothing")
    elif written[-1][4] != "digest-102":
        failures.append(f"the config digest was not sent: {written[-1]}")
    elif "net0" not in written[-1][3]:
        failures.append(f"the removed NIC was not deleted: {written[-1]}")
    else:
        print(f"[smoke] hardware saved with a digest: {written[-1][4]}")
    hw.destroy()
    pump(0.3)

    # A running VM must not offer the fields Proxmox would park as pending.
    run_guest = window.sidebar.guests[f"{CONN_ID}/pve-node-01/qemu/100"]
    run_guest.config = api.guest_config(run_guest.node, run_guest.vmid)
    live = VMSettingsDialog(window, api, run_guest)
    pump(0.4)
    if not live.running:
        failures.append("the running guest was treated as stopped")
    else:
        gated = {}

        def collect(widget):
            if isinstance(widget, Gtk.Container):
                for child in widget.get_children():
                    collect(child)
            tip = widget.get_tooltip_text() or ""
            if "Stop the VM to change" in tip:
                gated[widget] = widget.get_sensitive()

        collect(live.get_content_area())
        if not gated:
            failures.append("nothing was gated on a running VM")
        elif any(gated.values()):
            failures.append("a stopped-only field was editable while running")
        else:
            print(
                f"[smoke] {len(gated)} stopped-only fields disabled while the VM runs"
            )
    live.destroy()
    pump(0.3)
    FakeAPI.HARDWARE = {}

    # Defaults leave no block behind at all.
    dialog = VMSettingsDialog(window, api, settings_guest)
    dialog.values.update(notes_mod.SETTINGS_DEFAULTS)
    dialog.emit("response", Gtk.ResponseType.OK)
    deadline = time.time() + 6
    while time.time() < deadline and dialog._saving:
        pump(0.2)
    pump(0.3)
    if "settings" in notes_mod.parse(FakeAPI.NOTES.get(100, ""))[0]:
        failures.append("resetting to the defaults left a settings block")
    else:
        print("[smoke] settings reset to the defaults leave the notes clean")
    FakeAPI.NOTES = {}
    settings_guest.settings = {}

    # -- settings dialog -----------------------------------------------
    settings = SettingsDialog(window, config, on_change=window.apply_appearance)
    pump(0.3)
    settings.destroy()
    print("[smoke] settings dialog built")

    # -- in-application titlebar ----------------------------------------
    # Off by default, and it has to build a real window when switched on.
    # See docs/header-bar.md.
    if window.header_bar is not None:
        failures.append("the header bar is on without being asked for")
    header_config = Config(dict(config))
    header_config["use_header_bar"] = True
    header_window = build_window(FakeAPI(), header_config)
    pump(0.6)
    if header_window.header_bar is None:
        failures.append("the header bar setting did not take")
    elif header_window.get_titlebar() is not header_window.header_bar:
        failures.append("the header bar is not the window's titlebar")
    else:
        header_window._update_connection_label()
        subtitle = header_window.header_bar.get_subtitle() or ""
        if not subtitle:
            failures.append("the header bar shows no connection summary")
        else:
            print(f"[smoke] in-application titlebar builds: {subtitle!r}")

        # The menus move into the titlebar; they must be there exactly once,
        # and must not also be left in the window body.
        def descends_from(widget, ancestor):
            while widget is not None:
                if widget is ancestor:
                    return True
                widget = widget.get_parent()
            return False

        menubar = header_window.menubar
        if not descends_from(menubar, header_window.header_bar):
            failures.append("the menu bar did not move into the titlebar")
        elif descends_from(menubar, header_window.get_child()):
            failures.append("the menu bar is in the titlebar AND the window")
        else:
            labels = [c.get_label() for c in menubar.get_children()]
            print(f"[smoke] menus live in the titlebar: {', '.join(labels)}")

        # ...and the window controls have to survive sharing the bar with
        # them. An unset decoration layout is how they vanished before.
        if not header_window.header_bar.get_show_close_button():
            failures.append("the titlebar has no window controls")
        elif "close" not in (header_window.header_bar.get_decoration_layout() or ""):
            failures.append(
                "the titlebar's decoration layout has no close button: "
                f"{header_window.header_bar.get_decoration_layout()!r}"
            )
        else:
            print("[smoke] the titlebar keeps its window controls")

    # With the header bar off, the menus stay in the window body.
    if descends_from(window.menubar, window.get_child()) is False:
        failures.append("without a header bar the menu bar left the window")
    else:
        print("[smoke] without a header bar the menus stay where they were")

    header_window.shutdown()
    header_window.destroy()
    pump(0.3)

    # -- theme permutations --------------------------------------------
    for theme in ("Adwaita", "Fluent"):
        for mode in ("light", "dark", "system"):
            config["theme"] = theme
            config["color_mode"] = mode
            window.apply_appearance()
            pump(0.1)
    print("[smoke] all theme/colour permutations applied")

    for antialias in ("grayscale", "subpixel", "none", "default"):
        for hint in ("slight", "full", "medium", "none"):
            config["antialias"] = antialias
            config["hint_style"] = hint
            window.apply_appearance()
            pump(0.05)
    print("[smoke] all antialias/hinting permutations applied")

    # -- window state ------------------------------------------------------
    class _StateEvent:
        def __init__(self, state):
            self.new_window_state = state

    # An ordinary window records its size as it is resized.
    window._maximized = False
    window._fullscreen_state = False
    window._normal_size = (1111, 777)
    window._on_configure()
    resized = window._normal_size

    # Maximising must not overwrite the size to return to.
    window._on_window_state(window, _StateEvent(Gdk.WindowState.MAXIMIZED))
    window._on_configure()
    if not window._maximized:
        failures.append("maximising was not noticed")
    elif window._normal_size != resized:
        failures.append(f"maximising overwrote the restore size: {window._normal_size}")
    else:
        print(f"[smoke] maximised window keeps its restore size {resized}")

    # Fullscreen is a console mode, never a saved window preference.
    window._on_window_state(window, _StateEvent(Gdk.WindowState.FULLSCREEN))
    window._on_configure()
    if not window._maximized:
        failures.append("fullscreen cleared the maximised flag")
    elif window._normal_size != resized:
        failures.append("fullscreen overwrote the restore size")
    else:
        print("[smoke] fullscreen leaves the saved window state alone")

    window._on_window_state(window, _StateEvent(Gdk.WindowState.MAXIMIZED))
    window._save_layout()
    if not config["window_maximized"]:
        failures.append("maximised state was not saved")
    elif (config["window_width"], config["window_height"]) != resized:
        failures.append(
            f"saved size is {config['window_width']}x"
            f"{config['window_height']}, expected {resized}"
        )
    else:
        print(
            f"[smoke] saved maximised, restoring to "
            f"{config['window_width']}x{config['window_height']}"
        )

    # And an unmaximised window saves the size it actually has.
    window._on_window_state(window, _StateEvent(0))
    window._normal_size = (900, 640)
    window._save_layout()
    if config["window_maximized"]:
        failures.append("unmaximising did not clear the saved flag")
    elif (config["window_width"], config["window_height"]) != (900, 640):
        failures.append("unmaximised size was not saved")
    else:
        print("[smoke] unmaximised window saves its own size")

    window.shutdown()
    window.destroy()
    pump(0.2)

    print()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

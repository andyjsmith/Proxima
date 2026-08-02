"""Plain data holders for what /cluster/resources and /nodes return."""

import re
from dataclasses import dataclass, field


# Both halves of a guest's identity are worth showing -- the name says what
# it is, the VMID is what every Proxmox error message and CLI command uses.
# The setting only chooses which one leads.
NAME_STYLES = ("name", "id", "both")


def format_guest_name(guest, style="name"):
    """One guest, written the way the chosen style asks for.

    "both" exists for tab titles, where it means the same as "name": the
    name first with the VMID after it. Tabs get the third choice because
    "just the name" is a reasonable thing to want there and is not here --
    a tree of guests with no VMIDs is hard to match against Proxmox itself.
    """
    if style == "id":
        return f"{guest.vmid} ({guest.name})"
    return f"{guest.name} ({guest.vmid})"


# Proxmox validates a QEMU 'name' and an LXC 'hostname' as a DNS name: dot
# separated labels of letters, digits and hyphens, where no label starts or
# ends with a hyphen. Rejecting a bad name here saves a round trip that comes
# back as an opaque "parameter verification failed".
GUEST_NAME_CHARS = re.compile(r"[A-Za-z0-9.-]")
_DNS_LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
GUEST_NAME_RE = re.compile(rf"^{_DNS_LABEL}(\.{_DNS_LABEL})*$")


def valid_guest_name(name):
    """True when Proxmox will accept this as a guest name."""
    name = (name or "").strip()
    return bool(name) and len(name) <= 253 and bool(GUEST_NAME_RE.match(name))


def _human_bytes(value):
    if not value:
        return "-"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def human_age(timestamp, now=None):
    """A rough 'how long ago', for tooltips.

    Deliberately coarse: the exact second of a snapshot is never what you
    are asking when you hover a Revert button.
    """
    if not timestamp:
        return ""
    import time as _time
    seconds = int((now if now is not None else _time.time()) - int(timestamp))
    if seconds < 0:
        return "in the future"
    if seconds < 60:
        return "just now"
    for limit, size, name in ((3600, 60, "minute"),
                              (86400, 3600, "hour"),
                              (604800, 86400, "day"),
                              (2592000, 604800, "week"),
                              (31536000, 2592000, "month")):
        if seconds < limit:
            count = seconds // size
            return f"{count} {name}{'s' if count != 1 else ''} ago"
    count = seconds // 31536000
    return f"{count} year{'s' if count != 1 else ''} ago"


def _human_uptime(seconds):
    if not seconds:
        return "-"
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, _ = divmod(rest, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}"
    return f"{hours:02d}:{minutes:02d}"


@dataclass
class Node:
    name: str
    status: str = "unknown"
    uptime: int = 0
    cpu: float = 0.0
    maxcpu: int = 0
    mem: int = 0
    maxmem: int = 0

    @classmethod
    def from_api(cls, row):
        return cls(
            name=row.get("node") or row.get("name") or "?",
            status=row.get("status", "unknown"),
            uptime=int(row.get("uptime") or 0),
            cpu=float(row.get("cpu") or 0.0),
            maxcpu=int(row.get("maxcpu") or 0),
            mem=int(row.get("mem") or 0),
            maxmem=int(row.get("maxmem") or 0),
        )

    @property
    def uptime_text(self):
        return _human_uptime(self.uptime)


@dataclass
class Guest:
    vmid: int
    name: str
    node: str
    kind: str = "qemu"           # "qemu" or "lxc"
    status: str = "unknown"
    template: bool = False
    uptime: int = 0
    cpu: float = 0.0
    maxcpu: int = 0
    mem: int = 0
    maxmem: int = 0
    maxdisk: int = 0
    tags: str = ""
    lock: str = ""
    # Which server this guest came from; part of the key, so two clusters
    # using the same VMIDs stay distinct.
    connection: str = ""
    # Filled in lazily from the guest config once it is inspected.
    config: dict = field(default_factory=dict)
    display: str = ""            # the 'vga' setting, when known
    # None here means "unknown display type, worth trying SPICE" -- the same
    # meaning vga_is_spice() gives it. Whether the config has been read at
    # all is config_loaded's job; conflating the two made an unrecognised
    # adapter look permanently undetermined.
    spice_capable: bool = None
    config_loaded: bool = False
    # Proxmox's own delete guard, from the config's 'protection' flag. Only
    # known once the config has been read, so None means "not looked yet"
    # rather than "not protected".
    protected: bool = None
    console_note: str = ""       # why the last console chose what it did
    # Newest snapshot, or None. snapshots_loaded distinguishes "no snapshots"
    # from "not looked yet", which the Revert button has to tell apart.
    latest_snapshot: dict = None
    snapshots_loaded: bool = False
    # Folder path from the guest's notes, e.g. ["Production", "Customer A"].
    folder: tuple = ()
    notes_loaded: bool = False
    # Proxmox Manager settings, also from the notes. Empty until the config
    # (which carries the description) has been read; settings_loaded is what
    # tells "nothing set" apart from "not looked yet".
    settings: dict = field(default_factory=dict)
    settings_loaded: bool = False

    @classmethod
    def from_api(cls, row):
        return cls(
            vmid=int(row.get("vmid") or 0),
            name=row.get("name") or f"VM {row.get('vmid')}",
            node=row.get("node") or "?",
            kind=row.get("type") or "qemu",
            status=row.get("status") or "unknown",
            template=bool(row.get("template")),
            uptime=int(row.get("uptime") or 0),
            cpu=float(row.get("cpu") or 0.0),
            maxcpu=int(row.get("maxcpu") or 0),
            mem=int(row.get("mem") or 0),
            maxmem=int(row.get("maxmem") or 0),
            maxdisk=int(row.get("maxdisk") or 0),
            tags=row.get("tags") or "",
            lock=row.get("lock") or "",
        )

    # -- identity ------------------------------------------------------

    @property
    def key(self):
        return f"{self.connection}/{self.node}/{self.kind}/{self.vmid}"

    @property
    def label(self):
        return format_guest_name(self, "name")

    def display_name(self, style="name"):
        """How this guest should be written in the tree, tabs and messages."""
        return format_guest_name(self, style)

    @property
    def running(self):
        return self.status == "running"

    @property
    def is_container(self):
        return self.kind == "lxc"

    # -- display -------------------------------------------------------

    @property
    def uptime_text(self):
        return _human_uptime(self.uptime) if self.running else "-"

    @property
    def memory_text(self):
        if not self.maxmem:
            return "-"
        if not self.running:
            return _human_bytes(self.maxmem)
        return f"{_human_bytes(self.mem)} / {_human_bytes(self.maxmem)}"

    @property
    def cpu_text(self):
        if not self.running:
            return "-"
        return f"{self.cpu * 100:.0f}% of {self.maxcpu}"

    @property
    def disk_text(self):
        return _human_bytes(self.maxdisk)

    def merge_live(self, other):
        """Copy volatile fields from a freshly polled copy, keeping config."""
        for attr in ("name", "status", "uptime", "cpu", "maxcpu", "mem",
                     "maxmem", "maxdisk", "tags", "lock", "template", "node",
                     "connection"):
            setattr(self, attr, getattr(other, attr))


# QEMU display adapters Proxmox will serve over SPICE. Both families count:
# the qxl ones (qxl, qxl2, qxl3, qxl4) and the virtio ones, which includes
# plain 'virtio' -- what the Proxmox UI calls VirtIO-GPU -- as well as
# 'virtio-gl' (VirGL). Getting this wrong is invisible: the guest simply
# opens on VNC and nothing says why.
SPICE_VGA_PREFIXES = ("qxl", "virtio")

# Adapters that definitively have no SPICE display.
NON_SPICE_VGA_PREFIXES = ("std", "cirrus", "vmware", "none", "serial")


def audio_is_spice(audio_value):
    """Whether an 'audio0' config line routes sound over SPICE.

    Proxmox adds no audio device to a VM by default, so a SPICE console with
    no sound is almost always this rather than a client-side fault. The line
    looks like 'device=ich9-intel-hda,driver=spice'.
    """
    if not audio_value:
        return False
    return "driver=spice" in str(audio_value).replace(" ", "").lower()


def parse_spice_clients(text):
    """Read QEMU's 'info spice' output.

    Returns (client count, addresses), or None when the text does not look
    like 'info spice' output at all.

    The output is two sections and only the second one counts:

        Server:
             address: 0.0.0.0:61000
                auth: spice
        Channels: none

    A connected client contributes several channels -- display, inputs,
    cursor and so on -- each with its own line and its own source port, all
    sharing one session-id. Counting sessions is therefore the only way to
    get "how many people", and counting addresses would report one viewer as
    five.

    Output with no Channels section returns None -- "cannot tell" -- and
    never zero. The difference decides whether somebody gets thrown off
    their session: a reply this function does not recognise is a reason to
    ask the user, not a licence to assume the console is free. Only QEMU
    actually saying "Channels: none" counts as empty.
    """
    lines = (text or "").splitlines()
    sessions = []
    addresses = []
    in_channels = False
    seen_channels = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if lowered.startswith("channels:"):
            seen_channels = True
            # "Channels: none" settles it on the same line.
            remainder = stripped.split(":", 1)[1].strip().lower()
            if remainder in ("none", "no", "0"):
                return 0, []
            in_channels = True
            continue
        if lowered.endswith(":") and ":" not in lowered[:-1]:
            # Another section header, e.g. "Server:". Ends the channel list.
            in_channels = False
            continue
        if not in_channels:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "session-id":
            if value not in sessions:
                sessions.append(value)
        elif key == "address":
            addresses.append(value)

    if sessions:
        return len(sessions), addresses
    if addresses:
        # Channels with no session-id at all: an older QEMU, or a format
        # change. One client is the smallest claim consistent with what is
        # there.
        return 1, addresses
    if seen_channels:
        return 0, []
    return None                          # not 'info spice' output at all


def vga_is_spice(vga_value):
    """Whether a 'vga' config string implies a SPICE-capable display.

    Three-valued on purpose:

        True   -- a known SPICE adapter
        False  -- a known VNC-only adapter, or unset (Proxmox defaults to std)
        None   -- an adapter this client has never heard of

    None means "ask the server", not "give up". Proxmox is the authority on
    what it will serve, and it may grow display types after this code was
    written; a wrong guess in the optimistic direction costs one failed API
    call and still lands on a working VNC console, whereas a wrong guess in
    the pessimistic direction silently downgrades the guest forever.

    The value looks like 'qxl2,memory=32' or plain 'std', so only the type
    before the first comma matters.
    """
    if not vga_value:
        return False                     # unset means std -> VNC only
    kind = str(vga_value).split(",")[0].strip().lower()
    if any(kind.startswith(prefix) for prefix in SPICE_VGA_PREFIXES):
        return True
    if any(kind.startswith(prefix) for prefix in NON_SPICE_VGA_PREFIXES):
        return False
    return None

"""Reading and writing Proxmox's comma-separated device strings.

A guest config line looks like this:

    net0: virtio=BC:24:11:0A:1B:2C,bridge=vmbr0,tag=100,firewall=1
    vga:  qxl,memory=32

The rule that matters when editing one is that anything not understood must
survive. A NIC may carry rate limits, queues, an MTU or link_down; a display
may carry a memory size. Rewriting the line from only the fields shown in a
dialog would silently drop the rest, which is a very quiet way to change
somebody's traffic shaping. So a line is parsed into its parts in order,
individual parts are replaced, and the remainder is rendered back untouched.
"""

import random
import re

# Values that appear on their own, without a key, are the device model. The
# shorthand "virtio=<mac>" means model virtio with that MAC address.
NIC_MODELS = (
    "virtio",
    "e1000",
    "e1000e",
    "rtl8139",
    "vmxnet3",
    "pcnet",
    "i82551",
    "i82557b",
    "i82559er",
    "ne2k_isa",
    "ne2k_pci",
)

# PVE 8 hands out addresses from this prefix. Matching it keeps a NIC added
# here indistinguishable from one added in the web UI.
MAC_PREFIX = (0xBC, 0x24, 0x11)

# What Proxmox hot-plugs unless the guest's own 'hotplug' line says
# otherwise. Network is in the default set, which is why a bridge or VLAN
# change can be applied to a running guest and a memory change cannot.
DEFAULT_HOTPLUG = ("disk", "network", "usb")


def parse_pairs(value):
    """A device string as an ordered list of (key, value).

    A part with no "=" keeps None as its key, so the positional model token
    in "qxl,memory=32" round-trips as itself.
    """
    pairs = []
    for part in str(value or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            key, _, val = part.partition("=")
            pairs.append((key.strip(), val.strip()))
        else:
            pairs.append((None, part))
    return pairs


def render_pairs(pairs):
    return ",".join(part if key is None else f"{key}={part}" for key, part in pairs)


def get_pair(pairs, key, default=None):
    for name, value in pairs:
        if name == key:
            return value
    return default


def set_pair(pairs, key, value):
    """Set, replace or remove one field, keeping the order of the rest.

    Returns a new list. A value of None removes the field; a new field is
    appended, which is where Proxmox puts them too.
    """
    result = []
    replaced = False
    for name, existing in pairs:
        if name != key:
            result.append((name, existing))
            continue
        if value is not None and not replaced:
            result.append((key, value))
            replaced = True
        # Otherwise drop it: removing, or a duplicate of one already set.
    if value is not None and not replaced:
        result.append((key, value))
    return result


# -- network interfaces -------------------------------------------------


def nic_model(pairs):
    """(model, mac) for a NIC line, either spelling.

    Proxmox writes "virtio=<mac>", but "model=virtio" with a separate
    "macaddr=" is equally valid and is what an API client may have written.
    """
    for key, value in pairs:
        if key in NIC_MODELS:
            return key, value
        if key is None and value in NIC_MODELS:
            return value, get_pair(pairs, "macaddr", "")
        if key == "model":
            return value, get_pair(pairs, "macaddr", "")
    return "", ""


MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def valid_mac(text):
    return bool(MAC_RE.match((text or "").strip()))


def set_nic_mac(pairs, mac):
    """Replace a NIC's MAC, whichever spelling the line uses.

    "virtio=<mac>" carries the address in the model's own value, while
    "model=virtio" keeps it in a separate macaddr field. Both are valid and
    both turn up, so both are handled rather than normalised -- rewriting
    somebody's line into the other spelling is a gratuitous change.
    """
    model, _ = nic_model(pairs)
    for index, (key, value) in enumerate(pairs):
        if key == model and key in NIC_MODELS:
            return pairs[:index] + [(key, mac)] + pairs[index + 1 :]
    return set_pair(pairs, "macaddr", mac)


def random_mac():
    return ":".join(
        f"{part:02X}"
        for part in MAC_PREFIX + tuple(random.randint(0, 255) for _ in range(3))
    )


def new_nic(bridge="vmbr0", model="virtio", firewall=True):
    """A NIC line for a device being added.

    The MAC is generated here rather than left to Proxmox: the field is
    optional in the API, but a line with one is unambiguous whichever
    version is on the other end.
    """
    pairs = [(model, random_mac()), ("bridge", bridge)]
    if firewall:
        pairs.append(("firewall", "1"))
    return render_pairs(pairs)


def nic_slots(config):
    """Every netN key in a guest config, in numeric order."""
    slots = []
    for key in config or ():
        if key.startswith("net") and key[3:].isdigit():
            slots.append(key)
    return sorted(slots, key=lambda name: int(name[3:]))


def free_nic_slot(config, taken=()):
    """The lowest netN not in use. Proxmox allows net0 to net31."""
    used = set(nic_slots(config)) | set(taken)
    for index in range(32):
        name = f"net{index}"
        if name not in used:
            return name
    return None


def network_hotplug(config):
    """Whether this guest hot-plugs network changes.

    A running guest whose 'hotplug' line excludes network would take a
    bridge change as a pending change instead of applying it, which is
    precisely the behaviour worth warning about rather than reproducing.
    """
    raw = (config or {}).get("hotplug")
    if raw is None:
        return True  # the default set includes network
    text = str(raw).strip().lower()
    if text in ("0", "", "none"):
        return False
    if text == "1":
        return True  # 1 means the default set
    return "network" in [part.strip() for part in text.split(",")]

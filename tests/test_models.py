"""The rules that decide what a guest can do, checked without a UI."""

import pytest

from proxima.api.models import (
    parse_spice_clients,
    spice_usb_ports,
    valid_guest_name,
    vga_head_limit,
    vga_is_spice,
)

# The vga -> SPICE rule. VirtIO-GPU is plain 'virtio' in the config, which is
# exactly the value that must not be mistaken for a VNC-only adapter.
VGA_CASES = [
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
]


@pytest.mark.parametrize(
    ("value", "expected"),
    [(value, expected) for value, expected, _ in VGA_CASES],
    ids=[note for _, _, note in VGA_CASES],
)
def test_vga_is_spice(value, expected):
    assert vga_is_spice(value) is expected


# How many displays a client may ask an adapter for. Plain 'qxl' is already
# four: the qxl2/3/4 spellings add QXL devices, not the ability to have a
# second monitor at all.
HEAD_CASES = [
    ("qxl", 4, "QXL"),
    ("qxl2", 4, "QXL with two devices"),
    ("qxl,memory=64", 4, "QXL with options"),
    (" QXL ", 4, "case and whitespace tolerated"),
    ("virtio", 1, "VirtIO-GPU is one head"),
    ("virtio-gl", 1, "VirGL is one head"),
    ("std", 1, "std"),
    ("", 1, "unset"),
    (None, 1, "missing"),
]


@pytest.mark.parametrize(
    ("value", "expected"),
    [(value, expected) for value, expected, _ in HEAD_CASES],
    ids=[note for _, _, note in HEAD_CASES],
)
def test_vga_head_limit(value, expected):
    assert vga_head_limit(value) == expected


# Which 'usbN' lines are redirection ports. A passthrough entry shares the
# key and is not one: the hypervisor already owns that device.
USB_CASES = [
    ({"usb0": "spice"}, ["usb0"], "a redirection port"),
    ({"usb2": " SPICE "}, ["usb2"], "case and whitespace tolerated"),
    ({"usb0": "host=1234:5678"}, [], "passthrough is not redirection"),
    ({"usb0": "host=1-4,usb3=1"}, [], "passthrough by port"),
    ({"usb0": "spice", "usb1": "spice"}, ["usb0", "usb1"], "two ports"),
    ({"usbx": "spice"}, [], "not a numbered usb key"),
    ({"vga": "qxl", "net0": "virtio"}, [], "nothing to do with USB"),
    ({}, [], "empty config"),
    (None, [], "no config at all"),
]


@pytest.mark.parametrize(
    ("config", "expected"),
    [(config, expected) for config, expected, _ in USB_CASES],
    ids=[note for _, _, note in USB_CASES],
)
def test_spice_usb_ports(config, expected):
    assert spice_usb_ports(config) == expected


@pytest.mark.parametrize("name", ["web 01", "web/01", "-web01", "web01-", ""])
def test_invalid_guest_names_are_rejected(name):
    assert not valid_guest_name(name)


@pytest.mark.parametrize("name", ["web01", "web-01", "a.b.c", "1"])
def test_valid_guest_names_are_accepted(name):
    assert valid_guest_name(name)


# Verbatim from Proxmox with QEMU/SPICE 0.15.2. This exact text was once read
# as "nobody is connected", which silently threw people off their consoles, so
# it is pinned here rather than paraphrased.
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


@pytest.mark.parametrize(
    ("text", "clients", "channels"),
    [
        pytest.param(REAL_BUSY, 1, 4, id="one viewer, four channels"),
        pytest.param(REAL_IDLE, 0, 0, id="idle server"),
        # Two viewers differ only by their session number.
        pytest.param(
            REAL_BUSY + REAL_BUSY.replace("1938609120", "999999"),
            2,
            8,
            id="two viewers",
        ),
    ],
)
def test_parse_real_info_spice(text, clients, channels):
    parsed = parse_spice_clients(text)
    assert parsed is not None, "real 'info spice' output was not understood"
    assert parsed[0] == clients
    assert len(parsed[1]) == channels


def test_tls_flag_is_not_kept_in_the_address():
    _, addresses = parse_spice_clients(REAL_BUSY)
    assert not any(a.endswith("[tls]") for a in addresses)


@pytest.mark.parametrize(
    "text",
    [pytest.param("", id="empty"), "who knows", "Permission denied"],
)
def test_unrecognised_replies_mean_cannot_tell(text):
    # Never 'empty': "the monitor would not answer" is not evidence that
    # nobody is on the console.
    assert parse_spice_clients(text) is None

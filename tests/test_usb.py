"""USB redirection: the indicator, the VM menu, and what stands in its way.

Nothing here touches a real USB device. What is worth pinning down is the
wiring around spice-gtk -- which console the controls act on, and that each
"no" says which of the four different reasons it is.
"""

from gi.repository import Gtk

from proxima.console.usb import UsbDevice, UsbRedirection

from .conftest import key_for, pump, pump_until

RUNNING = key_for(100)  # qxl, so it opens on SPICE
VNC_ONLY = key_for(101)  # std, so it opens on VNC


class FakeUsb:
    """A UsbRedirection with a device list decided by the test."""

    def __init__(self, devices=(), channels=1, note="", advice=""):
        self.devices_list = list(devices)
        self.channels = channels
        self.note = note
        self.advice = advice
        self.toggled = []

    def snapshot(self):
        return list(self.devices_list), self.channels

    def devices(self):
        return list(self.devices_list)

    def is_busy(self, _key):
        return False

    def toggle(self, key, on_done=None):
        self.toggled.append(key)
        for device in self.devices_list:
            if device.key == key:
                device.connected = not device.connected
        if on_done:
            on_done(True, "")


class UsbConsoleStub(Gtk.Box):
    """A SPICE console whose USB side is entirely made up."""

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
        "usb": True,
    }
    connected = True

    def __init__(self, guest_key, usb):
        super().__init__()
        self.title = "usb-stub"
        self.guest_key = guest_key
        self.usb = usb
        self.pack_start(Gtk.Label(label="console"), True, True, 0)

    def usb_devices(self):
        return self.usb.devices()

    def usb_snapshot(self):
        return self.usb.snapshot()

    def usb_channels(self):
        return self.usb.snapshot()[1]

    def usb_note(self):
        return self.usb.note

    def usb_advice(self):
        return self.usb.advice

    def shutdown(self):
        pass


def install(window, console):
    window.consoles[console.guest_key] = console
    window.panes.append(console, Gtk.Label(label="usb"))
    pump(0.4)


def remove(window, console):
    window.close_console(console.guest_key)
    pump(0.3)


# -- the manager itself ------------------------------------------------


def test_a_manager_with_no_session_reports_why_rather_than_raising():
    # There is no SpiceSession to hang it on, which is the same shape as a
    # spice-gtk built without usbredir: it has to say so, not explode.
    redirection = UsbRedirection(None)
    assert not redirection.supported
    assert redirection.note, "an unusable manager gave no reason"
    assert redirection.devices() == []
    assert redirection.channels == 0


def test_acting_on_a_device_that_has_gone_is_reported_not_raised():
    redirection = UsbRedirection(None)
    answers = []
    redirection.connect_device("no such device", lambda ok, msg: answers.append(ok))
    assert answers == [False]


def test_the_device_list_is_read_over_and_over_without_re_enumerating():
    """The registry is the point, and this is the crash it exists to avoid.

    spice_usb_device_manager_get_devices() hands out devices it has not
    really given up ownership of: release one and the manager's own list
    points at freed memory, so the *next* call takes the process down.
    Nothing here may reach for that call again, however innocent the reason.
    """
    from proxima.console import spicelib

    if spicelib.SpiceGLib is None:
        return  # no spice-gtk on this machine; nothing to protect

    session = spicelib.SpiceGLib.Session()
    redirection = UsbRedirection(session)
    calls = []
    if redirection.manager is not None:
        redirection.manager.get_devices = lambda: calls.append(1) or []
    try:
        for _ in range(5):
            redirection.snapshot()
            redirection.devices()
            redirection.redirected()
        assert calls == [], "the device list was enumerated again after seeding"
    finally:
        redirection.shutdown()


# -- the status bar indicator -------------------------------------------


def test_the_indicator_is_dimmed_and_dead_on_a_vnc_console(window):
    window.open_console(VNC_ONLY)
    pump_until(lambda: VNC_ONLY in window.consoles, 6)
    pump(0.5)
    try:
        assert not window.usb_icon.can_toggle, "USB is offered on a VNC console"
        assert "VNC" in window.usb_icon.get_tooltip_text()
    finally:
        remove(window, window.consoles[VNC_ONLY])


def test_a_vm_with_no_spice_usb_port_says_so(window):
    # The Proxmox default: no 'usbN: spice' line, so nothing can be
    # redirected however healthy the client side is.
    console = UsbConsoleStub(RUNNING, FakeUsb(channels=0))
    install(window, console)
    try:
        window._update_usb_indicator(console)
        tooltip = window.usb_icon.get_tooltip_text()
        assert "Spice Port" in tooltip or "port" in tooltip, tooltip
    finally:
        remove(window, console)


def test_the_indicator_lights_when_a_device_is_redirected(window):
    device = UsbDevice("Acme Stick 1234:5678 at 1-2", "Acme Stick", True, True)
    console = UsbConsoleStub(RUNNING, FakeUsb([device]))
    install(window, console)
    try:
        window._update_usb_indicator(console)
        assert window.usb_icon.get_opacity() == window.usb_icon.OPACITY_ON
        assert "Acme Stick" in window.usb_icon.get_tooltip_text()

        device.connected = False
        window._update_usb_indicator(console)
        assert window.usb_icon.get_opacity() < window.usb_icon.OPACITY_ON
        assert "nothing redirected" in window.usb_icon.get_tooltip_text()
    finally:
        remove(window, console)


# -- the VM menu --------------------------------------------------------


def test_the_menu_needs_a_spice_console(window):
    window._rebuild_usb_menu()
    assert not window.usb_menu_item.get_sensitive(), (
        "the USB menu is live with no console open"
    )


def test_the_menu_lists_devices_and_toggling_one_acts_on_it(window):
    devices = [
        UsbDevice("Acme Stick 1234:5678 at 1-2", "Acme Stick", False, True),
        UsbDevice("Locked Hub 9999:0001 at 1-3", "Locked Hub", False, False, "in use"),
    ]
    usb = FakeUsb(devices)
    console = UsbConsoleStub(RUNNING, usb)
    install(window, console)
    try:
        window._rebuild_usb_menu()
        assert window.usb_menu_item.get_sensitive()

        checks = [
            child
            for child in window.usb_menu.get_children()
            if isinstance(child, Gtk.CheckMenuItem)
        ]
        assert len(checks) == 2, "the menu did not list both devices"
        assert checks[0].get_sensitive(), "a redirectable device was greyed out"
        assert not checks[1].get_sensitive(), "a device that refused was offered"

        checks[0].set_active(True)
        pump(0.2)
        assert usb.toggled == [devices[0].key]
    finally:
        remove(window, console)


def test_the_menu_explains_a_guest_with_no_port(window):
    console = UsbConsoleStub(RUNNING, FakeUsb(channels=0))
    install(window, console)
    try:
        window._rebuild_usb_menu()
        labels = [
            child.get_label()
            for child in window.usb_menu.get_children()
            if isinstance(child, Gtk.MenuItem) and child.get_label()
        ]
        assert any("usb0: spice" in text for text in labels), labels
    finally:
        remove(window, console)

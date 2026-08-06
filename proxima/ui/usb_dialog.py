"""Choosing which USB devices the guest gets.

Two dialogs, because there are two moments worth asking about: the list you
open on purpose from the VM menu, and the one that comes to you when
something is plugged in while a console is in front of you. VMware
Workstation asks the second question and it is the one that makes the
feature usable -- nobody wants to go through a menu every time they touch a
USB stick.

Neither dialog is modal. A device can be plugged or pulled while the list is
open, and a prompt that blocked the console it is asking about would be a
poor way to ask.
"""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from ..console.usb import USBDK_RELEASES, usbdk_installed
from ..theme import decorate as theme_decorate

# What to do about a guest with no redirection port. Proxmox writes this
# line itself when you add the device, but naming the line is faster than
# describing where the button is.
NO_PORT_HINT = (
    "This VM has no SPICE USB port, so nothing can be redirected to it. "
    "Add one in Proxmox under Hardware -> Add -> USB Device -> Spice Port "
    "(it writes 'usb0: spice'), then reopen the console."
)


class UsbDeviceDialog(Gtk.Dialog):
    """The host's USB devices, with a switch each.

    Holds no device state of its own: every row is rebuilt from the console
    whenever anything moves, so a device pulled out of the machine leaves
    the list rather than lingering as a switch that does nothing.
    """

    def __init__(self, parent, console, guest_label, on_status=None):
        super().__init__(title=f"USB Devices - {guest_label}", transient_for=parent)
        self.console = console
        self.on_status = on_status or (lambda text: None)
        self._rows_by_key = {}
        self._updating = False

        self.add_button("Close", Gtk.ResponseType.CLOSE)
        self.set_default_size(460, -1)

        content = self.get_content_area()
        content.set_border_width(12)
        content.set_spacing(8)

        # The one thing that cannot be fixed from inside this dialog, so it
        # goes above everything and carries the way to fix it.
        if usbdk_installed() is False:
            content.pack_start(self._usbdk_banner(), False, False, 0)

        self.note = Gtk.Label(xalign=0.0)
        self.note.set_line_wrap(True)
        self.note.get_style_context().add_class("dim")
        content.pack_start(self.note, False, False, 0)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        frame = Gtk.Frame()
        frame.add(self.listbox)
        content.pack_start(frame, True, True, 0)

        self.connect("response", lambda *_: self.destroy())
        theme_decorate(self)
        self.refresh()
        self.show_all()

    @staticmethod
    def _usbdk_banner():
        """The driver requirement, and where to go and get it.

        Devices still list without UsbDk and the list looks perfectly
        healthy, so the requirement has to be stated somewhere other than
        the failure it eventually causes.
        """
        frame = Gtk.Frame()
        frame.get_style_context().add_class("usb-driver-notice")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_border_width(8)

        icon = Gtk.Image.new_from_icon_name(
            "dialog-warning-symbolic", Gtk.IconSize.LARGE_TOOLBAR
        )
        icon.set_valign(Gtk.Align.START)
        box.pack_start(icon, False, False, 0)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        heading = Gtk.Label(xalign=0.0)
        heading.set_markup("<b>The UsbDk driver is not installed</b>")
        text.pack_start(heading, False, False, 0)

        detail = Gtk.Label(xalign=0.0)
        detail.set_line_wrap(True)
        detail.set_text(
            "Windows will not hand a USB device to the VM without it. "
            "Devices are listed below either way, but connecting one will "
            "fail. Proxima's installer can put it on for you, or:"
        )
        text.pack_start(detail, False, False, 0)

        link = Gtk.LinkButton.new_with_label(USBDK_RELEASES, "Download UsbDk")
        link.set_halign(Gtk.Align.START)
        text.pack_start(link, False, False, 0)

        box.pack_start(text, True, True, 0)
        frame.add(box)
        return frame

    # -- contents ------------------------------------------------------

    def refresh(self):
        """Rebuild the list from the console's current view of the host."""
        if self.console is None:
            return
        self._updating = True
        try:
            for child in self.listbox.get_children():
                self.listbox.remove(child)
            self._rows_by_key = {}

            devices, channels = self.console.usb_snapshot()
            note = self.console.usb_note()

            if note:
                self.note.set_text(note)
            elif channels == 0:
                self.note.set_text(NO_PORT_HINT)
            elif not devices:
                self.note.set_text("No USB devices are attached to this computer.")
            else:
                redirected = sum(1 for device in devices if device.connected)
                summary = (
                    f"{channels} redirection "
                    + ("port" if channels == 1 else "ports")
                    + f", {redirected} in use."
                )
                # What is missing on this machine belongs here too: the list
                # looks healthy right up to the moment a device is claimed.
                advice = self.console.usb_advice()
                self.note.set_text(
                    f"{summary} {advice[:1].upper()}{advice[1:]}".strip()
                )

            for device in devices:
                self.listbox.add(self._row(device, channels))
            self.listbox.show_all()
        finally:
            self._updating = False

    def _row(self, device, channels):
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_border_width(8)

        label = Gtk.Label(label=device.label, xalign=0.0)
        label.set_line_wrap(True)
        label.set_hexpand(True)
        box.pack_start(label, True, True, 0)

        switch = Gtk.Switch()
        switch.set_valign(Gtk.Align.CENTER)
        switch.set_active(device.connected)
        usable = device.redirectable and channels > 0
        switch.set_sensitive(usable and not self.console.usb.is_busy(device.key))
        if not usable:
            switch.set_tooltip_text(
                device.reason
                or (NO_PORT_HINT if channels == 0 else "This device cannot be shared")
            )
        switch.connect("notify::active", self._on_switched, device.key)
        box.pack_start(switch, False, False, 0)

        row.add(box)
        self._rows_by_key[device.key] = switch
        return row

    # -- acting --------------------------------------------------------

    def _on_switched(self, switch, _pspec, key):
        if self._updating:
            return
        wanted = switch.get_active()
        switch.set_sensitive(False)

        def done(ok, message):
            if not ok:
                self.on_status(f"USB: {message}")
                self._show_error(key, message)
            self.refresh()

        if wanted:
            self.console.usb.connect_device(key, done)
        else:
            self.console.usb.disconnect_device(key, done)

    def _show_error(self, key, message):
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK,
            text="That device could not be shared with the VM",
        )
        dialog.format_secondary_text(f"{key}\n\n{message}")
        theme_decorate(dialog)
        dialog.run()
        dialog.destroy()


class UsbPlugPrompt(Gtk.Dialog):
    """Asks whether the guest should have the device that just appeared.

    Answering is the whole dialog, so both answers are buttons and neither
    is destructive: leaving it on the host is what would have happened
    anyway.
    """

    CONNECT = 1
    KEEP = 2

    def __init__(self, parent, guest_label, device_label, on_answer):
        super().__init__(title="New USB Device", transient_for=parent)
        self.on_answer = on_answer

        self.add_button("Keep on This Computer", self.KEEP)
        connect = self.add_button(f"Connect to {guest_label}", self.CONNECT)
        connect.get_style_context().add_class("suggested-action")
        self.set_default_response(self.CONNECT)

        content = self.get_content_area()
        content.set_border_width(12)
        content.set_spacing(8)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        icon = Gtk.Image.new_from_icon_name(
            "drive-removable-media-symbolic", Gtk.IconSize.DIALOG
        )
        icon.set_valign(Gtk.Align.START)
        box.pack_start(icon, False, False, 0)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        heading = Gtk.Label(xalign=0.0)
        heading.set_markup("<b>A USB device was plugged in</b>")
        text.pack_start(heading, False, False, 0)

        detail = Gtk.Label(label=device_label, xalign=0.0)
        detail.set_line_wrap(True)
        text.pack_start(detail, False, False, 0)

        explain = Gtk.Label(xalign=0.0)
        explain.set_line_wrap(True)
        explain.get_style_context().add_class("dim")
        explain.set_text(
            f"Connecting it hands the device to {guest_label} and takes it "
            "away from this computer until the console is closed."
        )
        text.pack_start(explain, False, False, 0)

        self.remember = Gtk.CheckButton(label="Stop asking about USB devices")
        self.remember.set_tooltip_text(
            "Turn the question back on in Preferences -> Console"
        )
        text.pack_start(self.remember, False, False, 4)

        box.pack_start(text, True, True, 0)
        content.pack_start(box, True, True, 0)

        self.connect("response", self._on_response)
        theme_decorate(self)
        self.show_all()

    def _on_response(self, _dialog, response):
        connect = response == self.CONNECT
        stop_asking = self.remember.get_active()
        self.destroy()
        self.on_answer(connect, stop_asking)

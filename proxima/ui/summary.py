"""The summary side of a guest's tab.

Settings on the left, a picture of the guest on the right, and the facts
that change while you watch along the bottom. One of these belongs to each
tab, so what it shows is that tab's guest and nothing else.

Detail beyond what /cluster/resources already carries (the display adapter,
the guest agent, IP addresses) is fetched lazily on a worker thread, because
those are per-guest calls and the poll loop deliberately does not make them.
"""

import contextlib
import threading

import gi

gi.require_version("Gtk", "3.0")

from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

from ..api import notes as notes_meta
from ..api.models import audio_is_spice, vga_is_spice

# Only until the first allocation: the picture then takes the width it is
# given, however wide the window happens to be.
PREVIEW_WIDTH = 320
MIN_PREVIEW_HEIGHT = 180


class GuestSummary(Gtk.ScrolledWindow):
    """Read-only overview of one guest."""

    FIELDS = [
        ("name", "Name"),
        ("status", "Status"),
        ("node", "Node"),
        ("vmid", "VMID"),
        ("kind", "Type"),
        ("console", "Console"),
        ("display", "Display"),
        ("audio", "Audio"),
        ("agent", "Guest agent"),
        ("os", "Operating system"),
        ("cpu", "Processors"),
        ("memory", "Memory"),
        ("disk", "Disk"),
        ("address", "IP address"),
        ("uptime", "Uptime"),
        ("tags", "Tags"),
    ]

    def __init__(self, on_open_console=None, on_show_console=None, on_save_notes=None):
        super().__init__()
        self.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.on_open_console = on_open_console or (lambda: None)
        # Clicking the picture goes to the console that is already open;
        # the button opens one that is not.
        self.on_show_console = on_show_console or self.on_open_console
        self.on_save_notes = on_save_notes or (lambda text: None)
        self.guest = None
        self._detail_generation = 0
        self._detailed_key = None
        self._preview = None
        self._preview_width = 0
        self._notes_key = None
        self._notes_dirty = False

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_border_width(14)

        self.title = Gtk.Label(xalign=0.0)
        self.title.set_markup("<b>No guest selected</b>")
        outer.pack_start(self.title, False, False, 0)

        self.subtitle = Gtk.Label(xalign=0.0)
        self.subtitle.get_style_context().add_class("dim")
        self.subtitle.set_text("Select a guest in the sidebar.")
        outer.pack_start(self.subtitle, False, False, 0)

        # Settings on the left, the guest itself on the right. The settings
        # are as wide as they need to be; the picture takes the rest.
        columns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
        outer.pack_start(columns, True, True, 0)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        columns.pack_start(left, False, False, 0)

        grid = Gtk.Grid(row_spacing=3, column_spacing=20)
        self.values = {}
        for row, (key, label) in enumerate(self.FIELDS):
            name = Gtk.Label(label=label, xalign=0.0)
            name.get_style_context().add_class("summary-key")
            value = Gtk.Label(label="-", xalign=0.0)
            value.get_style_context().add_class("summary-value")
            value.set_selectable(True)
            value.set_line_wrap(True)
            grid.attach(name, 0, row, 1, 1)
            grid.attach(value, 1, row, 1, 1)
            self.values[key] = value
        left.pack_start(grid, False, False, 0)

        self.console_button = Gtk.Button(label="Open Console")
        self.console_button.set_halign(Gtk.Align.START)
        self.console_button.set_sensitive(False)
        self.console_button.connect("clicked", lambda *_: self.on_open_console())
        left.pack_start(self.console_button, False, False, 0)

        columns.pack_start(self._build_preview(), True, True, 0)

        # What changes while you are looking at it, along the bottom.
        outer.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0
        )
        self.footer = Gtk.Label(xalign=0.0)
        self.footer.get_style_context().add_class("dim")
        self.footer.set_line_wrap(True)
        self.footer.set_text("No guest selected.")
        outer.pack_start(self.footer, False, False, 0)

        outer.pack_start(self._build_notes(), False, False, 0)

        self.add(outer)

    def _build_notes(self):
        """The guest's notes, as Proxmox shows them and nothing more.

        Proxima keeps its own settings in the same field, inside a marked
        block. That block is never shown here and never typed here: it is
        taken off what is loaded and put back on what is saved, so editing
        the notes cannot lose a guest's folder or its console settings.
        """
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(6)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        label = Gtk.Label(label="Notes", xalign=0.0)
        label.get_style_context().add_class("summary-key")
        header.pack_start(label, False, False, 0)

        self.notes_status = Gtk.Label(xalign=0.0)
        self.notes_status.get_style_context().add_class("dim")
        header.pack_start(self.notes_status, False, False, 0)

        self.notes_save = Gtk.Button(label="Save")
        self.notes_save.set_sensitive(False)
        self.notes_save.connect("clicked", lambda *_: self.save_notes())
        header.pack_end(self.notes_save, False, False, 0)

        self.notes_revert = Gtk.Button(label="Revert")
        self.notes_revert.set_sensitive(False)
        self.notes_revert.connect("clicked", lambda *_: self._reset_notes())
        header.pack_end(self.notes_revert, False, False, 0)

        box.pack_start(header, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_size_request(-1, 110)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        self.notes_view = Gtk.TextView()
        self.notes_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.notes_view.set_left_margin(6)
        self.notes_view.set_right_margin(6)
        self.notes_view.set_sensitive(False)
        self.notes_buffer = self.notes_view.get_buffer()
        self.notes_buffer.connect("changed", self._on_notes_changed)
        scroller.add(self.notes_view)
        box.pack_start(scroller, False, False, 0)
        return box

    # -- notes ---------------------------------------------------------

    def _on_notes_changed(self, _buffer):
        if self._notes_key is None:
            return
        self._notes_dirty = self.notes_text() != self._notes_original
        self.notes_save.set_sensitive(self._notes_dirty)
        self.notes_revert.set_sensitive(self._notes_dirty)
        self.notes_status.set_text("unsaved changes" if self._notes_dirty else "")

    def notes_text(self):
        start, end = self.notes_buffer.get_bounds()
        return self.notes_buffer.get_text(start, end, False)

    def set_notes(self, key, raw_notes):
        """Show the user's half of a guest's notes.

        Ignored while there are unsaved edits: a poll landing mid-sentence
        must not take the sentence away.
        """
        if self._notes_dirty and self._notes_key == key:
            return
        _metadata, text = notes_meta.parse(raw_notes or "")
        self._notes_key = key
        self._notes_original = text
        self.notes_view.set_sensitive(True)
        self.notes_buffer.set_text(text)
        self._notes_dirty = False
        self.notes_save.set_sensitive(False)
        self.notes_revert.set_sensitive(False)
        self.notes_status.set_text("")

    def _reset_notes(self):
        self.notes_buffer.set_text(self._notes_original)

    def save_notes(self):
        if self._notes_key is None:
            return
        text = self.notes_text()
        self._notes_original = text
        self._notes_dirty = False
        self.notes_save.set_sensitive(False)
        self.notes_revert.set_sensitive(False)
        self.notes_status.set_text("saving...")
        self.on_save_notes(text)

    def notes_saved(self, ok=True, message=""):
        self.notes_status.set_text(message or ("saved" if ok else "could not save"))
        if not ok:
            self._notes_dirty = True
            self.notes_save.set_sensitive(True)
            self.notes_revert.set_sensitive(True)

    def _build_preview(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_valign(Gtk.Align.START)

        # An EventBox so the picture itself is clickable: clicking the guest
        # is the shortest way back to it.
        self.preview_button = Gtk.EventBox()
        self.preview_button.set_visible_window(False)
        self.preview_button.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.preview_button.connect("button-press-event", self._on_preview_clicked)

        frame = Gtk.Frame()
        frame.get_style_context().add_class("summary-preview")
        self.preview_image = Gtk.Image()
        self.preview_image.set_size_request(-1, MIN_PREVIEW_HEIGHT)
        frame.add(self.preview_image)
        self.preview_button.add(frame)
        box.pack_start(self.preview_button, False, False, 0)
        # Rescale to whatever width the window leaves us.
        frame.connect("size-allocate", self._on_preview_allocated)

        self.preview_note = Gtk.Label(xalign=0.5)
        self.preview_note.get_style_context().add_class("dim")
        box.pack_start(self.preview_note, False, False, 0)

        self._show_placeholder()
        return box

    # -- the picture ---------------------------------------------------

    def _show_placeholder(self, note="No picture yet"):
        self._preview = None
        self._preview_width = 0
        self.preview_image.set_from_icon_name(
            "video-display-symbolic", Gtk.IconSize.DIALOG
        )
        self.preview_note.set_text(note)

    def set_preview(self, pixbuf):
        """Show a frame grabbed from the console."""
        if pixbuf is None or pixbuf.get_width() <= 0 or pixbuf.get_height() <= 0:
            self._show_placeholder()
            return
        self._preview = pixbuf
        self._preview_width = 0  # force a rescale at the current width
        self.preview_note.set_text("")
        self._rescale_preview(self._allocated_width())

    def _allocated_width(self):
        width = self.preview_image.get_allocated_width()
        return width if width > 1 else PREVIEW_WIDTH

    def _on_preview_allocated(self, _widget, allocation):
        if self._preview is None:
            return
        # Only on a real change: setting an image from inside size-allocate
        # asks for another allocation, and matching widths is what stops
        # that turning into a loop.
        if abs(allocation.width - self._preview_width) < 8:
            return
        GLib.idle_add(self._rescale_preview, allocation.width)

    def _rescale_preview(self, width):
        pixbuf = self._preview
        if pixbuf is None or width < 16:
            return False
        self._preview_width = width
        height = max(1, int(pixbuf.get_height() * width / pixbuf.get_width()))
        self.preview_image.set_from_pixbuf(
            pixbuf.scale_simple(width, height, GdkPixbuf.InterpType.BILINEAR)
        )
        return False

    def _on_preview_clicked(self, _widget, event):
        """Clicking the guest goes to it, when there is anything to go to."""
        if event.button != 1:
            return False
        guest = self.guest
        if guest is None or not guest.running or guest.template:
            return False
        self.on_show_console()
        return True

    # -- population ----------------------------------------------------

    def clear(self):
        self.guest = None
        self._detailed_key = None
        self._detail_generation += 1
        self.title.set_markup("<b>No guest selected</b>")
        self.subtitle.set_text("Select a guest in the sidebar.")
        for value in self.values.values():
            value.set_text("-")
        self.console_button.set_sensitive(False)
        self.footer.set_text("No guest selected.")
        self._show_placeholder()
        self._notes_key = None
        self._notes_dirty = False
        self.notes_view.set_sensitive(False)
        self.notes_buffer.set_text("")

    def show_guest(self, guest, api=None):
        # This is re-entered on every inventory poll, not just on selection.
        # Only a genuine change of guest invalidates an in-flight detail
        # fetch -- bumping the generation on every poll used to cancel the
        # reply while the cache key still said "already requested", so the
        # page waited forever for an answer it had chosen to discard.
        changed = self._detailed_key != guest.key
        self.guest = guest
        if changed:
            self._detail_generation += 1
        generation = self._detail_generation

        self.title.set_markup(f"<b>{GLib.markup_escape_text(guest.name)}</b>")
        self.subtitle.set_text(
            f"{'Container' if guest.is_container else 'Virtual machine'} "
            f"{guest.vmid} on {guest.node}"
        )

        self._set("name", guest.name)
        self._set("status", "template" if guest.template else guest.status)
        self._set("node", guest.node)
        self._set("vmid", str(guest.vmid))
        self._set(
            "kind", "LXC container" if guest.is_container else "QEMU virtual machine"
        )
        self._set("cpu", guest.cpu_text)
        self._set("memory", guest.memory_text)
        self._set("disk", guest.disk_text)
        self._set("uptime", guest.uptime_text)
        self._set("tags", guest.tags or "-")

        self.console_button.set_sensitive(guest.running and not guest.template)
        self._update_footer(guest)
        if not guest.running and self._preview is None:
            self._show_placeholder(f"Guest is {guest.status}")

        if guest.is_container:
            self._set("console", "VNC (containers have no SPICE)")
            self._set("display", "-")
            self._set("agent", "-")
        elif not guest.config_loaded:
            self._set("console", "checking...")
            self._set("display", "checking...")
        else:
            self._describe_console(guest)

        # The per-guest detail calls (config, agent ping, interfaces) are not
        # cheap, so they only run when the selection actually changes. The
        # volatile fields above come from the poll and refresh every time.
        if not changed:
            return

        self._set("os", "-")
        self._set("address", "-")

        if api is not None and not guest.is_container:
            self._detailed_key = guest.key
            self._load_details(guest, api, generation)

    def _set(self, key, text):
        widget = self.values.get(key)
        if widget is not None:
            widget.set_text(str(text) if text not in (None, "") else "-")

    def _update_footer(self, guest):
        """The line along the bottom: state now, not configuration."""
        if guest.template:
            state = "Template"
        elif guest.running:
            state = f"Running for {guest.uptime_text}"
        else:
            state = f"Powered {guest.status}" if guest.status else "Powered off"

        parts = [state, f"{guest.cpu_text} · {guest.memory_text}"]
        address = self.values["address"].get_text()
        if address and address != "-":
            parts.append(address)
        self.footer.set_text("   •   ".join(parts))

    def _describe_console(self, guest):
        if guest.spice_capable is True:
            text = "SPICE"
        elif guest.spice_capable is None:
            text = "SPICE (unrecognised display type)"
        else:
            text = "VNC (no SPICE display)"
        # A note set while actually opening a console beats the prediction --
        # it is what happened rather than what should have.
        if guest.console_note:
            text = f"VNC  ({guest.console_note})"
        self._set("console", text)
        self._set("display", guest.display or "std (default)")
        audio = (guest.config or {}).get("audio0")
        if audio_is_spice(audio):
            self._set("audio", str(audio))
        elif audio:
            self._set("audio", f"{audio}  (not routed over SPICE)")
        else:
            self._set("audio", "none configured")

    # -- lazy detail ---------------------------------------------------

    def _load_details(self, guest, api, generation):
        def worker():
            config = {}
            osinfo = None
            address = ""
            with contextlib.suppress(Exception):
                config = api.guest_config(guest.node, guest.vmid, guest.kind)

            if guest.running:
                try:
                    osinfo = api.guest_agent_info(guest.node, guest.vmid)
                except Exception:
                    osinfo = None
                try:
                    address = self._first_address(
                        api.guest_interfaces(guest.node, guest.vmid)
                    )
                except Exception:
                    address = ""

            GLib.idle_add(
                self._apply_details, guest, generation, config, osinfo, address
            )

        threading.Thread(
            target=worker, daemon=True, name=f"summary-{guest.vmid}"
        ).start()

    @staticmethod
    def _first_address(interfaces):
        """The first non-loopback IPv4 the guest agent reports."""
        for interface in interfaces or []:
            if interface.get("name", "").startswith("lo"):
                continue
            for address in interface.get("ip-addresses") or []:
                if address.get("ip-address-type") != "ipv4":
                    continue
                value = address.get("ip-address", "")
                if value and not value.startswith("127."):
                    return value
        return ""

    def _apply_details(self, guest, generation, config, osinfo, address):
        # A different guest was selected while this was in flight. Compare
        # keys rather than object identity so a rebuilt Guest still counts.
        if (
            generation != self._detail_generation
            or self.guest is None
            or self.guest.key != guest.key
        ):
            return False

        if not config:
            # Nothing was learned, so let the next poll try again instead of
            # leaving the page on "checking..." indefinitely.
            self._detailed_key = None
            self._set("console", "unknown (config unreadable)")
            self._set("display", "unknown")
            return False

        guest.config = config
        guest.display = str(config.get("vga", ""))
        guest.spice_capable = vga_is_spice(guest.display)
        guest.config_loaded = True
        self._describe_console(guest)

        if osinfo:
            result = osinfo.get("result", osinfo) if isinstance(osinfo, dict) else {}
            pretty = result.get("pretty-name") or result.get("name") or ""
            self._set("os", pretty or "-")
            self._set("agent", "running")
        elif guest.running and config.get("agent"):
            self._set("agent", "enabled, not responding")
        else:
            self._set("agent", "not enabled" if guest.running else "-")

        self._set("address", address or "-")
        self._update_footer(guest)
        return False

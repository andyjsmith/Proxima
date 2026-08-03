"""The guest summary page shown when no console is open.

Detail beyond what /cluster/resources already carries (the display adapter,
the guest agent, IP addresses) is fetched lazily on a worker thread, because
those are per-guest calls and the poll loop deliberately does not make them.
"""

import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from ..api.models import audio_is_spice, vga_is_spice


class SummaryPage(Gtk.ScrolledWindow):
    """Read-only overview of the selected guest."""

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

    def __init__(self, on_open_console=None):
        super().__init__()
        self.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.on_open_console = on_open_console or (lambda: None)
        self.guest = None
        self._detail_generation = 0
        self._detailed_key = None

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_border_width(14)

        self.title = Gtk.Label(xalign=0.0)
        self.title.set_markup("<b>No guest selected</b>")
        outer.pack_start(self.title, False, False, 0)

        self.subtitle = Gtk.Label(xalign=0.0)
        self.subtitle.get_style_context().add_class("dim")
        self.subtitle.set_text("Select a guest in the sidebar.")
        outer.pack_start(self.subtitle, False, False, 0)

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
        outer.pack_start(grid, False, False, 0)

        self.console_button = Gtk.Button(label="Open Console")
        self.console_button.set_sensitive(False)
        self.console_button.connect("clicked", lambda *_: self.on_open_console())
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        buttons.pack_start(self.console_button, False, False, 0)
        outer.pack_start(buttons, False, False, 0)

        self.add(outer)

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
            try:
                config = api.guest_config(guest.node, guest.vmid, guest.kind)
            except Exception:
                pass

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
        return False

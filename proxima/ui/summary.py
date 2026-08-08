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

from ..api import devices
from ..api import notes as notes_meta
from ..api.models import audio_is_spice, os_type_name, vga_is_spice
from ..theme import current_dark
from . import actions as action_defs
from . import status_icons

# Only until the first allocation: the picture then takes the width it is
# given, however wide the window happens to be.
PREVIEW_WIDTH = 320
MIN_PREVIEW_HEIGHT = 180

# Bigger than the tree's 16px: this one sits beside a bold heading.
HEADING_ICON = 24


class PreviewHolder(Gtk.Bin):
    """Holds the preview picture without letting it set the layout's floor.

    A GtkImage's minimum size is its pixbuf's size, so a large frame made
    the summary's minimum wider than the window: the page scrolled instead
    of shrinking, the allocation stopped tracking the window, and the
    picture could only ever ratchet larger. Reporting a small minimum and
    the picture's own natural size fixes that -- the same trick
    DisplayHolder uses for the SPICE display next door.

    A GtkScrolledWindow does this too, and is what was here first, but it
    also swallows scroll events and paints GTK's overshoot glow when the
    pointer is over a picture that has nothing to scroll.
    """

    __gtype_name__ = "ProximaPreviewHolder"

    def do_get_preferred_width(self):
        child = self.get_child()
        natural = child.get_preferred_width()[1] if child is not None else 1
        return (1, max(1, natural))

    def do_get_preferred_height(self):
        child = self.get_child()
        natural = child.get_preferred_height()[1] if child is not None else 0
        return (1, max(MIN_PREVIEW_HEIGHT, natural))

    def do_get_preferred_width_for_height(self, _height):
        return self.do_get_preferred_width()

    def do_get_preferred_height_for_width(self, _width):
        return self.do_get_preferred_height()

    def do_size_allocate(self, allocation):
        self.set_allocation(allocation)
        child = self.get_child()
        if child is not None and child.get_visible():
            child.size_allocate(allocation)


class GuestSummary(Gtk.ScrolledWindow):
    """Read-only overview of one guest."""

    # Name, VMID, node and type are deliberately absent: the heading above
    # this grid already says "Virtual machine 108 on violet", and repeating
    # it four times pushed everything worth reading further down.
    #
    # IP address comes last because the network device rows are appended
    # after it, and the two belong together.
    FIELDS = [
        ("status", "Status"),
        ("uptime", "Uptime"),
        ("cpu", "Processors"),
        ("memory", "Memory"),
        ("disk", "Disk"),
        ("os", "Operating system"),
        ("agent", "Guest agent"),
        ("console", "Console"),
        ("display", "Display"),
        ("audio", "Audio"),
        ("tags", "Tags"),
        ("address", "IP address"),
    ]

    def __init__(
        self,
        on_open_console=None,
        on_show_console=None,
        on_save_notes=None,
        on_power_action=None,
        on_edit_settings=None,
    ):
        super().__init__()
        self.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.on_open_console = on_open_console or (lambda: None)
        # Clicking the picture goes to the console that is already open;
        # the button opens one that is not.
        self.on_show_console = on_show_console or self.on_open_console
        self.on_save_notes = on_save_notes or (lambda text: None)
        self.on_power_action = on_power_action or (lambda action_name: None)
        self.on_edit_settings = on_edit_settings or (lambda: None)
        self._power_action = None
        self.guest = None
        self._detail_generation = 0
        self._detailed_key = None
        self._preview = None
        self._preview_width = 0
        self._preview_budget = 0
        self._notes_key = None
        self._notes_dirty = False
        self._net_widgets = []

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_border_width(14)

        # Two columns and nothing above them. The heading and its buttons
        # belong to the left column rather than spanning the page: full width
        # they pushed the picture down to start level with the Status row,
        # wasting the tallest part of the space it had to work with.
        columns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
        outer.pack_start(columns, True, True, 0)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        columns.pack_start(left, False, False, 0)

        # The same icon the tree draws for this guest, in the same colour --
        # see ui.status_icons, which both go through.
        heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.status_icon = Gtk.Image()
        heading.pack_start(self.status_icon, False, False, 0)

        self.title = Gtk.Label(xalign=0.0)
        self.title.set_markup("<b>No guest selected</b>")
        heading.pack_start(self.title, False, False, 0)
        left.pack_start(heading, False, False, 0)

        self.subtitle = Gtk.Label(xalign=0.0)
        self.subtitle.get_style_context().add_class("dim")
        self.subtitle.set_text("Select a guest in the sidebar.")
        left.pack_start(self.subtitle, False, False, 0)

        # What you came here to do, directly under the guest's name rather
        # than buried under a screenful of read-only detail.
        self._actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._actions.set_halign(Gtk.Align.START)

        self.console_button = Gtk.Button()
        self.console_button.set_always_show_image(True)
        self.console_button.get_style_context().add_class("labelled-icon")
        self.console_button.set_sensitive(False)
        self.console_button.connect("clicked", self._on_primary_clicked)
        self._actions.pack_start(self.console_button, False, False, 0)

        self.settings_button = Gtk.Button(label="Edit settings")
        self.settings_button.set_always_show_image(True)
        self.settings_button.get_style_context().add_class("labelled-icon")
        self.settings_button.set_image(
            Gtk.Image.new_from_icon_name("emblem-system-symbolic", Gtk.IconSize.BUTTON)
        )
        self.settings_button.set_sensitive(False)
        self.settings_button.connect("clicked", lambda *_: self.on_edit_settings())
        self._actions.pack_start(self.settings_button, False, False, 0)
        left.pack_start(self._actions, False, False, 0)

        grid = self._details_grid = Gtk.Grid(row_spacing=3, column_spacing=20)
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
        # The notes live under the details rather than along the bottom: full
        # width they pushed the page taller than the window, and a picture
        # that grows with the window would then always be scrolling something
        # off the end. In the column they take the height the picture is not
        # using.
        left.pack_start(self._build_notes(), True, True, 0)

        self._preview_box = self._build_preview()
        columns.pack_start(self._preview_box, True, True, 0)

        self._outer = outer
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

        # Hidden rather than greyed out when there is nothing to save: with
        # no edit in progress there is no decision to explain, and two dead
        # buttons sitting over every guest's notes is just noise.
        # no-show-all, or the show_all() that puts a tab on screen would
        # bring them straight back.
        self.notes_save = Gtk.Button(label="Save")
        self.notes_save.set_no_show_all(True)
        self.notes_save.connect("clicked", lambda *_: self.save_notes())
        header.pack_end(self.notes_save, False, False, 0)

        self.notes_revert = Gtk.Button(label="Revert")
        self.notes_revert.set_no_show_all(True)
        self.notes_revert.connect("clicked", lambda *_: self._reset_notes())
        header.pack_end(self.notes_revert, False, False, 0)

        # The row keeps the height it has with the buttons in it, whether or
        # not they are showing. A button is taller than the "Notes" label, so
        # without this the label and the box below it were nudged down a few
        # pixels the moment an edit began.
        self._notes_header = header
        header.set_size_request(-1, self.notes_save.get_preferred_height()[1])
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
        # Expanding, so the notes take whatever height the column has left
        # rather than leaving a gap under a fixed 110px box.
        box.pack_start(scroller, True, True, 0)
        return box

    # -- notes ---------------------------------------------------------

    def _show_notes_buttons(self, showing):
        """Save and Revert exist only while there is an edit to act on."""
        self.notes_save.set_visible(bool(showing))
        self.notes_revert.set_visible(bool(showing))
        if showing:
            # Now that they are up and styled, their real height is known.
            # Only ever grows, so hiding them cannot shrink the row back.
            wanted = self.notes_save.get_preferred_height()[1]
            if wanted > self._notes_header.get_size_request().height:
                self._notes_header.set_size_request(-1, wanted)

    def _on_notes_changed(self, _buffer):
        if self._notes_key is None:
            return
        self._notes_dirty = self.notes_text() != self._notes_original
        self._show_notes_buttons(self._notes_dirty)
        # No "unsaved changes" caption: the buttons appearing is that. This
        # only clears a stale "saved" from the previous edit.
        self.notes_status.set_text("")

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
        self._show_notes_buttons(False)
        self.notes_status.set_text("")

    def _reset_notes(self):
        self.notes_buffer.set_text(self._notes_original)

    def save_notes(self):
        if self._notes_key is None:
            return
        text = self.notes_text()
        self._notes_original = text
        self._notes_dirty = False
        self._show_notes_buttons(False)
        self.notes_status.set_text("saving...")
        self.on_save_notes(text)

    def notes_saved(self, ok=True, message=""):
        self.notes_status.set_text(message or ("saved" if ok else "could not save"))
        if not ok:
            self._notes_dirty = True
            self._show_notes_buttons(True)

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

        # See PreviewHolder: small minimum so the picture can be made
        # smaller, natural size taken from the picture so the frame still
        # hugs it.
        holder = PreviewHolder()
        self.preview_image = Gtk.Image()
        holder.add(self.preview_image)
        frame.add(holder)
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

    def _height_budget(self):
        """The tallest the picture may be and still fit the visible page.

        Measured against the scroller's own height, which is what the window
        gives us, not against the content's -- the content is free to grow,
        which is exactly the runaway this exists to stop.

        Everything counted here sits above or below the picture and none of
        it depends on the picture's size, so this cannot feed back into
        itself and start a resize loop.
        """
        visible = self.get_allocated_height()
        if visible <= 1:  # not allocated yet; the width alone decides
            return 0
        # The border, and whatever is under the picture in its own column.
        # Nothing else is above or below it any more: the heading and its
        # buttons are beside it, and the status line along the bottom is
        # gone -- every figure on it was already a row in the grid.
        chrome = 2 * self._outer.get_border_width()
        if self.preview_note.get_visible():
            chrome += self.preview_note.get_preferred_height()[1]
            chrome += self._preview_box.get_spacing()
        return max(MIN_PREVIEW_HEIGHT, visible - chrome)

    def _on_preview_allocated(self, _widget, allocation):
        if self._preview is None:
            return
        # Only on a real change: setting an image from inside size-allocate
        # asks for another allocation, and matching what we last drew is what
        # stops that turning into a loop. The budget is checked too, so that
        # a window made shorter without getting narrower still rescales.
        budget = self._height_budget()
        if (
            abs(allocation.width - self._preview_width) < 8
            and abs(budget - self._preview_budget) < 8
        ):
            return
        GLib.idle_add(self._rescale_preview, allocation.width)

    def _rescale_preview(self, width):
        pixbuf = self._preview
        if pixbuf is None or width < 16:
            return False
        self._preview_width = width
        budget = self._preview_budget = self._height_budget()
        height = max(1, int(pixbuf.get_height() * width / pixbuf.get_width()))
        if budget and height > budget:
            # Too tall for the page: fit the height instead and let the
            # picture be narrower than the column rather than run off the
            # bottom. The frame keeps the full width either way, so this
            # cannot shrink the allocation it was measured from.
            height = budget
            width = max(16, int(pixbuf.get_width() * height / pixbuf.get_height()))
        self.preview_image.set_from_pixbuf(
            pixbuf.scale_simple(width, height, GdkPixbuf.InterpType.BILINEAR)
        )
        return False

    # -- the two buttons under the heading ------------------------------

    def _on_primary_clicked(self, _button):
        if self._power_action is None:
            self.on_open_console()
        else:
            # Same call the toolbar's Start makes, so it brings the console
            # forward on the way past exactly as that does.
            self.on_power_action(self._power_action)

    def _update_buttons(self, guest):
        """One button: open the console, or turn the guest on to get one."""
        self.settings_button.set_sensitive(guest is not None)
        if guest is None:
            self._power_action = None
            self.console_button.set_sensitive(False)
            self._set_button("Open Console", "video-display-symbolic")
            return

        if guest.has_console and not guest.template:
            self._power_action = None
            self.console_button.set_sensitive(True)
            self._set_button("Open Console", "video-display-symbolic")
            return

        action = action_defs.start_action_for(guest)
        applies = guest.status in action.states and guest.kind in action.kinds
        self._power_action = action.name if applies else None
        self.console_button.set_sensitive(applies and not guest.template)
        self._set_button(
            "Resume" if action.name == "resume" else "Power on",
            "media-playback-start-symbolic",
            green=True,
        )

    def _set_button(self, label, icon_name, green=False):
        image = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
        if green:
            # The theme's own running colour, so it matches the tree.
            image.get_style_context().add_class("status-running")
        self.console_button.set_image(image)
        self.console_button.set_label(label)

    # -- what the guest is configured as --------------------------------

    def _processors_text(self, guest):
        """Live usage while running, the configured size when not.

        A powered-off guest still has a processor count -- it is in its
        settings -- and "-" was a worse answer than the truth.
        """
        if guest.running:
            return guest.cpu_text
        config = guest.config or {}
        cores = int(config.get("cores") or 0)
        sockets = int(config.get("sockets") or 1)
        total = cores * sockets or guest.maxcpu
        if not total:
            return "-"
        if cores and sockets > 1:
            return f"{total} vCPU ({sockets} sockets x {cores} cores)"
        return f"{total} vCPU"

    def _os_text(self, guest, pretty=""):
        """What the agent says, or failing that what the guest is set up as.

        The agent only answers on a running guest with the tools installed,
        which is exactly when this field used to be empty for everyone else.
        """
        if pretty:
            return pretty
        configured = os_type_name((guest.config or {}).get("ostype"))
        if not configured:
            return "-"
        return f"{configured}  (configured)"

    def _network_rows(self, guest):
        """One description per NIC, in slot order."""
        config = guest.config or {}
        rows = []
        for slot in devices.nic_slots(config):
            pairs = devices.parse_pairs(config.get(slot))
            model, mac = devices.nic_model(pairs)
            bridge = devices.get_pair(pairs, "bridge", "")
            tag = devices.get_pair(pairs, "tag", "")
            text = model or "nic"
            if bridge:
                text += f" on {bridge}"
            if tag:
                text += f", VLAN {tag}"
            if mac:
                text += f"  ({mac})"
            if devices.get_pair(pairs, "link_down", "") == "1":
                text += "  [disconnected]"
            rows.append((slot, text))
        return rows

    def _set_networks(self, guest):
        """Rebuild the netN rows under IP address, one per adapter."""
        wanted = self._network_rows(guest)
        grid = self._details_grid
        for name, value in self._net_widgets:
            grid.remove(name)
            grid.remove(value)
        self._net_widgets = []
        for offset, (slot, text) in enumerate(wanted):
            row = len(self.FIELDS) + offset
            name = Gtk.Label(
                label=f"Network device{'' if len(wanted) == 1 else f' ({slot})'}",
                xalign=0.0,
            )
            name.get_style_context().add_class("summary-key")
            value = Gtk.Label(label=text, xalign=0.0)
            value.get_style_context().add_class("summary-value")
            value.set_selectable(True)
            value.set_line_wrap(True)
            grid.attach(name, 0, row, 1, 1)
            grid.attach(value, 1, row, 1, 1)
            self._net_widgets.append((name, value))
        grid.show_all()

    def _on_preview_clicked(self, _widget, event):
        """Clicking the guest goes to it, when there is anything to go to."""
        if event.button != 1:
            return False
        guest = self.guest
        if guest is None or not guest.has_console or guest.template:
            return False
        self.on_show_console()
        return True

    # -- population ----------------------------------------------------

    def clear(self):
        self.guest = None
        self._detailed_key = None
        self._detail_generation += 1
        self.title.set_markup("<b>No guest selected</b>")
        self._set_status_icon(None)
        self.subtitle.set_text("Select a guest in the sidebar.")
        for value in self.values.values():
            value.set_text("-")
        self._update_buttons(None)
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
        self._set_status_icon(guest)
        self.subtitle.set_text(
            f"{'Container' if guest.is_container else 'Virtual machine'} "
            f"{guest.vmid} on {guest.node}"
        )

        self._set("status", "template" if guest.template else guest.status)
        self._set("cpu", self._processors_text(guest))
        self._set("memory", guest.memory_text)
        self._set("disk", guest.disk_text)
        self._set("uptime", guest.uptime_text)
        self._set("tags", guest.tags or "-")

        self._update_buttons(guest)
        if not guest.has_console and self._preview is None:
            self._show_placeholder(f"Guest is {guest.status}")

        if guest.is_container:
            # A note is only ever set when opening a console actually fell
            # back, so it beats the prediction -- which is the whole reason
            # the field is not simply hard-coded.
            self._set(
                "console",
                f"VNC  ({guest.console_note})"
                if guest.console_note
                else "Serial (containers have no SPICE)",
            )
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

        self._set("os", self._os_text(guest))
        self._set("address", "-")
        self._set_networks(guest)

        if api is not None and not guest.is_container:
            self._detailed_key = guest.key
            self._load_details(guest, api, generation)

    def _set_status_icon(self, guest):
        """The tree's icon for this guest, beside its name."""
        pixbuf = (
            None
            if guest is None
            else status_icons.guest_icon(guest, dark=current_dark(), size=HEADING_ICON)
        )
        if pixbuf is None:
            self.status_icon.clear()
        else:
            self.status_icon.set_from_pixbuf(pixbuf)

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

        # The config has landed, so anything that can be answered from it
        # now has a better answer than it did a moment ago.
        self._set("cpu", self._processors_text(guest))
        self._set_networks(guest)

        if osinfo:
            result = osinfo.get("result", osinfo) if isinstance(osinfo, dict) else {}
            pretty = result.get("pretty-name") or result.get("name") or ""
            self._set("os", self._os_text(guest, pretty))
            self._set("agent", "running")
        elif guest.running and config.get("agent"):
            self._set("os", self._os_text(guest))
            self._set("agent", "enabled, not responding")
        else:
            self._set("os", self._os_text(guest))
            self._set("agent", "not enabled" if guest.running else "-")

        self._set("address", address or "-")
        return False

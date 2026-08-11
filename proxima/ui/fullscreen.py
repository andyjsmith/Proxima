"""Fullscreen console handling, shared by the main window and pop-outs.

The window goes fullscreen and its chrome is hidden, rather than reparenting
the console into a new toplevel: moving a SpiceDisplay between toplevels
destroys and rebuilds its GdkWindow underneath a live connection.

A floating bar sits at the top edge. Unpinned it hides itself and returns
when the pointer reaches the top. Reveal is polled rather than driven by
motion events, because the console owns the pointer while focused --
spice-gtk grabs it outright in server-mouse mode -- so motion-notify on this
side is not reliable.

A guest with more than one head can take more than one monitor. The extra
heads get bare fullscreen windows built here and thrown away on the way
out; the console makes their display widgets, so nothing is reparented on
this path either.
"""

import logging
import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from . import toolbar

log = logging.getLogger(__name__)

HOT_ZONE = 2  # px from the top edge that re-reveals the bar
POLL_MS = 120


def monitor_layout():
    """Every monitor on the desktop, left to right, as (number, x).

    Ordered by position rather than by number, so a guest's second head
    lands on the monitor sitting next to the first one rather than on
    whichever the desktop happens to have enumerated second. The number is
    the one gtk_window_fullscreen_on_monitor() wants, which is the display's
    own indexing.
    """
    display = Gdk.Display.get_default()
    if display is None:
        return []
    order = []
    for index in range(display.get_n_monitors()):
        monitor = display.get_monitor(index)
        if monitor is None:
            continue
        order.append((index, monitor.get_geometry().x))
    order.sort(key=lambda pair: pair[1])
    return order


def monitor_geometry(number):
    """One monitor's rectangle, or None if there is no such monitor."""
    display = Gdk.Display.get_default()
    if display is None or not 0 <= number < display.get_n_monitors():
        return None
    monitor = display.get_monitor(number)
    return None if monitor is None else monitor.get_geometry()


def _place_on(window, number):
    """Move a window onto one monitor. Returns its geometry, or None."""
    geometry = monitor_geometry(number)
    if geometry is not None:
        window.move(geometry.x, geometry.y)
        window.resize(geometry.width, geometry.height)
    return geometry


def _fullscreen_on(window, number):
    """Put a window fullscreen on one named monitor.

    Placed by hand before being asked, because being asked is not enough.
    gtk_window_fullscreen_on_monitor() is honoured by the X11 backend and
    quietly not by every other one -- on Windows it can come out as a plain
    fullscreen, which means "the monitor the window is already on", and that
    is precisely the monitor this call exists to avoid. Moving the window
    onto the target first makes the fallback land in the right place too.

    GTK also asserts rather than fails on a monitor number that is not
    there, and monitors do come and go while a session is open.
    """
    geometry = _place_on(window, number)
    screen = window.get_screen()
    if screen is not None and geometry is not None:
        try:
            window.fullscreen_on_monitor(screen, number)
            return
        except Exception as exc:
            log.info("fullscreen_on_monitor(%s) failed: %s", number, exc)
    window.fullscreen()


class FullscreenController:
    def __init__(
        self,
        window,
        overlay,
        get_console,
        chrome,
        on_ctrl_alt_del=None,
        on_enter=None,
        on_leave=None,
        title="",
        all_monitors=None,
        on_send_keys=None,
        on_power=None,
        on_snapshot=None,
    ):
        self.window = window
        self.overlay = overlay
        self.get_console = get_console
        self.chrome = chrome  # callable returning widgets to hide
        self.on_ctrl_alt_del = on_ctrl_alt_del or (lambda: None)
        # Given one of these, the bar grows the matching controls. The owner
        # is expected to fold power_items and snapshot_items into whatever it
        # already hands to toolbar.apply_power_state, so they follow the guest
        # without a second thing to keep in step.
        self.on_send_keys = on_send_keys
        self.on_power = on_power
        self.on_snapshot = on_snapshot
        self.power_items = {}
        self.snapshot_items = {}
        self.on_enter = on_enter or (lambda: None)
        self.on_leave = on_leave or (lambda: None)
        # Asked, not passed in. Every way into full screen has to agree, and
        # one of them is a key combination this class handles itself: with
        # the preference arriving as an argument, Ctrl+Alt+Enter had no way
        # to know it and quietly took one monitor every time.
        self.all_monitors = all_monitors or (lambda: False)

        self.active = False
        self.spanning = False
        self._poll_source = None
        self._hide_at = None
        self._hidden = []
        self._ungrab_armed = False
        self._extra_windows = []  # (head index, window)
        # Whose heads those windows are showing. Held rather than looked up
        # again: the tab in front can change while they are open, and the
        # heads must go back to the console they came from.
        self._spanning_console = None

        self.overlay.add_overlay(self._build_bar())
        self.set_title(title)

    # -- bar -----------------------------------------------------------

    def _build_bar(self):
        self.revealer = Gtk.Revealer()
        self.revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.revealer.set_transition_duration(150)
        self.revealer.set_halign(Gtk.Align.CENTER)
        self.revealer.set_valign(Gtk.Align.START)
        self.revealer.set_reveal_child(False)
        self.revealer.set_no_show_all(True)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        bar.get_style_context().add_class("fullscreen-bar")

        self.title_label = Gtk.Label()
        self.title_label.set_ellipsize(3)
        self.title_label.set_max_width_chars(30)
        bar.pack_start(self.title_label, False, False, 4)

        def separator():
            bar.pack_start(
                Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 2
            )

        separator()

        # The guest's own controls, in the order the toolbar has them, so that
        # going full screen stops meaning "leave the buttons behind".
        if self.on_power is not None:
            self.power_items = toolbar.add_bar_power_buttons(bar, self.on_power)
            separator()

        if self.on_snapshot is not None:
            self.snapshot_items = toolbar.add_bar_snapshot_buttons(
                bar, self.on_snapshot
            )
            separator()

        if self.on_send_keys is not None:
            toolbar.add_bar_send_key_button(bar, self.on_send_keys)
        else:
            # No key menu to offer, so the one key this ever really sends.
            keys = toolbar.BarButton(
                toolbar.SEND_KEY_ICON, toolbar.SEND_KEY_TOOLTIP, sensitive=True
            )
            keys.connect("clicked", lambda *_: self.on_ctrl_alt_del())
            bar.pack_start(keys, False, False, 0)

        separator()

        self.pin = Gtk.ToggleButton()
        self.pin.set_relief(Gtk.ReliefStyle.NONE)
        self.pin.add(
            Gtk.Image.new_from_icon_name("view-pin-symbolic", Gtk.IconSize.MENU)
        )
        self.pin.set_tooltip_text("Keep this bar visible")
        self.pin.connect("toggled", self._on_pin_toggled)
        bar.pack_start(self.pin, False, False, 0)

        leave = Gtk.Button()
        leave.set_relief(Gtk.ReliefStyle.NONE)
        leave.add(
            Gtk.Image.new_from_icon_name("view-restore-symbolic", Gtk.IconSize.MENU)
        )
        leave.set_tooltip_text("Exit full screen (Ctrl+Alt+Enter)")
        leave.connect("clicked", lambda *_: self.leave())
        bar.pack_start(leave, False, False, 0)

        self.revealer.add(bar)
        bar.show_all()
        return self.revealer

    def set_title(self, title):
        self.title_label.set_text(title or "")

    # -- enter / leave -------------------------------------------------

    def spare_monitors(self):
        """How many monitors are free once this window has taken one."""
        return max(0, len(monitor_layout()) - 1)

    def can_span_monitors(self, console=None):
        """Whether a console has heads to spread, and whether there is room.

        Both halves matter and both can be false on their own: a VNC console
        carries one framebuffer however the guest is configured, and a guest
        with two heads on a laptop with one screen has nowhere to put the
        second. Defaults to the console this controller is showing.
        """
        if console is None:
            console = self.get_console()
        if console is None:
            return False
        if not getattr(console, "supports", {}).get("multi_monitor"):
            return False
        if not callable(getattr(console, "create_head_display", None)):
            return False
        # Heads the console could show, not heads the guest is showing: a
        # SPICE guest makes a head when it is asked for one, so waiting to
        # see a second before offering to use it would mean never offering.
        heads = getattr(console, "available_heads", lambda: 0)()
        return heads > 1 and self.spare_monitors() > 0

    def toggle(self, all_monitors=None):
        self.leave() if self.active else self.enter(all_monitors)

    def enter(self, all_monitors=None):
        """Go full screen. `all_monitors` None means "whatever is set"."""
        if self.active or self.get_console() is None:
            return
        if all_monitors is None:
            all_monitors = self.all_monitors()
        self.spanning = bool(all_monitors) and self.can_span_monitors()
        self.active = True

        console = self.get_console()
        self.set_title(getattr(console, "title", ""))

        self._hidden = [w for w in self.chrome() if w.get_visible()]
        for widget in self._hidden:
            widget.hide()
        self.on_enter()

        self.revealer.show()
        # Start revealed: the way out has to be discoverable.
        self._set_revealed(True)
        if self.spanning:
            self._span_monitors(console)
        else:
            self.window.fullscreen()

        self._poll_source = GLib.timeout_add(POLL_MS, self._poll)
        self._hide_at = time.monotonic() + 2.0

        if console is not None and hasattr(console, "grab_focus_display"):
            GLib.idle_add(console.grab_focus_display)

    def leave(self):
        if not self.active:
            return
        self.active = False
        self.spanning = False
        self.stop()

        self._set_revealed(False)
        self.revealer.hide()
        self.window.unfullscreen()
        for widget in self._hidden:
            widget.show()
        self._hidden = []
        self.on_leave()

        console = self.get_console()
        if console is not None and hasattr(console, "grab_focus_display"):
            GLib.idle_add(console.grab_focus_display)

    def stop(self):
        if self._poll_source is not None:
            GLib.source_remove(self._poll_source)
            self._poll_source = None
        # Also on the shutdown path, where an extra window would otherwise
        # be left on screen with nothing behind it.
        self._close_extra_windows()

    # -- extra monitors --------------------------------------------------

    def _span_monitors(self, console):
        """This window on the monitor it is already on, a head on each other.

        Order comes from the desktop's own layout, so head 2 appears on the
        monitor to the right of head 1 rather than wherever the enumeration
        put it.

        Every window opens at once, including the ones for heads the guest
        has not made yet -- they wait with a message rather than appearing
        later out of nowhere. Some guests answer that first ask and some
        do not: going full screen resizes the first head, and the message
        describing that resize does not mention a head that has been asked
        for and not yet created, so on those guests the request is dropped.
        The answer is not to ask later instead -- that turned out to be
        worse on other guests -- but to ask again once the resize has
        landed, which is what people were doing by hand when they went full
        screen twice.
        """
        order = [number for number, _x in monitor_layout()]
        home = self._current_monitor()
        if home in order:
            order.remove(home)

        _fullscreen_on(self.window, home)
        self._spanning_console = console
        self._place_heads(console, order)

        # The resize this window is going through is what can lose a head.
        if hasattr(console, "after_primary_settles"):
            console.after_primary_settles(lambda: self._ask_again(console))
        else:
            GLib.timeout_add(self.HEAD_SETTLE_MS, self._ask_again, console)

    def _place_heads(self, console, order):
        """A window on each spare monitor, showing a head each."""
        if not self.active or console is not self._spanning_console:
            return  # full screen was left while the guest was thinking

        heads = console.available_heads()
        title = getattr(console, "title", "")
        # Every head except the one this window is already showing, which is
        # not always head 0: the tab attaches to whichever display channel
        # turned up first, and on a two-device guest that can be channel 1.
        shown = getattr(console, "primary_head_index", lambda: 0)()
        spare_heads = [index for index in range(heads) if index != shown]
        log.info(
            "spanning: monitors=%s spare=%s heads=%s showing=%s",
            monitor_layout(),
            order,
            heads,
            shown,
        )
        # Not strict: there are as many heads placed as there are monitors
        # free, and either side can be the shorter one.
        placed = []
        for number, (head, monitor) in enumerate(
            zip(spare_heads, order, strict=False), start=2
        ):
            holder = console.create_head_display(head)
            if holder is None:
                log.info("spanning stopped: head %s could not be built", head)
                break
            window = self._build_head_window(f"{title} - monitor {number}", holder)
            # Placed before it is mapped as well as after: a window that is
            # first shown on the wrong monitor can be fullscreened there by
            # a backend that ignores the request to do otherwise.
            _place_on(window, monitor)
            window.show_all()
            _fullscreen_on(window, monitor)
            self._extra_windows.append((head, window))
            placed.append((head, monitor))
            GLib.idle_add(self._report_placement, head, monitor, window)

        # How big each head's window is, for the record only. Where the
        # heads go is the guest's own business -- it arranges the ones it
        # has been given, and a client sending positions on top of that
        # argues with it continuously. The widgets report their sizes
        # themselves through resize-guest.
        for head, monitor in placed:
            geometry = monitor_geometry(monitor)
            if geometry is not None and hasattr(console, "set_head_size"):
                console.set_head_size(head, geometry.width, geometry.height)

    # Only for a console that cannot say when its guest has settled.
    HEAD_SETTLE_MS = 1500

    def _ask_again(self, console):
        """Have another go at any head the guest has not produced."""
        spanning = self.active and console is self._spanning_console
        if spanning and hasattr(console, "retry_missing_heads"):
            console.retry_missing_heads()
        return False

    @staticmethod
    def _report_placement(head, monitor, window):
        """Say where a head's window actually ended up.

        Asking for a monitor and getting it are different things, and the
        difference is invisible from here without looking: a window manager
        that ignores fullscreen_on_monitor() leaves the window on the
        monitor it was already on, which reads as "full screen only ever
        uses one monitor".
        """
        if not window.get_realized():
            return False
        gdk_window = window.get_window()
        origin = gdk_window.get_origin() if gdk_window is not None else None
        log.info(
            "head %s asked for monitor %s, landed at %s size %s",
            head,
            monitor,
            None if origin is None else (origin[1], origin[2]),
            window.get_size(),
        )
        return False

    def _current_monitor(self):
        """The monitor number this window is on, 0 if it cannot be said."""
        display = Gdk.Display.get_default()
        gdk_window = self.window.get_window()
        if display is None or gdk_window is None:
            return 0
        here = display.get_monitor_at_window(gdk_window)
        if here is None:
            return 0
        # By position rather than by identity: fullscreen_on_monitor() wants
        # a number, and two wrappers around the same monitor need not be the
        # same Python object.
        origin = here.get_geometry()
        for index in range(display.get_n_monitors()):
            monitor = display.get_monitor(index)
            if monitor is None:
                continue
            geometry = monitor.get_geometry()
            if (geometry.x, geometry.y) == (origin.x, origin.y):
                return index
        return 0

    def _build_head_window(self, title, holder):
        """A window that is nothing but one head of the guest.

        No chrome of any kind: the bar on the first monitor speaks for the
        whole session, and a second one would only be another thing sliding
        into view over the picture. Closing it or pressing the same key
        combination leaves fullscreen everywhere.
        """
        window = Gtk.Window(title=title)
        window.set_decorated(False)
        window.connect(
            "key-press-event", lambda _w, event: self.handle_key_press(event)
        )
        window.connect(
            "key-release-event", lambda _w, event: self.handle_key_release(event)
        )
        window.connect("delete-event", self._on_head_window_deleted)
        window.add(holder)
        return window

    def _on_head_window_deleted(self, *_args):
        self.leave()
        return True

    def _close_extra_windows(self):
        # Cleared even with no windows open: the claim is made before the
        # heads are asked for, so leaving full screen while the guest is
        # still answering has to withdraw it.
        console, self._spanning_console = self._spanning_console, None
        if not self._extra_windows:
            return
        windows, self._extra_windows = self._extra_windows, []
        for head, window in windows:
            window.destroy()
            # The window took the display widget down with it; this is what
            # lets the console forget it and the guest drop the head.
            if console is not None and hasattr(console, "release_head_display"):
                console.release_head_display(head)

    # -- reveal --------------------------------------------------------

    def _set_revealed(self, revealed):
        if self.revealer.get_reveal_child() != revealed:
            self.revealer.set_reveal_child(revealed)

    def _on_pin_toggled(self, button):
        if button.get_active():
            self._set_revealed(True)
        else:
            self._hide_at = time.monotonic() + 1.0

    def _poll(self):
        if not self.active:
            return False
        if self.pin.get_active():
            self._set_revealed(True)
            return True

        gdk_window = self.window.get_window()
        if gdk_window is None:
            return True
        seat = Gdk.Display.get_default().get_default_seat()
        pointer = seat.get_pointer() if seat is not None else None
        if pointer is None:
            return True
        _, x, y, _mask = gdk_window.get_device_position(pointer)

        # Spanning monitors, the position is still relative to this window,
        # so the top edge of the monitor next door reports the same y as
        # ours. Only the pointer actually over this window counts.
        if self.spanning and not 0 <= x <= gdk_window.get_width():
            return True

        if y <= HOT_ZONE:
            self._hide_at = None
            self._set_revealed(True)
            return True

        if self.revealer.get_reveal_child():
            height = self.revealer.get_allocated_height()
            if 0 <= y <= height + 4:
                self._hide_at = None
                return True
            if self._hide_at is None:
                self._hide_at = time.monotonic() + 0.4
            elif time.monotonic() >= self._hide_at:
                self._hide_at = None
                self._set_revealed(False)
        return True

    # -- keyboard ------------------------------------------------------

    CTRL_KEYS = (Gdk.KEY_Control_L, Gdk.KEY_Control_R)
    ALT_KEYS = (Gdk.KEY_Alt_L, Gdk.KEY_Alt_R, Gdk.KEY_Meta_L, Gdk.KEY_Meta_R)

    def handle_key_press(self, event):
        """Ctrl+Alt+Enter toggles; anything else disarms the release check."""
        keyval = event.keyval
        modifiers = event.state & Gtk.accelerator_get_default_mod_mask()
        wanted = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK

        if keyval in self.CTRL_KEYS or keyval in self.ALT_KEYS:
            # event.state describes the moment before this press, so fold in
            # the key now going down.
            ctrl = (
                bool(modifiers & Gdk.ModifierType.CONTROL_MASK)
                or keyval in self.CTRL_KEYS
            )
            alt = (
                bool(modifiers & Gdk.ModifierType.MOD1_MASK) or keyval in self.ALT_KEYS
            )
            if ctrl and alt:
                self._ungrab_armed = True
        else:
            self._ungrab_armed = False

        if modifiers == wanted and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.toggle()
            return True
        return False

    def handle_key_release(self, event):
        """Ctrl+Alt pressed and released alone hands input back."""
        if not self._ungrab_armed:
            return False
        if event.keyval not in self.CTRL_KEYS + self.ALT_KEYS:
            return False
        self._ungrab_armed = False
        console = self.get_console()
        if console is not None and hasattr(console, "release_input"):
            console.release_input()
        return False

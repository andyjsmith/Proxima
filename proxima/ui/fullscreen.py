"""Fullscreen console handling, shared by the main window and pop-outs.

The window goes fullscreen and its chrome is hidden, rather than reparenting
the console into a new toplevel: moving a SpiceDisplay between toplevels
destroys and rebuilds its GdkWindow underneath a live connection.

A floating bar sits at the top edge. Unpinned it hides itself and returns
when the pointer reaches the top. Reveal is polled rather than driven by
motion events, because the console owns the pointer while focused --
spice-gtk grabs it outright in server-mouse mode -- so motion-notify on this
side is not reliable.
"""

import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib  # noqa: E402

HOT_ZONE = 2          # px from the top edge that re-reveals the bar
POLL_MS = 120


class FullscreenController:
    def __init__(self, window, overlay, get_console, chrome,
                 on_ctrl_alt_del=None, on_enter=None, on_leave=None,
                 title=""):
        self.window = window
        self.overlay = overlay
        self.get_console = get_console
        self.chrome = chrome            # callable returning widgets to hide
        self.on_ctrl_alt_del = on_ctrl_alt_del or (lambda: None)
        self.on_enter = on_enter or (lambda: None)
        self.on_leave = on_leave or (lambda: None)

        self.active = False
        self._poll_source = None
        self._hide_at = None
        self._hidden = []
        self._ungrab_armed = False

        self.overlay.add_overlay(self._build_bar())
        self.set_title(title)

    # -- bar -----------------------------------------------------------

    def _build_bar(self):
        self.revealer = Gtk.Revealer()
        self.revealer.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_DOWN)
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

        bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL),
                       False, False, 2)

        keys = Gtk.Button(label="Ctrl+Alt+Del")
        keys.set_relief(Gtk.ReliefStyle.NONE)
        keys.connect("clicked", lambda *_: self.on_ctrl_alt_del())
        bar.pack_start(keys, False, False, 0)

        self.pin = Gtk.ToggleButton()
        self.pin.set_relief(Gtk.ReliefStyle.NONE)
        self.pin.add(Gtk.Image.new_from_icon_name("view-pin-symbolic",
                                                  Gtk.IconSize.MENU))
        self.pin.set_tooltip_text("Keep this bar visible")
        self.pin.connect("toggled", self._on_pin_toggled)
        bar.pack_start(self.pin, False, False, 0)

        leave = Gtk.Button()
        leave.set_relief(Gtk.ReliefStyle.NONE)
        leave.add(Gtk.Image.new_from_icon_name("view-restore-symbolic",
                                               Gtk.IconSize.MENU))
        leave.set_tooltip_text("Exit full screen (Ctrl+Alt+Enter)")
        leave.connect("clicked", lambda *_: self.leave())
        bar.pack_start(leave, False, False, 0)

        self.revealer.add(bar)
        bar.show_all()
        return self.revealer

    def set_title(self, title):
        self.title_label.set_text(title or "")

    # -- enter / leave -------------------------------------------------

    def toggle(self):
        self.leave() if self.active else self.enter()

    def enter(self):
        if self.active or self.get_console() is None:
            return
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
        self.window.fullscreen()

        self._poll_source = GLib.timeout_add(POLL_MS, self._poll)
        self._hide_at = time.monotonic() + 2.0

        if console is not None and hasattr(console, "grab_focus_display"):
            GLib.idle_add(console.grab_focus_display)

    def leave(self):
        if not self.active:
            return
        self.active = False
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
        _, _x, y, _mask = gdk_window.get_device_position(pointer)

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
    ALT_KEYS = (Gdk.KEY_Alt_L, Gdk.KEY_Alt_R,
                Gdk.KEY_Meta_L, Gdk.KEY_Meta_R)

    def handle_key_press(self, event):
        """Ctrl+Alt+Enter toggles; anything else disarms the release check."""
        keyval = event.keyval
        modifiers = event.state & Gtk.accelerator_get_default_mod_mask()
        wanted = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK

        if keyval in self.CTRL_KEYS or keyval in self.ALT_KEYS:
            # event.state describes the moment before this press, so fold in
            # the key now going down.
            ctrl = (bool(modifiers & Gdk.ModifierType.CONTROL_MASK)
                    or keyval in self.CTRL_KEYS)
            alt = (bool(modifiers & Gdk.ModifierType.MOD1_MASK)
                   or keyval in self.ALT_KEYS)
            if ctrl and alt:
                self._ungrab_armed = True
        else:
            self._ungrab_armed = False

        if (modifiers == wanted
                and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter)):
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

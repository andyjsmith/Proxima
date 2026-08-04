"""Notebook tab labels for console pages.

The protocol icon marks VNC tabs as distinct from SPICE ones, since VNC has
no guest resize, clipboard sharing or audio.
"""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk


class ConsoleTabLabel(Gtk.EventBox):
    """Protocol icon, title, and a close button."""

    def __init__(self, title, protocol, on_close):
        # An EventBox, not a plain Box: a Box has no GdkWindow of its own, so
        # middle clicks on the tab would never reach a handler.
        super().__init__()
        self.set_visible_window(False)
        self.on_close = on_close
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("button-press-event", self._on_button_press)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.add(box)

        self.icon = Gtk.Image.new_from_icon_name(
            "video-display-symbolic", Gtk.IconSize.MENU
        )
        box.pack_start(self.icon, False, False, 0)

        # No ellipsizing. An ellipsizing label reports a minimum width of
        # roughly nothing, so the notebook happily shrinks it to "..." once
        # the icon and close button have taken their share. Sizing to the
        # text instead makes the tab as wide as its contents need.
        self.label = Gtk.Label(label=self._fit(title))
        box.pack_start(self.label, False, False, 0)
        self.set_protocol(protocol)

        close = Gtk.Button()
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_focus_on_click(False)
        close.get_style_context().add_class("tab-close")
        close.add(
            Gtk.Image.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        )
        close.set_tooltip_text("Close")
        close.connect("clicked", lambda *_: on_close())
        box.pack_start(close, False, False, 0)

        self.show_all()

    def _on_button_press(self, _widget, event):
        """Middle click closes the tab, as in every browser."""
        if event.button == 2:
            self.on_close()
            return True
        return False

    MAX_CHARS = 28

    @classmethod
    def _fit(cls, title):
        """Trim only genuinely long names; the tooltip keeps the full one."""
        if len(title) <= cls.MAX_CHARS:
            return title
        return title[: cls.MAX_CHARS - 3] + "..."

    def set_title(self, title):
        self.label.set_text(self._fit(title))

    def set_protocol(self, protocol):
        """Say which kind of console the tab holds, once one exists.

        The same icon either way. A warning triangle on every VNC tab
        overstates it -- VNC is a working console, just a plainer one --
        and the status bar already names the protocol in the corner.
        """
        if protocol == "vnc":
            tooltip = "VNC: no guest resize, clipboard or audio"
        elif protocol:
            tooltip = "SPICE"
        else:
            tooltip = "No console open"
        self.icon.set_tooltip_text(tooltip)
        self.label.set_tooltip_text(tooltip)

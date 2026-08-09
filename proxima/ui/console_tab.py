"""Notebook tab labels for console pages.

The protocol icon marks VNC tabs as distinct from SPICE ones, since VNC has
no guest resize, clipboard sharing or audio.
"""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk


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
        self._icon_name = "video-display-symbolic"
        # Set by set_icon(), after which the protocol no longer chooses the
        # picture. False here so the constructor's own set_protocol() lands.
        self._fixed_icon = False
        box.pack_start(self.icon, False, False, 0)

        # No ellipsizing. An ellipsizing label reports a minimum width of
        # roughly nothing, so the notebook happily shrinks it to "..." once
        # the icon and close button have taken their share. Sizing to the
        # text instead makes the tab as wide as its contents need.
        self.label = Gtk.Label()
        # Whether this is the tab the toolbar and menus are aimed at. Only
        # ever true of one tab, and only worth saying at all once the window
        # is split -- with one pane the tab in front is the one in front.
        self._current = False
        self._title = self._fit(title)
        self._render()
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
        self._title = self._fit(title)
        self._render()

    def set_current(self, current):
        """Mark this as the tab the window is acting on, or stop.

        Split into panes, "the tab in front" stops being a single thing --
        every pane has one -- and nothing on screen said which of them the
        power buttons and the View menu were aimed at. This is that.
        """
        current = bool(current)
        if current == self._current:
            return
        self._current = current
        self._render()

    def _render(self):
        # Bold rather than a colour: it survives every theme, and it does
        # not compete with the tab strip's own idea of which tab is
        # selected, which is a different question with more than one pane.
        text = GLib.markup_escape_text(self._title)
        self.label.set_markup(f"<b>{text}</b>" if self._current else text)

    def set_icon(self, pixbuf, tooltip=""):
        """Wear a given picture instead of one of the protocol icons.

        A node's tab uses this to show exactly what the tree shows for that
        node, colour and all. It also switches set_protocol() off for this
        label: what tab this is does not change because a shell was opened
        in it, and a terminal icon in the tab strip would say it had.
        """
        self._fixed_icon = True
        self._icon_name = None
        if pixbuf is not None:
            self.icon.set_from_pixbuf(pixbuf)
        self.icon.set_tooltip_text(tooltip)
        self.label.set_tooltip_text(tooltip)

    def set_protocol(self, protocol):
        """Say which kind of console the tab holds, once one exists.

        The same icon for SPICE and VNC. A warning triangle on every VNC tab
        overstates it -- VNC is a working console, just a plainer one -- and
        the status bar already names the protocol in the corner. A serial
        console does get its own icon, because it is not a lesser version of
        the same thing: it is text rather than a picture, and which one a tab
        holds decides whether the mouse works in it at all.

        Ignored entirely on a label that has been given an icon of its own;
        see set_icon.
        """
        if self._fixed_icon:
            return
        if protocol == "serial":
            icon = "utilities-terminal-symbolic"
            tooltip = "Serial console: text only, with selectable output"
        elif protocol == "vnc":
            icon = "video-display-symbolic"
            tooltip = "VNC: no guest resize, clipboard or audio"
        elif protocol:
            icon = "video-display-symbolic"
            tooltip = "SPICE"
        else:
            icon = "video-display-symbolic"
            tooltip = "No console open"
        if icon != self._icon_name:
            self.icon.set_from_icon_name(icon, Gtk.IconSize.MENU)
            self._icon_name = icon
        self.icon.set_tooltip_text(tooltip)
        self.label.set_tooltip_text(tooltip)

"""The panel shown over a console whose connection has ended.

A remote display that loses its connection keeps showing the last frame it
received, which is indistinguishable from a guest that has simply stopped
redrawing. Powering a guest off or rolling it back leaves exactly that: a
frozen picture of a machine that no longer exists. This says what happened
and offers the way back.
"""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib  # noqa: E402

try:
    import cairo
except ImportError:                       # pragma: no cover
    cairo = None


def draw_offline_effect(context, width, height, dim=0.55):
    """Grey out and dim whatever has already been drawn.

    HSL_SATURATION takes the saturation of the source and the hue and
    luminosity of the destination, so painting flat grey through it leaves a
    correctly weighted greyscale rather than the muddy result of averaging
    channels. The dim pass afterwards is what makes the panel on top
    readable.
    """
    if cairo is None:
        return
    context.save()
    context.rectangle(0, 0, width, height)
    context.clip()
    context.set_operator(cairo.Operator.HSL_SATURATION)
    context.set_source_rgb(0.5, 0.5, 0.5)
    context.paint()
    context.set_operator(cairo.Operator.OVER)
    context.set_source_rgba(0, 0, 0, dim)
    context.paint()
    context.restore()


class ConsoleStatusPanel(Gtk.Box):
    """Centred message over the console, with a Reconnect button."""

    def __init__(self, on_reconnect=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.on_reconnect = on_reconnect or (lambda: None)

        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.get_style_context().add_class("console-status")
        self.set_no_show_all(True)

        self.icon = Gtk.Image.new_from_icon_name(
            "network-offline-symbolic", Gtk.IconSize.DIALOG)
        self.pack_start(self.icon, False, False, 0)

        self.title = Gtk.Label()
        self.title.get_style_context().add_class("console-status-title")
        self.pack_start(self.title, False, False, 0)

        self.detail = Gtk.Label()
        self.detail.set_line_wrap(True)
        self.detail.set_max_width_chars(52)
        self.detail.set_justify(Gtk.Justification.CENTER)
        self.pack_start(self.detail, False, False, 0)

        # Reconnect sits with any extra choices the caller adds, so a panel
        # offering "take it over or use VNC" reads as one row of options
        # rather than one button and a stray afterthought.
        self.buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                               spacing=8)
        self.buttons.set_halign(Gtk.Align.CENTER)
        self.pack_start(self.buttons, False, False, 0)

        self.reconnect_button = Gtk.Button(label="Reconnect")
        self.reconnect_button.connect("clicked",
                                      lambda *_: self.on_reconnect())
        self.buttons.pack_start(self.reconnect_button, False, False, 0)

        self._extra = []

        for child in (self.icon, self.title, self.detail, self.buttons,
                      self.reconnect_button):
            child.show()

    def show_message(self, title, detail="", icon="network-offline-symbolic",
                     can_reconnect=True, actions=None):
        """Put a message over the console.

        'actions' is a list of (label, callback) shown beside Reconnect. The
        panel owns them: each call clears the previous set, so a panel
        reused for a different situation cannot leave a button behind that
        does something from two disconnections ago.
        """
        for button in self._extra:
            self.buttons.remove(button)
            button.destroy()
        self._extra = []

        self.title.set_markup(f"<b>{GLib.markup_escape_text(str(title))}</b>")
        self.detail.set_text(detail or "")
        self.detail.set_visible(bool(detail))
        self.icon.set_from_icon_name(icon, Gtk.IconSize.DIALOG)
        self.reconnect_button.set_visible(can_reconnect)

        for label, callback in actions or ():
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda _b, fn=callback: fn())
            self.buttons.pack_start(button, False, False, 0)
            button.show()
            self._extra.append(button)

        self.buttons.set_visible(can_reconnect or bool(self._extra))
        self.show()

    def hide_message(self):
        self.hide()

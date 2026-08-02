"""Status bar indicators.

Three states rather than two, because "the guest cannot do this" and "you
turned this off" are different answers to the same question and used to look
identical:

    available     full strength, no marking
    switched off  struck through -- a deliberate choice, and reversible here
    unsupported   dimmed, as before

A toggleable indicator is also a control: clicking it is how clipboard
sharing and guest audio are turned on and off, which puts the switch on the
thing that reports the state rather than three menus away from it.
"""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402


class StatusIndicator(Gtk.Box):
    """One status bar icon: an image, a tooltip, and optionally a click.

    A toggleable one wraps its icon in a real flat Gtk.Button. That is not
    decoration: a button brings its own hover feedback, keyboard access and
    -- the reason a hand-rolled EventBox will not do -- its own GdkWindow. A
    non-windowed EventBox returns the *toplevel's* window from get_window(),
    so setting a pointer cursor on it sets it for the entire application
    window rather than for the icon.
    """

    # Opacity for each state, matching what the plain indicators used.
    OPACITY_ON = 1.0
    OPACITY_OFF = 0.75          # switched off, but by choice -- still legible
    OPACITY_UNSUPPORTED = 0.25

    def __init__(self, icon_name, label, on_toggle=None):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.label_text = label
        self.on_toggle = on_toggle
        self._struck = False
        self._supported = None

        self.image = Gtk.Image.new_from_icon_name(icon_name,
                                                  Gtk.IconSize.MENU)
        # After the default handler, so the strike lands on top of the icon
        # rather than under it.
        self.image.connect_after("draw", self._on_draw)

        if on_toggle is None:
            self.button = None
            self.pack_start(self.image, False, False, 0)
        else:
            self.button = Gtk.Button()
            self.button.set_relief(Gtk.ReliefStyle.NONE)
            self.button.set_focus_on_click(False)
            self.button.get_style_context().add_class("status-toggle")
            self.button.add(self.image)
            self.button.connect("clicked", lambda *_: self.on_toggle())
            self.pack_start(self.button, False, False, 0)

    def set_icon_name(self, icon_name):
        self.image.set_from_icon_name(icon_name, Gtk.IconSize.MENU)

    def set_tooltip_text(self, text):
        # On the button too, so hovering the clickable area explains itself.
        super().set_tooltip_text(text)
        if self.button is not None:
            self.button.set_tooltip_text(text)

    def set_state(self, supported, tooltip, enabled=True, can_toggle=None):
        """supported: True on, False off, None not applicable.

        'enabled' is the user's own switch, drawn as a strike. 'can_toggle'
        says whether clicking would mean anything right now -- a VNC console
        has no audio to switch off, so the button goes insensitive rather
        than accepting a click that could not do anything.
        """
        self._supported = supported
        self._struck = not enabled
        if supported is None:
            self.set_opacity(self.OPACITY_UNSUPPORTED)
        elif not enabled:
            self.set_opacity(self.OPACITY_OFF)
        elif supported:
            self.set_opacity(self.OPACITY_ON)
        else:
            self.set_opacity(0.45)
        self.set_tooltip_text(tooltip)
        if self.button is not None:
            if can_toggle is None:
                can_toggle = supported is not None
            self.button.set_sensitive(bool(can_toggle))
        self.queue_draw()

    @property
    def can_toggle(self):
        return self.button is not None and self.button.get_sensitive()

    @property
    def struck(self):
        return self._struck

    def _on_draw(self, widget, context):
        """Strike the icon corner to corner when it is switched off."""
        if not self._struck:
            return False
        allocation = widget.get_allocation()
        width, height = allocation.width, allocation.height
        if width <= 0 or height <= 0:
            return False

        colour = widget.get_style_context().get_color(widget.get_state_flags())
        inset = 1.5
        context.save()
        # A dark backing line under the bright one, so the strike stays
        # visible over both light and dark parts of an icon.
        for rgba, line_width in (((0, 0, 0, 0.55), 3.0),
                                 ((colour.red, colour.green, colour.blue,
                                   1.0), 1.6)):
            context.set_source_rgba(*rgba)
            context.set_line_width(line_width)
            context.set_line_cap(1)     # cairo.LINE_CAP_ROUND
            context.move_to(inset, height - inset)
            context.line_to(width - inset, inset)
            context.stroke()
        context.restore()
        return False

"""A console in a window of its own.

Keeps the power and snapshot controls so the guest is still manageable, but
drops the inventory tree -- the point of popping out is to give the guest a
monitor to itself.

Closing the window returns the console to a tab in the main window rather
than disconnecting it. Tearing a console out and shutting the window should
not be a way to lose a session by accident; the tab's own close button is
what ends it.
"""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib  # noqa: E402

from ..theme import decorate as theme_decorate
from . import actions as action_defs
from .fullscreen import FullscreenController
from .snapshots import describe_revert


class ConsoleWindow(Gtk.Window):
    def __init__(self, main_window, console, guest):
        super().__init__(title=f"{guest.name} - {guest.node}")
        self.main = main_window
        self.console = console
        self.guest_key = guest.key

        self.set_default_size(1100, 760)
        self.connect("delete-event", self._on_delete)
        self.connect("key-press-event", self._on_key_press)
        self.connect("key-release-event", self._on_key_release)
        self.connect("notify::is-active", self._on_active_changed)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(root)

        self.toolbar = self._build_toolbar()
        root.pack_start(self.toolbar, False, False, 0)

        self.overlay = Gtk.Overlay()
        self.overlay.add(console)
        root.pack_start(self.overlay, True, True, 0)

        self.fullscreen_control = FullscreenController(
            window=self, overlay=self.overlay,
            get_console=lambda: self.console,
            chrome=lambda: [self.toolbar],
            on_ctrl_alt_del=self._send_ctrl_alt_del,
            title=guest.name)

        theme_decorate(self)
        self.update_sensitivity()

    # -- chrome --------------------------------------------------------

    def _build_toolbar(self):
        bar = Gtk.Toolbar()
        bar.set_style(Gtk.ToolbarStyle.BOTH_HORIZ)
        bar.set_icon_size(Gtk.IconSize.SMALL_TOOLBAR)

        self._action_items = {}
        for name in action_defs.TOOLBAR_ACTIONS:
            action = action_defs.ACTIONS_BY_NAME[name]
            item = Gtk.ToolButton()
            item.set_label(action.label)
            item.set_icon_name(action.icon)
            item.set_tooltip_text(action.tooltip)
            item.set_is_important(name in ("start", "shutdown"))
            item.set_sensitive(False)
            item.connect(
                "clicked",
                lambda _b, which=name: self.main.run_action_for(
                    self.guest_key, which))
            self._action_items[name] = item
            bar.insert(item, -1)

        bar.insert(Gtk.SeparatorToolItem(), -1)

        self._snapshot_items = {}
        for which, label, icon, tooltip in (
                ("take", "Snapshot", "list-add-symbolic", "Take a snapshot"),
                ("revert", "Revert", "edit-undo-symbolic",
                 "Roll back to the most recent snapshot"),
                ("manage", "Manage", "document-open-symbolic",
                 "Manage snapshots")):
            item = Gtk.ToolButton()
            item.set_label(label)
            item.set_icon_name(icon)
            item.set_tooltip_text(tooltip)
            item.set_sensitive(False)
            item.connect(
                "clicked",
                lambda _b, w=which: self.main.snapshot_action_for(
                    self.guest_key, w))
            self._snapshot_items[which] = item
            bar.insert(item, -1)

        bar.insert(Gtk.SeparatorToolItem(), -1)

        keys = Gtk.ToolButton()
        keys.set_label("Ctrl+Alt+Del")
        keys.set_icon_name("input-keyboard-symbolic")
        keys.set_tooltip_text("Send Ctrl+Alt+Del to the guest")
        keys.connect("clicked", lambda *_: self._send_ctrl_alt_del())
        bar.insert(keys, -1)

        full = Gtk.ToolButton()
        full.set_label("Full Screen")
        full.set_icon_name("view-fullscreen-symbolic")
        full.set_tooltip_text("Full screen (Ctrl+Alt+Enter)")
        full.connect("clicked", lambda *_: self.fullscreen_control.toggle())
        bar.insert(full, -1)

        spacer = Gtk.SeparatorToolItem()
        spacer.set_draw(False)
        spacer.set_expand(True)
        bar.insert(spacer, -1)

        back = Gtk.ToolButton()
        back.set_label("Return to Tabs")
        back.set_icon_name("view-restore-symbolic")
        back.set_tooltip_text("Put this console back in the main window")
        back.connect("clicked", lambda *_: self.return_to_tabs())
        bar.insert(back, -1)

        return bar

    # -- state ---------------------------------------------------------

    def update_sensitivity(self):
        guest = self.main.sidebar.guests.get(self.guest_key)
        for name, item in self._action_items.items():
            action = action_defs.resolve(name, guest)
            item.set_sensitive(action_defs.enabled_for(action, guest))
            if name == "start":
                item.set_label(action.label)
                item.set_tooltip_text(action.tooltip)
        can_snapshot = guest is not None and not guest.template
        for which, item in self._snapshot_items.items():
            item.set_sensitive(can_snapshot)
        latest = guest.latest_snapshot if guest else None
        revert = self._snapshot_items["revert"]
        revert.set_sensitive(bool(can_snapshot and latest))
        revert.set_tooltip_text(describe_revert(latest))

    def replace_console(self, console):
        """Swap in a rebuilt console without disturbing the window."""
        if self.console is not None:
            self.overlay.remove(self.console)
        self.console = console
        self.overlay.add(console)
        console.show_all()
        self.update_sensitivity()

    def _on_active_changed(self, *_args):
        """Alt-tabbing away must not leave the guest holding the keyboard."""
        if self.is_active():
            return
        if self.console is not None and hasattr(self.console, "release_input"):
            self.console.release_input()

    def _send_ctrl_alt_del(self):
        if self.console is not None:
            self.console.send_ctrl_alt_del()

    # -- lifecycle -----------------------------------------------------

    def _on_key_press(self, _widget, event):
        return self.fullscreen_control.handle_key_press(event)

    def _on_key_release(self, _widget, event):
        return self.fullscreen_control.handle_key_release(event)

    def return_to_tabs(self):
        """Hand the console back to the main window's notebook."""
        if self.console is None:
            return
        console, self.console = self.console, None
        self.fullscreen_control.leave()
        self.overlay.remove(console)
        self.main.reclaim_console(self.guest_key, console)
        self.destroy()

    def _on_delete(self, *_args):
        self.return_to_tabs()
        return True     # return_to_tabs destroys the window itself

    def shutdown(self):
        """Called when the application is closing down."""
        self.fullscreen_control.stop()
        self.console = None

"""The main window: menu bar, toolbar, inventory sidebar, console notebook.

Layout follows VMware Workstation closely -- inventory on the left, tabbed
consoles on the right, power controls on the toolbar and duplicated in the
sidebar's context menu.

Threading rule for the whole file: every call that touches the network runs
on a worker thread and comes back through GLib.idle_add. Nothing else is
allowed to block the main loop, because the main loop is also what draws the
console.
"""

import os
import subprocess
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from .. import APP_NAME, __version__, secrets
from ..api import AuthError, ProxmoxError
from ..api import notes as notes_meta
from ..api.connection import FAILED, Connection, ConnectionManager
from ..api.models import audio_is_spice, valid_guest_name, vga_is_spice
from ..console import SPICE_AVAILABLE, SpiceConsole, VncConsole
from ..console.placeholder import PlaceholderConsole
from ..console.spice import IMAGE_COMPRESSION, VIDEO_CODECS
from ..theme import apply as apply_theme
from ..theme import css as theme_css
from ..theme import decorate as theme_decorate
from ..theme import keep_active as theme_keep_active
from ..theme.system import DarkModeWatcher
from . import actions as action_defs
from .clone import CloneDialog
from .console_tab import ConsoleTabLabel
from .console_window import ConsoleWindow
from .fullscreen import FullscreenController
from .indicators import StatusIndicator
from .login_dialog import run_login
from .settings_dialog import SettingsDialog
from .sidebar import Sidebar
from .snapshots import SnapshotManager, TakeSnapshotDialog, describe_revert
from .split import MAX_PANES, SplitView
from .summary import SummaryPage
from .task_feed import TaskFeed
from .vm_settings import VMSettingsDialog

# Distinguishes "work it out yourself" from "explicitly no console".
_CURRENT = object()


class _PendingChange:
    """One change asked for, and what would show that it happened.

    Proxmox reports an action seconds after accepting it, so a click has to
    leave some visible trace in the meantime. This is that trace: the guest
    spins until the inventory backs it up.

    Resolution is deliberately generous, because the only unacceptable
    outcome is a row that spins for ever:

      * the watched field reaching the value that was asked for  -- done;
      * the watched field changing to anything else -- somebody else got
        there first, or the guest went its own way, and either way what is
        on screen is now the truth;
      * a deadline -- covers a task that failed silently, a rename undone in
        the web UI before we ever saw it land, and anything else that means
        the value we are waiting for is never coming.

    A change whose target is already the current value never starts, so
    renaming a guest to the name it already has cannot begin a wait that
    nothing could end.
    """

    __slots__ = ("deadline", "field", "label", "name", "target", "was")

    def __init__(self, field, was, target, label, deadline, name=None):
        self.field = field  # "name" or "status"
        self.was = was
        self.target = target  # None when any change will do
        self.label = label  # shown in the row's tooltip
        self.deadline = deadline
        self.name = name  # name to display meanwhile, if any

    def resolved_by(self, guest, now):
        if guest is None or now >= self.deadline:
            return True
        current = getattr(guest, self.field, None)
        if self.target is not None and current == self.target:
            return True
        return current != self.was


class MainWindow(Gtk.Window):
    def __init__(self, config, on_disconnect=None):
        super().__init__(title=APP_NAME)
        self.config = config
        self.on_disconnect = on_disconnect or (lambda: None)
        self.connections = ConnectionManager()

        self.consoles = {}  # guest key -> console widget
        self._poll_source = None
        self._poll_busy = False
        self._closing = False
        self._action_items = {}  # action name -> [widgets]
        self._updating_view_menu = False
        # Set while a pane toggle is being put back in step with its pane,
        # so doing so does not read as a click on it.
        self._syncing_toggles = False
        self._telemetry_source = None
        self._popouts = {}  # guest key -> ConsoleWindow
        self._popout_pages = {}  # guest key -> notebook index
        self._console_offline = {}  # guest key -> status when it stopped
        self._burst_source = None
        self._burst_until = 0.0
        self._folder_scan = False
        # guest key -> (status when asked, deadline). Purely client side.
        self._pending_actions = {}
        # Changes asked for that the cluster has not reported yet, drawn as
        # a spinner on the guest's row. See _mark_busy.
        self._busy = {}  # guest key -> _PendingChange
        # Guest keys whose console was deliberately switched protocol for
        # this session. Cleared when the tab closes, so the next open goes
        # back to whatever the guest's own settings ask for. These override
        # both the per-VM protocol setting and the global "always use VNC" --
        # that is what makes them useful as a way out of a bad console.
        self._force_vnc = set()
        self._force_spice = set()
        # Clipboard and audio switched from the status bar, per guest, for
        # as long as the console is open. Deliberately not persisted: the
        # buttons are helpers for the session in front of you, and the
        # settled answer belongs in the guest's own settings.
        self._session_switches = {}  # (guest key, name) -> bool
        # When we last tore down a live SPICE session, per guest. QEMU keeps
        # counting a client for a moment after it goes, so without this
        # every reconnect would look like somebody else was already on it.
        self._recent_spice = {}  # guest key -> monotonic time
        # Consoles to reopen from the last session, and when to give up
        # waiting for their guests to appear in the inventory.
        self._restore_keys = []
        self._restore_until = 0.0
        # The notebook emits "switch-page" while the window is still being
        # assembled, before the status bar and menus exist. Handlers that
        # touch them must wait until construction finishes.
        self._ready = False

        # Restored in two parts: the size the window has when it is not
        # maximised, and whether it was maximised. Setting the default size
        # first means unmaximising lands on last session's size rather than
        # on GTK's idea of a minimum.
        self._normal_size = (
            int(config.get("window_width", 1280)),
            int(config.get("window_height", 800)),
        )
        self._maximized = bool(config.get("window_maximized", False))
        self._fullscreen_state = False
        self.set_default_size(*self._normal_size)
        if self._maximized:
            self.maximize()
        self.connect("window-state-event", self._on_window_state)
        self.connect("configure-event", self._on_configure)
        self.connect("delete-event", self._on_delete)
        self.connect("realize", lambda *_: self.apply_appearance())
        # Clicking into a console on a second monitor, or alt-tabbing to a
        # terminal, must not grey the whole application out.
        theme_keep_active(self)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(root)

        # The menu bar is built first either way, because with a header bar
        # it goes inside it. GTK decides between its own titlebar and the
        # window manager's when the window is created and will not change
        # its mind later, so that has to be settled here rather than later.
        self.menubar = self._build_menubar()
        self.toolbar = self._build_toolbar()

        self.header_bar = None
        if config.get("use_header_bar"):
            self.header_bar = self._build_header_bar()
            self.set_titlebar(self.header_bar)
        else:
            root.pack_start(self.menubar, False, False, 0)
        root.pack_start(self.toolbar, False, False, 0)

        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.set_position(int(config.get("sidebar_width", 280)))

        self.sidebar = Sidebar(
            row_ypad=theme_css.ROW_YPAD,
            name_format=config.get("tree_name_format", "name"),
            templates_last=bool(config.get("templates_last", True)),
            dnd_enabled=bool(config.get("enable_dnd", True)),
        )
        self.sidebar.connect("guest-selected", self._on_guest_selected)
        self.sidebar.connect("guest-activated", self._on_guest_activated)
        self.sidebar.connect("guest-action", self._on_guest_action)
        self.sidebar.connect("filter-changed", lambda *_: self.refresh())
        self.sidebar.connect("bulk-action", self._on_bulk_action)
        self.sidebar.connect("view-changed", self._on_view_changed)
        self.sidebar.connect("connect-requested", lambda *_: self.open_connect_dialog())
        self.sidebar.connect(
            "disconnect-requested", lambda _s, cid: self.disconnect_connection(cid)
        )
        self.sidebar.connect(
            "reconnect-requested", lambda _s, cid: self.reconnect_connection(cid)
        )
        self.sidebar.connect(
            "guest-moved", lambda _s, key, path: self.move_guest_to_folder(key, path)
        )
        self.sidebar.connect(
            "new-subfolder", lambda _s, key: self.prompt_new_subfolder(key)
        )
        self.sidebar.connect(
            "guest-renamed", lambda _s, key, name: self.rename_guest(key, name)
        )
        self.sidebar.connect("clone-requested", lambda _s, key: self.clone_guest(key))
        self.sidebar.connect("delete-requested", lambda _s, key: self.delete_guest(key))
        self.sidebar.connect(
            "settings-requested", lambda _s, key: self.open_guest_settings(key)
        )
        self.paned.pack1(self.sidebar, False, False)
        self.sidebar.set_visible(bool(config.get("sidebar_visible", True)))

        # Up to four console panes. self.notebook is the first of them: it
        # holds the Summary page and is where consoles open unless another
        # pane is in front.
        self.panes = SplitView()
        self.panes.connect("page-switched", self._on_page_switched)
        self.panes.connect("panes-changed", lambda *_: self._sync_view_menu())
        self.notebook = self.panes.primary

        self.summary = SummaryPage(on_open_console=self.open_console_selected)
        self.sidebar.folder_view = bool(config.get("folder_view", False))
        self.sidebar._update_view_button()
        # Through the split view, so the page is actually shown: the
        # notebooks carry no-show-all, so appending alone would leave the
        # Summary invisible and the pane reporting no current page.
        self.panes.append(
            self.summary, Gtk.Label(label="Summary"), notebook=self.notebook
        )
        # The Summary is not a console: it belongs to the first pane and
        # should not be draggable into one of the others.
        self.notebook.set_tab_detachable(self.summary, False)
        self.notebook.set_tab_reorderable(self.summary, False)

        # The panes sit under an overlay so the fullscreen bar can float
        # above the console without taking layout space from it.
        self.console_overlay = Gtk.Overlay()
        self.console_overlay.add(self.panes)
        self.paned.pack2(self.console_overlay, True, False)

        self.fullscreen_control = FullscreenController(
            window=self,
            overlay=self.console_overlay,
            get_console=self.current_console,
            chrome=lambda: [
                self.menubar,
                self.toolbar,
                self.statusbar_box,
                self.sidebar,
                self.task_feed,
            ],
            on_ctrl_alt_del=self._send_ctrl_alt_del,
            on_enter=lambda: self.panes.set_show_tabs(False),
            on_leave=lambda: self.panes.set_show_tabs(True),
        )

        root.pack_start(self.paned, True, True, 0)

        # The pane also closes from its own X, which has to leave the
        # toolbar button showing the truth.
        self.task_feed = TaskFeed(
            self.connections,
            on_closed=lambda: self._set_toggle(self.tasks_tool_item, False),
        )
        root.pack_start(self.task_feed, False, False, 0)

        self.statusbar_box = self._build_statusbar()
        root.pack_start(self.statusbar_box, False, False, 0)

        self.connect("key-press-event", self._on_key_press)
        self.connect("key-release-event", self._on_key_release)
        # Alt-tabbing away must not leave the guest holding the keyboard.
        self.connect("notify::is-active", self._on_active_changed)

        self._dark_watcher = DarkModeWatcher(self._on_system_theme_changed)

        self._begin_session_restore()

        self.set_status("No connections. Use File > Connect.")
        self._agent_ok = False
        self.refresh(initial=True)
        self._schedule_poll()
        self._telemetry_source = GLib.timeout_add_seconds(1, self._sample_telemetry)
        self._ready = True
        self._sync_view_menu()
        self._context_changed()

    # ------------------------------------------------------------------
    # Chrome
    # ------------------------------------------------------------------

    def _build_header_bar(self):
        """A GTK-drawn titlebar, so the frame is themed like the rest.

        The menus move into it rather than being duplicated there: two menu
        bars would be two sources of truth for what is enabled, and the row
        the menu bar used to occupy is the height this was meant to save.
        Everything else stays exactly where it is on the toolbar -- a header
        bar that also rearranged the toolbar would be much harder to take
        back out, and taking it back out is an explicitly supported outcome
        here. See docs/header-bar.md.
        """
        bar = Gtk.HeaderBar()
        bar.set_show_close_button(True)
        # Stated rather than inherited. The layout comes from
        # gtk-decoration-layout, which on Windows is frequently unset --
        # and an unset layout is how a header bar ends up with no close
        # button at all, which is exactly what happened the first time.
        bar.set_decoration_layout(":minimize,maximize,close")
        bar.set_title(APP_NAME)

        bar.pack_start(self.menubar)

        refresh = Gtk.Button.new_from_icon_name(
            "view-refresh-symbolic", Gtk.IconSize.MENU
        )
        refresh.set_tooltip_text("Refresh inventory (F5)")
        refresh.connect("clicked", lambda *_: self.refresh())
        bar.pack_start(refresh)

        settings = Gtk.Button.new_from_icon_name(
            "preferences-system-symbolic", Gtk.IconSize.MENU
        )
        settings.set_tooltip_text("Preferences")
        settings.connect("clicked", lambda *_: self.open_settings())
        bar.pack_end(settings)
        return bar

    def _build_menubar(self):
        bar = Gtk.MenuBar()

        file_menu = Gtk.Menu()
        file_item = Gtk.MenuItem(label="_File", use_underline=True)
        file_item.set_submenu(file_menu)
        self._menu_item(
            file_menu, "Connect...", self.open_connect_dialog, accel="<Control>n"
        )
        self._menu_item(file_menu, "Refresh Inventory", self.refresh, accel="F5")
        self._menu_item(file_menu, "Preferences...", self.open_settings)
        file_menu.append(Gtk.SeparatorMenuItem())
        self.disconnect_menu = Gtk.Menu()
        disconnect_item = Gtk.MenuItem(label="Disconnect")
        disconnect_item.set_submenu(self.disconnect_menu)
        disconnect_item.set_sensitive(False)
        self.disconnect_item = disconnect_item
        file_menu.append(disconnect_item)
        self._menu_item(file_menu, "Quit", self._quit)
        bar.append(file_item)

        vm_menu = Gtk.Menu()
        vm_item = Gtk.MenuItem(label="_VM", use_underline=True)
        vm_item.set_submenu(vm_menu)
        self._menu_item(vm_menu, "Open Console", self.open_console_selected)
        # Relabels itself: from SPICE it offers VNC, from VNC it offers SPICE
        # back. Two entries, one of them always dead, would say less.
        self.switch_protocol_item = self._menu_item(
            vm_menu, "Reopen Console with VNC", self._switch_console_protocol
        )
        self.switch_protocol_item.set_sensitive(False)
        self.ctrl_alt_del_item = self._menu_item(
            vm_menu, "Send Ctrl+Alt+Del", self._send_ctrl_alt_del
        )
        self.ctrl_alt_del_item.set_sensitive(False)
        self.screenshot_item = self._menu_item(
            vm_menu, "Save Console Screenshot...", self._save_screenshot
        )
        self.screenshot_item.set_sensitive(False)
        vm_menu.append(Gtk.SeparatorMenuItem())
        for action in action_defs.POWER_ACTIONS:
            if action.name == "resume":
                continue  # shown on the Start entry when it applies
            item = self._menu_item(
                vm_menu,
                action.label,
                lambda name=action.name: self._run_action_on_selection(name),
            )
            item.set_sensitive(False)
            self._action_items.setdefault(action.name, []).append(item)

        vm_menu.append(Gtk.SeparatorMenuItem())
        self.snapshot_menu_items = {
            "take": self._menu_item(
                vm_menu, "Take Snapshot...", lambda: self._snapshot_action("take")
            ),
            "revert": self._menu_item(
                vm_menu,
                "Revert to Latest Snapshot",
                lambda: self._snapshot_action("revert"),
            ),
            "manage": self._menu_item(
                vm_menu, "Manage Snapshots...", lambda: self._snapshot_action("manage")
            ),
        }
        for item in self.snapshot_menu_items.values():
            item.set_sensitive(False)

        vm_menu.append(Gtk.SeparatorMenuItem())
        agent_item = Gtk.MenuItem(label="Guest Agent")
        agent_menu = Gtk.Menu()
        agent_item.set_submenu(agent_menu)
        self.agent_items = {
            "info": self._menu_item(agent_menu, "OS Information", self._agent_os_info),
            "network": self._menu_item(
                agent_menu, "Network Interfaces", self._agent_network
            ),
            "exec": self._menu_item(
                agent_menu, "Run Command...", self._agent_run_command
            ),
        }
        agent_menu.append(Gtk.SeparatorMenuItem())
        self.agent_items["ssh"] = self._menu_item(
            agent_menu, "Open SSH", lambda: self._open_remote("ssh")
        )
        self.agent_items["rdp"] = self._menu_item(
            agent_menu, "Open RDP", lambda: self._open_remote("rdp")
        )
        for item in self.agent_items.values():
            item.set_sensitive(False)
        self.agent_menu_item = agent_item
        vm_menu.append(agent_item)
        bar.append(vm_item)

        bar.append(self._build_view_menu())

        help_menu = Gtk.Menu()
        help_item = Gtk.MenuItem(label="_Help", use_underline=True)
        help_item.set_submenu(help_menu)
        self._menu_item(help_menu, "About", self._about)
        bar.append(help_item)

        return bar

    def _build_view_menu(self):
        """Console view controls, which act on the console tab in front.

        These used to sit in a strip above each console. Keeping them in the
        menu means the console gets the whole tab, and the entries simply
        disable themselves for a protocol that does not support them.
        """
        menu = Gtk.Menu()
        item = Gtk.MenuItem(label="Vie_w", use_underline=True)
        item.set_submenu(menu)

        self.fullscreen_item = self._menu_item(
            menu, "Full Screen", self._toggle_fullscreen, accel="<Control><Alt>Return"
        )
        menu.append(Gtk.SeparatorMenuItem())

        self.auto_resize_item = Gtk.CheckMenuItem(label="Auto-resize Guest")
        self.auto_resize_item.set_tooltip_text("Requires spice-vdagent")
        self.auto_resize_item.connect("toggled", self._on_auto_resize_toggled)
        menu.append(self.auto_resize_item)

        self.scaling_item = Gtk.CheckMenuItem(label="Scale to Fit")
        self.scaling_item.connect("toggled", self._on_scaling_toggled)
        menu.append(self.scaling_item)

        menu.append(Gtk.SeparatorMenuItem())

        self.codec_item, self.codec_items = self._radio_submenu(
            menu,
            "Video Codec",
            [label for label, _ in VIDEO_CODECS],
            self._on_codec_selected,
        )
        self.compression_item, self.compression_items = self._radio_submenu(
            menu,
            "Image Compression",
            [label for label, _ in IMAGE_COMPRESSION],
            self._on_compression_selected,
        )

        menu.append(Gtk.SeparatorMenuItem())

        self.split_item = self._menu_item(
            menu, "Move Console to a New Pane", self._split_console
        )
        self.split_item.set_tooltip_text(
            "Show this console beside the others. Once there is more than "
            "one pane, tabs can be dragged between them."
        )
        self.gather_item = self._menu_item(
            menu, "Close Split View", self._gather_consoles
        )

        menu.append(Gtk.SeparatorMenuItem())

        self.refresh_frame_item = self._menu_item(
            menu, "Refresh Framebuffer", self._refresh_framebuffer
        )
        self.close_console_item = self._menu_item(
            menu, "Close Console", self._close_current_console, accel="<Control>w"
        )

        self._view_items = [
            self.fullscreen_item,
            self.auto_resize_item,
            self.scaling_item,
            self.codec_item,
            self.compression_item,
            self.split_item,
            self.gather_item,
            self.refresh_frame_item,
            self.close_console_item,
        ]
        for widget in self._view_items:
            widget.set_sensitive(False)
        return item

    def _radio_submenu(self, parent, label, options, callback):
        """A submenu of mutually exclusive options. Returns (item, widgets)."""
        item = Gtk.MenuItem(label=label)
        submenu = Gtk.Menu()
        widgets = []
        group = []
        for index, text in enumerate(options):
            radio = Gtk.RadioMenuItem(label=text)
            radio.set_property("group", group[0] if group else None)
            if not group:
                group.append(radio)
            radio.connect("toggled", callback, index)
            submenu.append(radio)
            widgets.append(radio)
        item.set_submenu(submenu)
        parent.append(item)
        return item, widgets

    def _menu_item(self, menu, label, callback, accel=None):
        item = Gtk.MenuItem(label=label)
        item.connect("activate", lambda *_: callback())
        menu.append(item)
        if accel:
            key, modifier = Gtk.accelerator_parse(accel)
            if not hasattr(self, "_accels"):
                self._accels = Gtk.AccelGroup()
                self.add_accel_group(self._accels)
            item.add_accelerator(
                "activate", self._accels, key, modifier, Gtk.AccelFlags.VISIBLE
            )
        return item

    def _build_toolbar(self):
        bar = Gtk.Toolbar()
        bar.set_style(Gtk.ToolbarStyle.BOTH_HORIZ)
        bar.set_icon_size(Gtk.IconSize.SMALL_TOOLBAR)

        console_item = Gtk.ToolButton()
        console_item.set_label("Console")
        console_item.set_icon_name("video-display-symbolic")
        console_item.set_is_important(True)
        console_item.set_tooltip_text("Open console")
        console_item.connect("clicked", lambda *_: self.open_console_selected())
        console_item.set_sensitive(False)
        self.console_tool_item = console_item
        bar.insert(console_item, -1)

        bar.insert(Gtk.SeparatorToolItem(), -1)

        for name in action_defs.TOOLBAR_ACTIONS:
            action = action_defs.ACTIONS_BY_NAME[name]
            item = Gtk.ToolButton()
            item.set_label(action.label)
            item.set_icon_name(action.icon)
            item.set_is_important(name in ("start", "shutdown"))
            item.set_tooltip_text(action.tooltip)
            item.set_sensitive(False)
            item.connect(
                "clicked",
                lambda _b, action_name=name: self._run_action_on_selection(action_name),
            )
            self._action_items.setdefault(name, []).append(item)
            bar.insert(item, -1)

        bar.insert(Gtk.SeparatorToolItem(), -1)

        self.snapshot_items = {}
        for name, label, icon, tooltip in (
            ("take", "Snapshot", "list-add-symbolic", "Take a snapshot"),
            (
                "revert",
                "Revert",
                "edit-undo-symbolic",
                "Roll back to the most recent snapshot",
            ),
            ("manage", "Manage", "document-open-symbolic", "Manage snapshots"),
        ):
            item = Gtk.ToolButton()
            item.set_label(label)
            item.set_icon_name(icon)
            item.set_tooltip_text(tooltip)
            item.set_is_important(name == "take")
            item.set_sensitive(False)
            item.connect("clicked", lambda _b, which=name: self._snapshot_action(which))
            self.snapshot_items[name] = item
            bar.insert(item, -1)

        bar.insert(Gtk.SeparatorToolItem(), -1)

        self.popout_item = Gtk.ToolButton()
        self.popout_item.set_label("Pop Out")
        self.popout_item.set_icon_name("window-new-symbolic")
        self.popout_item.set_tooltip_text("Move this console into its own window")
        self.popout_item.set_sensitive(False)
        self.popout_item.connect("clicked", lambda *_: self.popout_console())
        bar.insert(self.popout_item, -1)

        self.split_item_tb = Gtk.ToolButton()
        self.split_item_tb.set_label("Split")
        self.split_item_tb.set_icon_name("view-dual-symbolic")
        self.split_item_tb.set_tooltip_text("Show this console beside the others")
        self.split_item_tb.set_sensitive(False)
        self.split_item_tb.connect("clicked", lambda *_: self._split_console())
        bar.insert(self.split_item_tb, -1)

        self.fullscreen_item_tb = Gtk.ToolButton()
        self.fullscreen_item_tb.set_label("Full Screen")
        self.fullscreen_item_tb.set_icon_name("view-fullscreen-symbolic")
        self.fullscreen_item_tb.set_tooltip_text("Full screen (Ctrl+Alt+Enter)")
        self.fullscreen_item_tb.set_sensitive(False)
        self.fullscreen_item_tb.connect("clicked", lambda *_: self._toggle_fullscreen())
        bar.insert(self.fullscreen_item_tb, -1)

        bar.insert(Gtk.SeparatorToolItem(), -1)

        # Both panes are toggles, and both say so by staying pressed in
        # while their pane is open. A button that only ever opens something
        # leaves you hunting for the way to close it again.
        self.tree_tool_item = Gtk.ToggleToolButton()
        self.tree_tool_item.set_label("Tree")
        self.tree_tool_item.set_icon_name("sidebar-show-symbolic")
        self.tree_tool_item.set_tooltip_text("Show or hide the inventory tree")
        self.tree_tool_item.set_active(bool(self.config.get("sidebar_visible", True)))
        self.tree_tool_item.connect("toggled", self._on_tree_toggled)
        bar.insert(self.tree_tool_item, -1)

        self.tasks_tool_item = Gtk.ToggleToolButton()
        self.tasks_tool_item.set_label("Tasks")
        self.tasks_tool_item.set_icon_name("view-list-symbolic")
        self.tasks_tool_item.set_tooltip_text("Show or hide the cluster task list")
        self.tasks_tool_item.connect("toggled", self._on_tasks_toggled)
        bar.insert(self.tasks_tool_item, -1)

        refresh_item = Gtk.ToolButton()
        refresh_item.set_label("Refresh")
        refresh_item.set_icon_name("view-refresh-symbolic")
        refresh_item.set_tooltip_text("Refresh inventory (F5)")
        refresh_item.connect("clicked", lambda *_: self.refresh())
        bar.insert(refresh_item, -1)

        spacer = Gtk.SeparatorToolItem()
        spacer.set_draw(False)
        spacer.set_expand(True)
        bar.insert(spacer, -1)

        settings_item = Gtk.ToolButton()
        settings_item.set_label("Preferences")
        settings_item.set_icon_name("preferences-system-symbolic")
        settings_item.connect("clicked", lambda *_: self.open_settings())
        bar.insert(settings_item, -1)

        return bar

    # -- panes ---------------------------------------------------------

    def _set_toggle(self, item, active):
        """Put a toggle button back in step with the pane it describes."""
        if item.get_active() == active:
            return
        self._syncing_toggles = True
        try:
            item.set_active(active)
        finally:
            self._syncing_toggles = False

    def _on_tree_toggled(self, item):
        if self._syncing_toggles:
            return
        visible = item.get_active()
        # Hidden outright rather than collapsed to a sliver: a header saying
        # "the tree is over here" is still the tree taking up the console's
        # width, which is the whole reason for closing it.
        self.sidebar.set_visible(visible)
        self.config["sidebar_visible"] = visible
        self.config.save()

    def _on_tasks_toggled(self, item):
        if self._syncing_toggles:
            return
        if item.get_active():
            self.task_feed.open()
        else:
            # close() calls back into on_closed, which would set the button
            # we are already inside the handler for; _set_toggle notices it
            # is already correct and does nothing.
            self.task_feed.close()

    def _build_statusbar(self):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.get_style_context().add_class("statusbar-box")

        # A plain label rather than GtkStatusbar: the latter wraps its text
        # in a frame and box whose padding cannot be trimmed below a fairly
        # tall minimum, which is where the extra height came from.
        self.status_label_main = Gtk.Label(xalign=0.0)
        self.status_label_main.set_ellipsize(3)
        box.pack_start(self.status_label_main, True, True, 0)

        # Console throughput, frame rate and guest resolution.
        self.telemetry_label = Gtk.Label(xalign=1.0)
        self.telemetry_label.get_style_context().add_class("mono")
        self.telemetry_label.get_style_context().add_class("dim")
        box.pack_start(self.telemetry_label, False, False, 4)

        # Two different agents, two indicators. spice-vdagent runs inside the
        # SPICE session and is what clipboard sharing and guest resize need;
        # qemu-guest-agent answers the API and is what exec and IP reporting
        # need. Neither implies the other.
        #
        # The clipboard and audio ones are also switches: clicking either
        # turns that feature off for the guest, and the setting is kept
        # client side, per guest, across sessions.
        self.vdagent_icon = StatusIndicator(
            "edit-paste-symbolic", "SPICE agent", on_toggle=self._toggle_clipboard
        )
        box.pack_start(self.vdagent_icon, False, False, 2)

        self.qga_icon = StatusIndicator("utilities-terminal-symbolic", "Guest agent")
        box.pack_start(self.qga_icon, False, False, 2)

        # Whether the guest has an audio device routed over SPICE. Proxmox
        # adds none by default, so silence is nearly always this.
        self.audio_icon = StatusIndicator(
            "audio-volume-high-symbolic", "Audio", on_toggle=self._toggle_audio
        )
        box.pack_start(self.audio_icon, False, False, 2)

        # Dragging a guest between folders. Not about the console at all,
        # but it belongs with the other two: it is a thing that is either
        # armed or not, and this is where you find out which.
        self.dnd_icon = StatusIndicator(
            "list-drag-handle-symbolic", "Drag and drop", on_toggle=self._toggle_dnd
        )
        box.pack_start(self.dnd_icon, False, False, 2)

        self._set_indicator(self.vdagent_icon, None, "SPICE agent")
        self._set_indicator(self.qga_icon, None, "Guest agent")
        self._set_indicator(self.audio_icon, None, "Audio")
        self._update_dnd_indicator()

        # Protocol of the active console tab. This is the persistent signal
        # that a guest is on VNC rather than SPICE, now that the console has
        # no bar of its own.
        self.protocol_label = Gtk.Label(xalign=1.0)
        box.pack_start(self.protocol_label, False, False, 6)

        self.connection_label = Gtk.Label(xalign=1.0)
        self.connection_label.get_style_context().add_class("dim")
        box.pack_start(self.connection_label, False, False, 8)
        return box

    @staticmethod
    def _set_indicator(icon, state, label, enabled=True, detail=None, can_toggle=None):
        """state: True connected, False not running, None not applicable.

        'enabled' is the user's own switch, drawn as a strike through the
        icon so it never reads as "the guest cannot do this".
        """
        if detail is not None:
            tooltip = f"{label}: {detail}"
        elif not enabled:
            tooltip = f"{label}: turned off"
        elif state is None:
            tooltip = f"{label}: n/a"
        elif state:
            tooltip = f"{label}: connected"
        else:
            tooltip = f"{label}: not running"
        icon.set_state(state, tooltip, enabled=enabled, can_toggle=can_toggle)

    # -- telemetry and agents ------------------------------------------

    @staticmethod
    def _format_rate(rate):
        if rate is None:
            return ""

        if rate >= 1024 * 1024:
            s = f"{rate / (1024 * 1024):.1f} MB/s"
        elif rate >= 1024:
            s = f"{rate / 1024:.0f} kB/s"
        else:
            s = f"{rate:.0f} B/s"

        return f"{s:>11}"

    def _sample_telemetry(self):
        if self._closing:
            return False
        console = self.current_console()
        if console is None or not hasattr(console, "telemetry"):
            self.telemetry_label.set_text("")
            self.screenshot_item.set_sensitive(False)
            return True

        # A console connects a moment after its tab appears, so the menu
        # entries that depend on being connected are refreshed here rather
        # than only on a tab switch.
        self.screenshot_item.set_sensitive(
            hasattr(console, "screenshot") and getattr(console, "connected", False)
        )

        data = console.telemetry()
        if not data:
            self.telemetry_label.set_text("")
            return True

        parts = [
            p for p in (data.get("size"), self._format_rate(data.get("rate"))) if p
        ]
        if data.get("fps") is not None:
            parts.append(f"{data['fps']:.0f} fps")
        # The encoding actually in use, when the protocol will say. spice-glib
        # exposes no accessor for the negotiated video codec, so this stays
        # empty for SPICE rather than echoing back what was requested.
        if data.get("codec"):
            parts.append(data["codec"])
        self.telemetry_label.set_text("  ".join(parts))

        # spice-vdagent state comes from the console; VNC has none.
        self._update_clipboard_indicator(console)
        return True

    def _on_console_agent(self, console, connected):
        """spice-vdagent appeared or went away on a console."""

        def update():
            if console is self.current_console():
                self._update_clipboard_indicator(console, connected)
            return False

        GLib.idle_add(update)

    def _guest_switch(self, guest, name):
        """Whether clipboard sharing or audio is on for a guest right now.

        Two sources, in order: whatever the status bar button was last set
        to for this console, and otherwise the guest's own settings from the
        server. The button is a session-length override and nothing more --
        it never writes back into the settings, which is what makes it safe
        to click while poking at something.
        """
        if guest is None:
            return True
        override = self._session_switches.get((guest.key, name))
        if override is not None:
            return bool(override)
        return self.guest_settings(guest).get(name, "enabled") != "disabled"

    def guest_settings(self, guest):
        """The Proxmox Manager settings stored in a guest's notes."""
        if guest is None:
            return dict(notes_meta.SETTINGS_DEFAULTS)
        return notes_meta.normalise_settings(guest.settings)

    def _toggle_switch(self, name, label):
        """Flip a per-guest switch for this session and apply it live."""
        guest = self.context_guest()
        if guest is None:
            return
        enabled = not self._guest_switch(guest, name)
        self._session_switches[(guest.key, name)] = enabled

        console = self.consoles.get(guest.key)
        supported = bool(console and getattr(console, "supports", {}).get(name))
        setter = getattr(console, f"set_{name}_enabled", None) if console else None
        applied = bool(supported and setter and setter(enabled))

        state = "on" if enabled else "off"
        if applied:
            self.set_status(f"{guest.label}: {label} {state} for this console")
        elif supported:
            # The console knows the new setting but cannot act on it without
            # being rebuilt -- audio, which is fixed when the SPICE session
            # is created. Rebuild in place: the tab stays where it is.
            self.set_status(f"{guest.label}: {label} {state}, reconnecting...")
            # An explicit toggle overrides the in-flight-reconnect guard;
            # otherwise flipping the switch twice in a row does nothing the
            # second time.
            self._reconnecting = None
            self.reconnect_console(guest.key)
        else:
            # No console open, or one that cannot do it at all. The choice
            # is still held for this session and takes hold the next time it
            # can; anything longer-lived belongs in the guest's settings.
            self.set_status(
                f"{guest.label}: {label} {state} (applies when a SPICE console is open)"
            )
        self._context_changed()

    def _toggle_clipboard(self):
        self._toggle_switch("clipboard", "clipboard sharing")

    def _toggle_audio(self):
        self._toggle_switch("audio", "audio")

    def _toggle_dnd(self):
        """Arm or disarm dragging guests between folders.

        A client-wide preference rather than a per-guest one: it is about
        how the tree behaves under your hand, not about any one VM. Saved
        immediately, because it exists to stop an accident and would be no
        use if it forgot itself.
        """
        enabled = not bool(self.config.get("enable_dnd", True))
        self.config["enable_dnd"] = enabled
        self.config.save()
        self.sidebar.set_dnd_enabled(enabled)
        self._update_dnd_indicator()
        self.set_status("Drag and drop " + ("on" if enabled else "off"))

    def _update_dnd_indicator(self):
        enabled = bool(self.config.get("enable_dnd", True))
        label = "Drag and drop"
        if not self.sidebar.folder_view:
            # Node view has no folders to drop onto, so the setting is real
            # but has nothing to act on. Still toggleable: switching it on
            # here so it is ready in folder view is a reasonable thing to do.
            self._set_indicator(
                self.dnd_icon,
                None,
                label,
                enabled=enabled,
                can_toggle=True,
                detail=(
                    "n/a in node view - "
                    + ("on" if enabled else "off")
                    + " for folder view"
                ),
            )
            return
        self._set_indicator(
            self.dnd_icon,
            enabled,
            label,
            enabled=enabled,
            can_toggle=True,
            detail=(
                "guests can be dragged between folders - click to turn off"
                if enabled
                else "turned off - click to allow dragging guests between folders"
            ),
        )

    def _update_clipboard_indicator(self, console=_CURRENT, connected=None):
        """Clipboard sharing: whether it can work, and whether it is allowed.

        Driven by the console rather than the guest, because the agent state
        it reports is a property of the live session. The guest only decides
        which saved switch applies.
        """
        if console is _CURRENT:
            console = self.current_console()
        guest = self.context_guest(console)
        enabled = self._guest_switch(guest, "clipboard")
        if connected is None:
            connected = bool(getattr(console, "agent_connected", False))
        usable = bool(
            console is not None and getattr(console, "supports", {}).get("clipboard")
        )
        label = "Clipboard (SPICE agent)"

        if not usable:
            detail = (
                "n/a - this console is VNC, which has no clipboard channel"
                if console is not None
                else "n/a - needs an open SPICE console"
            )
            self._set_indicator(
                self.vdagent_icon,
                None,
                label,
                enabled=enabled,
                can_toggle=False,
                detail=detail,
            )
            return
        if not enabled:
            detail = "turned off - click to share the clipboard again"
        elif connected:
            detail = "sharing - click to turn off"
        else:
            detail = "spice-vdagent is not running in the guest"
        self._set_indicator(
            self.vdagent_icon,
            connected,
            label,
            enabled=enabled,
            can_toggle=True,
            detail=detail,
        )

    def _update_audio_indicator(self, console=_CURRENT):
        """SPICE audio needs a device on the guest, and a SPICE console.

        Driven by the console in front, not by the tree selection: only a
        SPICE session carries audio at all, so on a VNC tab there is nothing
        to switch and the button says so by being insensitive.
        """
        if console is _CURRENT:
            console = self.current_console()
        guest = self.context_guest(console)
        enabled = self._guest_switch(guest, "audio")
        label = "Audio"

        # The icon says whether sound can come out at all; the strike says
        # whether it has been switched off. Two facts, two signals.
        audio = (guest.config or {}).get("audio0") if guest else None
        has_device = audio_is_spice(audio)
        self.audio_icon.set_icon_name(
            "audio-volume-high-symbolic"
            if has_device
            else "audio-volume-muted-symbolic"
        )

        if guest is None or guest.is_container:
            self._set_indicator(self.audio_icon, None, label, can_toggle=False)
            return
        if not self.config.get("enable_audio", True):
            self._set_indicator(
                self.audio_icon,
                None,
                label,
                can_toggle=False,
                detail="off for every console in Preferences",
            )
            return
        if console is None or not getattr(console, "supports", {}).get("audio"):
            self._set_indicator(
                self.audio_icon,
                None,
                label,
                enabled=enabled,
                can_toggle=False,
                detail=(
                    "n/a - "
                    + (
                        "this console is VNC, which carries no audio"
                        if console is not None
                        else "needs an open SPICE console"
                    )
                ),
            )
            return
        if not guest.config_loaded:
            return  # unknown until the config is read

        if has_device:
            self._set_indicator(
                self.audio_icon,
                True,
                label,
                enabled=enabled,
                can_toggle=True,
                detail=f"{audio} - click to turn {'off' if enabled else 'on'}",
            )
        elif audio:
            self._set_indicator(
                self.audio_icon,
                False,
                label,
                enabled=enabled,
                can_toggle=False,
                detail=f"{audio} (not routed over SPICE)",
            )
        else:
            self._set_indicator(
                self.audio_icon,
                False,
                label,
                enabled=enabled,
                can_toggle=False,
                detail=(
                    "no device. Add audio0: "
                    "device=ich9-intel-hda,driver=spice in Proxmox."
                ),
            )

    def _refresh_guest_agent_indicator(self, guest):
        """Ping the QEMU guest agent for the selected guest, off-thread."""
        if guest is None or guest.is_container or not guest.running:
            self._set_indicator(self.qga_icon, None, "Guest agent")
            self._agent_ok = False
            self._update_agent_menu()
            return

        key = guest.key

        def worker():
            try:
                ok = self.api_for(guest).agent_ping(guest.node, guest.vmid)
            except Exception:
                # A guest without the agent is the normal case, not an error
                # worth surfacing; the indicator says so on its own.
                ok = False
            GLib.idle_add(self._apply_agent_ping, key, ok)

        threading.Thread(
            target=worker, daemon=True, name=f"agent-ping-{guest.vmid}"
        ).start()

    def _refresh_snapshot_state(self, guest):
        """Learn the newest snapshot so Revert can name it or grey out."""
        if guest is None or guest.template:
            self._update_snapshot_buttons()
            return
        key = guest.key

        def worker():
            try:
                rows = self.api_for(guest).snapshots(guest.node, guest.vmid, guest.kind)
            except Exception:
                rows = []
            GLib.idle_add(self._apply_snapshots, key, rows)

        threading.Thread(
            target=worker, daemon=True, name=f"snap-state-{guest.vmid}"
        ).start()

    def _apply_snapshots(self, key, rows):
        if self._closing:
            return False
        guest = self.sidebar.guests.get(key)
        if guest is None:
            return False
        guest.latest_snapshot = rows[0] if rows else None
        guest.snapshots_loaded = True
        current = self.context_guest()
        if current is not None and current.key == key:
            self._update_snapshot_buttons()
        self._update_popouts()
        return False

    def _update_snapshot_buttons(self, console=_CURRENT):
        """Revert is only offered when there is something to revert to."""
        guest = self.context_guest(console)
        can_snapshot = guest is not None and not guest.template
        latest = guest.latest_snapshot if guest else None

        for name in ("take", "manage"):
            self.snapshot_items[name].set_sensitive(can_snapshot)
            self.snapshot_menu_items[name].set_sensitive(can_snapshot)

        enabled = bool(can_snapshot and latest)
        tooltip = describe_revert(latest)
        for widget in (
            self.snapshot_items["revert"],
            self.snapshot_menu_items["revert"],
        ):
            widget.set_sensitive(enabled)
            widget.set_tooltip_text(tooltip)

    def _apply_agent_ping(self, key, ok):
        # A ping that lands after the window has gone has nothing to update,
        # and reading the tree's selection back from a destroyed widget
        # raises rather than returning nothing.
        if self._closing:
            return False
        current = self.context_guest()
        if current is None or current.key != key:
            return False
        self._set_indicator(self.qga_icon, ok, "Guest agent")
        self._agent_ok = ok
        self._update_agent_menu()
        return False

    def set_status(self, text):
        self.status_label_main.set_text(text or "")
        return False

    # ------------------------------------------------------------------
    # Appearance
    # ------------------------------------------------------------------

    def apply_appearance(self):
        theme_name, dark = apply_theme(self.config, self)
        self.sidebar.set_row_ypad(theme_css.ROW_YPAD)
        self.sidebar.set_dark(dark)
        self._update_connection_label(theme_name, dark)

    def _update_connection_label(self, theme_name=None, dark=None):
        count = len(self.connections.connected)
        total = len(self.connections)
        if total == 0:
            text = "not connected"
        elif count == total == 1:
            text = self.connections.all[0].label
        else:
            text = f"{count}/{total} servers"
        if theme_name:
            self._last_theme = (theme_name, dark)
        theme_name, dark = getattr(self, "_last_theme", ("", False))
        suffix = f"  |  {theme_name}{' dark' if dark else ''}" if theme_name else ""
        self.connection_label.set_text(text + suffix)
        if self.header_bar is not None:
            # The same fact, in the one place a header bar has room for it.
            self.header_bar.set_subtitle(text)

    def _on_system_theme_changed(self, _dark):
        if self.config.get("color_mode", "system") == "system":
            self.apply_appearance()

    def open_settings(self):
        before = (self.config.get("font_backend"), self.config.get("sw_decoders"))
        dialog = SettingsDialog(self, self.config, on_change=self.apply_appearance)
        dialog.run()
        self.task_feed.interval = max(
            1, int(self.config.get("task_refresh_seconds", 5))
        )
        self.task_feed.restart()
        after = (self.config.get("font_backend"), self.config.get("sw_decoders"))
        if before != after:
            self.set_status("Restart required for font backend / decoder changes")
        elif bool(self.config.get("use_header_bar")) != (self.header_bar is not None):
            # GTK picks a window's decorations when the window is created,
            # so this one genuinely cannot be applied in place.
            self.set_status("Restart required to change the titlebar")
        self._apply_name_formats()
        self._restart_poll()

    # ------------------------------------------------------------------
    # Connections
    # ------------------------------------------------------------------

    def api_for(self, key_or_guest):
        """The client owning a guest.

        Raises rather than falling back to some other server: acting on the
        wrong cluster is the one mistake here that could be destructive.
        """
        api = self.connections.api_for(key_or_guest)
        if api is None:
            raise ProxmoxError("that server is no longer connected")
        return api

    def connect_saved(self):
        """Dial every saved connection, in the background, on startup."""
        entries = self.config.get("connections") or []
        for entry in entries:
            connection = Connection.from_config(entry, secrets.decode)
            if connection.host:
                self.connections.add(connection)
        if not self.connections:
            return False

        self.sidebar.update(self.connections)
        for connection in self.connections.all:
            self._connect_async(connection)
        return False

    def _connect_async(self, connection):
        """Log a connection in without blocking. Failure is per connection."""
        connection.state = "connecting"
        self.sidebar.update(self.connections)
        self.set_status(f"Connecting to {connection.label}...")

        def worker():
            try:
                connection.connect()
            except Exception as exc:
                GLib.idle_add(self._connection_failed, connection, str(exc))
                return
            GLib.idle_add(self._connection_ready, connection)

        threading.Thread(
            target=worker, daemon=True, name=f"connect-{connection.host}"
        ).start()

    def _connection_ready(self, connection):
        self.set_status(f"Connected to {connection.label} as {connection.api.username}")
        self.sidebar.update(self.connections)
        self._update_connection_label()
        self.burst_poll(seconds=6)
        return False

    def _connection_failed(self, connection, message):
        # Left in the tree and marked failed, so the other servers are
        # unaffected and the reason stays visible.
        self.set_status(f"{connection.label}: {message}")
        self.sidebar.update(self.connections)
        self._update_connection_label()
        return False

    def open_connect_dialog(self):
        connection = run_login(self, self.config)
        if connection is None:
            return
        self.connections.add(connection)
        if connection.save:
            self._save_connections()
        self.sidebar.update(self.connections)
        self._connection_ready(connection)

    def disconnect_connection(self, connection_id):
        connection = self.connections.get(connection_id)
        if connection is None:
            return
        for key in [k for k in self.consoles if k.startswith(connection_id + "/")]:
            self.close_console(key)
        self.connections.remove(connection_id)
        # A manual disconnect is a decision: stop reconnecting it at startup.
        self._save_connections()
        self.sidebar.update(self.connections)
        self._update_connection_label()
        self._context_changed()
        self.set_status(f"Disconnected from {connection_id}")

    def reconnect_connection(self, connection_id):
        connection = self.connections.get(connection_id)
        if connection is not None:
            self._connect_async(connection)

    def _save_connections(self):
        self.config["connections"] = [
            c.to_config(secrets.encode) for c in self.connections.all if c.save
        ]
        self.config.save()

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

    # How long to keep trying to reopen a saved console. A guest that is
    # still not in the inventory by then is on a server that did not come
    # back, and retrying forever would reopen it minutes later out of
    # nowhere.
    RESTORE_TIMEOUT = 90

    def _begin_session_restore(self):
        """Queue last session's consoles and tree expansion."""
        if not self.config.get("restore_session", True):
            return
        self.sidebar.restore_expansion(self.config.get("session_expanded") or [])
        self._restore_keys = list(self.config.get("session_consoles") or [])
        if self._restore_keys:
            self._restore_until = time.monotonic() + self.RESTORE_TIMEOUT

    def _resume_session(self):
        """Reopen whichever saved consoles have turned up in the inventory.

        Called after every poll: guests arrive server by server, so the tabs
        come back as their servers finish connecting rather than all at once
        or not at all.
        """
        if not self._restore_keys:
            return
        if time.monotonic() >= self._restore_until:
            self._restore_keys = []
            return
        for key in list(self._restore_keys):
            guest = self.sidebar.guests.get(key)
            if guest is None:
                continue
            self._restore_keys.remove(key)
            if not guest.template:
                # Restoring is not a click: several consoles come back at
                # once, and each one that turns out to be in use has to
                # explain itself on its own tab rather than in a queue of
                # modal dialogs at startup.
                self.open_console(key, automatic=True)
        # Reopening moves the selection to the last console; the tree
        # selection is the more useful thing to leave in front.
        if not self._restore_keys and self.panes.total_pages() > 1:
            self.set_status(f"Reopened {self.panes.total_pages() - 1} console(s)")

    def _save_session(self):
        """Record the open consoles and tree expansion for the next start."""
        if not self.config.get("restore_session", True):
            return
        # Pane by pane and then tab order, so they come back in the order
        # they sat in. Which pane a console was in is not recorded: the
        # split is a way of looking at things for a while, not part of the
        # session, and restoring into four panes on startup would be a
        # surprise rather than a convenience.
        keys = []
        for page in self.panes.all_pages():
            key = getattr(page, "guest_key", None)
            if key:
                keys.append(key)
        # A popped-out console is still an open console; it simply is not in
        # the notebook. It comes back as a tab, which is the honest default.
        keys.extend(k for k in self._popouts if k not in keys)
        self.config["session_consoles"] = keys
        self.config["session_expanded"] = self.sidebar.expanded_ids()

    # ------------------------------------------------------------------
    # Folders
    # ------------------------------------------------------------------

    def move_guest_to_folder(self, key, path):
        """Rewrite a guest's notes so it lands in a folder."""
        guest = self.sidebar.guests.get(key)
        if guest is None:
            return
        parts = tuple(p for p in path.split("/") if p) if path else ()
        if tuple(guest.folder) == parts:
            return

        def worker():
            try:
                api = self.api_for(guest)
                current = api.guest_notes(guest.node, guest.vmid, guest.kind)
                api.set_guest_notes(
                    guest.node,
                    guest.vmid,
                    notes_meta.with_folder(current, parts),
                    guest.kind,
                )
            except Exception as exc:
                GLib.idle_add(self.set_status, f"{guest.label}: could not move - {exc}")
                return
            GLib.idle_add(self._folder_moved, key, parts)

        threading.Thread(
            target=worker, daemon=True, name=f"folder-{guest.vmid}"
        ).start()

    def _folder_moved(self, key, parts):
        guest = self.sidebar.guests.get(key)
        if guest is None:
            return False
        guest.folder = parts
        guest.notes_loaded = True
        self.sidebar.rebuild()
        self.set_status(
            f"{guest.label} moved to {'/'.join(parts) if parts else 'the root'}"
        )
        return False

    def prompt_new_subfolder(self, key):
        guest = self.sidebar.guests.get(key)
        if guest is None:
            return
        parent = "/".join(guest.folder)
        name = self._prompt(
            "New Subfolder",
            f"Subfolder of {parent or 'the root'}:",
            placeholder="Customer A",
        )
        if not name:
            return
        # "/" is the path separator, so it cannot appear in a single level.
        name = name.strip().replace("/", "-")
        self.move_guest_to_folder(key, f"{parent}/{name}" if parent else name)

    def _load_folders(self):
        """Read every unread guest's notes in one pass.

        Doing this in small batches made guests land in the tree in waves --
        VMs first, containers a second later -- each wave visibly jumping
        from the root into its folder. One pass, applied all at once, means
        the tree goes from "Loading" straight to its final shape.

        The calls are spread over a few threads because it is one HTTP round
        trip per guest and they are otherwise serial.
        """
        if not self.sidebar.folder_view or self._folder_scan:
            return
        pending = [g for g in self.sidebar.guests.values() if not g.notes_loaded]
        if not pending:
            return
        self._folder_scan = True

        def read(guest):
            try:
                text = self.api_for(guest).guest_notes(
                    guest.node, guest.vmid, guest.kind
                )
            except Exception:
                text = ""
            # The settings live in the same block, so take them while the
            # notes are in hand rather than reading them again later.
            return guest.key, notes_meta.folder_of(text), notes_meta.settings_of(text)

        def worker():
            try:
                with ThreadPoolExecutor(max_workers=6) as pool:
                    results = list(pool.map(read, pending))
            except Exception:
                results = []
            GLib.idle_add(self._apply_folders, results)

        threading.Thread(target=worker, daemon=True, name="folder-scan").start()

    def _apply_folders(self, results):
        self._folder_scan = False
        for key, folder, settings in results:
            guest = self.sidebar.guests.get(key)
            if guest is None:
                continue
            guest.notes_loaded = True
            guest.folder = folder
            guest.settings = settings
            guest.settings_loaded = True
        if self.sidebar.folder_view:
            self.sidebar.rebuild()
        # Guests that arrived while the scan was running still need reading.
        GLib.idle_add(lambda: (self._load_folders(), False)[1])
        return False

    # ------------------------------------------------------------------
    # Renaming and cloning
    # ------------------------------------------------------------------

    def rename_guest(self, key, name):
        """Apply an inline rename from the tree."""
        guest = self.sidebar.guests.get(key)
        if guest is None or name == guest.name:
            return
        if not valid_guest_name(name):
            self._error_dialog(
                "Invalid name",
                f"Proxmox will not accept {name!r}.\n\n"
                "A name is made of letters, digits, hyphens and dots, and "
                "no part of it may start or end with a hyphen.",
            )
            return

        self.set_status(f"{guest.label}: renaming to {name}...")
        # Immediately, and before the call returns: the row shows the new
        # name with a spinner from the moment the edit is committed.
        self._mark_busy(
            key,
            "name",
            guest.name,
            name,
            f"Renaming to {name}...",
            self.RENAME_TIMEOUT,
            name=name,
        )

        def worker():
            try:
                self.api_for(guest).rename_guest(
                    guest.node, guest.vmid, name, guest.kind
                )
            except Exception as exc:
                GLib.idle_add(self._rename_failed, guest, name, str(exc))
                return
            GLib.idle_add(self._renamed, key, name)

        threading.Thread(
            target=worker, daemon=True, name=f"rename-{guest.vmid}"
        ).start()

    def _renamed(self, key, name):
        guest = self.sidebar.guests.get(key)
        if guest is None:
            return False
        # The name is deliberately NOT written onto the guest here. The poll
        # overwrites it from the server, which for a second or two still
        # reports the old one -- that overwrite is what used to make the
        # name change, change back, and change again. The row shows the new
        # name from _busy instead, until the server agrees.
        self.set_status(f"Renamed to {name} ({guest.vmid})")
        self.burst_poll(seconds=5)
        return False

    def _rename_failed(self, guest, name, message):
        self._clear_busy(guest.key)
        self.set_status(f"{guest.label}: rename failed - {message}")
        self._error_dialog("Rename failed", message)
        self.sidebar.rebuild()
        return False

    # ------------------------------------------------------------------
    # Per-guest settings
    # ------------------------------------------------------------------

    def open_guest_settings(self, key):
        """Open the Hardware / Options / Proxmox Manager dialog for a guest.

        The settings live in the guest's notes, which arrive with its
        config, so a guest that has never been selected needs one round trip
        before the dialog can show anything truthful.
        """
        guest = self.sidebar.guests.get(key)
        if guest is None:
            return
        try:
            api = self.api_for(guest)
        except ProxmoxError as exc:
            self.set_status(str(exc))
            return

        # Always re-read, even when the config is already cached. The dialog
        # edits hardware, and it also carries the config's digest so Proxmox
        # can refuse the write if somebody else changes the VM meanwhile --
        # both want the current state, not whatever the last poll happened
        # to leave behind.
        self.set_status(f"{guest.label}: reading configuration...")

        def worker():
            try:
                config = api.guest_config(guest.node, guest.vmid, guest.kind)
            except Exception as exc:
                GLib.idle_add(self.set_status, f"{guest.label}: {exc}")
                return
            GLib.idle_add(self._guest_settings_loaded, key, config)

        threading.Thread(
            target=worker, daemon=True, name=f"settings-{guest.vmid}"
        ).start()

    def _guest_settings_loaded(self, key, config):
        guest = self.sidebar.guests.get(key)
        if guest is None:
            return False
        self.absorb_config(guest, config)
        try:
            api = self.api_for(guest)
        except ProxmoxError as exc:
            self.set_status(str(exc))
            return False
        self._show_guest_settings(guest, api)
        return False

    def _show_guest_settings(self, guest, api):
        self.set_status("")
        VMSettingsDialog(
            self,
            api,
            guest,
            on_saved=lambda settings, k=guest.key: self._guest_settings_saved(
                k, settings
            ),
        )

    def _guest_settings_saved(self, key, settings):
        """Take the new settings into use, and say what still needs a reopen.

        Clipboard sharing is a live property of the session, so it applies
        at once. Audio is fixed when the SPICE session is built and the
        protocol decides which session is built at all, so neither can be
        changed under a console that is already open -- and saying so is
        better than a reconnect nobody asked for.
        """
        guest = self.sidebar.guests.get(key)
        if guest is None:
            return
        label = guest.label
        console = self.consoles.get(key)
        pending = []

        # A session override still wins: an explicit click on the status bar
        # button is about this console, and saving settings is not a reason
        # to overrule it.
        if "clipboard" not in {name for k, name in self._session_switches if k == key}:
            wanted = settings.get("clipboard") != "disabled"
            setter = getattr(console, "set_clipboard_enabled", None)
            if setter is not None:
                setter(wanted)

        if console is not None and getattr(console, "supports", {}).get("audio"):
            if bool(getattr(console, "play_audio", True)) != (
                settings.get("audio") != "disabled"
            ):
                pending.append("audio")
        if (
            console is not None
            and key not in self._force_vnc
            and key not in self._force_spice
        ):
            if settings.get("protocol") == "vnc":
                mismatch = console.protocol != "vnc"
            else:
                # "Default" only implies SPICE where SPICE is actually on
                # offer; a VNC console on a guest with no SPICE display is
                # already what the default asks for.
                mismatch = (
                    console.protocol != "spice"
                    and self._can_use_spice(guest)
                    and not self.config.get("prefer_vnc")
                )
            if mismatch:
                pending.append("protocol")

        self._context_changed()
        if pending:
            self.set_status(
                f"{label}: settings saved - reopen the console for the new "
                f"{' and '.join(pending)} setting to take effect"
            )
        else:
            self.set_status(f"{label}: settings saved")

    def clone_guest(self, key):
        """Clone a template into a new guest."""
        guest = self.sidebar.guests.get(key)
        if guest is None:
            return
        try:
            api = self.api_for(guest)
        except ProxmoxError as exc:
            self.set_status(str(exc))
            return

        dialog = CloneDialog(self, api, guest)
        response = dialog.run()
        name, vmid, target, full, storage = dialog.values()
        dialog.destroy()
        if response != Gtk.ResponseType.OK or not vmid:
            return

        self.set_status(f"{guest.label}: cloning to {vmid} {name}...")

        def worker():
            try:
                api.clone_guest(
                    guest.node,
                    guest.vmid,
                    vmid,
                    name=name,
                    target=target,
                    full=full,
                    storage=storage,
                    kind=guest.kind,
                )
            except Exception as exc:
                GLib.idle_add(self._clone_failed, guest, str(exc))
                return
            GLib.idle_add(self._clone_started, guest, vmid, name, full)

        threading.Thread(target=worker, daemon=True, name=f"clone-{guest.vmid}").start()

    def _clone_started(self, guest, vmid, name, full):
        kind = "Full" if full else "Linked"
        self.set_status(f"{kind} clone {vmid} ({name}) requested")
        # A full clone copies disks and can run for minutes; the task feed is
        # where its progress actually is.
        self.task_feed.refresh()
        self.burst_poll(seconds=20)
        return False

    def _clone_failed(self, guest, message):
        self.set_status(f"{guest.label}: clone failed - {message}")
        self._error_dialog("Clone failed", message)
        return False

    def delete_guest(self, key):
        """Destroy a guest, after re-checking that it is safe to."""
        guest = self.sidebar.guests.get(key)
        if guest is None:
            return
        blocked = Sidebar._delete_blocked_reason(guest)
        if blocked:
            self.set_status(f"{guest.label}: cannot delete - {blocked}")
            return

        # The protection flag is read from the config, which may not have
        # been fetched yet -- and it is the one guard worth confirming
        # against the server rather than against a cached copy.
        self.set_status(f"{guest.label}: checking...")

        def worker():
            try:
                config = self.api_for(guest).guest_config(
                    guest.node, guest.vmid, guest.kind
                )
            except Exception as exc:
                GLib.idle_add(self.set_status, f"{guest.label}: {exc}")
                return
            GLib.idle_add(self._confirm_delete, key, config)

        threading.Thread(
            target=worker, daemon=True, name=f"delete-check-{guest.vmid}"
        ).start()

    def _confirm_delete(self, key, config):
        guest = self.sidebar.guests.get(key)
        if guest is None:
            return False
        self.absorb_config(guest, config)
        if guest.protected:
            self.set_status(f"{guest.label}: protected in Proxmox")
            self._error_dialog(
                "Guest is protected",
                f"{guest.label} has Proxmox's protection flag set.\n\n"
                "Clear it in the guest's options before deleting.",
            )
            self.sidebar.rebuild()
            return False

        kind = (
            "template"
            if guest.template
            else ("container" if guest.is_container else "VM")
        )
        purge = self._confirm_destroy(
            title=f"Delete {kind}",
            message=(
                f"Permanently delete {guest.label} and every disk it "
                f"owns?\n\nThis cannot be undone."
            ),
            expected=str(guest.vmid),
        )
        if purge is None:
            self.set_status("")
            return False

        self.set_status(f"{guest.label}: deleting...")
        # A console onto something being destroyed has nothing left to show.
        self.close_console(key)

        def worker():
            try:
                self.api_for(guest).delete_guest(
                    guest.node, guest.vmid, guest.kind, purge=purge
                )
            except Exception as exc:
                GLib.idle_add(self._delete_failed, guest, str(exc))
                return
            GLib.idle_add(self._delete_started, guest)

        threading.Thread(
            target=worker, daemon=True, name=f"delete-{guest.vmid}"
        ).start()
        return False

    def _delete_started(self, guest):
        self.set_status(f"{guest.label}: delete requested")
        self.task_feed.refresh()
        self.burst_poll(seconds=20)
        return False

    def _delete_failed(self, guest, message):
        self.set_status(f"{guest.label}: delete failed - {message}")
        self._error_dialog("Delete failed", message)
        return False

    # ------------------------------------------------------------------
    # Inventory
    # ------------------------------------------------------------------

    def burst_poll(self, seconds=None, interval=1):
        """Poll hard for a while after an action.

        A power action takes a few seconds to show up in /cluster/resources,
        and the normal interval makes the UI look like it ignored the click.
        """
        if seconds is None:
            seconds = int(self.config.get("burst_seconds", 15))
        if seconds <= 0:
            self.refresh()
            return
        self._burst_until = time.monotonic() + seconds
        if self._burst_source is None:
            self._burst_source = GLib.timeout_add_seconds(interval, self._burst_tick)
        self.refresh()

    def _burst_tick(self):
        if self._closing or time.monotonic() >= self._burst_until:
            self._burst_source = None
            return False
        self.refresh()
        return True

    def _schedule_poll(self):
        if self._poll_source is not None:
            GLib.source_remove(self._poll_source)
        interval = max(1, int(self.config.get("refresh_seconds", 4)))
        self._poll_source = GLib.timeout_add_seconds(interval, self._on_poll)

    def _restart_poll(self):
        self._schedule_poll()

    def _on_poll(self):
        if self._closing:
            return False
        self.refresh()
        return True

    def refresh(self, initial=False):
        """Poll the cluster on a worker thread."""
        if self._poll_busy or self._closing:
            return
        self._poll_busy = True

        connections = self.connections.connected

        def worker():
            failures = []
            for connection in connections:
                try:
                    connection.poll()
                except AuthError as exc:
                    connection.state = FAILED
                    connection.error = str(exc)
                    failures.append(f"{connection.label}: {exc}")
                except ProxmoxError as exc:
                    # A momentary failure is not a reason to drop the
                    # connection; the tree keeps its last known guests.
                    failures.append(f"{connection.label}: {exc}")
                except Exception as exc:
                    failures.append(f"{connection.label}: {type(exc).__name__}: {exc}")
            GLib.idle_add(self._on_polled, failures, initial)

        threading.Thread(target=worker, daemon=True, name="proxmox-poll").start()

    def _on_polled(self, failures, initial):
        self._poll_busy = False
        if self._closing:
            return False
        # Before the rebuild, so the tree is drawn once with the right set
        # of spinners rather than twice.
        self._resolve_busy()
        self.sidebar.update(self.connections)
        self._update_connection_label()
        self._rebuild_disconnect_menu()
        self._load_folders()
        self._resume_session()
        guests = self.sidebar.guests.values()
        self._sync_console_states()
        if failures:
            self.set_status("; ".join(failures[:2]))
        elif initial and self.connections:
            total = len(self.sidebar.guests)
            running = sum(1 for guest in guests if guest.running)
            self.set_status(f"{total} guests, {running} running")
        self._update_action_sensitivity()
        self._update_popouts()
        guest = self.sidebar.selected_guest()
        if guest is not None and self.panes.current_page() is self.summary:
            self.summary.show_guest(guest, self.api_for(guest))
        return False

    PENDING_TIMEOUT = 45  # give up waiting for a status change
    RENAME_TIMEOUT = 30
    REBOOT_ACK = 8  # an action with no status to wait for

    # -- changes the cluster has not caught up with ---------------------

    def _mark_busy(self, key, field, was, target, label, timeout, name=None):
        """Spin a guest's row until the inventory shows the change.

        Refuses to start when the guest is already in the state being asked
        for: there would be nothing left to wait for, and the deadline is a
        backstop, not a plan.
        """
        guest = self.sidebar.guests.get(key)
        if guest is None:
            return
        if target is not None and getattr(guest, field, None) == target:
            return
        self._busy[key] = _PendingChange(
            field, was, target, label, time.monotonic() + timeout, name
        )
        self._push_busy()

    def _clear_busy(self, key):
        if self._busy.pop(key, None) is not None:
            self._push_busy()

    def _push_busy(self):
        self.sidebar.set_busy(
            {key: (change.label, change.name) for key, change in self._busy.items()}
        )

    def _resolve_busy(self):
        """Drop every wait the freshly polled inventory has answered.

        Runs against the connections rather than the sidebar, because it has
        to happen before the tree is rebuilt -- otherwise the rebuild draws
        the old busy set and a second one is needed to correct it.
        """
        if not self._busy:
            return
        now = time.monotonic()
        done = [
            key
            for key, change in self._busy.items()
            if change.resolved_by(self._polled_guest(key), now)
        ]
        renamed = any(self._busy[key].field == "name" for key in done)
        for key in done:
            del self._busy[key]
        self._push_busy()
        if renamed:
            # The console tab still carries the old caption; the tree is
            # about to be rebuilt anyway, but the tabs are not.
            GLib.idle_add(lambda: (self._apply_name_formats(), False)[1])

    def _polled_guest(self, key):
        for connection in self.connections.all:
            guest = connection.guests.get(key)
            if guest is not None:
                return guest
        return None

    def _clear_pending(self, key):
        self._pending_actions.pop(key, None)
        console = self.consoles.get(key)
        if console is not None and hasattr(console, "clear_pending_state"):
            console.clear_pending_state()

    def _sync_console_states(self):
        """Reconcile each open console with what the guest is actually doing.

        SPICE in particular does not reliably tear its session down when a
        guest stops -- the display goes black with the pointer still grabbed.
        The inventory is the authority, so it drives this.
        """
        for key, console in list(self.consoles.items()):
            guest = self.sidebar.guests.get(key)
            if guest is None:
                continue

            pending = self._pending_actions.get(key)
            if pending is not None:
                was, deadline = pending
                if guest.status != was or time.monotonic() >= deadline:
                    self._clear_pending(key)
                else:
                    # Still waiting; leave the "Stopping..." panel up.
                    continue

            if not guest.running:
                # A stopped guest has no SPICE session, ours or anyone's, so
                # any claim we had on one is void. Without this, a guest
                # that stops and starts inside the linger window could let
                # us mistake somebody else's new session for our old one.
                self._recent_spice.pop(key, None)
                if self._console_offline.get(key) != guest.status:
                    self._console_offline[key] = guest.status
                    report = getattr(console, "show_guest_state", None)
                    if report is not None:
                        report(guest.status)
                    self.set_status(f"{guest.label} is {guest.status}")
                continue

            # Running again after being stopped: the old session is dead and
            # will never show the new boot, so rebuild it. Nobody asked for
            # this, so if someone else got to the console while the guest
            # was down, the choice goes on the tab rather than into a modal.
            if key in self._console_offline:
                del self._console_offline[key]
                self.set_status(f"{guest.label}: reconnecting")
                GLib.idle_add(
                    lambda k=key: (self.reconnect_console(k, automatic=True), False)[1]
                )

    def _on_view_changed(self, _sidebar):
        """Folder view needs each guest's notes, which node view never reads."""
        if self.sidebar.folder_view:
            self.set_status("Folder view: reading guest notes...")
            self._load_folders()
        self.config["folder_view"] = self.sidebar.folder_view
        self.config.save()
        # Dragging only means anything in folder view.
        self._update_dnd_indicator()

    def _rebuild_disconnect_menu(self):
        for child in self.disconnect_menu.get_children():
            self.disconnect_menu.remove(child)
        connections = self.connections.all
        for connection in connections:
            item = Gtk.MenuItem(label=connection.label)
            item.connect(
                "activate",
                lambda _i, cid=connection.id: self.disconnect_connection(cid),
            )
            self.disconnect_menu.append(item)
        self.disconnect_menu.show_all()
        self.disconnect_item.set_sensitive(bool(connections))

    def _on_poll_failed(self, message):
        self._poll_busy = False
        self.set_status(f"Refresh failed: {message}")
        return False

    def _on_auth_lost(self, message):
        self._poll_busy = False
        self.set_status(f"Session lost: {message}")
        if self._poll_source is not None:
            GLib.source_remove(self._poll_source)
            self._poll_source = None
        self._error_dialog("Session expired", message)
        self._disconnect()
        return False

    # ------------------------------------------------------------------
    # Selection and actions
    # ------------------------------------------------------------------

    def _on_guest_selected(self, _sidebar, key):
        guest = self.sidebar.guests.get(key) if key else None
        self._update_action_sensitivity()
        if guest is None:
            self.summary.clear()
            self._set_indicator(self.qga_icon, None, "Guest agent")
            self._agent_ok = False
            self._update_agent_menu()
            return
        if self.panes.current_page() is self.summary:
            self.summary.show_guest(guest, self.api_for(guest))
        self._context_changed()

    def _on_guest_activated(self, _sidebar, key):
        self.open_console(key)

    def _on_guest_action(self, _sidebar, key, action_name):
        if action_name == "console":
            self.open_console(key)
        elif action_name == "refresh":
            self.refresh()
        elif action_name == "snapshot-take":
            self._snapshot_action("take", key)
        elif action_name == "snapshot-manage":
            self._snapshot_action("manage", key)
        else:
            self._run_action(key, action_name)

    def _on_bulk_action(self, _sidebar, action_name):
        """Apply one action to every selected guest."""
        guests = self.sidebar.selected_guests()
        if action_name == "snapshot-take":
            self._bulk_snapshot(guests)
            return

        if action_defs.ACTIONS_BY_NAME.get(action_name) is None:
            return
        targets = [
            g
            for g in guests
            if action_defs.enabled_for(action_defs.resolve(action_name, g), g)
        ]
        if not targets:
            return
        action = action_defs.resolve(action_name, targets[0])

        names = ", ".join(g.label for g in targets[:6])
        if len(targets) > 6:
            names += f", and {len(targets) - 6} more"
        if not self._confirm(
            action.label, f"{action.label} {len(targets)} guests?\n{names}"
        ):
            return

        for guest in targets:
            self._run_action(guest.key, action_name, confirm=False)
        self.set_status(f"{action.label} requested for {len(targets)} guests")

    def _bulk_snapshot(self, guests):
        targets = [g for g in guests if not g.template]
        if not targets:
            return
        name = self._prompt(
            "Take Snapshot",
            f"Snapshot name for {len(targets)} guests:",
            placeholder="before-maintenance",
        )
        if not name:
            return
        for guest in targets:
            self._run_snapshot(
                guest,
                f"Snapshot {name}",
                lambda g=guest: self.api_for(g).create_snapshot(
                    g.node, g.vmid, name, "", False, g.kind
                ),
            )

    def context_guest(self, console=_CURRENT):
        """The guest the toolbar and VM menu act on.

        A console tab in front owns the context: clicking around the tree
        while watching a console must not silently re-aim Stop or Reset at
        something else. The Summary page has no console of its own, so there
        the tree selection is the context.
        """
        if console is _CURRENT:
            console = self.current_console()
        if console is not None:
            return self.sidebar.guests.get(getattr(console, "guest_key", None))
        return self.sidebar.selected_guest()

    def _context_changed(self, console=_CURRENT):
        """Refresh everything keyed to the current context guest."""
        guest = self.context_guest(console)
        self._update_action_sensitivity(console)
        self._refresh_guest_agent_indicator(guest)
        self._refresh_snapshot_state(guest)
        self._update_audio_indicator(console)
        self._update_clipboard_indicator(console)
        self._ensure_config_loaded(guest)

    def _ensure_config_loaded(self, guest):
        """Read the guest config once.

        Containers are included even though they have no display or audio to
        report: the config is also where the delete-protection flag lives,
        and that decides whether Delete is offered at all.
        """
        if guest is None or guest.config_loaded:
            return
        key = guest.key

        def worker():
            try:
                config = self.api_for(guest).guest_config(
                    guest.node, guest.vmid, guest.kind
                )
            except Exception:
                config = None
            GLib.idle_add(self._apply_config, key, config)

        threading.Thread(
            target=worker, daemon=True, name=f"config-{guest.vmid}"
        ).start()

    @staticmethod
    def absorb_config(guest, config):
        """Take everything this client reads out of a guest config.

        One place, because the config is fetched from three: selecting a
        guest, opening a console, and re-checking before a delete. The
        description is in there too, which is where the guest's settings
        live -- so reading them costs no extra round trip.
        """
        if guest is None or not config:
            return
        guest.config = config
        guest.protected = bool(int(config.get("protection", 0) or 0))
        if not guest.is_container:
            guest.display = str(config.get("vga", ""))
            guest.spice_capable = vga_is_spice(guest.display)
        guest.settings = notes_meta.settings_of(config.get("description", ""))
        guest.settings_loaded = True
        guest.config_loaded = True

    def _apply_config(self, key, config):
        guest = self.sidebar.guests.get(key)
        if guest is None or not config:
            return False
        self.absorb_config(guest, config)
        current = self.context_guest()
        if current is not None and current.key == key:
            self._update_audio_indicator()
            self._update_clipboard_indicator()
        return False

    def _update_action_sensitivity(self, console=_CURRENT):
        guest = self.context_guest(console)
        for name, widgets in self._action_items.items():
            # "start" is the shared Start/Resume control, so it takes its
            # label from whichever of the two currently applies.
            action = action_defs.resolve(name, guest)
            enabled = action_defs.enabled_for(action, guest)
            for widget in widgets:
                widget.set_sensitive(enabled)
                if name == "start":
                    widget.set_label(action.label)
                    if hasattr(widget, "set_tooltip_text"):
                        widget.set_tooltip_text(action.tooltip)
        self.console_tool_item.set_sensitive(
            guest is not None and guest.running and not guest.template
        )

        # Snapshots work on stopped guests too; only templates are excluded.
        # Revert additionally needs a snapshot to exist.
        self._update_snapshot_buttons(console)

    def _run_action_on_selection(self, action_name):
        guest = self.context_guest()
        if guest is not None:
            self._run_action(guest.key, action_name)

    def _run_action(self, key, action_name, confirm=True):
        guest = self.sidebar.guests.get(key)
        action = action_defs.resolve(action_name, guest)
        if guest is None or action is None:
            return
        action_name = action.name

        text = (
            action_defs.confirmation_text(action, guest, self.config)
            if confirm
            else None
        )
        if text and not self._confirm(action.label, text):
            return

        self.set_status(f"{action.label}: {guest.label}...")

        def worker():
            try:
                self.api_for(guest).power(
                    guest.node, guest.vmid, action_name, guest.kind
                )
            except ProxmoxError as exc:
                GLib.idle_add(self._action_failed, action, guest, str(exc))
                return
            except Exception as exc:
                GLib.idle_add(
                    self._action_failed, action, guest, f"{type(exc).__name__}: {exc}"
                )
                return
            GLib.idle_add(self._action_done, action, guest)

        threading.Thread(target=worker, daemon=True, name=f"power-{guest.vmid}").start()

    def _action_done(self, action, guest):
        self.set_status(f"{guest.label}: {action.label} requested")
        self.task_feed.refresh()

        # Proxmox takes a few seconds to report the new status, so say what
        # was asked for rather than leaving the console looking untouched.
        verb = action_defs.IN_PROGRESS.get(action.name)
        console = self.consoles.get(guest.key)
        if verb and console is not None:
            self._pending_actions[guest.key] = (
                guest.status,
                time.monotonic() + self.PENDING_TIMEOUT,
            )
            console.show_pending_state(f"{verb}...")

        # And on the row, whether or not a console is open: the task can
        # finish before the next inventory poll, which used to leave the
        # tree claiming the guest was still running for a couple of seconds
        # after everything else said otherwise.
        expected = action_defs.EXPECTED_STATUS.get(action.name)
        self._mark_busy(
            guest.key,
            "status",
            guest.status,
            expected,
            f"{verb or action.label}...",
            # A reboot ends where it started, so nothing in the inventory
            # will ever confirm it. Acknowledge the click briefly rather
            # than spinning for the full timeout waiting for a change that
            # is not coming.
            self.PENDING_TIMEOUT if expected else self.REBOOT_ACK,
        )

        self.burst_poll()
        return False

    def _action_failed(self, action, guest, message):
        self._clear_busy(guest.key)
        self.set_status(f"{guest.label}: {action.label} failed - {message}")
        self._error_dialog(f"{action.label} failed", message)
        return False

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def _snapshot_action(self, which, key=None):
        guest = self.sidebar.guests.get(key) if key else self.context_guest()
        if guest is None or guest.template:
            return

        if which == "manage":
            dialog = SnapshotManager(self, self.api_for(guest), guest)
            dialog.run()
            dialog.destroy()
            return

        if which == "take":
            dialog = TakeSnapshotDialog(self, guest)
            response = dialog.run()
            name, description, vmstate = dialog.values()
            dialog.destroy()
            if response != Gtk.ResponseType.OK or not name:
                return
            self._run_snapshot(
                guest,
                f"Snapshot {name}",
                lambda: self.api_for(guest).create_snapshot(
                    guest.node, guest.vmid, name, description, vmstate, guest.kind
                ),
            )
            return

        # Revert: resolve the newest snapshot first, then confirm by name so
        # the user is never asked to approve an unnamed rollback.
        self.set_status(f"{guest.label}: finding the latest snapshot...")

        def worker():
            try:
                rows = self.api_for(guest).snapshots(guest.node, guest.vmid, guest.kind)
            except ProxmoxError as exc:
                GLib.idle_add(self.set_status, f"{guest.label}: {exc}")
                return
            GLib.idle_add(self._confirm_revert, guest, rows)

        threading.Thread(
            target=worker, daemon=True, name=f"snap-latest-{guest.vmid}"
        ).start()

    def _confirm_revert(self, guest, rows):
        if not rows:
            self.set_status(f"{guest.label} has no snapshots")
            return False
        latest = rows[0]
        name = latest.get("name")
        if not self._confirm(
            "Roll Back",
            f"Roll {guest.label} back to '{name}'?\n"
            "Changes made since the snapshot will be lost.",
        ):
            self.set_status("")
            return False
        self._run_snapshot(
            guest,
            f"Rollback to {name}",
            lambda: self.api_for(guest).rollback_snapshot(
                guest.node, guest.vmid, name, guest.kind
            ),
        )
        return False

    def _run_snapshot(self, guest, label, call):
        self.set_status(f"{guest.label}: {label}...")

        def worker():
            try:
                call()
            except ProxmoxError as exc:
                GLib.idle_add(self.set_status, f"{guest.label}: {label} failed - {exc}")
                return
            GLib.idle_add(self._snapshot_done, guest, label)

        threading.Thread(target=worker, daemon=True, name=f"snap-{guest.vmid}").start()

    def _snapshot_done(self, guest, label):
        self.set_status(f"{guest.label}: {label} requested")
        # Snapshots run as a server-side task; the feed is where progress
        # actually shows up.
        self.task_feed.refresh()
        GLib.timeout_add_seconds(
            2,
            lambda: (
                self.refresh(),
                self._refresh_snapshot_state(self.sidebar.guests.get(guest.key)),
                False,
            )[2],
        )
        return False

    # ------------------------------------------------------------------
    # Guest agent
    # ------------------------------------------------------------------

    def _update_agent_menu(self):
        guest = self.context_guest()
        usable = bool(
            guest
            and guest.running
            and not guest.is_container
            and getattr(self, "_agent_ok", False)
        )
        for name, item in self.agent_items.items():
            item.set_sensitive(usable)
        self.agent_menu_item.set_sensitive(bool(guest and not guest.is_container))

    def _agent_call(self, title, call, render):
        guest = self.context_guest()
        if guest is None:
            return
        self.set_status(f"{guest.label}: {title}...")

        def worker():
            try:
                result = call(guest)
            except ProxmoxError as exc:
                GLib.idle_add(self.set_status, f"{guest.label}: {title} failed - {exc}")
                return
            except Exception as exc:
                GLib.idle_add(self.set_status, f"{guest.label}: {title} failed - {exc}")
                return
            GLib.idle_add(
                lambda: (
                    self._show_text(f"{title} - {guest.name}", render(result)) or False
                )
            )

        threading.Thread(target=worker, daemon=True, name=f"agent-{guest.vmid}").start()

    def _agent_os_info(self):
        def render(result):
            data = (result or {}).get("result", result) or {}
            if not isinstance(data, dict):
                return str(data)
            return "\n".join(f"{k:<16} {v}" for k, v in sorted(data.items()))

        self._agent_call(
            "OS information",
            lambda guest: self.api_for(guest).guest_agent_info(guest.node, guest.vmid),
            render,
        )

    def _agent_network(self):
        def render(interfaces):
            lines = []
            for interface in interfaces or []:
                name = interface.get("name", "?")
                mac = interface.get("hardware-address", "")
                lines.append(f"{name}  {mac}")
                for address in interface.get("ip-addresses") or []:
                    lines.append(
                        f"    {address.get('ip-address-type', '')} "
                        f"{address.get('ip-address', '')}"
                        f"/{address.get('prefix', '')}"
                    )
            return "\n".join(lines) or "(no interfaces reported)"

        self._agent_call(
            "Network interfaces",
            lambda guest: self.api_for(guest).guest_interfaces(guest.node, guest.vmid),
            render,
        )

    def _agent_run_command(self):
        guest = self.context_guest()
        if guest is None:
            return
        command = self._prompt(
            "Run Command",
            "Command to run via the guest agent:",
            placeholder="/bin/systemctl status sshd",
        )
        if not command:
            return

        def render(status):
            parts = []
            if status.get("exitcode") is not None:
                parts.append(f"exit code {status['exitcode']}")
            for key, label in (("out-data", "stdout"), ("err-data", "stderr")):
                if status.get(key):
                    parts.append(f"--- {label} ---\n{status[key]}")
            return "\n".join(parts) or "(no output)"

        # The agent takes argv, not a shell line.
        argv = command.split()
        self._agent_call(
            "Run command",
            lambda guest: self.api_for(guest).agent_exec_wait(
                guest.node, guest.vmid, argv
            ),
            render,
        )

    def _open_remote(self, kind):
        """Launch the local SSH or RDP client against the agent's address."""
        guest = self.context_guest()
        if guest is None:
            return
        self.set_status(f"{guest.label}: looking up the address...")

        def worker():
            try:
                interfaces = self.api_for(guest).guest_interfaces(
                    guest.node, guest.vmid
                )
            except Exception as exc:
                GLib.idle_add(self.set_status, f"{guest.label}: {exc}")
                return
            address = SummaryPage._first_address(interfaces)
            GLib.idle_add(self._launch_remote, guest, kind, address)

        threading.Thread(
            target=worker, daemon=True, name=f"agent-addr-{guest.vmid}"
        ).start()

    def _launch_remote(self, guest, kind, address):
        if not address:
            self.set_status(f"{guest.label}: the guest agent reported no IPv4 address")
            return False
        if kind == "rdp":
            command = (
                ["mstsc", f"/v:{address}"]
                if os.name == "nt"
                else ["xfreerdp", f"/v:{address}"]
            )
        else:
            command = (
                ["cmd", "/c", "start", "", "ssh", address]
                if os.name == "nt"
                else ["x-terminal-emulator", "-e", f"ssh {address}"]
            )
        try:
            subprocess.Popen(command, close_fds=True)
        except OSError as exc:
            self.set_status(f"could not launch {command[0]}: {exc}")
            return False
        self.set_status(f"{guest.label}: {kind.upper()} to {address}")
        return False

    # ------------------------------------------------------------------
    # Consoles
    # ------------------------------------------------------------------

    def open_console_selected(self):
        guest = self.sidebar.selected_guest()
        if guest is not None:
            self.open_console(guest.key)

    def open_console(self, key, replace=False, takeover=False, automatic=False):
        """Open, or rebuild, a guest's console.

        'takeover' means the user has already been told somebody else is on
        the guest's SPICE session and said to go ahead, so the occupancy
        check is skipped for this attempt.

        'automatic' means nothing was clicked -- a session restore, or the
        poll noticing a guest came back. Those must never raise a dialog
        over whatever the user is actually doing; they put the choice on the
        tab instead.
        """
        guest = self.sidebar.guests.get(key)
        if guest is None:
            return

        window = self._popouts.get(key)
        if window is not None and not replace:
            # Already open in its own window. page_num() cannot see it, so
            # without this the console would be built a second time.
            window.present()
            return

        existing = self.consoles.get(key)
        if existing is not None and not replace:
            if self.panes.focus_page(existing):
                return
            # Orphaned: in self.consoles but in no tab and no pop-out. Drop
            # it rather than leaving a console nothing can reach.
            self.close_console_widget(existing)

        if guest.template:
            self.set_status("Templates have no console")
            return

        if not guest.running:
            # Hold the tab open anyway; it becomes a real console as soon as
            # the guest starts.
            self._install_console(
                guest,
                PlaceholderConsole(
                    title=guest.name,
                    status=guest.status,
                    on_reconnect=lambda k=guest.key: self.reconnect_console(k),
                ),
            )
            self._console_offline[guest.key] = guest.status
            self.set_status(f"{guest.label} is {guest.status}")
            return

        # The tab opens immediately and reports its own progress. Fetching
        # the config and then a SPICE or VNC ticket is two round trips and
        # can take several seconds, which used to be spent staring at an
        # unchanged window with one line in the status bar.
        self._install_console(
            guest,
            PlaceholderConsole(
                title=guest.name,
                status="connecting",
                on_reconnect=lambda k=guest.key: self.reconnect_console(k),
            ),
        )
        self.set_status(f"{guest.label}: connecting...")

        def worker():
            try:
                plan = self._plan_console(guest, takeover=takeover)
            except ProxmoxError as exc:
                GLib.idle_add(self._console_failed, guest, str(exc))
                return
            except Exception as exc:
                traceback.print_exc()
                GLib.idle_add(
                    self._console_failed, guest, f"{type(exc).__name__}: {exc}"
                )
                return
            GLib.idle_add(self._build_console, guest, plan, replace, automatic)

        threading.Thread(
            target=worker, daemon=True, name=f"console-{guest.vmid}"
        ).start()

    def _switch_console_protocol(self):
        """Reopen the console in front on the other protocol.

        The choice lives on the tab, not on the guest: it holds while this
        console is open and is forgotten when the tab closes, so the next
        open goes back to whatever the guest asks for. Anything
        longer-lived is what the VM's Protocol setting is for.
        """
        console = self.current_console()
        key = getattr(console, "guest_key", None) if console else None
        if not key:
            return
        if console.protocol == "spice":
            self._force_vnc.add(key)
            self._force_spice.discard(key)
            self.set_status("Reopening on VNC...")
        else:
            self._force_vnc.discard(key)
            # Overrules the VM's own "VNC only" setting and the global
            # preference for this console; without it, the reopen would
            # land straight back on VNC and look like nothing happened.
            self._force_spice.add(key)
            self.set_status("Reopening on SPICE...")
        self.reconnect_console(key)

    def _can_use_spice(self, guest):
        """Whether SPICE is worth offering for a guest at all.

        Neither the global "always use VNC" nor the guest's own protocol
        setting appears here: both are answers to "what should this open
        as", and Reopen with SPICE exists precisely to overrule them once.
        Only a guest that has no SPICE display makes the entry pointless.
        """
        return bool(
            SPICE_AVAILABLE
            and guest is not None
            and not guest.is_container
            and guest.spice_capable is not False
        )

    # How long a session we just closed may still be counted by QEMU.
    SPICE_LINGER = 15

    def _holds_spice_session(self, key):
        """Whether this client already has a live SPICE session on a guest.

        Matters because QEMU counts our own connection like anyone else's:
        rebuilding a console we are already holding would otherwise look
        like somebody else was on it, and every reconnect would stop to ask.
        """
        console = self.consoles.get(key)
        return bool(
            console is not None
            and getattr(console, "protocol", "") == "spice"
            and getattr(console, "connected", False)
        )

    def _spice_occupancy(self, guest):
        """How many *other* clients hold this guest's SPICE session.

        Returns (count, addresses), or None for "could not tell". The two
        are not interchangeable: a failed question must never be read as
        "nobody is there", because acting on that is what throws somebody
        off their session. Runs on a worker thread.
        """
        if not self.config.get("spice_session_check", True):
            return None
        if guest.is_container:
            return None
        try:
            api = self.api_for(guest)
        except ProxmoxError:
            return None
        if not getattr(api, "monitor_available", True):
            return None
        try:
            count, addresses = api.spice_clients(guest.node, guest.vmid)
        except ProxmoxError as exc:
            print(
                f"[console] {guest.label}: could not check for other SPICE "
                f"clients ({exc})"
            )
            return None
        except Exception as exc:
            print(
                f"[console] {guest.label}: SPICE client check failed "
                f"({type(exc).__name__}: {exc})"
            )
            return None
        ours = 1 if self._holds_spice_session(guest.key) else 0
        if not ours:
            dropped = self._recent_spice.get(guest.key)
            if dropped is not None and (time.monotonic() - dropped < self.SPICE_LINGER):
                # We pulled our own session down a moment ago -- rebuilding
                # this very console is the usual reason we are here -- and
                # QEMU may not have finished noticing. Claim it as ours.
                ours = 1
        return max(0, count - ours), addresses

    def _plan_console(self, guest, takeover=False):
        """Decide between SPICE and VNC, and fetch whatever that needs.

        Runs on a worker thread. Returns a dict the main thread can turn
        into a widget without any further network access.

        Four things have a say, in order: this session's explicit choice for
        this tab, the guest's own protocol setting, the global preference,
        and finally what the guest's display can actually do.
        """
        # The config says whether a SPICE display exists at all, and carries
        # the notes the guest's settings live in, so it is read first
        # regardless of which way the decision is going to go. Selecting a
        # guest already reads it, so this is usually free.
        try:
            if guest.config_loaded and guest.config:
                config = guest.config
            else:
                config = self.api_for(guest).guest_config(
                    guest.node, guest.vmid, guest.kind
                )
                self.absorb_config(guest, config)
        except ProxmoxError as exc:
            config = {}
            print(f"[console] could not read the config for {guest.label}: {exc}")

        forced_vnc = guest.key in self._force_vnc
        forced_spice = guest.key in self._force_spice
        settings_vnc = (
            self.guest_settings(guest).get("protocol") == "vnc" and not forced_spice
        )
        prefer_vnc = bool(self.config.get("prefer_vnc")) and not forced_spice

        spice_possible = (
            SPICE_AVAILABLE
            and not guest.is_container
            and not forced_vnc
            and not settings_vnc
            and not prefer_vnc
        )

        reason = ""
        if forced_vnc:
            reason = "VNC chosen for this console"

        if spice_possible:
            # False is the only verdict that skips SPICE. An unrecognised
            # display type (None) is worth an attempt -- the server decides.
            if guest.spice_capable is not False:
                # Asked immediately before the ticket, so the window in
                # which somebody else could connect between the answer and
                # our own connect is as small as it can be made. It cannot
                # be closed: QEMU has no way to claim a session.
                if not takeover:
                    occupancy = self._spice_occupancy(guest)
                    if occupancy is not None and occupancy[0] > 0:
                        return {
                            "protocol": "occupied",
                            "clients": occupancy[0],
                            "addresses": occupancy[1],
                        }
                try:
                    params = self.api_for(guest).spice_config(
                        guest.node, guest.vmid, guest.kind
                    )
                    guest.console_note = ""
                    return {"protocol": "spice", "params": params}
                except ProxmoxError as exc:
                    # Fall through to VNC rather than failing outright; a
                    # console you can use beats a correct error message.
                    guest.spice_capable = False
                    reason = f"spiceproxy refused: {exc}"
                    print(f"[console] {guest.label}: {reason}")
            else:
                reason = f"display {guest.display or 'std'} has no SPICE"
        elif not guest.is_container and not forced_vnc:
            # Skipping SPICE because the client or the settings said so is
            # not evidence about the guest, so spice_capable stays as it is;
            # only a display that cannot do SPICE settles that question.
            if settings_vnc:
                reason = "VNC only in this VM's settings"
            elif prefer_vnc:
                reason = "VNC forced in preferences"
            elif not SPICE_AVAILABLE:
                reason = "spice-gtk is not installed"
                guest.spice_capable = False

        guest.console_note = reason
        api = self.api_for(guest)
        session = api.vnc_ticket(guest.node, guest.vmid, guest.kind)
        url = api.vnc_websocket_url(
            guest.node, guest.vmid, session["port"], session["ticket"], guest.kind
        )
        return {
            "protocol": "vnc",
            "url": url,
            "headers": api.basic_ws_headers(),
            "password": session["ticket"],
            "reason": reason,
        }

    def guest_prefs(self, key):
        """Console settings for one guest, falling back to the globals."""
        stored = (self.config.get("guest_prefs") or {}).get(key, {})
        return {
            "scaling": stored.get(
                "scale_to_fit", self.config.get("scale_to_fit", False)
            ),
            "auto_resize": stored.get(
                "auto_resize", self.config.get("auto_resize", True)
            ),
            "codec_index": stored.get("codec_index", 0),
            "compression_index": stored.get("compression_index", 0),
            # Clipboard sharing, audio and the console protocol are NOT
            # here: they are the guest's own settings and live in its notes
            # on the server. What is left in this dict is genuinely about
            # this machine -- how big the console is drawn and how hard it
            # is willing to work to draw it.
        }

    def _save_guest_pref(self, name, value, key=None):
        """Remember a setting against the guest it was made on."""
        if key is None:
            console = self.current_console()
            key = getattr(console, "guest_key", None) if console else None
        if not key:
            return
        prefs = dict(self.config.get("guest_prefs") or {})
        entry = dict(prefs.get(key) or {})
        entry[name] = value
        prefs[key] = entry
        self.config["guest_prefs"] = prefs
        self.config.save()

    def _build_console(self, guest, plan, replace=False, automatic=False):
        if self._closing:
            return False

        if plan["protocol"] == "occupied":
            self._console_occupied(guest, plan, automatic)
            return False

        title = guest.name
        prefs = self.guest_prefs(guest.key)
        if plan["protocol"] == "spice":
            console = SpiceConsole(
                plan["params"],
                title=title,
                on_status=lambda text: self._console_status(guest, text),
                enable_audio=bool(self.config.get("enable_audio", True)),
                auto_resize=bool(prefs["auto_resize"]),
                scale_to_fit=bool(prefs["scaling"]),
                share_clipboard=self._guest_switch(guest, "clipboard"),
                play_audio=self._guest_switch(guest, "audio"),
                on_agent=lambda connected, c=None: self._on_console_agent(
                    self.consoles.get(guest.key), connected
                ),
                on_disconnect=lambda reason, k=guest.key: self._on_console_disconnected(
                    k, reason
                ),
                on_reconnect=lambda k=guest.key: self.reconnect_console(k),
            )
        else:
            console = VncConsole(
                plan["url"],
                plan["headers"],
                plan["password"],
                title=title,
                on_status=lambda text: self._console_status(guest, text),
                verify_ssl=bool(self.config.get("verify_ssl", False)),
                scale_to_fit=bool(prefs["scaling"]),
                on_disconnect=lambda reason, k=guest.key: self._on_console_disconnected(
                    k, reason
                ),
                on_reconnect=lambda k=guest.key: self.reconnect_console(k),
            )

        if plan["protocol"] == "spice" and (
            prefs["codec_index"] or prefs["compression_index"]
        ):

            def apply_prefs():
                # Only meaningful once the display channel exists.
                console.set_codec_index(prefs["codec_index"])
                console.set_compression_index(prefs["compression_index"])
                return False

            GLib.timeout_add_seconds(2, apply_prefs)

        self._install_console(guest, console)

        if plan["protocol"] == "vnc" and not guest.is_container:
            reason = plan.get("reason") or "no SPICE display configured"
            self.set_status(f"{guest.label}: VNC - {reason}")
        elif (
            plan["protocol"] == "spice"
            and self.config.get("enable_audio", True)
            and guest.config
            and not audio_is_spice(guest.config.get("audio0"))
        ):
            # Nothing client-side can produce sound for a VM that has no
            # audio device, and that is the Proxmox default.
            self.set_status(
                f"{guest.label}: no SPICE audio device "
                "(add audio0: device=ich9-intel-hda,driver=spice in Proxmox)"
            )
        return False

    # -- somebody else is already on it ---------------------------------

    @staticmethod
    def _occupancy_text(count, addresses):
        """One sentence about who is already connected.

        The addresses are what QEMU sees, and everything reaches it through
        the node's own spiceproxy, so they are usually the proxy rather than
        the person. They are shown when they are not obviously local and
        described as where the connection arrives from, which is all they
        honestly say.
        """
        subject = "Another client is" if count == 1 else f"{count} other clients are"
        text = f"{subject} already connected to this VM's SPICE console."
        hosts = sorted({a.rsplit(":", 1)[0].strip("[]") for a in addresses or () if a})
        remote = [h for h in hosts if h not in ("127.0.0.1", "::1", "localhost", "")]
        if remote:
            text += f"  Arriving from {', '.join(remote[:3])}."
        return text

    def _console_occupied(self, guest, plan, automatic):
        """Offer the ways out when a guest's SPICE session is taken.

        QEMU serves one SPICE client at a time, so connecting anyway is not
        sharing -- it disconnects whoever is there. That has to be a
        decision, never a side effect of double-clicking a VM.
        """
        key = guest.key
        summary = self._occupancy_text(plan.get("clients", 1), plan.get("addresses"))
        self.set_status(f"{guest.label}: {summary}")

        def take_over():
            self.set_status(f"{guest.label}: taking over the console...")
            self.open_console(key, replace=True, takeover=True)

        def use_vnc():
            # VNC genuinely is multi-client, so this is the answer that
            # leaves the other person where they are.
            self._force_vnc.add(key)
            self._force_spice.discard(key)
            self.set_status(f"{guest.label}: opening on VNC...")
            self.open_console(key, replace=True)

        def leave_on_tab():
            console = self.consoles.get(key)
            if console is not None and hasattr(console, "show_choice_state"):
                console.show_choice_state(
                    "Console already in use",
                    summary + "\n\nQEMU serves one SPICE client at a time, "
                    "so taking over will disconnect them. VNC can be shared.",
                    actions=[("Take Over", take_over), ("Open with VNC", use_vnc)],
                )

        if automatic:
            # Nothing was clicked -- a restored session, or the poll finding
            # the guest back up. A modal here would land on top of whatever
            # the user is doing, possibly several at once.
            leave_on_tab()
            return

        choice = self._ask_takeover(guest, summary)
        if choice == "take":
            take_over()
        elif choice == "vnc":
            use_vnc()
        else:
            # Cancelled. The tab is still sitting on "Connecting...", which
            # would be a lie, so put the choice on it.
            leave_on_tab()

    def _ask_takeover(self, guest, summary):
        """Ask what to do about an occupied console. Returns take/vnc/None."""
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text=f"{guest.label}: console already in use",
        )
        dialog.format_secondary_text(
            summary + "\n\nQEMU serves one SPICE client at a time, so taking "
            "over will disconnect them. VNC can be shared, at the cost of "
            "the clipboard, audio and guest resize."
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        vnc = dialog.add_button("Open with VNC", 2)
        vnc.get_style_context().add_class("suggested-action")
        take = dialog.add_button("Take Over", 1)
        take.get_style_context().add_class("destructive-action")
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        theme_decorate(dialog)
        response = dialog.run()
        dialog.destroy()
        return {1: "take", 2: "vnc"}.get(response)

    def tab_title(self, guest):
        """A console tab's caption, in whichever form the settings ask for."""
        style = self.config.get("tab_title_format", "name")
        if style == "id":
            return str(guest.vmid)
        if style == "both":
            return f"{guest.name} ({guest.vmid})"
        return guest.name

    def _apply_name_formats(self):
        """Redraw every name after the tab or tree format changed."""
        self.sidebar.set_name_format(
            self.config.get("tree_name_format", "name"),
            templates_last=bool(self.config.get("templates_last", True)),
        )
        for page in self.panes.all_pages():
            guest = self.sidebar.guests.get(getattr(page, "guest_key", None) or "")
            notebook = self.panes.notebook_of(page)
            label = notebook.get_tab_label(page) if notebook else None
            if guest is not None and hasattr(label, "set_title"):
                label.set_title(self.tab_title(guest))

    def _install_console(self, guest, console):
        """Put a console on screen, replacing any existing one for the guest.

        Replacing in place keeps the tab -- or the pop-out window -- exactly
        where it was, which is what makes a reconnect look like the picture
        coming back rather than the tab closing and reopening.
        """
        console.guest_key = guest.key
        old = self.consoles.get(guest.key)

        window = self._popouts.get(guest.key)
        if window is not None:
            window.replace_console(console)
            self.consoles[guest.key] = console
            if old is not None and old is not console:
                self._shutdown_console(old)
            self._sync_view_menu()
            return -1

        label = ConsoleTabLabel(
            self.tab_title(guest),
            console.protocol,
            on_close=lambda c=console: self.close_console_widget(c),
        )

        # Replacing in the pane the old console was in, so a reconnect
        # does not yank the tab back to pane 0.
        notebook = (
            self.panes.notebook_of(old)
            if old is not None and old is not console
            else None
        )
        page = notebook.page_num(old) if notebook is not None else -1
        if page >= 0:
            self.panes.insert(console, label, notebook, page)
            # The old page has shifted along by one now.
            notebook.remove_page(page + 1)
        else:
            page = self.panes.append(console, label)
            notebook = self.panes.notebook_of(console)

        notebook.set_current_page(notebook.page_num(console))
        self.consoles[guest.key] = console
        if old is not None and old is not console:
            self._shutdown_console(old)
        self._sync_view_menu()
        self._refresh_console_indicators(console)
        return page

    def _refresh_console_indicators(self, console):
        """Re-read the clipboard and audio state for a console just installed.

        A rebuilt console is a different widget with a different protocol
        and a fresh session, so the two switches in the status bar have to
        be asked again -- otherwise they keep describing the console that
        was replaced.
        """
        if console is not self.current_console():
            return
        self._update_audio_indicator(console)
        self._update_clipboard_indicator(console)

    def _shutdown_console(self, console):
        key = getattr(console, "guest_key", None)
        if (
            key
            and getattr(console, "protocol", "") == "spice"
            and getattr(console, "connected", False)
        ):
            # Remember that the session QEMU is about to notice losing was
            # ours, so the occupancy check does not mistake it for a
            # stranger a second from now.
            self._recent_spice[key] = time.monotonic()
        try:
            console.shutdown()
        except Exception:
            traceback.print_exc()

    def _console_status(self, guest, text):
        def apply():
            self.set_status(f"{guest.label}: {text}")
            # Connecting finishes after the tab exists, so the entries that
            # need a live console are refreshed here too.
            self._sync_view_menu()
            return False

        GLib.idle_add(apply)

    def _console_failed(self, guest, message):
        self.set_status(f"{guest.label}: console failed - {message}")
        console = self.consoles.get(guest.key)
        if console is not None and hasattr(console, "show_error_state"):
            # Keeps the tab, explains itself, and offers Reconnect -- better
            # than a modal that has to be dismissed before anything else.
            console.show_error_state(message)
        else:
            self._error_dialog(f"Console failed: {guest.label}", message)
        return False

    def _on_console_disconnected(self, key, reason):
        """A console's connection ended by itself."""
        guest = self.sidebar.guests.get(key)
        label = guest.label if guest else key
        self.set_status(f"{label}: {reason}")
        # The guest's own state usually explains it, so poll hard for a bit
        # and let the panel wording follow.
        self.burst_poll()
        console = self.consoles.get(key)
        if console is not None and guest is not None:
            GLib.timeout_add_seconds(
                2, lambda: (self._explain_disconnect(key), False)[1]
            )

    def _explain_disconnect(self, key):
        """Reword the panel once the inventory says what the guest is doing."""
        console = self.consoles.get(key)
        guest = self.sidebar.guests.get(key)
        if console is None or guest is None:
            return
        panel = getattr(console, "status_panel", None)
        if panel is None or not panel.get_visible():
            return
        if guest.status == "running":
            panel.show_message(
                "Connection closed",
                "The guest is running. It may have been reset or migrated.",
                can_reconnect=True,
            )
            # A running guest that dropped us is the one case worth a second
            # question: being displaced by another client looks exactly like
            # a network blip from here, and only QEMU knows the difference.
            if getattr(console, "protocol", "") == "spice":
                self._check_displaced(key)
        elif guest.status in ("stopped", "suspended"):
            panel.show_message(
                f"Guest is {guest.status}",
                "Start the guest to open a console again.",
                icon="media-playback-stop-symbolic",
                can_reconnect=False,
            )
        elif guest.status == "paused":
            panel.show_message(
                "Guest is paused",
                "Resume the guest to reconnect.",
                icon="media-playback-pause-symbolic",
                can_reconnect=False,
            )

    def _check_displaced(self, key):
        """Ask whether somebody else is on the console we just lost.

        Off-thread, and purely to improve the wording -- the panel already
        says the right thing if this never answers.
        """
        guest = self.sidebar.guests.get(key)
        if guest is None:
            return
        # Our own session is gone; anything QEMU still reports is somebody
        # else, so the linger allowance must not apply here.
        self._recent_spice.pop(key, None)

        def worker():
            occupancy = self._spice_occupancy(guest)
            if occupancy is None or occupancy[0] <= 0:
                return
            GLib.idle_add(self._say_displaced, key, occupancy)

        threading.Thread(
            target=worker, daemon=True, name=f"displaced-{guest.vmid}"
        ).start()

    def _say_displaced(self, key, occupancy):
        console = self.consoles.get(key)
        guest = self.sidebar.guests.get(key)
        if console is None or guest is None:
            return False
        panel = getattr(console, "status_panel", None)
        if panel is None or not panel.get_visible():
            return False

        def take_back():
            self.open_console(key, replace=True, takeover=True)

        def use_vnc():
            self._force_vnc.add(key)
            self._force_spice.discard(key)
            self.open_console(key, replace=True)

        # Both buttons are explicit actions and Reconnect is off: the panel
        # has already said what taking it back would do to the other person,
        # and asking the same question twice would only teach people to
        # click through it.
        panel.show_message(
            "Someone else took over this console",
            self._occupancy_text(occupancy[0], occupancy[1])
            + "\n\nTaking it back will disconnect them in turn. VNC can be "
            "shared.",
            icon="dialog-warning-symbolic",
            can_reconnect=False,
            actions=[("Take It Back", take_back), ("Open with VNC", use_vnc)],
        )
        self.set_status(f"{guest.label}: another client took over the console")
        return False

    def reconnect_console(self, key, automatic=False):
        """Rebuild a guest's console in place.

        The tab is deliberately left alone: closing and reopening it makes
        the console vanish and come back, moves it to the end of the tab
        strip, and drops the pop-out window if there was one. Only the
        widget inside is replaced.
        """
        guest = self.sidebar.guests.get(key)
        if guest is None:
            return
        self._console_offline.pop(key, None)
        if getattr(self, "_reconnecting", None) == key:
            return  # a reconnect is already in flight
        self._reconnecting = key
        GLib.timeout_add_seconds(
            8, lambda: (setattr(self, "_reconnecting", None), False)[1]
        )
        self.open_console(key, replace=True, automatic=automatic)

    # -- pop out -------------------------------------------------------

    def popout_console(self):
        """Move the active console into a window of its own."""
        console = self.current_console()
        if console is None:
            return
        key = getattr(console, "guest_key", None)
        guest = self.sidebar.guests.get(key) if key else None
        if guest is None:
            return

        if self.fullscreen_control.active:
            self.fullscreen_control.leave()

        notebook = self.panes.notebook_of(console)
        page = notebook.page_num(console) if notebook is not None else -1
        if page < 0:
            return
        # Remember where it was so returning it lands in the same place.
        self._popout_pages[key] = page
        # detach(), not remove_page(): the widget has to survive the move,
        # and self.consoles holds the only other reference.
        self.panes.detach(console)

        window = ConsoleWindow(self, console, guest)
        self._popouts[key] = window
        window.show_all()
        self._sync_view_menu()
        self.set_status(f"{guest.label}: console popped out")

    def reclaim_console(self, key, console):
        """Take a console back from a pop-out window into the notebook."""
        self._popouts.pop(key, None)
        if self._closing:
            return
        guest = self.sidebar.guests.get(key)
        title = self.tab_title(guest) if guest else getattr(console, "title", "console")

        label = ConsoleTabLabel(
            title, console.protocol, on_close=lambda: self.close_console(key)
        )
        self.panes.insert(
            console, label, self.panes.primary, self._popout_pages.pop(key, -1)
        )
        self.panes.focus_page(console)
        self.consoles[key] = console
        self._sync_view_menu()

    def run_action_for(self, key, action_name):
        """Power action from a pop-out window's toolbar."""
        self._run_action(key, action_name)

    def snapshot_action_for(self, key, which):
        """Snapshot action from a pop-out window's toolbar."""
        self._snapshot_action(which, key)

    def _update_popouts(self):
        for window in list(self._popouts.values()):
            window.update_sensitivity()

    def close_console_widget(self, console):
        """Close a specific console widget, whoever owns its key now."""
        self.panes.remove_page(console)
        key = getattr(console, "guest_key", None)
        # Only forget the key if it still points at this widget; a reconnect
        # may already have replaced it.
        if key and self.consoles.get(key) is console:
            self.consoles.pop(key, None)
            self._console_offline.pop(key, None)
            self._pending_actions.pop(key, None)
            self._clear_session_choices(key)
        self._shutdown_console(console)
        self._sync_view_menu()

    def _clear_session_choices(self, key):
        """Forget the temporary console choices made for one guest.

        Closing the tab is what ends a session: the protocol it was switched
        to, and the clipboard and audio buttons, all go back to what the
        guest's own settings say the next time it is opened.
        """
        self._force_vnc.discard(key)
        self._force_spice.discard(key)
        for name in ("clipboard", "audio"):
            self._session_switches.pop((key, name), None)

    def close_console(self, key):
        self._clear_session_choices(key)
        window = self._popouts.pop(key, None)
        if window is not None:
            window.shutdown()
            window.destroy()
        console = self.consoles.pop(key, None)
        if console is None:
            return
        # Closing the console that is filling the screen would otherwise
        # leave the window fullscreen with no chrome and nothing in it.
        if self.fullscreen_control.active and console is self.current_console():
            self.fullscreen_control.leave()
        self.panes.remove_page(console)
        self._shutdown_console(console)
        self._sync_view_menu()

    def _close_current_console(self):
        widget = self.panes.current_page()
        if widget is None or widget is self.summary:
            return
        if widget is not None:
            # By widget, not by key: after a reconnect the key can point at a
            # different tab, and closing that one leaves this one stranded.
            self.close_console_widget(widget)

    def _on_page_switched(self, _panes, _notebook, page_widget, _page_num):
        if not self._ready:
            return

        # "switch-page" fires before the page becomes current, so the
        # incoming widget is passed in rather than read back a frame later.
        # Reading it back is what made the toolbar flash the old state.
        # None here is deliberate: the summary page has no console, and
        # reading it back would still return the outgoing tab's.
        incoming = page_widget if hasattr(page_widget, "protocol") else None
        self._sync_view_menu(incoming)
        self._context_changed(incoming)

        if page_widget is self.summary:
            guest = self.sidebar.selected_guest()
            if guest is not None:
                self.summary.show_guest(guest, self.api_for(guest))
            return
        key = getattr(page_widget, "guest_key", None)
        if key:
            self.sidebar.select_key(key)
        # Give the console the keyboard as soon as its tab is shown.
        if hasattr(page_widget, "grab_focus_display"):
            GLib.idle_add(page_widget.grab_focus_display)

    # -- view menu <-> active console ----------------------------------

    def current_console(self):
        """The console on the visible tab, or None on the summary page."""
        widget = self.panes.current_page()
        return widget if hasattr(widget, "protocol") else None

    def _sync_view_menu(self, console=_CURRENT):
        """Point the view menu at the active console.

        Set with the guard up: assigning to a CheckMenuItem emits 'toggled'
        exactly as a click does, which would otherwise push this state
        straight back into the console it was just read from.
        """
        if console is _CURRENT:
            console = self.current_console()
        self._updating_view_menu = True
        try:
            supports = getattr(console, "supports", {}) if console else {}

            self.auto_resize_item.set_sensitive(bool(supports.get("auto_resize")))
            self.auto_resize_item.set_active(
                bool(console and getattr(console, "auto_resize", False))
            )

            self.scaling_item.set_sensitive(bool(supports.get("scaling")))
            self.scaling_item.set_active(
                bool(console and getattr(console, "scaling", False))
            )

            self.codec_item.set_sensitive(bool(supports.get("codec")))
            if console is not None and supports.get("codec"):
                index = getattr(console, "codec_index", 0)
                self.codec_items[index].set_active(True)

            self.compression_item.set_sensitive(bool(supports.get("compression")))
            if console is not None and supports.get("compression"):
                index = getattr(console, "compression_index", 0)
                self.compression_items[index].set_active(True)

            self.refresh_frame_item.set_sensitive(bool(supports.get("refresh")))

            # Splitting needs a console to move and somewhere to put it, and
            # is pointless when it is the only tab in a pane -- that would
            # just move an empty gap around.
            panes = self.panes.pane_count()
            holder = self.panes.notebook_of(console) if console is not None else None
            can_split = bool(
                console is not None
                and panes < MAX_PANES
                and holder is not None
                and holder.get_n_pages() > 1
            )
            self.split_item.set_sensitive(can_split)
            self.split_item_tb.set_sensitive(can_split)
            self.gather_item.set_sensitive(panes > 1)

            self.fullscreen_item.set_sensitive(console is not None)
            self.fullscreen_item_tb.set_sensitive(console is not None)
            self.popout_item.set_sensitive(console is not None)
            self.ctrl_alt_del_item.set_sensitive(bool(supports.get("ctrl_alt_del")))
            self.screenshot_item.set_sensitive(
                console is not None
                and hasattr(console, "screenshot")
                and getattr(console, "connected", False)
            )
            self.close_console_item.set_sensitive(console is not None)
            self._sync_protocol_switch(console)

            if console is None:
                self.protocol_label.set_text("")
            elif console.protocol == "vnc":
                self.protocol_label.set_markup("<span foreground='#e5a50a'>VNC</span>")
                self.protocol_label.set_tooltip_text(
                    "No guest resize, clipboard or audio"
                )
            else:
                self.protocol_label.set_text("SPICE")
                self.protocol_label.set_tooltip_text("")
        finally:
            self._updating_view_menu = False

    def _sync_protocol_switch(self, console):
        """Point the reopen entry at whichever protocol is not in use."""
        item = self.switch_protocol_item
        guest = (
            self.sidebar.guests.get(getattr(console, "guest_key", None) or "")
            if console
            else None
        )
        if console is None or guest is None:
            item.set_label("Reopen Console with VNC")
            item.set_sensitive(False)
            item.set_tooltip_text("")
            return

        if console.protocol == "spice":
            item.set_label("Reopen Console with VNC")
            item.set_sensitive(True)
            item.set_tooltip_text(
                "Reconnect this console over VNC. It stays on VNC until the "
                "tab is closed."
            )
        else:
            item.set_label("Reopen Console with SPICE")
            usable = self._can_use_spice(guest)
            item.set_sensitive(usable)
            item.set_tooltip_text(
                "" if usable else guest.console_note or "SPICE is not available here"
            )

    def _on_auto_resize_toggled(self, item):
        if self._updating_view_menu:
            return
        console = self.current_console()
        if console is not None and hasattr(console, "set_auto_resize"):
            console.set_auto_resize(item.get_active())
            self._save_guest_pref("auto_resize", item.get_active())

    def _on_scaling_toggled(self, item):
        if self._updating_view_menu:
            return
        console = self.current_console()
        if console is not None and hasattr(console, "set_scaling"):
            console.set_scaling(item.get_active())
            self._save_guest_pref("scale_to_fit", item.get_active())

    def _on_codec_selected(self, item, index):
        if self._updating_view_menu or not item.get_active():
            return
        console = self.current_console()
        if console is not None and hasattr(console, "set_codec_index"):
            console.set_codec_index(index)
            self._save_guest_pref("codec_index", index)

    def _on_compression_selected(self, item, index):
        if self._updating_view_menu or not item.get_active():
            return
        console = self.current_console()
        if console is not None and hasattr(console, "set_compression_index"):
            console.set_compression_index(index)
            self._save_guest_pref("compression_index", index)

    def _split_console(self):
        """Give the console in front a pane of its own."""
        console = self.current_console()
        if console is None:
            return
        if self.fullscreen_control.active:
            self.fullscreen_control.leave()
        if self.panes.pane_count() >= MAX_PANES:
            self.set_status(f"Already showing {MAX_PANES} panes")
            return
        if self.panes.split(console) is None:
            self.set_status("Could not split this console out")
            return
        self.set_status(f"{self.panes.pane_count()} panes - drag tabs between them")

    def _gather_consoles(self):
        if self.panes.pane_count() <= 1:
            return
        if self.fullscreen_control.active:
            self.fullscreen_control.leave()
        self.panes.gather()
        self.set_status("Back to one pane")

    def _refresh_framebuffer(self):
        console = self.current_console()
        if console is not None and hasattr(console, "refresh_framebuffer"):
            console.refresh_framebuffer()

    def _send_ctrl_alt_del(self):
        console = self.current_console()
        if console is not None:
            console.send_ctrl_alt_del()

    # -- fullscreen console --------------------------------------------

    def _save_chooser(self, title, filename):
        """A Save dialog, native to the platform where GTK can manage one.

        GtkFileChooserNative hands off to the real Windows save dialog -- the
        one with the sidebar, the recent places and the folder layout the
        rest of the system uses -- instead of GTK's own, which on Windows
        looks like nothing else on screen. It is a plain GtkFileChooserDialog
        anywhere GTK has no native backend, so the fallback is automatic;
        the explicit one below is only for a GTK too old to have the class.
        """
        try:
            chooser = Gtk.FileChooserNative.new(
                title, self, Gtk.FileChooserAction.SAVE, "_Save", "_Cancel"
            )
        except (AttributeError, TypeError):
            chooser = Gtk.FileChooserDialog(
                title=title, transient_for=self, action=Gtk.FileChooserAction.SAVE
            )
            chooser.add_buttons(
                "Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.ACCEPT
            )
            # Only the GTK dialog is ours to theme; the native one is the
            # system's and must be left alone.
            theme_decorate(chooser)
        chooser.set_do_overwrite_confirmation(True)
        chooser.set_current_name(filename)

        png = Gtk.FileFilter()
        png.set_name("PNG image")
        png.add_pattern("*.png")
        chooser.add_filter(png)

        response = chooser.run()
        path = chooser.get_filename()
        chooser.destroy()
        if response not in (Gtk.ResponseType.ACCEPT, Gtk.ResponseType.OK):
            return None
        return path

    def _save_screenshot(self):
        console = self.current_console()
        if console is None:
            return
        path = self._save_chooser("Save Console Screenshot", f"{console.title}.png")
        if not path:
            return
        # The native dialog does not append the filter's extension.
        if not os.path.splitext(path)[1]:
            path += ".png"
        try:
            ok = console.screenshot(path)
        except Exception as exc:
            self.set_status(f"Screenshot failed: {exc}")
            return
        self.set_status(
            f"Screenshot saved to {path}" if ok else "Nothing to capture yet"
        )

    def _toggle_fullscreen(self):
        self.fullscreen_control.toggle()

    def _on_key_press(self, _widget, event):
        return self.fullscreen_control.handle_key_press(event)

    def _on_key_release(self, _widget, event):
        return self.fullscreen_control.handle_key_release(event)

    def _on_active_changed(self, *_args):
        """Alt-tabbing away must not leave the guest holding the keyboard.

        spice-gtk's keyboard grab is a low-level hook on Windows, so without
        this it keeps swallowing keystrokes meant for whatever was focused
        next -- including the Ctrl+Alt that would have released it.
        """
        if self.is_active():
            return
        for console in list(self.consoles.values()):
            if hasattr(console, "release_input"):
                console.release_input()

    # ------------------------------------------------------------------
    # Dialogs and lifecycle
    # ------------------------------------------------------------------

    def _confirm(self, title, message):
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text=title,
        )
        dialog.format_secondary_text(message)
        theme_decorate(dialog)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        confirm = dialog.add_button(title, Gtk.ResponseType.OK)
        confirm.get_style_context().add_class("destructive-action")
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.OK

    def _confirm_destroy(self, title, message, expected):
        """Confirm something irreversible by typing the guest's VMID.

        A plain Yes/No is not enough here: destroying the wrong guest is the
        one mistake in this application that cannot be undone, and a dialog
        dismissed by reflex is exactly how it would happen. Returns whether
        to purge, or None if cancelled.
        """
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        confirm = dialog.add_button(title, Gtk.ResponseType.OK)
        confirm.get_style_context().add_class("destructive-action")
        confirm.set_sensitive(False)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        dialog.set_default_size(460, -1)

        content = dialog.get_content_area()
        content.set_spacing(8)
        content.set_border_width(12)

        heading = Gtk.Label(xalign=0.0)
        heading.set_line_wrap(True)
        heading.set_text(message)
        content.pack_start(heading, False, False, 0)

        ask = Gtk.Label(xalign=0.0)
        ask.get_style_context().add_class("dim")
        ask.set_text(f"Type {expected} to confirm:")
        content.pack_start(ask, False, False, 0)

        entry = Gtk.Entry()
        entry.set_activates_default(True)
        entry.connect(
            "changed", lambda e: confirm.set_sensitive(e.get_text().strip() == expected)
        )
        content.pack_start(entry, False, False, 0)

        purge = Gtk.CheckButton(label="Also remove from backup jobs and HA")
        purge.set_active(True)
        purge.set_tooltip_text(
            "Without this, a nightly backup job keeps trying to back up a "
            "guest that no longer exists"
        )
        content.pack_start(purge, False, False, 0)

        theme_decorate(dialog)
        dialog.show_all()
        entry.grab_focus()
        response = dialog.run()
        wanted = purge.get_active()
        matched = entry.get_text().strip() == expected
        dialog.destroy()
        if response != Gtk.ResponseType.OK or not matched:
            return None
        return wanted

    def _prompt(self, title, message, placeholder=""):
        """One-line text input. Returns the text, or None if cancelled."""
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        ok = dialog.add_button(title, Gtk.ResponseType.OK)
        ok.get_style_context().add_class("suggested-action")
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.set_default_size(460, -1)

        content = dialog.get_content_area()
        content.set_spacing(6)
        content.set_border_width(12)
        content.pack_start(Gtk.Label(label=message, xalign=0.0), False, False, 0)
        entry = Gtk.Entry()
        entry.set_activates_default(True)
        if placeholder:
            entry.set_placeholder_text(placeholder)
        content.pack_start(entry, False, False, 0)

        theme_decorate(dialog)
        dialog.show_all()
        response = dialog.run()
        text = entry.get_text().strip()
        dialog.destroy()
        return text if response == Gtk.ResponseType.OK and text else None

    def _show_text(self, title, body):
        """Scrollable monospace output, for agent results and the like."""
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.set_default_size(640, 400)

        buffer = Gtk.TextBuffer()
        buffer.set_text(body or "(no output)")
        view = Gtk.TextView(buffer=buffer, editable=False, monospace=True)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        scroll = Gtk.ScrolledWindow()
        scroll.add(view)
        dialog.get_content_area().pack_start(scroll, True, True, 0)

        theme_decorate(dialog)
        dialog.show_all()
        self.set_status("")
        dialog.run()
        dialog.destroy()

    def _error_dialog(self, title, message):
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=title,
        )
        dialog.format_secondary_text(message)
        theme_decorate(dialog)
        dialog.run()
        dialog.destroy()

    def _about(self):
        dialog = Gtk.AboutDialog(transient_for=self, modal=True)
        dialog.set_program_name(APP_NAME)
        dialog.set_version(__version__)
        dialog.set_comments(
            "Proxmox VE console client.\nSPICE via spice-gtk, with VNC fallback."
        )
        theme_decorate(dialog)
        dialog.run()
        dialog.destroy()

    def _on_window_state(self, _widget, event):
        """Track maximised and fullscreen, so neither is saved as the other.

        Fullscreen is a console mode, not a window preference: it comes and
        goes with Ctrl+Alt+Enter and must never be what the app reopens as.
        The maximised flag is therefore left at its last non-fullscreen
        value while a console is filling the screen.
        """
        state = event.new_window_state
        self._fullscreen_state = bool(state & Gdk.WindowState.FULLSCREEN)
        if not self._fullscreen_state:
            self._maximized = bool(state & Gdk.WindowState.MAXIMIZED)
        return False

    def _on_configure(self, *_args):
        """Remember the size the window has while it is an ordinary window.

        get_size() reports the maximised or fullscreen size in those states,
        so sampling it there would mean unmaximising into a window the size
        of the screen -- and, next launch, a "restored" window that fills it.
        """
        if not self._maximized and not self._fullscreen_state:
            self._normal_size = self.get_size()
        return False

    def _save_layout(self):
        width, height = self._normal_size
        self.config["window_width"] = width
        self.config["window_height"] = height
        self.config["window_maximized"] = self._maximized
        self.config["sidebar_width"] = self.paned.get_position()
        self.config.save()

    def shutdown(self):
        self._closing = True
        # Before anything is closed: from here on the notebook empties out
        # and there would be no session left to record.
        self._save_session()
        if self._poll_source is not None:
            GLib.source_remove(self._poll_source)
            self._poll_source = None
        if self._burst_source is not None:
            GLib.source_remove(self._burst_source)
            self._burst_source = None
        if self._burst_source is not None:
            GLib.source_remove(self._burst_source)
            self._burst_source = None
        if self._telemetry_source is not None:
            GLib.source_remove(self._telemetry_source)
            self._telemetry_source = None
        self.task_feed.stop()
        self.sidebar.stop()
        self.fullscreen_control.stop()
        for window in list(self._popouts.values()):
            window.shutdown()
            window.destroy()
        self._popouts.clear()
        self._dark_watcher.stop()
        for key in list(self.consoles):
            self.close_console(key)
        self._save_layout()

    def _disconnect(self):
        for connection in self.connections.all:
            self.disconnect_connection(connection.id)

    def _quit(self):
        self.shutdown()
        self.destroy()
        Gtk.main_quit()

    def _on_delete(self, *_args):
        self.shutdown()
        Gtk.main_quit()
        return False

"""The main window: menu bar, toolbar, inventory sidebar, console notebook.

Layout follows VMware Workstation closely -- inventory on the left, tabbed
consoles on the right, power controls on the toolbar and duplicated in the
sidebar's context menu.

Threading rule for the whole file: every call that touches the network runs
on a worker thread and comes back through GLib.idle_add. Nothing else is
allowed to block the main loop, because the main loop is also what draws the
console.
"""

import logging
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from .. import APP_NAME, __version__, logs, secrets, update
from ..api import AuthError, ProxmoxError, certs
from ..api import notes as notes_meta
from ..api.client import CertificateMismatch, CertificateUntrusted
from ..api.connection import CONNECTING, FAILED, Connection, ConnectionManager
from ..api.models import (
    audio_is_spice,
    spice_usb_ports,
    valid_guest_name,
    vga_head_limit,
    vga_is_spice,
    vga_memory_mib,
)
from ..console import (
    SERIAL_AVAILABLE,
    SPICE_AVAILABLE,
    SerialConsole,
    SpiceConsole,
    VncConsole,
)
from ..console import keys as console_keys
from ..console.placeholder import PlaceholderConsole
from ..console.scaling import (
    CONSOLE_SCALES,
    DEFAULT_CONSOLE_SCALE,
    clamp_console_scale,
    console_scale_index,
)
from ..console.serial import DEFAULT_FONT_SIZE
from ..console.spice import IMAGE_COMPRESSION, VIDEO_CODECS
from ..console.termproxy import open_session as open_term_session
from ..theme import apply as apply_theme
from ..theme import css as theme_css
from ..theme import decorate as theme_decorate
from ..theme import keep_active as theme_keep_active
from ..theme.system import DarkModeWatcher
from . import actions as action_defs
from . import desktop, status_icons, trust_dialog
from . import sidebar as sidebar_mod
from . import toolbar as toolbar_defs
from .clone import CloneDialog
from .console_tab import ConsoleTabLabel
from .console_window import ConsoleWindow
from .fullscreen import FullscreenController
from .guest_tab import CONSOLE as VIEW_CONSOLE
from .guest_tab import SUMMARY as VIEW_SUMMARY
from .guest_tab import GuestTab, console_of, tab_of
from .indicators import StatusIndicator
from .login_dialog import run_login
from .node_summary import NodeSummary
from .node_tab import NodeTab
from .settings_dialog import SettingsDialog
from .sidebar import Sidebar
from .snapshots import SnapshotManager, TakeSnapshotDialog
from .split import (
    SPLIT_NONE,
    SPLIT_SIDE_BY_SIDE,
    SPLIT_STACKED,
    SplitView,
)

# What the split button and its menu entry say they will do next, and the
# picture they wear while saying it.
SPLIT_LABELS = {
    SPLIT_SIDE_BY_SIDE: "Split Side by Side",
    SPLIT_STACKED: "Split Top and Bottom",
    SPLIT_NONE: "Close Split View",
}
SPLIT_STATUS = {
    SPLIT_SIDE_BY_SIDE: "Side by side - drag tabs between the panes",
    SPLIT_STACKED: "One above the other - drag tabs between the panes",
    SPLIT_NONE: "Back to one pane",
}
from .summary import GuestSummary
from .task_feed import TaskFeed
from .update_dialog import UpdateDialog
from .usb_dialog import UsbDeviceDialog, UsbPlugPrompt
from .vm_settings import VMSettingsDialog

log = logging.getLogger(__name__)

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
        # Nodes are kept apart from guests rather than sharing the two
        # dictionaries above. Everything that walks self.consoles is about a
        # guest -- power state, SPICE occupancy, snapshots -- and a node in
        # there would have to be skipped in every one of those loops.
        self.node_tabs = {}  # node key -> NodeTab
        self.node_consoles = {}  # node key -> shell widget
        self._poll_source = None
        self._poll_busy = False
        self._closing = False
        self._action_items = {}  # action name -> [widgets]
        self._updating_view_menu = False
        self._updating_usb_menu = False
        self._usb_dialog = None  # the device chooser, while it is open
        self._usb_prompt = None  # the "something was plugged in" question
        self._update_pending = False
        # Set while a pane toggle is being put back in step with its pane,
        # so doing so does not read as a click on it.
        self._syncing_toggles = False
        self._telemetry_source = None
        self._popouts = {}  # guest key -> ConsoleWindow
        self._popout_pages = {}  # guest key -> notebook index
        self._console_offline = {}  # guest key -> status when it stopped
        # The poll's own cadence: which interval the timer is currently on,
        # and how long a recent action keeps it on the faster one.
        self._poll_every = 0
        self._active_until = 0.0
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
        # The same idea for containers, which choose between a serial console
        # and VNC rather than between SPICE and VNC.
        self._force_serial = set()
        # Clipboard and audio switched from the status bar, per guest, for
        # as long as the console is open. Deliberately not persisted: the
        # buttons are helpers for the session in front of you, and the
        # settled answer belongs in the guest's own settings.
        self._session_switches = {}  # (guest key, name) -> bool
        # Guests whose console is being rebuilt, and when to stop believing
        # it. Per guest, not one for the window: two tabs reconnecting have
        # nothing to do with each other. See reconnect_console.
        self._reconnecting = {}  # guest key -> deadline
        # When we last tore down a live SPICE session, per guest. QEMU keeps
        # counting a client for a moment after it goes, so without this
        # every reconnect would look like somebody else was already on it.
        self._recent_spice = {}  # guest key -> monotonic time
        # Consoles to reopen from the last session, and when to give up
        # waiting for their guests to appear in the inventory.
        self._restore_keys = []
        self._restore_nodes = []
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
            self._embed_menubar_in_toolbar()
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
        self.sidebar.connect("node-selected", self._on_node_selected)
        self.sidebar.connect("node-activated", self._on_node_activated)
        self.sidebar.connect("node-action", self._on_node_action)
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
        # Clicking into a pane says which console the window should be
        # acting on, without necessarily switching tabs.
        self.panes.connect("pane-activated", self._on_pane_activated)
        self.notebook = self.panes.primary

        # A tab per guest, each holding that guest's console and summary.
        # There is no global summary page: a guest you have not opened has
        # no tab, exactly as it has no console.
        self.tabs = {}  # guest key -> GuestTab
        self.sidebar.view_mode = sidebar_mod.resolve_view(config.get("tree_view"))
        self.sidebar._update_view_button()

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
            all_monitors=self.all_monitors_enabled,
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

    def _embed_menubar_in_toolbar(self):
        """The menus at the left of the toolbar, sharing its row.

        The same move the header bar makes, for the window that has no
        header bar: the menus go somewhere that already exists rather than
        taking a row of their own, which is a lot of vertical space for four
        short words. Moved, not copied -- two menu bars would be two sources
        of truth for what is enabled.
        """
        holder = Gtk.ToolItem()
        holder.add(self.menubar)
        # The menu bar draws its own background and border, which look wrong
        # sitting inside a toolbar; the class is what the stylesheet keys on
        # to take them off.
        holder.get_style_context().add_class("toolbar-menus")
        self._menubar_item = holder
        self.toolbar.insert(holder, 0)
        self.toolbar.insert(Gtk.SeparatorToolItem(), 1)

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

        # Just the menus. Preferences lives on the File menu, an inch to the
        # left of where a button for it would have been.
        bar.pack_start(self.menubar)
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
        # Ctrl+Alt+Del keeps an entry of its own as well as its place at the
        # top of the submenu: it is the one people go looking for by name,
        # and being three inches from where it has always been is not an
        # improvement.
        self.ctrl_alt_del_item = self._menu_item(
            vm_menu, "Send Ctrl+Alt+Del", self._send_ctrl_alt_del
        )
        self.ctrl_alt_del_item.set_sensitive(False)
        self.send_key_item = Gtk.MenuItem(label="Send Key")
        self.send_key_item.set_submenu(toolbar_defs.send_key_menu(self._send_key))
        self.send_key_item.set_sensitive(False)
        vm_menu.append(self.send_key_item)
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
        # Built when the menu opens rather than kept in step with the host:
        # what is plugged in changes without asking us, and a stale list of
        # devices is worse than a list that takes a moment to appear.
        self.usb_menu = Gtk.Menu()
        self.usb_menu_item = Gtk.MenuItem(label="USB Devices")
        self.usb_menu_item.set_submenu(self.usb_menu)
        self.usb_menu_item.set_sensitive(False)
        vm_menu.append(self.usb_menu_item)
        # Built when the menu opens rather than kept in step with the host:
        # what is plugged in changes without asking us, and a stale list of
        # devices is worse than a list that takes a moment to appear.
        self.vm_menu = vm_menu
        vm_menu.connect("show", lambda *_: self._rebuild_usb_menu())

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

        view_item = self._build_view_menu()
        bar.append(view_item)

        help_menu = Gtk.Menu()
        help_item = Gtk.MenuItem(label="_Help", use_underline=True)
        help_item.set_submenu(help_menu)
        self._menu_item(help_menu, "Check for Updates...", self._check_updates_now)
        self._menu_item(help_menu, "Open Log Folder", self._open_log_folder)
        help_menu.append(Gtk.SeparatorMenuItem())
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

        # The same flip as the toolbar's Summary button, on the tab in front.
        self.summary_view_item = Gtk.CheckMenuItem(label="Summary")
        self.summary_view_item.set_tooltip_text(
            "Show this guest's summary instead of its console"
        )
        self.summary_view_item.set_sensitive(False)
        self.summary_view_item.connect("toggled", self._on_summary_toggled)
        self._add_accel(self.summary_view_item, "<Control><Alt>s")
        menu.append(self.summary_view_item)

        self.fullscreen_item = self._menu_item(
            menu, "Full Screen", self._toggle_fullscreen, accel="<Control><Alt>Return"
        )

        # Only ever sensitive for a guest that has a second head to show and
        # a second monitor to show it on, which is why it says "monitors"
        # rather than "displays": the ones it means are yours, not the
        # guest's.
        self.all_monitors_item = Gtk.CheckMenuItem(label="Use All Monitors")
        self.all_monitors_item.set_active(
            bool(self.config.get("fullscreen_all_monitors", True))
        )
        self.all_monitors_item.connect("toggled", self._on_all_monitors_toggled)
        menu.append(self.all_monitors_item)

        menu.append(Gtk.SeparatorMenuItem())

        self.auto_resize_item = Gtk.CheckMenuItem(label="Auto-resize Guest")
        self.auto_resize_item.set_tooltip_text("Requires spice-vdagent")
        self.auto_resize_item.connect("toggled", self._on_auto_resize_toggled)
        menu.append(self.auto_resize_item)

        self.scaling_item = Gtk.CheckMenuItem(label="Scale to Fit")
        self.scaling_item.connect("toggled", self._on_scaling_toggled)
        menu.append(self.scaling_item)

        self.console_scale_item, self.console_scale_items = self._radio_submenu(
            menu,
            "Display Scaling",
            [f"{percent}%" for percent in CONSOLE_SCALES],
            self._on_console_scale_selected,
        )
        self.console_scale_item.set_tooltip_text(
            "Draw the guest larger. On SPICE this asks the guest for fewer "
            "pixels, which is also less to send"
        )

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

        # One entry, not two. Splitting and unsplitting were separate
        # actions with separate rules about when each was allowed, and
        # between them they were grey more often than not. This is a cycle:
        # one pane, side by side, one above the other, back to one.
        self.split_item = self._menu_item(
            menu, SPLIT_LABELS[SPLIT_SIDE_BY_SIDE], self._cycle_split
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
            self.all_monitors_item,
            self.auto_resize_item,
            self.scaling_item,
            self.console_scale_item,
            self.codec_item,
            self.compression_item,
            self.split_item,
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
            self._add_accel(item, accel)
        return item

    def _add_accel(self, item, accel):
        key, modifier = Gtk.accelerator_parse(accel)
        if not hasattr(self, "_accels"):
            self._accels = Gtk.AccelGroup()
            self.add_accel_group(self._accels)
        item.add_accelerator(
            "activate", self._accels, key, modifier, Gtk.AccelFlags.VISIBLE
        )

    def _build_toolbar(self):
        bar = Gtk.Toolbar()
        bar.set_style(Gtk.ToolbarStyle.BOTH_HORIZ)
        bar.set_icon_size(Gtk.IconSize.SMALL_TOOLBAR)

        # One button, not a Console/Summary pair: the two were exact
        # opposites of each other, so a single toggle says the same thing in
        # half the space. Down means the console is showing, up means the
        # summary is. With nothing open it is also the way in -- pressing it
        # opens the selected guest's console. The View menu keeps a Summary
        # entry, which is where the keyboard shortcut lives.
        self.console_tool_item = Gtk.ToggleToolButton()
        self.console_tool_item.set_label("Console")
        self.console_tool_item.set_icon_name("video-display-symbolic")
        self.console_tool_item.set_is_important(True)
        self.console_tool_item.set_tooltip_text(
            "Switch between console and summary view"
        )
        self.console_tool_item.set_sensitive(False)
        self.console_tool_item.connect("toggled", self._on_console_toggled)
        bar.insert(self.console_tool_item, -1)

        bar.insert(Gtk.SeparatorToolItem(), -1)

        for name, item in toolbar_defs.add_power_buttons(
            bar, self._run_action_on_selection
        ).items():
            self._action_items.setdefault(name, []).append(item)

        bar.insert(Gtk.SeparatorToolItem(), -1)

        self.snapshot_items = toolbar_defs.add_snapshot_buttons(
            bar, self._snapshot_action, important=()
        )

        bar.insert(Gtk.SeparatorToolItem(), -1)

        self.popout_item = Gtk.ToolButton()
        self.popout_item.set_label("Pop Out")
        self.popout_item.set_icon_name("window-new-symbolic")
        self.popout_item.set_tooltip_text("Open this console in its own window")
        self.popout_item.set_sensitive(False)
        self.popout_item.connect("clicked", lambda *_: self.popout_console())
        bar.insert(self.popout_item, -1)

        self.split_item_tb = Gtk.ToolButton()
        self.split_item_tb.set_label("Split")
        # One picture for all three steps of the cycle. What the next press
        # will do is in the tooltip and on the menu entry; the button itself
        # stays put.
        self.split_item_tb.set_icon_name("view-dual-symbolic")
        self.split_item_tb.set_sensitive(False)
        self.split_item_tb.connect("clicked", lambda *_: self._cycle_split())
        bar.insert(self.split_item_tb, -1)

        self.fullscreen_item_tb = Gtk.ToolButton()
        self.fullscreen_item_tb.set_label("Full Screen")
        self.fullscreen_item_tb.set_icon_name("view-fullscreen-symbolic")
        self.fullscreen_item_tb.set_tooltip_text("Full screen (Ctrl+Alt+Enter)")
        self.fullscreen_item_tb.set_sensitive(False)
        self.fullscreen_item_tb.connect("clicked", lambda *_: self._toggle_fullscreen())
        bar.insert(self.fullscreen_item_tb, -1)

        # The one key combination nobody can send by pressing it, on the
        # toolbar rather than two menus deep.
        self.send_key_item_tb = toolbar_defs.send_key_button(self._send_key)
        bar.insert(self.send_key_item_tb, -1)

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

        # No Preferences button, and so no expanding spacer to push one to
        # the right: it is on the File menu, which is now on this same row.
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

        # Whether a USB device is currently in the guest's hands. Not a
        # switch like the two before it -- there is nothing to turn on
        # without saying which device -- so clicking opens the chooser.
        self.usb_icon = StatusIndicator(
            "drive-removable-media-symbolic",
            "USB redirection",
            on_toggle=self.open_usb_dialog,
        )
        box.pack_start(self.usb_icon, False, False, 2)

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
        self._set_indicator(self.usb_icon, None, "USB redirection")
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
        # The guest's USB ports appear when its usbredir channels connect,
        # and nothing signals that, so the poll is what notices.
        self._update_usb_indicator(console)
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
            # No need to clear the in-flight guard first: it only holds off
            # the poll's own reconnects, and this is somebody flipping a
            # switch. See reconnect_console.
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

    # -- USB redirection -----------------------------------------------

    def _usb_console(self, console=_CURRENT):
        """The console USB redirection would act on, or None.

        Only a connected SPICE console qualifies: usbredir is a SPICE
        channel, so on VNC there is nothing to carry a device over.
        """
        if console is _CURRENT:
            console = self.current_console()
        if console is None or not getattr(console, "supports", {}).get("usb"):
            return None
        # No manager at all means spice-gtk never built a session -- it is
        # not installed, and the tab says so already.
        if getattr(console, "usb", None) is None:
            return None
        return console

    def _update_usb_indicator(self, console=_CURRENT):
        """Whether a host device is currently in the guest's hands.

        Four different "no" answers, and they want different fixes: the
        console is VNC, spice-gtk cannot do USB on this machine, the VM has
        no port to redirect into, or nothing is redirected yet. Only the
        last of those is a state to click out of, so the others say what
        they need instead of just going dim.
        """
        if console is _CURRENT:
            console = self.current_console()
        guest = self.context_guest(console)
        label = "USB redirection"
        usable = self._usb_console(console)

        if usable is None:
            self._set_indicator(
                self.usb_icon,
                None,
                label,
                can_toggle=False,
                detail=(
                    "n/a - this console is VNC, which has no USB channel"
                    if console is not None
                    else "n/a - needs an open SPICE console"
                ),
            )
            return

        note = console.usb_note()
        if note:
            self._set_indicator(
                self.usb_icon, None, label, can_toggle=False, detail=note
            )
            return

        devices, channels = console.usb_snapshot()
        if channels == 0:
            # A VM with no 'usbN: spice' line has nowhere to put a device,
            # and that is the Proxmox default. Before the config has been
            # read, say the truthful weaker thing.
            if guest is not None and guest.config_loaded:
                detail = (
                    "no SPICE USB port on this VM. Add one in Proxmox: "
                    "Hardware -> Add -> USB Device -> Spice Port."
                    if not spice_usb_ports(guest.config)
                    else "the guest's USB port has not connected yet"
                )
            else:
                detail = "no redirection port available yet"
            self._set_indicator(
                self.usb_icon, False, label, can_toggle=True, detail=detail
            )
            return

        redirected = [device for device in devices if device.connected]
        if not redirected:
            # The host-side driver is only missing on Windows, and only
            # matters once there is somewhere to redirect to -- so it is
            # said here rather than over the reason the VM gave.
            self._set_indicator(
                self.usb_icon,
                False,
                label,
                can_toggle=True,
                detail=console.usb_advice()
                or "nothing redirected - click to choose a device",
            )
            return

        if len(redirected) == 1:
            summary = redirected[0].label
        else:
            summary = f"{len(redirected)} devices redirected"
        self._set_indicator(
            self.usb_icon,
            True,
            label,
            can_toggle=True,
            detail=f"{summary} - click to change",
        )

    def _rebuild_usb_menu(self):
        """Fill the VM menu's USB submenu from the console in front."""
        for child in self.usb_menu.get_children():
            self.usb_menu.remove(child)

        console = self._usb_console()
        self.usb_menu_item.set_sensitive(console is not None)
        if console is None:
            self.usb_menu_item.set_tooltip_text(
                "USB redirection needs a SPICE console. VNC has no channel "
                "to carry a device over."
            )
            return
        self.usb_menu_item.set_tooltip_text("Share a USB device with this guest")

        note = console.usb_note()
        devices, channels = console.usb_snapshot()
        if note or channels == 0:
            self._usb_menu_note(
                note or "This VM has no SPICE USB port (add 'usb0: spice' in Proxmox)"
            )
        else:
            if not devices:
                self._usb_menu_note("No USB devices are attached to this computer")
            for device in devices:
                item = Gtk.CheckMenuItem(label=device.label)
                self._updating_usb_menu = True
                try:
                    item.set_active(device.connected)
                finally:
                    self._updating_usb_menu = False
                item.set_sensitive(
                    device.redirectable and not console.usb.is_busy(device.key)
                )
                if not device.redirectable and device.reason:
                    item.set_tooltip_text(device.reason)
                item.connect("toggled", self._on_usb_menu_toggled, device.key)
                self.usb_menu.append(item)
            if console.usb_advice():
                self._usb_menu_note(console.usb_advice())

        self.usb_menu.append(Gtk.SeparatorMenuItem())
        self._menu_item(self.usb_menu, "USB Devices...", self.open_usb_dialog)
        self.usb_menu.show_all()

    def _usb_menu_note(self, text):
        """A line of explanation where the device list would have been."""
        item = Gtk.MenuItem(label=text)
        item.set_sensitive(False)
        self.usb_menu.append(item)

    def _on_usb_menu_toggled(self, item, key):
        if self._updating_usb_menu:
            return
        console = self._usb_console()
        if console is None:
            return
        item.set_sensitive(False)
        console.usb.toggle(
            key, lambda ok, message: self._usb_result(console, key, ok, message)
        )

    def _usb_result(self, console, key, ok, message):
        connected = console is not None and any(
            device.key == key and device.connected for device in console.usb_devices()
        )
        if not ok:
            self.set_status(f"USB: {message}")
        elif connected:
            self.set_status(f"USB: {key} connected to the guest")
        else:
            self.set_status(f"USB: {key} returned to this computer")
        self._update_usb_indicator()

    def open_usb_dialog(self, console=None):
        """The full device list, as a window that stays open while you plug.

        Takes a console explicitly for the popped-out window, which has one
        of its own and is not the tab in front of anything.
        """
        console = self._usb_console(console if console is not None else _CURRENT)
        if console is None:
            self.set_status("USB redirection needs an open SPICE console")
            return
        if self._usb_dialog is not None:
            self._usb_dialog.present()
            return
        guest = self.context_guest(console)
        # A popped-out console has a window of its own; a tab's toplevel is
        # this one. get_toplevel() returns the widget itself when it is in
        # neither, which is not something a dialog can be transient for.
        toplevel = console.get_toplevel()
        self._usb_dialog = UsbDeviceDialog(
            toplevel if isinstance(toplevel, Gtk.Window) else self,
            console,
            guest.label if guest is not None else console.title,
            on_status=self.set_status,
        )
        self._usb_dialog.connect("destroy", self._on_usb_dialog_closed)

    def _on_usb_dialog_closed(self, *_args):
        self._usb_dialog = None

    def _on_console_usb(self, key):
        """A device was plugged, pulled, or changed hands on a console."""

        def update():
            if self._closing:
                return False
            console = self.consoles.get(key)
            if console is not None and console is self.current_console():
                self._update_usb_indicator(console)
            if self._usb_dialog is not None and self._usb_dialog.console is console:
                self._usb_dialog.refresh()
            return False

        GLib.idle_add(update)

    def _on_console_usb_plugged(self, key, device_key, device_label):
        """Offer the new device to the guest, the way Workstation does."""

        def ask():
            if self._closing or not self.config.get("usb_autoprompt", True):
                return False
            if self._usb_prompt is not None:
                return False  # one question at a time
            console = self.consoles.get(key)
            if console is None or not getattr(console, "connected", False):
                return False
            # Every open SPICE session sees the same plug event, so only the
            # console being looked at is allowed to ask about it.
            window = self._popouts.get(key)
            if window is not None:
                if not window.is_active():
                    return False
            elif console is not self.current_console() or not self.is_active():
                return False
            if console.usb_channels() == 0:
                return False

            guest = self.sidebar.guests.get(key)
            self._usb_prompt = UsbPlugPrompt(
                window or self,
                guest.label if guest is not None else "this VM",
                device_label,
                lambda connect, stop: self._usb_prompt_answered(
                    key, device_key, connect, stop
                ),
            )
            return False

        GLib.idle_add(ask)

    def _usb_prompt_answered(self, key, device_key, connect, stop_asking):
        self._usb_prompt = None
        if stop_asking:
            self.config["usb_autoprompt"] = False
            self.config.save()
            self.set_status(
                "USB devices will not be offered again (Preferences -> Console)"
            )
        if not connect:
            return
        console = self.consoles.get(key)
        if console is None or getattr(console, "usb", None) is None:
            return
        console.usb.connect_device(
            device_key,
            lambda ok, message: self._usb_result(console, device_key, ok, message),
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
        # The toolbar button and the menu entry for each one, together.
        paired = {
            name: (self.snapshot_items[name], self.snapshot_menu_items[name])
            for name in self.snapshot_items
        }
        toolbar_defs.apply_snapshot_state(paired, guest)

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
        dark = apply_theme(self.config, self)
        self.sidebar.set_row_ypad(theme_css.ROW_YPAD)
        self.sidebar.set_dark(dark)
        self._update_connection_label()

    def _update_connection_label(self):
        count = len(self.connections.connected)
        total = len(self.connections)
        if total == 0:
            text = "not connected"
        elif count == total == 1:
            text = self.connections.all[0].label
        else:
            text = f"{count}/{total} servers"
        self.connection_label.set_text(text)
        if self.header_bar is not None:
            # The same fact, in the one place a header bar has room for it.
            self.header_bar.set_subtitle(text)

    def _on_system_theme_changed(self, _dark):
        if self.config.get("color_mode", "system") == "system":
            self.apply_appearance()

    def _settings_changed(self):
        """Everything in Preferences that takes effect while it is open.

        The tree's shape is here as well as on its own button: both write
        the same setting, so changing it in one place has to be visible in
        the other rather than waiting for a restart.
        """
        self.apply_appearance()
        self.sidebar.set_view_mode(
            sidebar_mod.resolve_view(self.config.get("tree_view"))
        )

    def open_settings(self):
        before = (self.config.get("font_backend"), self.config.get("sw_decoders"))
        dialog = SettingsDialog(self, self.config, on_change=self._settings_changed)
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
            connection = Connection.from_config(
                entry,
                secrets.decode,
                fingerprint=certs.pinned(
                    self.config, entry.get("host", ""), entry.get("port") or 8006
                ),
            )
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
            except (CertificateMismatch, CertificateUntrusted) as refusal:
                GLib.idle_add(self._connection_untrusted, connection, refusal)
                return
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

    def _connection_untrusted(self, connection, refusal):
        """A saved server whose certificate has not been approved.

        Asked about rather than failed: a first run after adding a server by
        hand, or a certificate that has been renewed since. Nothing was sent
        to the server before this -- the refusal happens during the TLS
        handshake, before the credentials go anywhere.
        """
        if self._closing:
            return False
        mismatch = isinstance(refusal, CertificateMismatch)
        info = refusal.info or {}
        if not info.get("sha256"):
            return self._connection_failed(
                connection, f"could not read the certificate for {refusal.host}"
            )
        if not trust_dialog.ask(
            self,
            refusal.host,
            refusal.port,
            info,
            mismatch,
            getattr(refusal, "expected", ""),
        ):
            return self._connection_failed(
                connection, "the certificate was not trusted"
            )

        certs.trust(self.config, refusal.host, refusal.port, info)
        self.config.save()
        connection.api.trust(info["sha256"])
        self._connect_async(connection)
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
        for key in [k for k in self.node_tabs if k.startswith(connection_id + "/")]:
            self.close_node(key)
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
        # Node pages come back too, and without their shells: a shell is a
        # session on the machine, not a view of it, and silently reopening
        # one at startup is not something to do on somebody's behalf.
        self._restore_nodes = list(self.config.get("session_nodes") or [])
        if self._restore_keys or self._restore_nodes:
            self._restore_until = time.monotonic() + self.RESTORE_TIMEOUT

    def _resume_session(self):
        """Reopen whichever saved consoles have turned up in the inventory.

        Called after every poll: guests arrive server by server, so the tabs
        come back as their servers finish connecting rather than all at once
        or not at all.
        """
        if not self._restore_keys and not self._restore_nodes:
            return
        if time.monotonic() >= self._restore_until:
            self._restore_keys = []
            self._restore_nodes = []
            return

        for key in list(self._restore_nodes):
            if self.node_for(key) is None:
                continue
            self._restore_nodes.remove(key)
            self.open_node(key, present=False)

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
        nodes = []
        for page in self.panes.all_pages():
            key = getattr(page, "guest_key", None)
            if key:
                keys.append(key)
            node_key = getattr(page, "node_key", None)
            if node_key:
                nodes.append(node_key)
        # A popped-out console is still an open console; it simply is not in
        # the notebook. It comes back as a tab, which is the honest default.
        keys.extend(k for k in self._popouts if k not in keys)
        self.config["session_consoles"] = keys
        self.config["session_nodes"] = nodes
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

        if (
            console is not None
            and getattr(console, "supports", {}).get("audio")
            and bool(getattr(console, "play_audio", True))
            != (settings.get("audio") != "disabled")
        ):
            pending.append("audio")
        if (
            console is not None
            and key not in self._force_vnc
            and key not in self._force_spice
            and key not in self._force_serial
        ):
            if guest.is_container:
                # "Default" means the serial console for a container, so the
                # only setting that asks for VNC is the one that says VNC.
                wanted = "vnc" if settings.get("protocol") == "vnc" else "serial"
                mismatch = console.protocol != wanted and (
                    wanted == "vnc" or SERIAL_AVAILABLE
                )
            elif settings.get("protocol") == "vnc":
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

    def burst_poll(self, seconds=None):
        """Watch closely for a while, and look now.

        Called after anything the cluster will take a few seconds to report.
        It does not start a timer of its own any more: it says that a change
        is expected, and the one poll timer moves to its faster cadence for
        as long as that holds. Two timers meant an action was answered with
        both of them at once, which is most of the traffic this used to
        cost.
        """
        if seconds is None:
            seconds = int(self.config.get("poll_active_for", 15))
        if seconds > 0:
            self._active_until = max(self._active_until, time.monotonic() + seconds)
            self._schedule_poll()
        self.refresh()

    def poll_intervals(self):
        """(while waiting, at rest) in seconds, as configured.

        The resting cadence is never faster than the active one: two
        settings that can be put the wrong way round would otherwise make
        the window poll harder for doing nothing.
        """
        active = max(1, int(self.config.get("poll_active_seconds", 2)))
        idle = max(active, int(self.config.get("poll_idle_seconds", 6)))
        return active, idle

    def _waiting_for_something(self):
        """Whether a change is expected that only the inventory can confirm.

        Everything here is something this window asked for and has not seen
        answered yet -- a power action, a rename, a console being restored,
        a server still connecting. While one of those is outstanding the
        poll is what ends the wait, so it runs at the faster cadence; with
        none of them outstanding the poll is only watching in case somebody
        else changes something, which does not need the same hurry.

        A guest that merely carries a lock is deliberately not counted: a
        backup can hold one for an hour, and an hour of fast polling is not
        what anybody meant by "waiting".
        """
        if time.monotonic() < self._active_until:
            return True
        if self._busy or self._pending_actions:
            return True
        if self._restore_keys or self._restore_nodes:
            return True
        return any(c.state == CONNECTING for c in self.connections.all)

    def _poll_interval(self):
        active, idle = self.poll_intervals()
        return active if self._waiting_for_something() else idle

    def _schedule_poll(self):
        """Arm the poll timer, or move it to the cadence now wanted."""
        interval = self._poll_interval()
        if self._poll_source is not None:
            if interval == self._poll_every:
                return
            GLib.source_remove(self._poll_source)
        self._poll_every = interval
        self._poll_source = GLib.timeout_add_seconds(interval, self._on_poll)

    def _restart_poll(self):
        """Re-arm from scratch, for when the numbers themselves changed."""
        if self._poll_source is not None:
            GLib.source_remove(self._poll_source)
            self._poll_source = None
        self._poll_every = 0
        self._schedule_poll()

    def _on_poll(self):
        if self._closing:
            self._poll_source = None
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
        self._refresh_open_summaries()
        self._refresh_open_node_summaries()
        # This poll is what answers most of what the cadence depends on, so
        # the next interval is decided here rather than guessed at when the
        # request went out.
        self._schedule_poll()
        return False

    def _refresh_open_summaries(self):
        """Keep the summaries that are actually on screen up to date.

        Only the visible ones: a summary behind a console redraws when its
        tab is flipped to, and the fields it shows come from the poll that
        has just run either way.
        """
        for key, tab in list(self.tabs.items()):
            guest = self.sidebar.guests.get(key)
            if guest is None or tab.view != VIEW_SUMMARY:
                continue
            if tab is self.panes.current_page():
                tab.summary.show_guest(guest, self.api_for(guest))

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
        # Something started or stopped being waited on, which is exactly
        # what decides how hard the inventory is polled.
        self._schedule_poll()

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

            if not guest.has_console:
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
                # There is nothing to watch, so show what there is to read.
                self._follow_guest_state(guest)
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
            # Running again: the console is the point of the tab once more.
            self._follow_guest_state(guest)

    def _follow_guest_state(self, guest):
        """Let a guest's power state pick the view its tab shows.

        Off or suspended, there is nothing on the console worth looking at,
        so the summary comes forward; powering back on brings the console
        back. A view the user chose by hand stands until the guest's power
        state actually changes.
        """
        tab = self.tabs.get(guest.key)
        if tab is None:
            return
        before = tab.view
        tab.follow_guest_state(guest)
        if tab.view != before and tab is self.panes.current_page():
            if tab.view == VIEW_SUMMARY:
                tab.summary.show_guest(guest, self.api_for(guest))
            self._sync_tab_view(tab)

    def _on_view_changed(self, _sidebar):
        """Folder view needs each guest's notes, which node view never reads."""
        if self.sidebar.folder_view:
            self.set_status("Folder view: reading guest notes...")
            self._load_folders()
        self.config["tree_view"] = self.sidebar.view_mode
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
            self._set_indicator(self.qga_icon, None, "Guest agent")
            self._agent_ok = False
            self._update_agent_menu()
            return
        # Selecting a guest that already has a tab open on its summary
        # refreshes it; a guest with no tab is not put on screen at all.
        tab = self.tabs.get(key)
        if tab is not None and tab.view == VIEW_SUMMARY:
            tab.summary.show_guest(guest, self.api_for(guest))
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
        self._update_usb_indicator(console)
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
        # The display adapter can be changed under a console that is already
        # open -- it needs a stop and start to take, but the config says so
        # straight away, and what it says decides whether extra monitors are
        # on offer.
        console = self.consoles.get(key)
        if console is not None and hasattr(console, "set_head_limit"):
            console.set_head_limit(vga_head_limit(guest.display))
            console.video_memory = max(1, vga_memory_mib(guest.display))
        current = self.context_guest()
        if current is not None and current.key == key:
            self._update_audio_indicator()
            self._update_clipboard_indicator()
        return False

    def _update_action_sensitivity(self, console=_CURRENT):
        guest = self.context_guest(console)
        toolbar_defs.apply_power_state(self._action_items, guest)
        # Live with a tab in front (there is a view to flip) or with a guest
        # that could open one.
        self.console_tool_item.set_sensitive(
            self.current_tab() is not None
            or (guest is not None and guest.has_console and not guest.template)
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

        # Starting a guest goes straight to its console, saying "Starting...",
        # rather than sitting on the summary until the cluster catches up.
        # Watching it boot is the point of having pressed the button.
        if action_defs.EXPECTED_STATUS.get(action.name) == "running":
            self._show_console_for_start(guest.key)

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

    def _show_console_for_start(self, key):
        """Bring a starting guest's console forward, before it is running.

        Marked as chosen so the poll that still reports "stopped" for the
        next second or two does not push the summary straight back.
        """
        tab = self.tabs.get(key)
        if tab is None or tab.console is None:
            return
        tab.show_view(VIEW_CONSOLE, by_user=True)
        if tab is self.panes.current_page():
            self._sync_tab_view(tab)

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
        for item in self.agent_items.values():
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
            address = GuestSummary._first_address(interfaces)
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

        existing_tab = self.tabs.get(key)
        if existing_tab is not None and not replace:
            # The guest already has a tab, so bring it forward rather than
            # opening a second one. A tab showing its summary flips to the
            # console -- but only where there is a picture to flip to. A
            # stopped or sleeping guest has none, and double-clicking one in
            # the tree used to land on an empty console; its summary is the
            # useful half, so that is what comes forward.
            self.panes.focus_page(existing_tab)
            if existing_tab.console is not None:
                wanted = (
                    VIEW_CONSOLE
                    if guest.has_console and not guest.template
                    else VIEW_SUMMARY
                )
                existing_tab.show_view(wanted, by_user=True)
                self._sync_tab_view(existing_tab)
                return
            if self.consoles.get(key) is not None:
                return

        existing = self.consoles.get(key)
        if existing is not None and not replace and existing_tab is None:
            if self.panes.focus_page(existing):
                return
            # Orphaned: in self.consoles but in no tab and no pop-out. Drop
            # it rather than leaving a console nothing can reach.
            self.close_console_widget(existing)

        if guest.template:
            self.set_status("Templates have no console")
            return

        if not guest.has_console:
            # Hold the tab open anyway; it becomes a real console as soon as
            # the guest starts. A guest stopped on an I/O error is not in
            # this branch: QEMU is up and still serving its frozen screen.
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
        waiting = PlaceholderConsole(
            title=guest.name,
            status="connecting",
            on_reconnect=lambda k=guest.key: self.reconnect_console(k),
        )
        self._install_console(guest, waiting)
        self.set_status(f"{guest.label}: connecting...")

        log.info(
            "opening a console for %s (replace=%s automatic=%s takeover=%s)",
            guest.label,
            replace,
            automatic,
            takeover,
        )
        GLib.timeout_add_seconds(
            self.CONSOLE_WATCHDOG,
            lambda: self._console_still_waiting(guest.key, waiting),
        )

        def worker():
            started = time.monotonic()
            try:
                plan = self._plan_console(guest, takeover=takeover)
            except ProxmoxError as exc:
                log.warning("%s: planning the console failed: %s", guest.label, exc)
                GLib.idle_add(self._console_failed, guest, str(exc))
                return
            except Exception as exc:
                log.exception("%s: planning the console raised", guest.label)
                GLib.idle_add(
                    self._console_failed, guest, f"{type(exc).__name__}: {exc}"
                )
                return
            log.info(
                "%s: plan is %s after %.2fs",
                guest.label,
                plan.get("protocol"),
                time.monotonic() - started,
            )
            GLib.idle_add(self._build_console, guest, plan, replace, automatic)

        threading.Thread(
            target=worker, daemon=True, name=f"console-{guest.vmid}"
        ).start()

    # Longer than the API's own timeout, so an ordinary slow server has
    # already given up and reported itself before this has anything to say.
    CONSOLE_WATCHDOG = 45

    def _console_still_waiting(self, key, placeholder):
        """A console that never became one. Say so instead of spinning.

        There is no legitimate way to sit here this long: every request the
        opening path makes carries a timeout, and every failure along it
        reports into the tab. Reaching this means something got lost --
        which used to look exactly like "the app does nothing", with the
        spinner still going and no way to find out more.
        """
        if self._closing or self.consoles.get(key) is not placeholder:
            return False
        if getattr(placeholder, "last_status", "") != "connecting":
            return False
        log.error(
            "%s: still waiting for a console after %ss",
            key,
            self.CONSOLE_WATCHDOG,
        )
        placeholder.show_error_state(
            f"Proxmox did not answer within {self.CONSOLE_WATCHDOG} seconds, "
            "and nothing reported why.\n\n"
            f"The log has the detail: {logs.current_log_file() or logs.log_dir()} "
            "(Help > Open Log Folder)."
        )
        self.set_status(f"{key}: the console never opened -- see the log")
        return False

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
        guest = self.sidebar.guests.get(key)
        if guest is not None and guest.is_container:
            # A container's pair is serial and VNC. Same rule as the VM one:
            # the choice belongs to this tab and dies with it.
            if console.protocol == "serial":
                self._force_vnc.add(key)
                self._force_serial.discard(key)
                self.set_status("Reopening on VNC...")
            else:
                self._force_vnc.discard(key)
                self._force_serial.add(key)
                self.set_status("Reopening on the serial console...")
            self.reconnect_console(key)
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
            log.warning(
                "%s: could not check for other SPICE clients (%s)", guest.label, exc
            )
            return None
        except Exception as exc:
            log.warning(
                "%s: SPICE client check failed (%s: %s)",
                guest.label,
                type(exc).__name__,
                exc,
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

    def _plan_container_console(self, guest, protocol_setting):
        """Decide between a serial console and VNC for a container.

        Containers have no SPICE display, so the choice they have is a
        different one: Proxmox's termproxy gives a real character terminal,
        and vncterm gives a picture of one. The terminal is the better answer
        by default -- its text can be selected, it uses the whole tab rather
        than a fixed 80x24, and it is a fraction of the traffic -- but VNC
        stays one menu entry away, because a container whose console is
        wedged is exactly when a second opinion is worth having.

        Returns (plan, reason). A plan of None means fall through to VNC,
        and the reason is what to tell the user about why.
        """
        if not SERIAL_AVAILABLE:
            return None, "pycairo is not installed"
        if guest.key in self._force_vnc:
            return None, "VNC chosen for this console"

        forced_serial = guest.key in self._force_serial
        if not forced_serial:
            if protocol_setting == "vnc":
                return None, "VNC only in this container's settings"
            if protocol_setting != "serial" and self.config.get("prefer_vnc"):
                return None, "VNC forced in preferences"

        try:
            session = open_term_session(
                self.api_for(guest), guest.node, guest.vmid, guest.kind
            )
        except ProxmoxError as exc:
            # Same reasoning as the SPICE path: a console you can use beats a
            # correct error message. The commonest cause is an API token
            # without VM.Console, which VNC will refuse too -- but that is
            # for the VNC attempt to discover and report.
            reason = f"termproxy refused: {exc}"
            log.info("%s: %s", guest.label, reason)
            return None, reason

        return dict(session, protocol="serial"), ""

    def _plan_console(self, guest, takeover=False):
        """Decide between SPICE, VNC and a serial console, and fetch what that needs.

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
            log.warning("could not read the config for %s: %s", guest.label, exc)

        forced_vnc = guest.key in self._force_vnc
        forced_spice = guest.key in self._force_spice
        protocol_setting = self.guest_settings(guest).get("protocol")
        settings_vnc = protocol_setting == "vnc" and not forced_spice

        # Containers never reach the SPICE question at all, so they are
        # settled first rather than threaded through a decision about a
        # display they do not have.
        container_reason = ""
        if guest.is_container:
            plan, container_reason = self._plan_container_console(
                guest, protocol_setting
            )
            if plan is not None:
                guest.console_note = ""
                return plan

        prefer_vnc = bool(self.config.get("prefer_vnc")) and not forced_spice

        spice_possible = (
            SPICE_AVAILABLE
            and not guest.is_container
            and not forced_vnc
            and not settings_vnc
            and not prefer_vnc
        )

        # A container that got here has already been asked and answered; the
        # SPICE reasoning below is about VMs and would overwrite it with
        # something that is not about this guest at all.
        reason = container_reason
        if forced_vnc and not guest.is_container:
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
                    log.info("%s: %s", guest.label, reason)
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
            # Per guest for the same reason the font size is: a guest whose
            # desktop is unreadable at 100% on this screen is not a fact
            # about the other guests.
            "console_scale": clamp_console_scale(
                stored.get(
                    "console_scale",
                    self.config.get("console_scale", DEFAULT_CONSOLE_SCALE),
                )
            ),
            "codec_index": stored.get("codec_index", 0),
            "compression_index": stored.get("compression_index", 0),
            # How big the text is on a serial console. This machine's
            # business, like the two above it: the same container read on a
            # laptop and on a 27" monitor wants different answers.
            "font_size": stored.get(
                "font_size", self.config.get("font_size", DEFAULT_FONT_SIZE)
            ),
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
        """Turn a plan into a console widget. Runs as a GLib idle callback.

        Wrapped whole, because of where it runs: an exception escaping an
        idle callback is caught by PyGObject and written to stderr, and a
        packaged Windows build has no stderr. The tab would be left on
        "connecting..." with nothing anywhere to say why -- which is exactly
        how this presented before there was a log file to read.
        """
        try:
            return self._build_console_now(guest, plan, replace, automatic)
        except Exception as exc:
            log.exception("%s: building the console raised", guest.label)
            self._console_failed(guest, f"{type(exc).__name__}: {exc}")
            return False

    def _build_console_now(self, guest, plan, replace=False, automatic=False):
        if self._closing:
            return False

        # Whatever this turns into -- a console, or the question below --
        # the rebuild that was in flight for this guest is over.
        self._reconnect_finished(guest.key)

        if plan["protocol"] == "occupied":
            self._console_occupied(guest, plan, automatic)
            return False

        log.info("%s: building a %s console", guest.label, plan["protocol"])
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
                console_scale=prefs["console_scale"],
                share_clipboard=self._guest_switch(guest, "clipboard"),
                play_audio=self._guest_switch(guest, "audio"),
                on_agent=lambda connected, c=None: self._on_console_agent(
                    self.consoles.get(guest.key), connected
                ),
                on_disconnect=lambda reason, k=guest.key: self._on_console_disconnected(
                    k, reason
                ),
                on_reconnect=lambda k=guest.key: self.reconnect_console(k),
                on_usb=lambda k=guest.key: self._on_console_usb(k),
                on_monitors=lambda count, k=guest.key: self._on_console_monitors(
                    self.consoles.get(k), count
                ),
                # How many displays this guest's adapter can be asked for.
                # QXL is four whether or not it is spelled 'qxl2'; VirtIO-GPU
                # is one, and Proxmox offers no way to raise it.
                head_limit=vga_head_limit(guest.display),
                # QXL holds every head in this one allocation, so it is what
                # decides whether a second full-screen display can exist.
                video_memory=vga_memory_mib(guest.display),
                on_usb_plugged=lambda device_key, label, k=guest.key: (
                    self._on_console_usb_plugged(k, device_key, label)
                ),
            )
        elif plan["protocol"] == "serial":
            console = SerialConsole(
                plan["url"],
                plan["headers"],
                plan["user"],
                plan["ticket"],
                title=title,
                on_status=lambda text: self._console_status(guest, text),
                # Same certificate the API session was approved against; a
                # console ticket is not something to hand to a server whose
                # identity has changed since.
                fingerprint=getattr(self.api_for(guest), "fingerprint", None),
                font_size=prefs["font_size"],
                on_disconnect=lambda reason, k=guest.key: self._on_console_disconnected(
                    k, reason
                ),
                on_reconnect=lambda k=guest.key: self.reconnect_console(k),
                on_font_size=lambda size, k=guest.key: self._save_guest_pref(
                    "font_size", size, key=k
                ),
            )
        else:
            console = VncConsole(
                plan["url"],
                plan["headers"],
                plan["password"],
                title=title,
                on_status=lambda text: self._console_status(guest, text),
                # The console's own connection to the same server, so it is
                # held to the same certificate the user approved. Without
                # this, turning verification on would refuse every VNC
                # console on a self-signed Proxmox.
                fingerprint=getattr(self.api_for(guest), "fingerprint", None),
                scale_to_fit=bool(prefs["scaling"]),
                console_scale=prefs["console_scale"],
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
        elif plan["protocol"] == "vnc" and plan.get("reason"):
            # A container on VNC used to be the only possibility and needed
            # no explanation. Now that it is the second choice, say which of
            # the several reasons it was.
            self.set_status(f"{guest.label}: VNC - {plan['reason']}")
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

    def guest_tab(self, guest, present=False):
        """The guest's tab, opening one if it has none.

        The tab is what lives in the notebook, so a console arriving later,
        or going away entirely, never disturbs it.
        """
        tab = self.tabs.get(guest.key)
        if tab is None:
            summary = GuestSummary(
                on_open_console=lambda k=guest.key: self.open_console(k),
                on_show_console=lambda k=guest.key: self.show_tab_view(
                    k, VIEW_CONSOLE, by_user=True
                ),
                on_save_notes=lambda text, k=guest.key: self.save_guest_notes(k, text),
                on_power_action=lambda name, k=guest.key: self._run_action(k, name),
                on_edit_settings=lambda k=guest.key: self.open_guest_settings(k),
            )
            tab = GuestTab(
                guest.key,
                summary,
                view=(
                    VIEW_CONSOLE
                    if guest.has_console and not guest.template
                    else VIEW_SUMMARY
                ),
            )
            self.tabs[guest.key] = tab
            label = ConsoleTabLabel(
                self.tab_title(guest),
                None,
                on_close=lambda k=guest.key: self.close_console(k),
            )
            self.panes.append(tab, label)
            tab.show_all()
            summary.show_guest(guest, self.api_for(guest))
            self._load_guest_notes(guest)
            tab.follow_guest_state(guest)
        if present:
            self.panes.focus_page(tab)
        return tab

    # -- notes -----------------------------------------------------------

    def _load_guest_notes(self, guest):
        """Read a guest's notes for its summary, off the main thread."""
        api = self.api_for(guest)
        if api is None:
            return
        key = guest.key

        def worker():
            try:
                raw = api.guest_notes(guest.node, guest.vmid, guest.kind)
            except Exception as exc:
                GLib.idle_add(self._notes_failed, key, str(exc))
                return
            GLib.idle_add(self._notes_loaded, key, raw)

        threading.Thread(target=worker, daemon=True, name=f"notes-{guest.vmid}").start()

    def _notes_loaded(self, key, raw):
        tab = self.tabs.get(key)
        if tab is not None:
            tab.summary.set_notes(key, raw)
        return False

    def _notes_failed(self, key, message):
        tab = self.tabs.get(key)
        if tab is not None:
            tab.summary.notes_saved(
                ok=False, message=f"could not read notes: {message}"
            )
        return False

    def save_guest_notes(self, key, text):
        """Write the user's notes back, keeping Proxima's block intact.

        The block is re-read immediately before writing rather than
        remembered from the load: a folder change or a settings save in
        between belongs in the notes too, and would otherwise be undone by
        whatever was typed here.
        """
        guest = self.sidebar.guests.get(key)
        api = self.api_for(guest) if guest else None
        if guest is None or api is None:
            return

        def worker():
            try:
                current = api.guest_notes(guest.node, guest.vmid, guest.kind)
                metadata, _user = notes_meta.parse(current or "")
                api.set_guest_notes(
                    guest.node,
                    guest.vmid,
                    notes_meta.update(text, metadata),
                    guest.kind,
                )
            except Exception as exc:
                GLib.idle_add(self._notes_failed, key, str(exc))
                return
            GLib.idle_add(self._notes_saved, key)

        threading.Thread(
            target=worker, daemon=True, name=f"notes-save-{guest.vmid}"
        ).start()

    def _notes_saved(self, key):
        tab = self.tabs.get(key)
        if tab is not None:
            tab.summary.notes_saved(ok=True)
        return False

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    def node_for(self, key):
        """The Node a key names, or None if no connected server has it."""
        return self.connections.node(key) if key else None

    def guests_on(self, node):
        """Every guest the inventory places on a node."""
        if node is None:
            return []
        return [
            guest
            for guest in self.sidebar.guests.values()
            if guest.node == node.name and guest.connection == node.connection
        ]

    def _on_node_selected(self, _sidebar, key):
        """Clicking a node refreshes its page, exactly as a guest does.

        And, exactly as a guest does, it does not open one: a node you have
        not asked for has no tab.
        """
        node = self.node_for(key)
        tab = self.node_tabs.get(key)
        if node is None or tab is None or tab.view != VIEW_SUMMARY:
            return
        tab.summary.show_node(node, self.api_for(node), self.guests_on(node))

    def _on_node_activated(self, _sidebar, key):
        self.open_node(key)

    def _on_node_action(self, _sidebar, key, action_name):
        if action_name == "shell":
            self.open_node_shell(key)
        elif action_name == "refresh":
            self.refresh()

    def open_node(self, key, present=True):
        """Open, or bring forward, a node's tab."""
        node = self.node_for(key)
        if node is None:
            self.set_status(f"{key} is not a node on any connected server")
            return None
        return self.node_tab(node, present=present)

    def node_tab(self, node, present=False):
        """The node's tab, opening one if it has none."""
        tab = self.node_tabs.get(node.key)
        if tab is None:
            summary = NodeSummary(
                on_open_shell=lambda k=node.key: self.open_node_shell(k)
            )
            tab = NodeTab(node.key, summary)
            self.node_tabs[node.key] = tab
            label = ConsoleTabLabel(
                node.name, None, on_close=lambda k=node.key: self.close_node(k)
            )
            self.panes.append(tab, label)
            tab.show_all()
            self._sync_node_tab_icon(tab, node)
            summary.show_node(node, self.api_for(node), self.guests_on(node))
        if present:
            self.panes.focus_page(tab)
        return tab

    def _sync_node_tab_icon(self, tab, node):
        """The tab wears the node's own icon, as the tree draws it.

        Not the console's: a node's tab is the node, and opening a shell in
        it does not turn it into a terminal. It does follow the node going
        offline, and the theme flipping, since both change what the tree
        shows for the same machine.
        """
        notebook = self.panes.notebook_of(tab)
        label = notebook.get_tab_label(tab) if notebook is not None else None
        if label is None or not hasattr(label, "set_icon"):
            return
        dark = self.sidebar._dark
        stamp = (node.status, dark)
        if getattr(label, "_node_stamp", None) == stamp:
            return
        label._node_stamp = stamp
        label.set_icon(
            status_icons.node_icon(node, dark=dark),
            f"{node.name}\n{node.status}",
        )

    def open_node_shell(self, key, replace=False):
        """Open a terminal on the node itself.

        The same termproxy console a container gets, on the endpoint Proxmox
        hangs off the node rather than off a guest -- which is what the web
        interface's Shell button opens. There is no second protocol to fall
        back to here: a node has no framebuffer, and the VNC shell Proxmox
        also offers is the same text in a picture of a terminal.
        """
        node = self.node_for(key)
        if node is None:
            return
        if not SERIAL_AVAILABLE:
            self._error_dialog(
                "No shell available",
                "pycairo is required to draw a terminal.\n"
                "Install mingw-w64-ucrt-x86_64-python-cairo.",
            )
            return
        if not node.online:
            self.set_status(f"{node.name} is {node.status}")
            return

        tab = self.node_tab(node, present=True)
        if self.node_consoles.get(key) is not None and not replace:
            # Already open: show it rather than dialling a second session.
            tab.show_view(VIEW_CONSOLE, by_user=True)
            self._sync_tab_view(tab)
            return

        waiting = PlaceholderConsole(
            title=node.name,
            status="connecting",
            on_reconnect=lambda k=key: self.open_node_shell(k, replace=True),
        )
        self._install_node_console(node, waiting)
        self.set_status(f"{node.name}: opening a shell...")
        log.info("opening a shell on %s", node.key)

        api = self.api_for(node)

        def worker():
            try:
                # vmid=None is the node's own termproxy endpoint. The kind is
                # not part of that path at all -- see _console_base.
                session = open_term_session(api, node.name, None)
            except ProxmoxError as exc:
                GLib.idle_add(self._node_shell_failed, node.key, str(exc))
                return
            except Exception as exc:
                log.exception("%s: opening a shell raised", node.key)
                GLib.idle_add(
                    self._node_shell_failed, node.key, f"{type(exc).__name__}: {exc}"
                )
                return
            GLib.idle_add(self._build_node_shell, node, session)

        threading.Thread(
            target=worker, daemon=True, name=f"node-shell-{node.name}"
        ).start()

    def _build_node_shell(self, node, session):
        if self._closing:
            return False
        try:
            console = SerialConsole(
                session["url"],
                session["headers"],
                session["user"],
                session["ticket"],
                title=node.name,
                on_status=lambda text, k=node.key: self._node_console_status(k, text),
                # The same certificate the API session was approved against,
                # for the same reason the guest consoles use it.
                fingerprint=getattr(self.api_for(node), "fingerprint", None),
                font_size=self.guest_prefs(node.key)["font_size"],
                on_disconnect=lambda reason, k=node.key: (
                    self._on_node_console_disconnected(k, reason)
                ),
                on_reconnect=lambda k=node.key: self.open_node_shell(k, replace=True),
                on_font_size=lambda size, k=node.key: self._save_guest_pref(
                    "font_size", size, key=k
                ),
            )
        except Exception as exc:
            log.exception("%s: building the shell raised", node.key)
            self._node_shell_failed(node.key, f"{type(exc).__name__}: {exc}")
            return False
        self._install_node_console(node, console)
        return False

    def _install_node_console(self, node, console):
        """Put a shell into a node's tab, replacing whatever was there."""
        console.guest_key = None
        console.node_key = node.key
        tab = self.node_tab(node)
        old = self.node_consoles.get(node.key)
        replaced = tab.set_console(console)
        self.node_consoles[node.key] = console

        notebook = self.panes.notebook_of(tab)
        if notebook is not None:
            # The tab label is deliberately left alone: it wears the node's
            # icon, and a shell opening in it does not make it a terminal.
            notebook.set_current_page(notebook.page_num(tab))
        # Asking for a shell is asking to look at it, unlike a guest console
        # which can be built by a poll noticing the guest came back.
        tab.show_view(VIEW_CONSOLE)
        self._sync_tab_view(tab)

        for stale in (old, replaced):
            if stale is not None and stale is not console:
                self._shutdown_console(stale)
        self._sync_view_menu()
        return tab

    def _node_console_status(self, key, text):
        def apply():
            node = self.node_for(key)
            self.set_status(f"{node.name if node else key}: {text}")
            self._sync_view_menu()
            return False

        GLib.idle_add(apply)

    def _node_shell_failed(self, key, message):
        node = self.node_for(key)
        name = node.name if node else key
        self.set_status(f"{name}: shell failed - {message}")
        console = self.node_consoles.get(key)
        if console is not None and hasattr(console, "show_error_state"):
            console.show_error_state(message)
        else:
            self._error_dialog(f"Shell failed: {name}", message)
        return False

    def _on_node_console_disconnected(self, key, reason):
        node = self.node_for(key)
        self.set_status(f"{node.name if node else key}: {reason}")

    def close_node(self, key):
        """Close a node's tab: its summary and its shell together."""
        tab = self.node_tabs.pop(key, None)
        console = self.node_consoles.pop(key, None)
        if tab is None and console is None:
            return
        if self.fullscreen_control.active and console is self.current_console():
            self.fullscreen_control.leave()
        if tab is not None:
            tab.take_console()
            self.panes.remove_page(tab)
        elif console is not None:
            self.panes.remove_page(console)
        if console is not None:
            self._shutdown_console(console)
        self._after_tab_closed()

    def _refresh_open_node_summaries(self):
        """Keep the node pages that are on screen up to date.

        Only the visible ones, as for guests -- and here it matters more:
        each refresh is a per-node status call, and a hidden page's graphs
        would be fetching history nobody is looking at.
        """
        for key, tab in list(self.node_tabs.items()):
            node = self.node_for(key)
            if node is None:
                continue
            self._sync_node_tab_icon(tab, node)
            tab.follow_node_state(node)
            if tab.view != VIEW_SUMMARY or tab is not self.panes.current_page():
                continue
            tab.summary.show_node(node, self.api_for(node), self.guests_on(node))

    def _install_console(self, guest, console):
        """Put a console into the guest's tab, replacing any existing one.

        Replacing inside the tab is what makes a reconnect look like the
        picture coming back: the tab itself never moves, because it is the
        tab and not the console that the notebook holds.
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

        tab = self.guest_tab(guest)
        replaced = tab.set_console(console)
        self.consoles[guest.key] = console

        notebook = self.panes.notebook_of(tab)
        page = notebook.page_num(tab) if notebook is not None else -1
        label = notebook.get_tab_label(tab) if notebook is not None else None
        if label is not None and hasattr(label, "set_protocol"):
            label.set_protocol(console.protocol)
        if notebook is not None:
            notebook.set_current_page(page)

        # A console arriving for a running guest is what the tab is for.
        tab.follow_guest_state(guest)

        for stale in (old, replaced):
            if stale is not None and stale is not console:
                self._shutdown_console(stale)
        self._sync_view_menu()
        self._refresh_console_indicators(console)
        return page

    # -- flipping a tab between its console and its summary -------------

    def current_tab(self):
        return tab_of(self.panes.current_page())

    def toggle_tab_view(self):
        """The toolbar button: flip the tab in front, and only that one."""
        tab = self.current_tab()
        if tab is None:
            return
        tab.toggle(by_user=True)
        self._sync_tab_view()

    def show_tab_view(self, key, view, by_user=False):
        tab = self.tabs.get(key)
        if tab is not None:
            tab.show_view(view, by_user=by_user)
            if tab is self.current_tab():
                self._sync_tab_view()

    def _sync_tab_view(self, tab=_CURRENT):
        """Point the Summary button at whichever tab is in front."""
        if tab is _CURRENT:
            tab = self.current_tab()
        showing_summary = tab is not None and tab.view == VIEW_SUMMARY
        self._updating_view_menu = True
        try:
            self.summary_view_item.set_sensitive(tab is not None)
            self.summary_view_item.set_active(showing_summary)
            # Only the state here: the button stays usable with no tab in
            # front, because that is how the first console gets opened.
            # _update_action_sensitivity() owns whether it is live.
            self.console_tool_item.set_active(tab is not None and not showing_summary)
        finally:
            self._updating_view_menu = False

    def _on_console_toggled(self, widget):
        """The toolbar's Console button: flip the tab, or open one."""
        if self._updating_view_menu:
            return
        tab = self.current_tab()
        if tab is None:
            # Nothing in front to flip, so this is a request to open one.
            if widget.get_active():
                self.open_console_selected()
            return
        if tab.node_key and tab.console is None:
            # A node's tab only grows a shell when one is asked for, and
            # pressing the Console button is asking.
            if widget.get_active():
                self.open_node_shell(tab.node_key)
            self._sync_tab_view(tab)
            return
        tab.show_view(
            VIEW_CONSOLE if widget.get_active() else VIEW_SUMMARY, by_user=True
        )
        self._sync_tab_view(tab)

    def _on_summary_toggled(self, widget):
        if self._updating_view_menu:
            return
        tab = self.current_tab()
        if tab is None:
            return
        tab.show_view(
            VIEW_SUMMARY if widget.get_active() else VIEW_CONSOLE, by_user=True
        )
        self._sync_tab_view(tab)

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
        self._update_usb_indicator(console)

    def _shutdown_console(self, console):
        key = getattr(console, "guest_key", None)
        if self._usb_dialog is not None and self._usb_dialog.console is console:
            # Its device list belongs to a session that is about to end.
            self._usb_dialog.destroy()
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
            log.exception("shutting down the console for %s raised", key)

    def _console_status(self, guest, text):
        def apply():
            self.set_status(f"{guest.label}: {text}")
            # Connecting finishes after the tab exists, so the entries that
            # need a live console are refreshed here too.
            self._sync_view_menu()
            return False

        GLib.idle_add(apply)

    def _console_failed(self, guest, message):
        self._reconnect_finished(guest.key)
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
        elif guest.status == "io-error":
            panel.show_message(
                "Guest stopped on an I/O error",
                "Proxmox stopped it because its storage stopped answering. "
                "Fix the storage, then reset or stop the guest.",
                icon="dialog-warning-symbolic",
                can_reconnect=True,
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

    # How long an unfinished rebuild suppresses automatic ones. A backstop
    # only: a rebuild that lands, fails or is refused clears itself.
    RECONNECT_GUARD = 8

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

        # Only automatic reconnects are held off while one is in flight.
        #
        # The guard is here for the poll: _sync_console_states asks for a
        # reconnect every time it sees a guest that has come back, which is
        # every few seconds until the new console reports itself, and
        # without this each of those would build another console.
        #
        # A click is different, and used to be swallowed by the same test:
        # Reopen Console set the status line, found a rebuild still marked
        # in flight, and returned. Nothing else wrote to the status bar
        # after that, so the window sat on "Reopening on SPICE..." having
        # done nothing -- and since the mark was only ever cleared by a
        # timer, every further click inside that window went the same way.
        # What somebody asks for by hand always happens.
        now = time.monotonic()
        if automatic and self._reconnecting.get(key, 0.0) > now:
            log.debug("%s: a console rebuild is already in flight", key)
            return
        self._reconnecting[key] = now + self.RECONNECT_GUARD
        self.open_console(key, replace=True, automatic=automatic)

    def _reconnect_finished(self, key):
        """This guest's rebuild is over, however it turned out."""
        self._reconnecting.pop(key, None)

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

        # The tab goes with the console: a summary with nothing behind it is
        # not worth a tab of its own while the console is in another window.
        tab = self.tabs.get(key)
        holder = tab if tab is not None else console
        notebook = self.panes.notebook_of(holder)
        page = notebook.page_num(holder) if notebook is not None else -1
        if page < 0:
            return
        # Remember where it was so returning it lands in the same place.
        self._popout_pages[key] = page
        if tab is not None:
            tab.take_console()
            self.tabs.pop(key, None)
            self.panes.remove_page(tab)
        else:
            # detach(), not remove_page(): the widget has to survive the
            # move, and self.consoles holds the only other reference.
            self.panes.detach(console)

        window = ConsoleWindow(self, console, guest)
        self._popouts[key] = window
        window.show_all()
        self._sync_view_menu()
        self.set_status(f"{guest.label}: console popped out")

    def reclaim_console(self, key, console):
        """Take a console back from a pop-out window into a tab."""
        self._popouts.pop(key, None)
        if self._closing:
            return
        guest = self.sidebar.guests.get(key)
        if guest is None:
            # Its guest has gone from the inventory; there is no tab to
            # build around it.
            self._shutdown_console(console)
            return

        summary = GuestSummary(on_open_console=lambda k=key: self.open_console(k))
        tab = GuestTab(key, summary)
        self.tabs[key] = tab
        label = ConsoleTabLabel(
            self.tab_title(guest),
            console.protocol,
            on_close=lambda: self.close_console(key),
        )
        self.panes.insert(
            tab, label, self.panes.primary, self._popout_pages.pop(key, -1)
        )
        tab.show_all()
        summary.show_guest(guest, self.api_for(guest))
        tab.set_console(console)
        tab.follow_guest_state(guest)
        self.panes.focus_page(tab)
        self.consoles[key] = console
        self._after_tab_closed()

    def run_action_for(self, key, action_name):
        """Power action from a pop-out window's toolbar."""
        self._run_action(key, action_name)

    def snapshot_action_for(self, key, which):
        """Snapshot action from a pop-out window's toolbar."""
        self._snapshot_action(which, key)

    def _update_popouts(self):
        for window in list(self._popouts.values()):
            window.update_sensitivity()

    def close_console_widget(self, page):
        """Close a page, whether it is a guest's tab, a node's or a console."""
        node_key = getattr(page, "node_key", None)
        if node_key:
            self.close_node(node_key)
            return

        tab = tab_of(page)
        console = console_of(page)
        if tab is not None:
            tab.take_console()
        self.panes.remove_page(page)
        key = getattr(page, "guest_key", None) or getattr(console, "guest_key", None)
        # Only forget the key if it still points at this widget; a reconnect
        # may already have replaced it.
        if key and (tab is not None or self.consoles.get(key) is console):
            self.consoles.pop(key, None)
            self.tabs.pop(key, None)
            self._console_offline.pop(key, None)
            self._pending_actions.pop(key, None)
            self._clear_session_choices(key)
        if console is not None:
            self._shutdown_console(console)
        self._after_tab_closed()

    def _after_tab_closed(self):
        """Put the chrome back in step after a tab goes.

        Closing the last tab emits no page switch, so nothing else would
        tell the status bar that the console it is describing has gone.
        """
        self._sync_view_menu()
        self._sync_tab_view()
        self._context_changed()
        self._update_audio_indicator()
        self._update_clipboard_indicator()

    def _clear_session_choices(self, key):
        """Forget the temporary console choices made for one guest.

        Closing the tab is what ends a session: the protocol it was switched
        to, and the clipboard and audio buttons, all go back to what the
        guest's own settings say the next time it is opened.
        """
        self._force_vnc.discard(key)
        self._force_spice.discard(key)
        self._force_serial.discard(key)
        # A rebuild that was in flight is over too, whatever became of it:
        # the tab it would have landed in has gone.
        self._reconnecting.pop(key, None)
        for name in ("clipboard", "audio"):
            self._session_switches.pop((key, name), None)

    def close_console(self, key):
        """Close the guest's tab: its console and its summary together."""
        self._clear_session_choices(key)
        window = self._popouts.pop(key, None)
        if window is not None:
            window.shutdown()
            window.destroy()
        console = self.consoles.pop(key, None)
        tab = self.tabs.pop(key, None)
        if console is None and tab is None:
            return
        # Closing the console that is filling the screen would otherwise
        # leave the window fullscreen with no chrome and nothing in it.
        if self.fullscreen_control.active and console is self.current_console():
            self.fullscreen_control.leave()
        self._console_offline.pop(key, None)
        if tab is not None:
            tab.take_console()
            self.panes.remove_page(tab)
        elif console is not None:
            self.panes.remove_page(console)
        if console is not None:
            self._shutdown_console(console)
        self._after_tab_closed()

    def _close_current_console(self):
        page = self.panes.current_page()
        if page is None:
            return
        # By widget, not by key: after a reconnect the key can point at a
        # different tab, and closing that one leaves this one stranded.
        self.close_console_widget(page)

    def _on_page_switched(self, _panes, _notebook, page_widget, _page_num):
        if not self._ready:
            return

        # "switch-page" fires before the page becomes current, so the
        # incoming widget is passed in rather than read back a frame later.
        # Reading it back is what made the toolbar flash the old state.
        incoming = console_of(page_widget)
        self._sync_view_menu(incoming)
        self._context_changed(incoming)
        self._sync_tab_view(tab_of(page_widget))

        tab = tab_of(page_widget)
        key = getattr(page_widget, "guest_key", None)
        node_key = getattr(page_widget, "node_key", None)
        if tab is not None and tab.view == VIEW_SUMMARY:
            guest = self.sidebar.guests.get(key)
            if guest is not None:
                tab.summary.show_guest(guest, self.api_for(guest))
            node = self.node_for(node_key)
            if node is not None:
                # A node page that has been sitting behind another tab is
                # showing figures from whenever it was last in front, so it
                # is brought up to date on the way in rather than at the
                # next poll.
                tab.summary.show_node(node, self.api_for(node), self.guests_on(node))
        if key or node_key:
            self.sidebar.select_key(key or node_key)
        # Give the console the keyboard as soon as its tab is shown, unless
        # the tab is showing its summary, which has its own focus.
        if tab is not None:
            if tab.view == VIEW_CONSOLE:
                GLib.idle_add(tab.focus_console)
        elif hasattr(page_widget, "grab_focus_display"):
            GLib.idle_add(page_widget.grab_focus_display)

    # -- view menu <-> active console ----------------------------------

    def current_console(self):
        """The console in the tab in front, if it has one."""
        return console_of(self.panes.current_page())

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

            self.console_scale_item.set_sensitive(bool(supports.get("console_scale")))
            if console is not None and supports.get("console_scale"):
                index = console_scale_index(getattr(console, "console_scale", 100))
                self.console_scale_items[index].set_active(True)

            self.codec_item.set_sensitive(bool(supports.get("codec")))
            if console is not None and supports.get("codec"):
                index = getattr(console, "codec_index", 0)
                self.codec_items[index].set_active(True)

            self.compression_item.set_sensitive(bool(supports.get("compression")))
            if console is not None and supports.get("compression"):
                index = getattr(console, "compression_index", 0)
                self.compression_items[index].set_active(True)

            self.refresh_frame_item.set_sensitive(bool(supports.get("refresh")))

            # Two tabs and the split button works: there is something to put
            # on each side of it. That is the whole rule. It used to depend
            # on which pane held what, and on the console in front rather
            # than on the tabs, which left it grey at moments when splitting
            # was plainly reasonable.
            self._sync_split_controls()

            self.fullscreen_item.set_sensitive(console is not None)
            self.fullscreen_item_tb.set_sensitive(console is not None)
            self._sync_all_monitors_item(console)
            # A pop-out window is built around a guest -- its toolbar is that
            # guest's power controls, and returning it rebuilds that guest's
            # tab. A node's shell has none of those, so it stays where it is
            # rather than offering a button that would quietly do nothing.
            self.popout_item.set_sensitive(
                console is not None and bool(getattr(console, "guest_key", None))
            )
            # One capability, three controls: the menu entry, the submenu
            # and the toolbar button all need a console that can be typed at.
            # Two capabilities, not one. A serial console takes typing but
            # has no keyboard controller to send Ctrl+Alt+Del to, so the
            # entries that used to move together now ask separately.
            can_type = bool(supports.get("send_keys", supports.get("ctrl_alt_del")))
            self.ctrl_alt_del_item.set_sensitive(bool(supports.get("ctrl_alt_del")))
            self.send_key_item.set_sensitive(can_type)
            self.send_key_item_tb.set_sensitive(can_type)
            self.screenshot_item.set_sensitive(
                console is not None
                and hasattr(console, "screenshot")
                and getattr(console, "connected", False)
            )
            self.close_console_item.set_sensitive(console is not None)
            self._sync_protocol_switch(console)

            if console is None:
                self.protocol_label.set_text("")
            elif console.protocol == "serial":
                self.protocol_label.set_text("Serial")
                self.protocol_label.set_tooltip_text(
                    "A text console. Select with the mouse, Ctrl+Shift+C to "
                    "copy, Ctrl+Shift+V to paste."
                )
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

        if guest.is_container:
            if console.protocol == "serial":
                item.set_label("Reopen Console with VNC")
                item.set_sensitive(True)
                item.set_tooltip_text(
                    "Reconnect this console over VNC. It stays on VNC until "
                    "the tab is closed."
                )
            else:
                item.set_label("Reopen Console with Serial")
                item.set_sensitive(SERIAL_AVAILABLE)
                item.set_tooltip_text(
                    ""
                    if SERIAL_AVAILABLE
                    else guest.console_note or "The serial console is not available"
                )
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

    def _on_console_scale_selected(self, item, index):
        if self._updating_view_menu or not item.get_active():
            return
        console = self.current_console()
        if console is not None and hasattr(console, "set_console_scale"):
            percent = CONSOLE_SCALES[index]
            console.set_console_scale(percent)
            self._save_guest_pref("console_scale", percent)

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

    def _on_pane_activated(self, _panes):
        """A pane was clicked into, so the window now acts on that one.

        The same refresh switching tabs does, because it is the same change:
        which console the window is pointed at. The tree follows too, so the
        selection agrees with the pane that is now in charge.
        """
        if not self._ready:
            return
        page = self.panes.current_page()
        self._sync_view_menu()
        self._context_changed()
        key = getattr(page, "guest_key", None) or getattr(page, "node_key", None)
        if key:
            self.sidebar.select_key(key)

    def _sync_split_controls(self):
        """Point the split button and its menu entry at the next arrangement.

        Live whenever there is more than one tab open, wherever those tabs
        are: two tabs is what splitting needs and all it needs.
        """
        mode = self.panes.next_split_mode()
        live = self.panes.total_pages() > 1
        self.split_item.set_label(SPLIT_LABELS[mode])
        self.split_item.set_sensitive(live)
        self.split_item_tb.set_tooltip_text(SPLIT_LABELS[mode])
        self.split_item_tb.set_sensitive(live)

    def _cycle_split(self):
        """One pane, side by side, one above the other, and round again.

        What moves is the notebook page -- the guest's tab, with its summary
        and its console in it -- not the console widget, which the notebook
        has never heard of.
        """
        if self.fullscreen_control.active:
            self.fullscreen_control.leave()
        mode = self.panes.next_split_mode()
        if not self.panes.set_split_mode(mode):
            return
        self.set_status(SPLIT_STATUS[mode])
        self._sync_view_menu()

    def _refresh_framebuffer(self):
        console = self.current_console()
        if console is not None and hasattr(console, "refresh_framebuffer"):
            console.refresh_framebuffer()

    def _send_ctrl_alt_del(self):
        self._send_key(console_keys.CTRL_ALT_DEL)

    def _send_key(self, keysyms):
        """Send one combination to the console in front.

        The guest gets the keyboard back afterwards: these are sent from a
        menu, so the pointer is somewhere over the menu rather than over the
        console, and without this the next thing typed would go to the
        window rather than to the guest that was just poked.
        """
        console = self.current_console()
        if console is None or not hasattr(console, "send_keys"):
            return
        console.send_keys(keysyms)
        if hasattr(console, "grab_focus_display"):
            console.grab_focus_display()

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

    def all_monitors_enabled(self):
        """Whether full screen should use every monitor when it can.

        A preference, not a promise: a console with one head, or a desktop
        with one monitor, goes full screen on the one screen regardless.
        """
        return bool(self.config.get("fullscreen_all_monitors", True))

    def _on_all_monitors_toggled(self, item):
        if self._updating_view_menu:
            return
        self.config["fullscreen_all_monitors"] = item.get_active()
        self.config.save()

    def _sync_all_monitors_item(self, console):
        """Offer 'Use All Monitors' only where it would do something."""
        item = self.all_monitors_item
        # Called from outside _sync_view_menu too, and set_active emits
        # 'toggled' exactly as a click does.
        guarded, self._updating_view_menu = self._updating_view_menu, True
        try:
            item.set_active(self.all_monitors_enabled())
        finally:
            self._updating_view_menu = guarded
        can = self.fullscreen_control.can_span_monitors(console)
        item.set_sensitive(can)
        if can:
            item.set_tooltip_text(
                "Full screen gives the guest a display on each of your monitors"
            )
        elif console is None:
            item.set_tooltip_text("")
        elif not getattr(console, "supports", {}).get("multi_monitor"):
            item.set_tooltip_text(
                "Only a SPICE console can put a guest's displays on separate monitors"
            )
        elif self.fullscreen_control.spare_monitors() < 1:
            item.set_tooltip_text("There is only one monitor to show it on")
        elif not getattr(console, "connected", False):
            item.set_tooltip_text("The console is not connected")
        else:
            # The remaining case is an adapter that has one head and cannot
            # be asked for another, which in Proxmox means VirtIO-GPU.
            item.set_tooltip_text(
                "This guest's display adapter has one display. SPICE (qxl), "
                "under Hardware -> Display, offers up to four."
            )

    def _on_console_monitors(self, console, _count):
        """A guest's second head can appear well after its console does."""

        def update():
            if console is self.current_console():
                self._sync_all_monitors_item(console)
            return False

        GLib.idle_add(update)

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

    # -- updates -------------------------------------------------------

    def check_for_updates(self, automatic=True):
        """Ask GitHub whether there is a newer release.

        Automatic checks are silent unless there is something to say. A
        check asked for by hand answers either way, including "nothing new"
        -- an explicit question that produces no reply reads as broken.
        """
        if self._update_pending:
            return False

        def landed(release, asked):
            GLib.idle_add(self._update_result, release, asked, automatic)

        started = update.check(self.config, landed, automatic=automatic)
        self._update_pending = started
        return started

    def _check_updates_now(self):
        self.set_status("Checking for updates...")
        if not self.check_for_updates(automatic=False):
            self.set_status("An update check is already running")

    def _update_result(self, release, asked, automatic):
        self._update_pending = False
        if self._closing:
            return False
        if release is not None:
            UpdateDialog(
                self,
                release,
                __version__,
                on_setting=self._set_update_checking,
            )
        elif not automatic:
            self.set_status(
                f"Proxima {__version__} is up to date"
                if asked
                else "Could not reach GitHub to check for updates"
            )
        return False

    def _set_update_checking(self, enabled):
        if bool(self.config.get("check_updates", True)) == bool(enabled):
            return
        self.config["check_updates"] = bool(enabled)
        self.config.save()

    def _open_log_folder(self):
        """Show this run's log in the file manager.

        The first thing a bug report needs, and on Windows the folder is
        under AppData where nobody goes by accident.
        """
        directory = Path(logs.log_dir())
        current = logs.current_log_file()
        if not desktop.open_folder(directory, parent=self):
            # The path is worth showing either way: it can be pasted
            # somewhere even when nothing here can open it.
            self._error_dialog(
                "Could not open the log folder",
                f"The logs are in:\n\n{directory}",
            )
            return
        self.set_status(f"Logs: {current or directory}")

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
        for key in list(self.node_tabs):
            self.close_node(key)
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

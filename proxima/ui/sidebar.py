"""The inventory tree.

Three shapes, cycled by the button beside the search box:

  * node view    -- server -> node -> guest, which is how Proxmox itself
                    organises things.
  * folder view  -- server -> nodes, then folders -> guest. Folders are a
                    client-side idea stored in each guest's notes, so they
                    span the whole datacenter and a guest appears once
                    regardless of which node happens to be running it.
  * tag view     -- server -> tag -> guest, from the tags Proxmox itself
                    keeps. Unlike the other two this one repeats guests:
                    tags are not a hierarchy and a guest with 'prod' and
                    'web' belongs under both. Grouping by the combination
                    instead would make a separate group for every set of
                    tags anyone had ever used, which is the arrangement
                    that stops being useful the moment it is populated.
                    Guests with no tags collect under "Untagged", last.

Rebuilt from scratch on each poll. An earlier version updated rows in place
to preserve expansion and selection, but with two view shapes, folders that
appear and vanish, and multiple servers, tracking every row identity was
buying complexity rather than saving work -- expansion and selection are
saved and restored around the rebuild instead.
"""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GdkPixbuf, GLib, GObject, Gtk

from ..api.connection import CONNECTED, CONNECTING, FAILED
from ..api.models import GUEST_NAME_CHARS, format_guest_name, guest_tags
from . import actions as action_defs
from .status_icons import ICON_SIZE, PALETTES, IconCache, guest_icon

# Model columns
COL_KEY = 0  # guest key, or "" for structural rows
COL_LABEL = 1
COL_TOOLTIP = 2
COL_ICON = 3
COL_KIND = 4  # "connection" | "node" | "folder" | "tag" | "guest"
COL_ID = 5  # connection id, or the folder path joined by "/"

# The three shapes, in the order the view button cycles them.
NODE_VIEW = "node"
FOLDER_VIEW = "folder"
TAG_VIEW = "tag"
VIEWS = (NODE_VIEW, FOLDER_VIEW, TAG_VIEW)

# The button wears the view it is in, not the one it would move to: it is
# the only thing on screen that says how the tree is currently grouped, and
# a control that shows its destination cannot also answer that. Where the
# click leads is in the tooltip instead.
VIEW_ICONS = {
    NODE_VIEW: "computer-symbolic",
    FOLDER_VIEW: "folder-symbolic",
    TAG_VIEW: "user-bookmarks-symbolic",
}

VIEW_NAMES = {NODE_VIEW: "node", FOLDER_VIEW: "folder", TAG_VIEW: "tag"}

# Where guests with no tags of their own collect. Named rather than left
# out: a tag view that silently hides half the estate is a worse answer
# than one that says where the rest went.
UNTAGGED = "Untagged"

# What each view is called where a person reads it: Preferences, and the
# label below. Kept here so the tree and the settings dialog cannot end up
# describing the same three things differently.
VIEW_LABELS = (
    (NODE_VIEW, "Node  -  server, node, guest"),
    (FOLDER_VIEW, "Folder  -  your own folders, stored in each guest's notes"),
    (TAG_VIEW, "Tag  -  Proxmox tags; a guest appears under each of its tags"),
)


def resolve_view(name):
    """A stored view name, or the default if it is not one we know."""
    return name if name in VIEWS else NODE_VIEW


# How often a spinning row advances a frame.
PULSE_MS = 100

# Drag and drop moves a guest between folders. A private target keeps the
# tree from accepting drops from unrelated applications.
DRAG_TARGET = Gtk.TargetEntry.new("proxima/guest", Gtk.TargetFlags.SAME_WIDGET, 0)


class _Renamed:
    """A guest wearing a name it has been asked to take.

    format_guest_name() needs something with .name and .vmid, and the real
    guest still carries what the server last reported. Rather than write the
    new name onto the guest -- where the next poll would overwrite it, which
    is the flicker being fixed -- the label is built from this.
    """

    __slots__ = ("name", "vmid")

    def __init__(self, guest, name):
        self.name = name
        self.vmid = guest.vmid


class Sidebar(Gtk.Box):
    """Tree of servers, nodes, folders and guests."""

    __gsignals__ = {
        "guest-selected": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "guest-activated": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # (guest_key, action_name)
        "guest-action": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        # (action_name) applied to every selected guest
        "bulk-action": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "filter-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "view-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # ask the window to open the connect dialog
        "connect-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # (connection_id)
        "disconnect-requested": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "reconnect-requested": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # (guest_key, folder path joined by "/") -- "" means the root
        "guest-moved": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        # (guest_key)
        "new-subfolder": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # (guest_key, new name) from the inline editor
        "guest-renamed": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        # (guest_key) -- clone a template
        "clone-requested": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # (guest_key) -- destroy a guest
        "delete-requested": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # (guest_key) -- open the per-guest settings dialog
        "settings-requested": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(
        self, row_ypad=1, name_format="name", templates_last=True, dnd_enabled=True
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        self.guests = {}
        self.renderers = []
        self.icons = IconCache()
        self._dark = False
        self._palette = PALETTES[False]
        self.filter_text = ""
        # The shape the tree is drawn in. Not self.view -- that is the
        # GtkTreeView itself.
        self.view_mode = NODE_VIEW
        self.name_format = name_format
        self.templates_last = templates_last
        self.dnd_enabled = bool(dnd_enabled)
        self._connections = []
        self._menu = None
        self._folders = set()  # folder paths currently in the tree
        self._suppress_selection = False
        # Expansion carried over from the last session. Used only until the
        # tree has an expansion state of its own to read back.
        self._saved_expanded = set()
        # Guest key currently being renamed inline, if any. The poll rebuilds
        # this tree from scratch every couple of seconds, which would take
        # the editor with it, so rebuilds are held off while one is open.
        self._editing_key = None
        self._reinserting = False
        # Guests waiting on a change the cluster has not reported yet:
        # key -> (tooltip line, name to show meanwhile or None).
        #
        # Kept beside the model rather than in it. The tree is rebuilt from
        # scratch on every poll, so a spinner's frame counter stored in a
        # row would reset twice a second; and the busy set changes on a
        # different clock from the inventory, which would mean rebuilding
        # the whole tree to start one spinner.
        self.busy = {}
        self._pulse = 0
        self._pulse_source = None

        self.store = Gtk.TreeStore(str, str, str, GdkPixbuf.Pixbuf, str, str)
        self.view = Gtk.TreeView(model=self.store)
        self.view.set_headers_visible(False)
        self.view.set_enable_tree_lines(True)
        self.view.set_enable_search(False)
        self.view.set_tooltip_column(COL_TOOLTIP)
        self.view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)

        self._build_columns(row_ypad)
        self._setup_dnd()

        self.view.get_selection().connect("changed", self._on_selection)
        self.view.connect("row-activated", self._on_row_activated)
        self.view.connect("button-press-event", self._on_button_press)
        self.view.connect("popup-menu", self._on_popup_menu_key)

        search = self._build_search()
        self.pack_start(search, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.view)
        self.pack_start(scroll, True, True, 0)

        self.empty_state = self._build_empty_state()
        self.pack_start(self.empty_state, True, True, 0)
        self.scroll = scroll

        # The whole sidebar can be hidden from the toolbar, and a hidden one
        # must stay hidden through the window's show_all(). Its children are
        # shown here instead, so show()/hide() on the sidebar itself is all
        # the toggle has to do -- the same arrangement the task pane uses.
        for child in (search, scroll):
            child.show_all()
        self.set_no_show_all(True)

    # -- construction --------------------------------------------------

    def _build_columns(self, row_ypad):
        column = Gtk.TreeViewColumn()

        # The spinner stands in the status icon's place rather than beside
        # it, so a row that is working does not get wider than its
        # neighbours and shove the whole tree sideways.
        spinner = Gtk.CellRendererSpinner()
        spinner.set_property("xpad", 2)
        spinner.set_property("size", Gtk.IconSize.MENU)
        spinner.set_property("active", True)
        column.pack_start(spinner, False)
        column.set_cell_data_func(spinner, self._spinner_data)

        icon = Gtk.CellRendererPixbuf()
        icon.set_property("xpad", 2)
        column.pack_start(icon, False)
        column.add_attribute(icon, "pixbuf", COL_ICON)
        column.set_cell_data_func(icon, self._icon_data)
        text = Gtk.CellRendererText()
        text.set_property("ypad", row_ypad)
        text.set_property("ellipsize", 3)
        # Editing is GTK's own inline row editor, but it is switched on only
        # for the duration of one deliberate rename. Left permanently
        # editable, a second click on a selected row starts an edit, which
        # is a very easy way to rename a VM by accident.
        text.connect("edited", self._on_name_edited)
        text.connect("editing-started", self._on_editing_started)
        text.connect("editing-canceled", lambda *_: self._end_editing())
        self.name_renderer = text
        self.renderers.append(text)
        column.pack_start(text, True)
        column.add_attribute(text, "text", COL_LABEL)
        column.set_expand(True)
        self.name_column = column
        self.view.append_column(column)

    # -- busy rows -----------------------------------------------------

    def _spinner_data(self, _column, cell, model, row, _data):
        key = model.get_value(row, COL_KEY)
        busy = bool(key) and key in self.busy
        cell.set_property("visible", busy)
        if busy:
            # Every spinner shares one counter, so they turn together
            # instead of drifting apart by whenever each one started.
            cell.set_property("pulse", self._pulse)

    def _icon_data(self, _column, cell, model, row, _data):
        key = model.get_value(row, COL_KEY)
        cell.set_property("visible", not (bool(key) and key in self.busy))

    def set_busy(self, busy):
        """Which guests are waiting on a change they have asked for.

        'busy' maps a guest key to (tooltip, name to show meanwhile). The
        name lets a rename display its new value straight away: the old one
        coming back for a poll or two, then changing again, is the thing
        this replaces.
        """
        busy = dict(busy or {})
        if busy == self.busy:
            return
        had = bool(self.busy)
        # Compare what is actually drawn, not just the key set: a rename's
        # displayed name lives in here too.
        relabel = {k: v[1] for k, v in busy.items()} != {
            k: v[1] for k, v in self.busy.items()
        }
        self.busy = busy
        if relabel:
            self.rebuild()
        else:
            self.view.queue_draw()
        if bool(busy) != had:
            self._sync_pulse()

    def _sync_pulse(self):
        """Run the animation timer only while something is spinning."""
        if self.busy and self._pulse_source is None:
            self._pulse_source = GLib.timeout_add(PULSE_MS, self._on_pulse)
        elif not self.busy and self._pulse_source is not None:
            GLib.source_remove(self._pulse_source)
            self._pulse_source = None

    def _on_pulse(self):
        if not self.busy:
            self._pulse_source = None
            return False
        self._pulse += 1
        self.view.queue_draw()
        return True

    def stop(self):
        """Drop the animation timer, so a closing window leaves none behind."""
        self.busy = {}
        self._sync_pulse()

    def _build_search(self):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box.get_style_context().add_class("sidebar-search")

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search guests")
        self.search_entry.set_tooltip_text(
            "Filter every server by name, VMID, node, status, tag or type"
        )
        self.search_entry.connect("search-changed", self._on_search_changed)
        box.pack_start(self.search_entry, True, True, 0)

        self.view_button = Gtk.Button()
        self.view_button.set_relief(Gtk.ReliefStyle.NONE)
        self.view_image = Gtk.Image.new_from_icon_name(
            "folder-symbolic", Gtk.IconSize.MENU
        )
        self.view_button.add(self.view_image)
        self.view_button.connect("clicked", lambda *_: self.cycle_view())
        box.pack_start(self.view_button, False, False, 0)
        self._update_view_button()
        return box

    def _build_empty_state(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        box.set_no_show_all(True)

        label = Gtk.Label(label="No connections")
        label.get_style_context().add_class("dim")
        box.pack_start(label, False, False, 0)

        button = Gtk.Button(label="Connect...")
        button.connect("clicked", lambda *_: self.emit("connect-requested"))
        box.pack_start(button, False, False, 0)

        for child in (label, button):
            child.show()
        return box

    def _setup_dnd(self):
        """Dragging a guest onto a folder moves it, in folder view only."""
        self.view.connect("drag-data-get", self._on_drag_data_get)
        self.view.connect("drag-data-received", self._on_drag_data_received)
        self.view.connect("drag-motion", self._on_drag_motion)
        self._apply_dnd()

    def _apply_dnd(self):
        """Arm or disarm dragging entirely.

        Unset rather than refused in the handlers: a drag that starts, draws
        a row under the pointer and then quietly does nothing is worse than
        one that never starts. With the source unset the tree behaves like an
        ordinary list again, which is the point of turning it off.
        """
        if self.dnd_enabled:
            self.view.enable_model_drag_source(
                Gdk.ModifierType.BUTTON1_MASK, [DRAG_TARGET], Gdk.DragAction.MOVE
            )
            self.view.enable_model_drag_dest([DRAG_TARGET], Gdk.DragAction.MOVE)
        else:
            self.view.unset_rows_drag_source()
            self.view.unset_rows_drag_dest()

    def set_dnd_enabled(self, enabled):
        self.dnd_enabled = bool(enabled)
        self._apply_dnd()

    # -- appearance ----------------------------------------------------

    def set_row_ypad(self, ypad):
        for renderer in self.renderers:
            renderer.set_property("ypad", ypad)
        self.view.queue_resize()

    def set_dark(self, dark):
        palette = PALETTES[bool(dark)]
        if palette is self._palette:
            return
        self._dark = bool(dark)
        self._palette = palette
        self.icons.clear()
        self.rebuild()

    def _update_view_button(self):
        current = self.view_mode
        self.view_image.set_from_icon_name(VIEW_ICONS[current], Gtk.IconSize.MENU)
        self.view_button.set_tooltip_text(f"Grouped by {VIEW_NAMES[current]}")

    def cycle_view(self):
        """Move to the next shape: node, then folder, then tag, then back."""
        self.set_view_mode(VIEWS[(VIEWS.index(self.view_mode) + 1) % len(VIEWS)])

    def set_view_mode(self, view):
        if view not in VIEWS or view == self.view_mode:
            return
        self.view_mode = view
        self._update_view_button()
        self.rebuild()
        self.emit("view-changed")

    @property
    def folder_view(self):
        """Whether the folder shape is showing.

        Kept as a property because folders are the only view with machinery
        of their own -- drag and drop, the folder menu entries, the notes
        scan -- and every one of those asks this question rather than caring
        which of the other two is up.
        """
        return self.view_mode == FOLDER_VIEW

    @folder_view.setter
    def folder_view(self, on):
        self.set_view_mode(FOLDER_VIEW if on else NODE_VIEW)

    def set_name_format(self, style, templates_last=None):
        if templates_last is None:
            templates_last = self.templates_last
        if style == self.name_format and templates_last == self.templates_last:
            return
        self.name_format = style
        self.templates_last = bool(templates_last)
        self.rebuild()

    # -- renaming ------------------------------------------------------

    def start_rename(self, key):
        """Begin an inline edit of a guest's name.

        GTK's own row editor rather than a dialog, so it reads like renaming
        a file. The cell is only editable while this is in progress.
        """
        row = self._find_row(key)
        if row is None:
            return
        guest = self.guests.get(key)
        if guest is None:
            return
        self._editing_key = key
        # The editor should start from the bare name, not the formatted
        # "name (id)" the row displays -- otherwise every rename begins by
        # deleting the VMID the user did not type in the first place.
        self.store.set_value(row, COL_LABEL, guest.name)
        self.name_renderer.set_property("editable", True)
        self.view.set_cursor(self.store.get_path(row), self.name_column, True)

    def _on_editing_started(self, _renderer, editable, _path):
        """Restrict what can be typed to what Proxmox will accept."""
        if isinstance(editable, Gtk.Entry):
            editable.connect("insert-text", self._on_name_insert)

    def _on_name_insert(self, editable, text, _length, _position):
        if self._reinserting:
            return
        filtered = "".join(c for c in text if GUEST_NAME_CHARS.match(c))
        if filtered == text:
            return
        # Rejected characters are dropped silently: a typed space or slash is
        # a slip, and an error popup mid-word would be worse than nothing
        # happening.
        editable.stop_emission("insert-text")
        if not filtered:
            return
        self._reinserting = True
        try:
            position = editable.get_position()
            editable.insert_text(filtered, position)
            editable.set_position(position + len(filtered))
        finally:
            self._reinserting = False

    def _on_name_edited(self, _renderer, path, text):
        key, self._editing_key = self._editing_key, None
        self.name_renderer.set_property("editable", False)
        guest = self.guests.get(key) if key else None
        text = (text or "").strip()
        if guest is not None and text and text != guest.name:
            self.emit("guest-renamed", key, text)
        self.rebuild()

    def _end_editing(self):
        self._editing_key = None
        self.name_renderer.set_property("editable", False)
        self.rebuild()

    # -- filtering -----------------------------------------------------

    def _on_search_changed(self, entry):
        self.filter_text = entry.get_text().strip().lower()
        self.rebuild()
        self.emit("filter-changed")

    def matches_filter(self, guest):
        if not self.filter_text:
            return True
        haystack = " ".join(
            (
                guest.name or "",
                str(guest.vmid),
                guest.node or "",
                guest.status or "",
                guest.tags or "",
                guest.kind or "",
                guest.connection or "",
                "/".join(guest.folder),
                "container ct" if guest.is_container else "vm",
            )
        ).lower()
        return all(term in haystack for term in self.filter_text.split())

    # -- data ----------------------------------------------------------

    def update(self, connections):
        """Take the current connection list and redraw the tree."""
        self._connections = list(connections)
        self.guests = {}
        for connection in self._connections:
            for key, guest in connection.guests.items():
                self.guests[key] = guest
        self.rebuild()

    def rebuild(self):
        if self._editing_key is not None:
            return  # an inline rename is open; leave it alone
        selected = self.selected_keys()
        expanded = self._expanded_ids() or self._saved_expanded

        # Clearing the store empties the selection, which would otherwise
        # emit "guest-selected" with nothing selected on every poll. Anything
        # listening -- the summary page above all -- would then throw its
        # detail away and fetch it again, which is the flicker.
        self._suppress_selection = True
        try:
            self.store.clear()
            self._folders = set()

            for connection in self._connections:
                self._add_connection(connection)

            has_connections = bool(self._connections)
            self.scroll.set_visible(has_connections)
            self.empty_state.set_visible(not has_connections)

            self._restore_expansion(expanded)
            # Once there are rows to remember expansion on, the tree is the
            # authority again -- otherwise collapsing everything would be
            # undone by the restored session on the next poll.
            if self.store.get_iter_first() is not None:
                self._saved_expanded = set()
            for key in selected:
                self.select_key(key, add=True)
        finally:
            self._suppress_selection = False

        # Only tell anyone if the selection genuinely moved -- a guest that
        # vanished from the tree, say.
        if self.selected_keys() != selected:
            self.emit("guest-selected", self.selected_key() or "")

    def _add_connection(self, connection):
        icon_colour = {
            CONNECTED: self._palette["group"],
            CONNECTING: self._palette["pending"],
            FAILED: self._palette["failed"],
        }.get(connection.state, self._palette["stopped"])
        icon_name = (
            "network-server-symbolic"
            if connection.state == CONNECTED
            else "network-error-symbolic"
        )

        label = connection.label
        tooltip = connection.label
        if connection.state == FAILED:
            label = f"{connection.label}  (failed)"
            tooltip = f"{connection.label}\n{connection.error}"
        elif connection.state == CONNECTING:
            label = f"{connection.label}  (connecting)"
        elif connection.state != CONNECTED:
            label = f"{connection.label}  ({connection.state})"

        root = self.store.append(
            None,
            [
                "",
                label,
                tooltip,
                self.icons.get(icon_name, icon_colour),
                "connection",
                connection.id,
            ],
        )

        if connection.state == CONNECTED and not connection.loaded:
            self.store.append(
                root,
                [
                    "",
                    "Loading...",
                    "Fetching the inventory",
                    self.icons.get(
                        "content-loading-symbolic", self._palette["stopped"]
                    ),
                    "loading",
                    connection.id,
                ],
            )
            return

        guests = [g for g in connection.guests.values() if self.matches_filter(g)]
        if self.view_mode == FOLDER_VIEW:
            self._add_folder_view(root, connection, guests)
        elif self.view_mode == TAG_VIEW:
            self._add_tag_view(root, connection, guests)
        else:
            self._add_node_view(root, connection, guests)

    def _guest_sort_key(self, guest):
        """Order guests by whichever half of their name is shown first.

        Sorting by VMID under a name-first label reads as no order at all,
        so the sort follows the label: name first means alphabetical, ID
        first means numeric. The other half breaks ties, so two guests with
        the same name still have a stable order.

        Templates optionally sort as a block after everything else, keeping
        the same ordering rule among themselves.
        """
        group = 1 if (self.templates_last and guest.template) else 0
        if self.name_format == "id":
            return (group, guest.vmid, (guest.name or "").lower())
        return (group, (guest.name or "").lower(), guest.vmid)

    @staticmethod
    def _folder_sort_key(path):
        """Folders sort by name, case-insensitively, at every level.

        Independent of how guests are sorted: folders are always named
        things and always sit above the guests, so the VMID has no say here.
        Comparing lowercased tuples keeps a parent immediately before its
        children, which the tree build relies on.
        """
        return tuple(part.lower() for part in path)

    def _add_node_view(self, root, connection, guests):
        by_node = {}
        for guest in guests:
            by_node.setdefault(guest.node, []).append(guest)

        for node_name in sorted(by_node):
            members = by_node[node_name]
            running = sum(1 for g in members if g.running)
            node_iter = self.store.append(
                root,
                [
                    "",
                    f"{node_name}  ({running}/{len(members)})",
                    f"{node_name}\n{running} of {len(members)} running",
                    self.icons.get("computer-symbolic", self._palette["group"]),
                    "node",
                    f"{connection.id}/{node_name}",
                ],
            )
            for guest in sorted(members, key=self._guest_sort_key):
                self._add_guest(node_iter, guest)

    def _add_node_rows(self, root, connection):
        """The server's nodes, listed but not expanded into.

        Both of the views that group guests by something other than the
        node still show them: which machines are in the cluster, and how
        much is running on each, is worth knowing whatever the guests below
        are sorted by. Counted over every guest rather than the filtered
        set, so a search narrows the groups without making a node look half
        empty.
        """
        for node_name in sorted({g.node for g in connection.guests.values()}):
            members = [g for g in connection.guests.values() if g.node == node_name]
            running = sum(1 for g in members if g.running)
            self.store.append(
                root,
                [
                    "",
                    f"{node_name}  ({running}/{len(members)})",
                    f"{node_name}\n{running} of {len(members)} running",
                    self.icons.get("computer-symbolic", self._palette["group"]),
                    "node",
                    f"{connection.id}/{node_name}",
                ],
            )

    def _add_tag_view(self, root, connection, guests):
        """A group per tag, and guests under every tag they carry.

        The repetition is the design, not an oversight -- see the module
        docstring. Each group counts its own members, so the numbers add up
        to more than the estate and are meant to: they say how many guests
        carry that tag, which is the question a tag group answers.
        """
        self._add_node_rows(root, connection)

        # Grouped case-insensitively: Proxmox accepts 'Prod' and 'prod' as
        # two tags, and nobody reading a tree thinks of them as two things.
        # The spelling shown is the first one seen in guest order, so it is
        # one somebody actually typed and it does not change between polls.
        #
        # Untagged is keyed by None rather than by its own name, so a guest
        # genuinely tagged "untagged" keeps a group of its own.
        by_tag = {}
        # Sorted once, here: the members of every group then come out in
        # order without sorting each of them again, and the spelling picked
        # for a group is decided by the same stable order.
        for guest in sorted(guests, key=self._guest_sort_key):
            for tag in guest_tags(guest.tags) or [None]:
                key = tag.casefold() if tag else None
                by_tag.setdefault(key, (tag or UNTAGGED, []))[1].append(guest)

        ordered = sorted(key for key in by_tag if key is not None)
        if None in by_tag:
            ordered.append(None)

        for key in ordered:
            tag, members = by_tag[key]
            running = sum(1 for g in members if g.running)
            colour = "stopped" if key is None else "group"
            tag_iter = self.store.append(
                root,
                [
                    "",
                    f"{tag}  ({running}/{len(members)})",
                    f"{tag}\n{running} of {len(members)} running",
                    self.icons.get("user-bookmarks-symbolic", self._palette[colour]),
                    "tag",
                    f"{connection.id}#{tag}",
                ],
            )
            for guest in members:
                self._add_guest(tag_iter, guest)

    def _add_folder_view(self, root, connection, guests):
        """Nodes first, then the folder tree; guests live only in folders.

        A guest whose notes have not been read yet is held back rather than
        drawn at the root, because it would jump into its folder a moment
        later. On the first load that means the server shows "Loading..."
        until every folder is known.
        """
        known = [g for g in guests if g.notes_loaded]
        if not known and guests:
            self.store.append(
                root,
                [
                    "",
                    "Loading folders...",
                    "Reading guest notes",
                    self.icons.get(
                        "content-loading-symbolic", self._palette["stopped"]
                    ),
                    "loading",
                    connection.id,
                ],
            )
            return
        guests = known

        self._add_node_rows(root, connection)

        # Every folder that any guest claims, including intermediate levels,
        # so "Production/Customer A" creates "Production" too.
        paths = set()
        for guest in guests:
            for depth in range(1, len(guest.folder) + 1):
                paths.add(tuple(guest.folder[:depth]))

        folder_iters = {(): root}
        for path in sorted(paths, key=self._folder_sort_key):
            parent = folder_iters.get(path[:-1], root)
            joined = "/".join(path)
            self._folders.add(joined)
            folder_iters[path] = self.store.append(
                parent,
                [
                    "",
                    path[-1],
                    joined,
                    self.icons.get("folder-symbolic", self._palette["group"]),
                    "folder",
                    f"{connection.id}\t{joined}",
                ],
            )

        for guest in sorted(
            guests,
            key=lambda g: (self._folder_sort_key(g.folder), self._guest_sort_key(g)),
        ):
            parent = folder_iters.get(tuple(guest.folder), root)
            self._add_guest(parent, guest)

    def _add_guest(self, parent, guest):
        status = guest.status
        if guest.template:
            status = "template"
        elif guest.lock:
            status = f"{guest.status} ({guest.lock})"

        pending = self.busy.get(guest.key)
        if pending is not None and pending[1]:
            # Show the name that was asked for. The server has not caught up
            # yet, and flicking back to the old one in the meantime is
            # exactly what the spinner exists to avoid.
            label = format_guest_name(_Renamed(guest, pending[1]), self.name_format)
        else:
            label = format_guest_name(guest, self.name_format)
        if guest.is_container:
            label = f"{label}  [CT]"

        tooltip = f"{guest.name}\n{status}"
        if pending is not None:
            tooltip = f"{guest.name}\n{pending[0]}"
        if guest.running:
            tooltip += f"\nUptime {guest.uptime_text}\nMemory {guest.memory_text}"
        tooltip += f"\nNode {guest.node}"
        if guest.folder:
            tooltip += f"\nFolder {'/'.join(guest.folder)}"

        self.store.append(
            parent,
            [
                guest.key,
                label,
                tooltip,
                self._guest_icon(guest),
                "guest",
                guest.connection,
            ],
        )

    def _guest_icon(self, guest):
        # Shared with the summary, so the same guest cannot be green in one
        # place and grey in the other. The tree passes its own cache, which
        # it clears when the palette flips.
        return guest_icon(guest, dark=self._dark, size=ICON_SIZE, cache=self.icons)

    # -- expansion / selection -----------------------------------------

    def _row_identity(self, row):
        """A stable name for a row, so expansion survives a rebuild."""
        kind = self.store.get_value(row, COL_KIND)
        return f"{kind}:{self.store.get_value(row, COL_ID)}"

    def _expanded_ids(self):
        expanded = set()

        def collect(_view, path, *_rest):
            expanded.add(self._row_identity(self.store.get_iter(path)))

        self.view.map_expanded_rows(collect, None)
        return expanded

    def expanded_ids(self):
        """What is expanded right now, for saving across sessions."""
        return sorted(self._expanded_ids() or self._saved_expanded)

    def restore_expansion(self, identities):
        """Seed the expansion state from a previous session.

        Applied on the next rebuild rather than immediately, because at
        startup the tree is still empty -- the servers have not answered yet.
        """
        self._saved_expanded = set(identities or ())

    def _restore_expansion(self, expanded):
        if not expanded:
            self.view.expand_all()
            return

        def walk(parent):
            row = self.store.iter_children(parent)
            while row is not None:
                if self._row_identity(row) in expanded:
                    self.view.expand_row(self.store.get_path(row), False)
                walk(row)
                row = self.store.iter_next(row)

        walk(None)

    def visible_keys(self):
        """Guest keys currently in the tree, i.e. after filtering."""
        keys = []

        def walk(parent):
            row = self.store.iter_children(parent)
            while row is not None:
                key = self.store.get_value(row, COL_KEY)
                if key:
                    keys.append(key)
                walk(row)
                row = self.store.iter_next(row)

        walk(None)
        return keys

    def visible_guests(self):
        return [self.guests[k] for k in self.visible_keys() if k in self.guests]

    def selected_keys(self):
        model, paths = self.view.get_selection().get_selected_rows()
        keys = []
        for path in paths:
            key = model.get_value(model.get_iter(path), COL_KEY)
            if key:
                keys.append(key)
        return keys

    def selected_key(self):
        keys = self.selected_keys()
        return keys[0] if keys else None

    def selected_guest(self):
        key = self.selected_key()
        return self.guests.get(key) if key else None

    def selected_guests(self):
        return [self.guests[k] for k in self.selected_keys() if k in self.guests]

    def _find_row(self, key):
        found = []

        def walk(parent):
            row = self.store.iter_children(parent)
            while row is not None:
                if self.store.get_value(row, COL_KEY) == key:
                    found.append(row)
                    return True
                if walk(row):
                    return True
                row = self.store.iter_next(row)
            return False

        walk(None)
        return found[0] if found else None

    def select_key(self, key, add=False):
        row = self._find_row(key)
        if row is None:
            return False
        path = self.store.get_path(row)
        self.view.expand_to_path(path)
        selection = self.view.get_selection()
        if not add:
            if selection.count_selected_rows() == 1 and selection.path_is_selected(
                path
            ):
                return True
            selection.unselect_all()
        selection.select_path(path)
        return True

    # -- drag and drop -------------------------------------------------

    def _on_drag_data_get(self, _view, _context, selection_data, _info, _time):
        keys = self.selected_keys()
        if keys:
            selection_data.set(
                selection_data.get_target(), 8, "\n".join(keys).encode("utf-8")
            )

    def _on_drag_motion(self, view, context, x, y, time):
        """Only folders and folder-view roots are valid drop targets."""
        if not self.folder_view or not self.dnd_enabled:
            Gdk.drag_status(context, 0, time)
            return True
        result = view.get_dest_row_at_pos(x, y)
        if result is None:
            Gdk.drag_status(context, Gdk.DragAction.MOVE, time)
            return True
        path, _position = result
        kind = self.store.get_value(self.store.get_iter(path), COL_KIND)
        allowed = kind in ("folder", "connection")
        Gdk.drag_status(context, Gdk.DragAction.MOVE if allowed else 0, time)
        return True

    def _on_drag_data_received(self, view, context, x, y, selection_data, _info, time):
        if not self.folder_view or not self.dnd_enabled:
            Gtk.drag_finish(context, False, False, time)
            return
        payload = selection_data.get_data()
        keys = payload.decode("utf-8").split("\n") if payload else []

        target = ""
        result = view.get_dest_row_at_pos(x, y)
        if result is not None:
            row = self.store.get_iter(result[0])
            kind = self.store.get_value(row, COL_KIND)
            if kind == "folder":
                target = self.store.get_value(row, COL_ID).split("\t", 1)[-1]
            elif kind != "connection":
                Gtk.drag_finish(context, False, False, time)
                return

        for key in keys:
            if key in self.guests:
                self.emit("guest-moved", key, target)
        Gtk.drag_finish(context, True, False, time)

    # -- events --------------------------------------------------------

    def _on_selection(self, _selection):
        if self._suppress_selection:
            return
        self.emit("guest-selected", self.selected_key() or "")

    def _on_row_activated(self, view, path, _column):
        row = self.store.get_iter(path)
        key = self.store.get_value(row, COL_KEY)
        if key:
            self.emit("guest-activated", key)
        elif view.row_expanded(path):
            view.collapse_row(path)
        else:
            view.expand_row(path, False)

    def _on_button_press(self, view, event):
        if event.type != Gdk.EventType.BUTTON_PRESS or event.button != 3:
            return False

        result = view.get_path_at_pos(int(event.x), int(event.y))
        if result is None:
            # Empty space below the tree: offer to add a server.
            self._show_background_menu(event)
            return True

        path = result[0]
        selection = view.get_selection()
        if not selection.path_is_selected(path):
            selection.unselect_all()
            selection.select_path(path)

        row = self.store.get_iter(path)
        kind = self.store.get_value(row, COL_KIND)
        if kind == "connection":
            self._show_connection_menu(self.store.get_value(row, COL_ID), event)
            return True

        guests = self.selected_guests()
        if not guests:
            # A node, a folder or the "Loading..." placeholder. None of them
            # has an action of its own, and adding a server has nothing to do
            # with the row that was clicked -- that belongs to the server row
            # and to the empty space below the tree.
            return True
        self._show_menu(guests, event)
        return True

    def _on_popup_menu_key(self, _view):
        guests = self.selected_guests()
        if not guests:
            return False
        self._show_menu(guests, None)
        return True

    # -- menus ---------------------------------------------------------

    def _popup(self, menu, event):
        menu.show_all()
        self._menu = menu
        if event is not None:
            menu.popup_at_pointer(event)
        else:
            menu.popup_at_widget(
                self.view, Gdk.Gravity.CENTER, Gdk.Gravity.NORTH_WEST, None
            )

    def _show_background_menu(self, event):
        menu = Gtk.Menu()
        item = Gtk.MenuItem(label="Connect...")
        item.connect("activate", lambda *_: self.emit("connect-requested"))
        menu.append(item)
        self._popup(menu, event)

    def _show_connection_menu(self, connection_id, event):
        menu = Gtk.Menu()
        connection = next((c for c in self._connections if c.id == connection_id), None)

        if connection is not None and connection.state == FAILED:
            retry = Gtk.MenuItem(label="Reconnect")
            retry.connect(
                "activate", lambda *_: self.emit("reconnect-requested", connection_id)
            )
            menu.append(retry)

        disconnect = Gtk.MenuItem(label="Disconnect")
        disconnect.connect(
            "activate", lambda *_: self.emit("disconnect-requested", connection_id)
        )
        menu.append(disconnect)

        menu.append(Gtk.SeparatorMenuItem())
        add = Gtk.MenuItem(label="Connect...")
        add.connect("activate", lambda *_: self.emit("connect-requested"))
        menu.append(add)
        self._popup(menu, event)

    def _show_menu(self, guests, event):
        menu = Gtk.Menu()
        if len(guests) > 1:
            self._build_bulk_menu(menu, guests)
        else:
            self._build_single_menu(menu, guests[0])
        self._popup(menu, event)

    def _build_single_menu(self, menu, guest):
        """Guest actions.

        Snapshots are deliberately absent: they are on the toolbar and in the
        VM menu, and a context menu that lists everything is a context menu
        nobody reads.
        """
        if guest.template:
            # A template does not run, so power, console and snapshots are
            # all dead entries. Cloning is the only thing you do with one.
            self._build_template_menu(menu, guest)
            return

        console = Gtk.MenuItem(label="Open Console")
        console.connect(
            "activate", lambda *_: self.emit("guest-action", guest.key, "console")
        )
        menu.append(console)
        menu.append(Gtk.SeparatorMenuItem())

        for action in action_defs.visible_actions(guest):
            if guest.kind not in action.kinds:
                continue
            item = Gtk.MenuItem(label=action.label)
            item.set_sensitive(action_defs.enabled_for(action, guest))
            item.connect(
                "activate",
                lambda _i, name=action.name: self.emit("guest-action", guest.key, name),
            )
            menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())
        self._append_common_items(menu, guest)

    def _build_template_menu(self, menu, guest):
        clone = Gtk.MenuItem(label="Clone...")
        clone.connect("activate", lambda *_: self.emit("clone-requested", guest.key))
        menu.append(clone)
        menu.append(Gtk.SeparatorMenuItem())
        self._append_common_items(menu, guest)

    def _append_common_items(self, menu, guest):
        """The entries every guest row gets, template or not."""
        rename = Gtk.MenuItem(label="Rename...")
        rename.connect("activate", lambda *_: self.start_rename(guest.key))
        menu.append(rename)

        if self.folder_view:
            # Only the "new" case: existing folders are reachable by dragging
            # the guest onto them, and a subfolder cannot be created any
            # other way, which is the whole reason this entry exists.
            subfolder = Gtk.MenuItem(label="Move to New Subfolder...")
            subfolder.connect(
                "activate", lambda *_: self.emit("new-subfolder", guest.key)
            )
            menu.append(subfolder)

        delete = Gtk.MenuItem(label="Delete...")
        reason = self._delete_blocked_reason(guest)
        delete.set_sensitive(reason is None)
        delete.set_tooltip_text(reason or "Destroy this guest and its disks")
        delete.connect("activate", lambda *_: self.emit("delete-requested", guest.key))
        menu.append(delete)

        menu.append(Gtk.SeparatorMenuItem())
        refresh = Gtk.MenuItem(label="Refresh")
        refresh.connect(
            "activate", lambda *_: self.emit("guest-action", guest.key, "refresh")
        )
        menu.append(refresh)

        # Last, and separated: everything above acts now, this one opens a
        # dialog. Containers are left out because the only settings there
        # are today are about the SPICE console, which they do not have.
        if not guest.is_container:
            menu.append(Gtk.SeparatorMenuItem())
            settings = Gtk.MenuItem(label="Settings")
            settings.set_tooltip_text(
                "Console settings for this VM, stored on the server"
            )
            settings.connect(
                "activate", lambda *_: self.emit("settings-requested", guest.key)
            )
            menu.append(settings)

    @staticmethod
    def _delete_blocked_reason(guest):
        """Why this guest cannot be deleted, or None if it can.

        Proxmox would refuse each of these itself, but an entry that is
        greyed out with a reason is a better answer than one that opens a
        confirmation dialog and then fails.
        """
        if guest.protected:
            return "Protected in Proxmox. Clear the protection flag before deleting."
        if guest.lock:
            return f"A task is running on it ({guest.lock})."
        if guest.status not in ("stopped", "unknown"):
            return "Stop the guest before deleting it."
        return None

    def _build_bulk_menu(self, menu, guests):
        heading = Gtk.MenuItem(label=f"{len(guests)} guests selected")
        heading.set_sensitive(False)
        menu.append(heading)
        menu.append(Gtk.SeparatorMenuItem())

        for action in action_defs.POWER_ACTIONS:
            if action.name == "resume":
                continue  # counted under Start, per guest
            applicable = [
                g
                for g in guests
                if action_defs.enabled_for(action_defs.resolve(action.name, g), g)
            ]
            label = action.label
            if action.name == "start" and applicable:
                # A mixed selection can need both, so say so rather than
                # picking one of the two names.
                names = {action_defs.resolve("start", g).label for g in applicable}
                label = "/".join(sorted(names))
            item = Gtk.MenuItem(label=f"{label} ({len(applicable)})")
            item.set_sensitive(bool(applicable))
            item.connect(
                "activate", lambda _i, name=action.name: self.emit("bulk-action", name)
            )
            menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())
        snapshot = Gtk.MenuItem(label=f"Take Snapshot ({len(guests)})")
        snapshot.set_sensitive(any(not g.template for g in guests))
        snapshot.connect(
            "activate", lambda *_: self.emit("bulk-action", "snapshot-take")
        )
        menu.append(snapshot)

"""Snapshot dialogs.

Proxmox reports a synthetic snapshot named 'current' representing the live
state; the API client filters it out, so everything here can treat the list
as real snapshots only.
"""

import logging
import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from ..api import ProxmoxError
from ..api.client import task_upid
from ..api.models import human_age
from ..theme import decorate as theme_decorate

log = logging.getLogger(__name__)


def describe_revert(latest):
    """Tooltip for the Revert button, naming what it would roll back to."""
    if not latest:
        return "No snapshots to revert to"
    name = latest.get("name", "?")
    age = human_age(latest.get("snaptime"))
    return f'Revert to "{name}" ({age})' if age else f'Revert to "{name}"'


def _timestamp(value):
    if not value:
        return "-"
    import datetime

    return datetime.datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")


class TakeSnapshotDialog(Gtk.Dialog):
    """Name, description, and whether to include guest RAM."""

    def __init__(self, parent, guest):
        super().__init__(title="Take Snapshot", transient_for=parent, modal=True)
        self.guest = guest
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        take = self.add_button("Take Snapshot", Gtk.ResponseType.OK)
        take.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_default_size(420, -1)

        content = self.get_content_area()
        content.set_spacing(6)
        content.set_border_width(12)

        grid = Gtk.Grid(row_spacing=6, column_spacing=10)
        content.pack_start(grid, False, False, 0)

        grid.attach(self._label("Name"), 0, 0, 1, 1)
        self.name_entry = Gtk.Entry()
        self.name_entry.set_hexpand(True)
        self.name_entry.set_activates_default(True)
        self.name_entry.set_text(self._default_name())
        # Proxmox only accepts [A-Za-z0-9_-] starting with a letter.
        self.name_entry.set_tooltip_text(
            "Letters, digits, hyphen and underscore; must start with a letter"
        )
        grid.attach(self.name_entry, 1, 0, 1, 1)

        grid.attach(self._label("Description"), 0, 1, 1, 1)
        self.description = Gtk.Entry()
        self.description.set_activates_default(True)
        grid.attach(self.description, 1, 1, 1, 1)

        self.vmstate = Gtk.CheckButton(label="Include guest RAM")
        self.vmstate.set_active(False)
        self.vmstate.set_tooltip_text(
            "Saves memory so the snapshot restores to a running guest. "
            "Slower, and needs free space equal to the guest's RAM."
        )
        # Only meaningful for a running QEMU guest.
        self.vmstate.set_sensitive(guest.running and not guest.is_container)
        content.pack_start(self.vmstate, False, False, 0)

        self.message = Gtk.Label(xalign=0.0)
        self.message.set_line_wrap(True)
        content.pack_start(self.message, False, False, 0)

        theme_decorate(self)
        self.show_all()
        self.name_entry.grab_focus()
        self.name_entry.select_region(0, -1)

    @staticmethod
    def _label(text):
        label = Gtk.Label(label=text, xalign=1.0)
        label.get_style_context().add_class("dim")
        return label

    @staticmethod
    def _default_name():
        import datetime

        return datetime.datetime.now().strftime("snap%Y%m%d-%H%M%S")

    def values(self):
        return (
            self.name_entry.get_text().strip(),
            self.description.get_text().strip(),
            self.vmstate.get_active(),
        )


def build_snapshot_tree(rows):
    """Arrange snapshots into the parent/child tree Proxmox actually keeps.

    Each snapshot records the one it was taken from in 'parent', so rolling
    back and then snapshotting again forks the history rather than extending
    it. A flat list hides that completely: two snapshots an hour apart can be
    on branches that share nothing after the fork.

    Returns a list of (row, children) pairs, oldest first at every level.
    Anything whose parent is missing -- including a snapshot whose parent was
    deleted -- becomes a root, so no row is ever dropped.
    """
    by_name = {row.get("name"): row for row in rows}
    children = {}
    roots = []
    for row in rows:
        parent = row.get("parent")
        if parent and parent in by_name and parent != row.get("name"):
            children.setdefault(parent, []).append(row)
        else:
            roots.append(row)

    def order(items):
        return sorted(
            items, key=lambda r: (r.get("snaptime") or 0, r.get("name") or "")
        )

    def build(items, seen):
        result = []
        for row in order(items):
            name = row.get("name")
            if name in seen:
                continue  # a parent cycle; never loop on one
            seen.add(name)
            result.append((row, build(children.get(name, []), seen)))
        return result

    return build(roots, set())


class SnapshotManager(Gtk.Dialog):
    """Show the snapshot tree and act on the selected snapshot."""

    COL_NAME, COL_TIME, COL_DESC, COL_REAL = 0, 1, 2, 3

    def __init__(self, parent, api, guest, on_changed=None):
        super().__init__(
            title=f"Snapshots - {guest.name}", transient_for=parent, modal=True
        )
        self.api = api
        self.guest = guest
        self.on_changed = on_changed or (lambda: None)
        self._busy = False

        self.add_button("Close", Gtk.ResponseType.CLOSE)
        self.set_default_size(560, 340)

        content = self.get_content_area()
        content.set_spacing(6)
        content.set_border_width(10)

        # A tree, not a list: COL_REAL marks the rows that are snapshots, as
        # opposed to the synthetic "NOW" leaf marking where the live state
        # hangs off the history.
        self.store = Gtk.TreeStore(str, str, str, bool)
        self.view = Gtk.TreeView(model=self.store)
        self.view.set_enable_tree_lines(True)
        for title, index in (
            ("Name", self.COL_NAME),
            ("Taken", self.COL_TIME),
            ("Description", self.COL_DESC),
        ):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ypad", 1)
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            self.view.append_column(column)
        self.view.get_selection().connect("changed", lambda *_: self._update_buttons())

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.view)
        content.pack_start(scroll, True, True, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.take_button = Gtk.Button(label="Take")
        self.take_button.connect("clicked", lambda *_: self.take())
        buttons.pack_start(self.take_button, False, False, 0)

        self.rollback_button = Gtk.Button(label="Roll Back")
        self.rollback_button.connect("clicked", lambda *_: self.rollback())
        buttons.pack_start(self.rollback_button, False, False, 0)

        self.delete_button = Gtk.Button(label="Delete")
        self.delete_button.get_style_context().add_class("destructive-action")
        self.delete_button.connect("clicked", lambda *_: self.delete())
        buttons.pack_start(self.delete_button, False, False, 0)
        content.pack_start(buttons, False, False, 0)

        self.message = Gtk.Label(xalign=0.0)
        self.message.set_line_wrap(True)
        self.message.get_style_context().add_class("dim")
        content.pack_start(self.message, False, False, 0)

        theme_decorate(self)
        self.show_all()
        self.reload()

    # -- data ----------------------------------------------------------

    def reload(self):
        self._set_busy(True, "Loading...")

        def worker():
            try:
                rows = self.api.snapshots(
                    self.guest.node,
                    self.guest.vmid,
                    self.guest.kind,
                    include_current=True,
                )
            except ProxmoxError as exc:
                GLib.idle_add(self._failed, str(exc))
                return
            GLib.idle_add(self._populate, rows)

        threading.Thread(target=worker, daemon=True, name="snapshot-list").start()

    def _populate(self, rows):
        self.store.clear()

        def add(parent, nodes):
            for row, children in nodes:
                name = row.get("name", "?")
                current = name == "current"
                node = self.store.append(
                    parent,
                    [
                        "NOW" if current else name,
                        "" if current else _timestamp(row.get("snaptime")),
                        (
                            "You are here"
                            if current
                            else row.get("description", "") or ""
                        ),
                        not current,
                    ],
                )
                add(node, children)

        add(None, build_snapshot_tree(rows))
        self.view.expand_all()
        real = sum(1 for row in rows if row.get("name") != "current")
        self._set_busy(False, f"{real} snapshot(s)" if real else "No snapshots")
        self._update_buttons()
        self.on_changed()
        return False

    def _failed(self, message):
        self._set_busy(False, message)
        return False

    def selected(self):
        """The selected snapshot's name, or None on NOW or nothing."""
        model, row = self.view.get_selection().get_selected()
        if row is None or not model.get_value(row, self.COL_REAL):
            return None
        return model.get_value(row, self.COL_NAME)

    def _set_busy(self, busy, message=""):
        self._busy = busy
        self.message.set_text(message)
        self._update_buttons()

    def _update_buttons(self):
        has_selection = self.selected() is not None
        self.take_button.set_sensitive(not self._busy)
        self.rollback_button.set_sensitive(not self._busy and has_selection)
        self.delete_button.set_sensitive(not self._busy and has_selection)

    # -- actions -------------------------------------------------------

    def take(self):
        dialog = TakeSnapshotDialog(self, self.guest)
        response = dialog.run()
        name, description, vmstate = dialog.values()
        dialog.destroy()
        if response != Gtk.ResponseType.OK or not name:
            return
        self._run(
            "Creating snapshot",
            lambda: self.api.create_snapshot(
                self.guest.node,
                self.guest.vmid,
                name,
                description,
                vmstate,
                self.guest.kind,
            ),
        )

    def rollback(self):
        name = self.selected()
        if name is None:
            return
        if not confirm(
            self,
            "Roll Back",
            f"Roll {self.guest.label} back to '{name}'?\n"
            "Changes made since the snapshot will be lost.",
        ):
            return
        self._run(
            f"Rolling back to {name}",
            lambda: self.api.rollback_snapshot(
                self.guest.node, self.guest.vmid, name, self.guest.kind
            ),
        )

    def delete(self):
        name = self.selected()
        if name is None:
            return
        if not confirm(
            self, "Delete", f"Delete snapshot '{name}'? This cannot be undone."
        ):
            return
        self._run(
            f"Deleting {name}",
            lambda: self.api.delete_snapshot(
                self.guest.node, self.guest.vmid, name, self.guest.kind
            ),
        )

    def _run(self, label, call):
        self._set_busy(True, f"{label}...")

        def worker():
            try:
                upid = call()
            except ProxmoxError as exc:
                GLib.idle_add(self._failed, f"{label} failed: {exc}")
                return
            # Accepting the request is not doing the work: a rollback onto a
            # storage with no room is accepted and then fails on the node, and
            # reloading a list would only show the snapshot still sitting
            # there with no word of why.
            upid = task_upid(upid)
            if upid is not None:
                try:
                    outcome = self.api.wait_for_task(self.guest.node, upid)
                except ProxmoxError as exc:
                    log.info("could not follow snapshot task %s: %s", upid, exc)
                else:
                    if not outcome.ok:
                        GLib.idle_add(
                            self._failed, f"{label} failed: {outcome.message}"
                        )
                        return
            # The task runs server side; reloading picks it up once it lands.
            GLib.idle_add(self.reload)

        threading.Thread(target=worker, daemon=True, name="snapshot-action").start()


def confirm(parent, title, message):
    dialog = Gtk.MessageDialog(
        transient_for=parent,
        modal=True,
        message_type=Gtk.MessageType.QUESTION,
        buttons=Gtk.ButtonsType.NONE,
        text=title,
    )
    dialog.format_secondary_text(message)
    dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
    ok = dialog.add_button(title, Gtk.ResponseType.OK)
    ok.get_style_context().add_class("destructive-action")
    dialog.set_default_response(Gtk.ResponseType.CANCEL)
    theme_decorate(dialog)
    response = dialog.run()
    dialog.destroy()
    return response == Gtk.ResponseType.OK

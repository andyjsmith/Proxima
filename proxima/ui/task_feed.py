"""Cluster task feed.

/cluster/tasks reports recent tasks across every node -- migrations, backups,
snapshots, and the power actions this client issues. Without it every action
here is fire-and-forget, with no indication that it finished or failed.

Hidden entirely until opened from the toolbar, and closed with its own X --
there is no permanently docked strip taking height from the console. Polling
only runs while the pane is visible.
"""

import datetime
import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from ..api import ProxmoxError
from ..theme import decorate as theme_decorate

(COL_START, COL_END, COL_SERVER, COL_NODE, COL_USER, COL_DESC, COL_STATUS, COL_UPID) = (
    range(8)
)

RUNNING = "running"


def _clock(value):
    """Date and time. A task list without dates is unreadable once it spans
    midnight, which the cluster's own history routinely does."""
    if not value:
        return ""
    return datetime.datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")


def _describe(row):
    """What Proxmox's own task list calls the description column."""
    kind = row.get("type", "")
    target = str(row.get("id", "") or "")
    return f"{kind}  {target}".strip()


def _status_of(row):
    if row.get("endtime") is None:
        return RUNNING
    status = row.get("status") or "unknown"
    return "OK" if status == "OK" else status


class TaskFeed(Gtk.Box):
    """Closable pane listing recent cluster tasks."""

    def __init__(self, connections, interval=5, on_closed=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.connections = connections
        self.interval = interval
        self.on_closed = on_closed or (lambda: None)
        self._source = None
        self._busy = False

        # show_all() on the window must not reveal this; it is opened only
        # on request.
        self.set_no_show_all(True)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.get_style_context().add_class("pane-header")
        self.title_label = Gtk.Label(label="Tasks", xalign=0.0)
        header.pack_start(self.title_label, True, True, 0)

        close = Gtk.Button()
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.add(
            Gtk.Image.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        )
        close.set_tooltip_text("Close")
        close.connect("clicked", lambda *_: self.close())
        header.pack_start(close, False, False, 0)
        header.show_all()
        self.pack_start(header, False, False, 0)

        self.store = Gtk.ListStore(str, str, str, str, str, str, str, str)
        self.view = Gtk.TreeView(model=self.store)
        self.view.set_enable_search(False)
        # Only the description ellipsizes; giving every renderer an
        # ellipsize mode makes each column report a minimum width of almost
        # nothing, which is what collapsed them all to "...".
        for title, index in (
            ("Start", COL_START),
            ("End", COL_END),
            ("Server", COL_SERVER),
            ("Node", COL_NODE),
            ("User", COL_USER),
        ):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ypad", 1)
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            self.view.append_column(column)

        renderer = Gtk.CellRendererText()
        renderer.set_property("ypad", 1)
        renderer.set_property("ellipsize", 3)
        description = Gtk.TreeViewColumn("Description", renderer, text=COL_DESC)
        description.set_resizable(True)
        description.set_expand(True)  # takes the leftover width
        description.set_min_width(160)
        self.view.append_column(description)

        renderer = Gtk.CellRendererText()
        renderer.set_property("ypad", 1)
        status = Gtk.TreeViewColumn("Status", renderer, text=COL_STATUS)
        status.set_resizable(True)
        self.view.append_column(status)

        self.view.connect("row-activated", self._on_row_activated)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_size_request(-1, 170)
        scroll.add(self.view)
        scroll.show_all()
        self.pack_start(scroll, True, True, 0)

    # -- visibility ----------------------------------------------------

    def open(self):
        self.show()
        self.refresh()
        if self._source is None:
            self._source = GLib.timeout_add_seconds(self.interval, self._tick)

    def close(self):
        self.hide()
        self.stop()
        self.on_closed()

    def toggle(self):
        self.close() if self.get_visible() else self.open()

    # -- polling -------------------------------------------------------

    def _tick(self):
        self.refresh()
        return True

    def stop(self):
        if self._source is not None:
            GLib.source_remove(self._source)
            self._source = None

    def restart(self):
        """Re-arm the timer, e.g. after the interval setting changed."""
        if not self.get_visible():
            return
        self.stop()
        self._source = GLib.timeout_add_seconds(self.interval, self._tick)

    def refresh(self):
        # Callers refresh after an action without caring whether the pane is
        # open; polling a hidden pane is a wasted round trip per server.
        if self._busy or not self.get_visible():
            return
        self._busy = True

        def worker():
            rows = []
            errors = []
            for connection in self.connections.connected:
                try:
                    for row in connection.cluster_tasks():
                        row["_server"] = connection.label
                        rows.append(row)
                except ProxmoxError as exc:
                    errors.append(f"{connection.label}: {exc}")
                except Exception as exc:
                    errors.append(f"{connection.label}: {exc}")
            # Newest first across every server, not grouped by server.
            rows.sort(key=lambda r: r.get("starttime") or 0, reverse=True)
            if errors and not rows:
                GLib.idle_add(self._failed, "; ".join(errors[:2]))
                return
            GLib.idle_add(self._populate, rows)

        threading.Thread(target=worker, daemon=True, name="task-feed").start()

    def _failed(self, message):
        self._busy = False
        self.title_label.set_text(f"Tasks - {message}")
        return False

    def _populate(self, rows):
        self._busy = False
        self.store.clear()
        running = 0
        for row in rows:
            status = _status_of(row)
            if status == RUNNING:
                running += 1
            self.store.append(
                [
                    _clock(row.get("starttime")),
                    _clock(row.get("endtime")),
                    row.get("_server", ""),
                    row.get("node", ""),
                    row.get("user", ""),
                    _describe(row),
                    status,
                    row.get("upid", ""),
                ]
            )
        self.title_label.set_text(f"Tasks ({running} running)" if running else "Tasks")
        return False

    # -- task log ------------------------------------------------------

    def _on_row_activated(self, view, path, _column):
        row = self.store.get_iter(path)
        upid = self.store.get_value(row, COL_UPID)
        node = self.store.get_value(row, COL_NODE)
        if not upid or not node:
            return
        self._show_log(
            node,
            upid,
            self.store.get_value(row, COL_DESC),
            self.store.get_value(row, COL_SERVER),
        )

    def _api_for_server(self, server):
        for connection in self.connections.connected:
            if connection.label == server:
                return connection.api
        raise ProxmoxError(f"{server} is no longer connected")

    def _show_log(self, node, upid, title, server):
        window = Gtk.Dialog(
            title=f"Task log - {title}", transient_for=self.get_toplevel(), modal=True
        )
        window.add_button("Close", Gtk.ResponseType.CLOSE)
        window.set_default_size(720, 420)

        buffer = Gtk.TextBuffer()
        buffer.set_text("Loading...")
        view = Gtk.TextView(buffer=buffer, editable=False, monospace=True)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        scroll = Gtk.ScrolledWindow()
        scroll.add(view)
        window.get_content_area().pack_start(scroll, True, True, 0)

        theme_decorate(window)
        window.show_all()

        def worker():
            try:
                lines = self._api_for_server(server).task_log(node, upid)
            except Exception as exc:
                lines = [f"could not read the task log: {exc}"]
            GLib.idle_add(lambda: buffer.set_text("\n".join(lines) or "(empty)"))

        threading.Thread(target=worker, daemon=True, name="task-log").start()
        window.run()
        window.destroy()

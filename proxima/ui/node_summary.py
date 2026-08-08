"""The summary side of a node's tab: what Proxmox's own node Summary shows.

Three bands, in the order the question usually gets asked:

  * the meters -- CPU, IO delay, load, RAM, swap, root filesystem -- which
    say whether the machine is in trouble right now;
  * the graphs, which say whether it has been;
  * the facts that do not move: processor, kernel, manager version.

Two calls feed it. /nodes/<node>/status carries everything the meters and
the facts need and is cheap enough to re-read on the window's own poll;
/nodes/<node>/rrddata carries the history and is re-read far less often,
because the server only records a new sample every minute and asking again
between samples is a round trip for the same answer.

Both run on worker threads. Neither is allowed to be in flight twice at
once: the poll is on a timer, and a node that is slow to answer would
otherwise collect a queue of requests it is already too busy to serve.
"""

import threading
import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from ..api.models import NodeStatus
from ..theme import current_dark
from . import status_icons
from .graphs import (
    SERIES_COLOURS,
    Series,
    TimeSeriesGraph,
    bytes_label,
    percent_label,
    rate_label,
)

# Bigger than the tree's 16px: this one sits beside a bold heading. The same
# size the guest summary uses, so the two pages line up.
HEADING_ICON = 24

# How long a history sample is worth keeping before asking for another. The
# server records one a minute at the finest resolution it offers, so anything
# under that is a request for a graph that cannot have changed.
RRD_INTERVAL = 60

TIMEFRAMES = [
    ("hour", "Hour"),
    ("day", "Day"),
    ("week", "Week"),
    ("month", "Month"),
    ("year", "Year"),
]


class Meter(Gtk.Box):
    """One labelled bar: what it measures, how full it is, and the figures.

    The reading goes beside the name rather than inside the bar. GTK draws
    a progress bar's own text centred over the fill, where it is unreadable
    exactly when the bar is half full -- which is most of the time.
    """

    def __init__(self, label):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        name = Gtk.Label(label=label, xalign=0.0)
        name.get_style_context().add_class("summary-key")
        header.pack_start(name, False, False, 0)

        self.value = Gtk.Label(label="-", xalign=1.0)
        self.value.get_style_context().add_class("summary-value")
        self.value.set_selectable(True)
        header.pack_end(self.value, False, False, 0)
        self.pack_start(header, False, False, 0)

        self.bar = Gtk.ProgressBar()
        self.bar.get_style_context().add_class("meter")
        self.bar.set_show_text(False)
        self.bar.set_hexpand(True)
        self.pack_start(self.bar, False, False, 0)

    def set(self, fraction, text):
        self.bar.set_fraction(max(0.0, min(1.0, float(fraction or 0.0))))
        self.value.set_text(text or "-")

    def clear(self):
        self.bar.set_fraction(0.0)
        self.value.set_text("-")


class NodeSummary(Gtk.ScrolledWindow):
    """Read-only overview of one node."""

    METERS = [
        ("cpu", "CPU usage"),
        ("wait", "IO delay"),
        ("load", "Load average"),
        ("memory", "RAM usage"),
        ("swap", "SWAP usage"),
        ("disk", "HD space (root)"),
    ]

    FIELDS = [
        ("status", "Status"),
        ("uptime", "Uptime"),
        ("guests", "Guests"),
        ("processors", "Processors"),
        ("kernel", "Kernel version"),
        ("manager", "PVE manager"),
    ]

    def __init__(self, on_open_shell=None):
        super().__init__()
        self.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.on_open_shell = on_open_shell or (lambda: None)

        self.node = None
        self.status = None
        self._api = None
        self._key = None
        self._generation = 0
        self._status_busy = False
        self._rrd_busy = False
        self._rrd_at = 0.0
        self._timeframe = TIMEFRAMES[0][0]

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_border_width(14)

        outer.pack_start(self._build_heading(), False, False, 0)
        outer.pack_start(self._build_meters(), False, False, 0)
        outer.pack_start(self._build_details(), False, False, 0)
        outer.pack_start(self._build_graphs(), True, True, 0)

        self.add(outer)

    # -- construction ----------------------------------------------------

    def _build_heading(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.status_icon = Gtk.Image()
        heading.pack_start(self.status_icon, False, False, 0)

        self.title = Gtk.Label(xalign=0.0)
        self.title.set_markup("<b>No node selected</b>")
        heading.pack_start(self.title, False, False, 0)
        box.pack_start(heading, False, False, 0)

        self.subtitle = Gtk.Label(xalign=0.0)
        self.subtitle.get_style_context().add_class("dim")
        self.subtitle.set_text("Select a node in the sidebar.")
        box.pack_start(self.subtitle, False, False, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_halign(Gtk.Align.START)
        self.shell_button = Gtk.Button(label="Open Shell")
        self.shell_button.set_always_show_image(True)
        self.shell_button.get_style_context().add_class("labelled-icon")
        self.shell_button.set_image(
            Gtk.Image.new_from_icon_name(
                "utilities-terminal-symbolic", Gtk.IconSize.BUTTON
            )
        )
        self.shell_button.set_tooltip_text(
            "Open a terminal on the node itself, as the web interface's Shell does"
        )
        self.shell_button.set_sensitive(False)
        self.shell_button.connect("clicked", lambda *_: self.on_open_shell())
        actions.pack_start(self.shell_button, False, False, 0)
        box.pack_start(actions, False, False, 0)
        return box

    def _build_meters(self):
        """Two columns of bars, so six of them do not fill the page.

        They collapse to one column when the tab is narrow, because a bar
        squeezed under 200px stops being readable as a proportion.
        """
        grid = Gtk.Grid(row_spacing=8, column_spacing=24)
        grid.set_column_homogeneous(True)
        self.meters = {}
        for index, (key, label) in enumerate(self.METERS):
            meter = Meter(label)
            self.meters[key] = meter
            grid.attach(meter, index % 2, index // 2, 1, 1)
        self._meter_grid = grid
        grid.connect("size-allocate", self._on_meters_allocated)
        self._meter_columns = 2
        return grid

    def _on_meters_allocated(self, _widget, allocation):
        wanted = 2 if allocation.width >= 560 else 1
        if wanted == self._meter_columns:
            return
        self._meter_columns = wanted
        # Re-laying out from inside size-allocate asks for another
        # allocation, so it is deferred to an idle -- the same reason the
        # guest summary's preview rescale is.
        GLib.idle_add(self._relayout_meters)

    def _relayout_meters(self):
        columns = self._meter_columns
        for index, (key, _label) in enumerate(self.METERS):
            meter = self.meters[key]
            self._meter_grid.remove(meter)
            self._meter_grid.attach(meter, index % columns, index // columns, 1, 1)
        self._meter_grid.show_all()
        return False

    def _build_details(self):
        grid = Gtk.Grid(row_spacing=3, column_spacing=20)
        self.values = {}
        for row, (key, label) in enumerate(self.FIELDS):
            name = Gtk.Label(label=label, xalign=0.0)
            name.get_style_context().add_class("summary-key")
            value = Gtk.Label(label="-", xalign=0.0)
            value.get_style_context().add_class("summary-value")
            value.set_selectable(True)
            value.set_line_wrap(True)
            grid.attach(name, 0, row, 1, 1)
            grid.attach(value, 1, row, 1, 1)
            self.values[key] = value
        self._details_grid = grid
        return grid

    def _build_graphs(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        label = Gtk.Label(label="History", xalign=0.0)
        label.get_style_context().add_class("summary-key")
        header.pack_start(label, False, False, 0)

        self.timeframe_combo = Gtk.ComboBoxText()
        for name, caption in TIMEFRAMES:
            self.timeframe_combo.append(name, caption)
        self.timeframe_combo.set_active_id(self._timeframe)
        self.timeframe_combo.connect("changed", self._on_timeframe_changed)
        header.pack_end(self.timeframe_combo, False, False, 0)
        box.pack_start(header, False, False, 0)

        # CPU and IO delay share a plot: they are the same axis, measured
        # against the same processors, and reading one without the other is
        # how a storage problem gets diagnosed as a busy CPU.
        self.cpu_graph = TimeSeriesGraph(
            "CPU usage", formatter=percent_label, maximum=1.0, fill=False
        )
        self.memory_graph = TimeSeriesGraph(
            "Memory usage", formatter=bytes_label, binary=True
        )
        self.network_graph = TimeSeriesGraph(
            "Network traffic", formatter=rate_label, fill=False, binary=True
        )
        for graph in (self.cpu_graph, self.memory_graph, self.network_graph):
            frame = Gtk.Frame()
            frame.get_style_context().add_class("summary-preview")
            frame.add(graph)
            box.pack_start(frame, False, False, 0)
        return box

    # -- population --------------------------------------------------------

    def clear(self):
        self.node = None
        self.status = None
        self._key = None
        self._generation += 1
        self.title.set_markup("<b>No node selected</b>")
        self.status_icon.clear()
        self.subtitle.set_text("Select a node in the sidebar.")
        self.shell_button.set_sensitive(False)
        for meter in self.meters.values():
            meter.clear()
        for value in self.values.values():
            value.set_text("-")

    def show_node(self, node, api=None, guests=None):
        """Draw what the inventory already knows, then fetch the rest.

        Re-entered on every poll, not just on selection, so the expensive
        half is guarded: a change of node invalidates anything in flight,
        and the per-node calls are throttled independently of this.
        """
        if node is None:
            self.clear()
            return
        changed = self._key != node.key
        self.node = node
        self._api = api
        if changed:
            self._key = node.key
            self._generation += 1
            self.status = None
            self._rrd_at = 0.0
            for meter in self.meters.values():
                meter.clear()
            for graph in (self.cpu_graph, self.memory_graph, self.network_graph):
                graph.set_series([])

        self.title.set_markup(f"<b>{GLib.markup_escape_text(node.name)}</b>")
        self._set_status_icon(node)
        self.subtitle.set_text(
            f"Node on {node.connection}" if node.connection else "Node"
        )
        self.shell_button.set_sensitive(node.online and api is not None)

        self._set("status", node.status)
        self._set("uptime", node.uptime_text)
        if guests is not None:
            running = sum(1 for guest in guests if guest.running)
            self._set("guests", f"{running} running of {len(guests)}")

        if not node.online:
            # A node that has stopped answering is not a node using 14% of
            # its processors: the last figures it sent are now history, and
            # leaving them on the bars says it is fine.
            self.status = None
            for meter in self.meters.values():
                meter.clear()
            return

        # The cluster's own figures, so the bars say something before the
        # per-node call comes back. They are replaced by the finer ones a
        # moment later; showing nothing until then made every tab open on a
        # page of empty bars.
        if self.status is None:
            self._apply_coarse(node)

        self.refresh()

    def _apply_coarse(self, node):
        if not node.online:
            for meter in self.meters.values():
                meter.clear()
            return
        self.meters["cpu"].set(node.cpu, node.cpu_text)
        if node.maxmem:
            self.meters["memory"].set(node.mem / node.maxmem, node.memory_text)
        if node.maxdisk:
            self.meters["disk"].set(node.disk / node.maxdisk, node.disk_text)

    def _set_status_icon(self, node):
        pixbuf = status_icons.node_icon(node, dark=current_dark(), size=HEADING_ICON)
        if pixbuf is None:
            self.status_icon.clear()
        else:
            self.status_icon.set_from_pixbuf(pixbuf)

    def _set(self, key, text):
        widget = self.values.get(key)
        if widget is not None:
            widget.set_text(str(text) if text not in (None, "") else "-")

    # -- the per-node calls ------------------------------------------------

    def refresh(self):
        """Ask for whatever is stale. Safe to call on every poll."""
        node, api = self.node, self._api
        if node is None or api is None or not node.online:
            return
        if not self._status_busy:
            self._load_status(node, api)
        if not self._rrd_busy and time.monotonic() - self._rrd_at >= RRD_INTERVAL:
            self._load_rrd(node, api, self._timeframe)

    def _on_timeframe_changed(self, combo):
        chosen = combo.get_active_id() or TIMEFRAMES[0][0]
        if chosen == self._timeframe:
            return
        self._timeframe = chosen
        # A different range is a different question, so it is asked at once
        # rather than at the next poll.
        self._rrd_at = 0.0
        if self.node is not None and self._api is not None and not self._rrd_busy:
            self._load_rrd(self.node, self._api, chosen)

    def _load_status(self, node, api):
        generation = self._generation
        self._status_busy = True

        def worker():
            try:
                raw = api.node_status(node.name)
            except Exception:
                raw = None
            GLib.idle_add(self._apply_status, generation, raw)

        threading.Thread(
            target=worker, daemon=True, name=f"node-status-{node.name}"
        ).start()

    def _apply_status(self, generation, raw):
        self._status_busy = False
        if generation != self._generation:
            return False
        if not raw:
            # Left as it was rather than blanked: a single refused or timed
            # out call is not evidence that the node has stopped reporting,
            # and the next poll will try again.
            return False

        status = self.status = NodeStatus.from_api(raw)
        self.meters["cpu"].set(status.cpu, status.cpu_text)
        self.meters["wait"].set(status.wait, f"{status.wait * 100:.1f}%")
        self.meters["load"].set(status.load_fraction, status.load_text)
        self.meters["memory"].set(status.memory_fraction, status.memory_text)
        self.meters["swap"].set(status.swap_fraction, status.swap_text)
        self.meters["disk"].set(status.disk_fraction, status.disk_text)

        if status.uptime:
            self._set("uptime", status.uptime_text)
        self._set("processors", status.processors_text)
        self._set("kernel", status.kernel)
        self._set("manager", status.pve_version)
        return False

    def _load_rrd(self, node, api, timeframe):
        generation = self._generation
        self._rrd_busy = True

        def worker():
            try:
                rows = api.node_rrd(node.name, timeframe=timeframe)
            except Exception:
                rows = None
            GLib.idle_add(self._apply_rrd, generation, timeframe, rows)

        threading.Thread(
            target=worker, daemon=True, name=f"node-rrd-{node.name}"
        ).start()

    def _apply_rrd(self, generation, timeframe, rows):
        self._rrd_busy = False
        if generation != self._generation or timeframe != self._timeframe:
            return False
        if rows is None:
            return False
        self._rrd_at = time.monotonic()

        self.cpu_graph.set_series(
            [
                Series("CPU", series(rows, "cpu"), SERIES_COLOURS[0]),
                Series("IO delay", series(rows, "iowait"), SERIES_COLOURS[2]),
            ]
        )
        # The installed memory is the ceiling, rather than whatever the peak
        # happened to be: the graph is then a proportion of the machine, and
        # the "Total" line sits along the top where it belongs.
        total = series(rows, "memtotal")
        installed = max((value for _at, value in total if value), default=0)
        self.memory_graph.maximum = installed or None
        self.memory_graph.set_series(
            [
                Series("Used", series(rows, "memused"), SERIES_COLOURS[0]),
                Series("Total", total, SERIES_COLOURS[1]),
            ]
        )
        self.network_graph.set_series(
            [
                Series("In", series(rows, "netin"), SERIES_COLOURS[1]),
                Series("Out", series(rows, "netout"), SERIES_COLOURS[2]),
            ]
        )
        return False


def series(rows, name):
    """One column out of an rrddata reply, as (time, value) points.

    A row with no entry for the column contributes None, which the graph
    draws as a gap. Proxmox omits the key rather than sending a zero when it
    has no sample, and turning that into a zero draws a drop to the floor
    that never happened.
    """
    points = []
    for row in rows or []:
        at = row.get("time")
        if at is None:
            continue
        value = row.get(name)
        try:
            value = None if value is None else float(value)
        except (TypeError, ValueError):
            value = None
        points.append((int(at), value))
    points.sort(key=lambda point: point[0])
    return points

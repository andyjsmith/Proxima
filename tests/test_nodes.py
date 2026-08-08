"""Nodes open like guests do: a tab, a summary, and a shell on request.

The summary is the interesting half. It is fed by two calls the rest of the
application never makes -- /nodes/<node>/status and /nodes/<node>/rrddata --
and everything on the page is derived from them, so the tests here mostly
check that the derivation survives contact with a reply.
"""

import pytest
from gi.repository import Gtk

from proxima.api.models import Node, NodeStatus
from proxima.console.serial import SerialConsole
from proxima.ui import graphs
from proxima.ui import sidebar as sidebar_mod
from proxima.ui import status_icons as icons_mod
from proxima.ui.guest_tab import CONSOLE, SUMMARY
from proxima.ui.node_summary import series
from proxima.ui.node_tab import NodeTab

from .conftest import CONN_ID, FakeAPI, pump, pump_until

NODE = f"{CONN_ID}/pve-node-01"
OTHER = f"{CONN_ID}/pve-node-02"
OFFLINE = f"{CONN_ID}/pve-node-03"


@pytest.fixture
def closed_nodes(window):
    """Leave no node tabs behind, whatever a test opened."""
    yield
    for key in list(window.node_tabs):
        window.close_node(key)
    pump(0.3)


def wait_for_nodes(window):
    assert pump_until(lambda: NODE in window.sidebar.nodes, 10), (
        "the poll never picked up any nodes"
    )


# -- the model ------------------------------------------------------------


def test_a_node_key_cannot_be_mistaken_for_a_guest_key():
    node = Node(name="pve", connection="server")
    assert node.key == "server/pve"
    # A guest key has two more segments, which is what lets one dictionary of
    # tabs hold both without a type tag.
    assert node.key.count("/") == 1


def test_node_status_reads_the_shape_proxmox_sends():
    status = NodeStatus.from_api(FakeAPI().node_status("pve-node-01"))
    assert status.cpus == 16
    assert status.loadavg == (1.24, 0.98, 0.75)
    assert status.load_fraction == pytest.approx(1.24 / 16)
    assert status.memory_fraction == pytest.approx(0.5)
    assert status.swap_fraction == pytest.approx(0.125)
    assert status.disk_fraction == pytest.approx(0.4)
    assert "AMD Ryzen" in status.processors_text
    assert "6.8.12" in status.kernel


def test_a_node_with_no_swap_says_so_rather_than_dividing_by_zero():
    status = NodeStatus.from_api({"swap": {"total": 0, "used": 0}})
    assert status.swap_fraction == 0.0
    assert status.swap_text == "none configured"


def test_load_never_overflows_its_bar():
    """A load of 40 on 16 processors is a full bar, not a bar and a half."""
    status = NodeStatus.from_api(
        {"loadavg": ["40", "38", "30"], "cpuinfo": {"cpus": 16}}
    )
    assert status.load_fraction == 1.0
    assert status.load_text.startswith("40.00")


# -- the history ----------------------------------------------------------


def test_a_missing_sample_is_a_gap_not_a_zero():
    rows = FakeAPI().node_rrd("pve-node-01")
    points = series(rows, "cpu")
    assert len(points) == len(rows)
    missing = [value for _at, value in points if value is None]
    assert len(missing) == 1, "the gap in the history was filled in"


def test_a_graph_splits_its_line_at_a_gap():
    rows = FakeAPI().node_rrd("pve-node-01")
    graph = graphs.TimeSeriesGraph("CPU", maximum=1.0)
    line = graphs.Series("CPU", series(rows, "cpu"))
    graph.set_series([line])
    span = graph._span()
    runs = graph._points(line, 0, 0, 100, 50, span, 1.0)
    assert len(runs) == 2, f"the gap did not break the line: {len(runs)} run(s)"


def test_an_empty_graph_says_so_instead_of_drawing_nothing():
    graph = graphs.TimeSeriesGraph("CPU")
    assert not graph.has_data()
    assert graph._span() is None


def test_a_graph_actually_paints(window, closed_nodes):
    """Including the hover readout, which nothing else here would draw.

    A draw handler that raises is swallowed by PyGObject and written to a
    stderr a packaged build does not have, so the graph would simply be
    blank with nothing anywhere to say why. This calls it directly.
    """
    import cairo

    wait_for_nodes(window)
    window.open_node(NODE)
    summary = window.node_tabs[NODE].summary
    assert pump_until(lambda: summary.cpu_graph.has_data(), 8)

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 600, 140)
    for graph in (summary.cpu_graph, summary.memory_graph, summary.network_graph):
        graph._on_draw(graph, cairo.Context(surface))
        graph._hover = 300.0
        try:
            graph._on_draw(graph, cairo.Context(surface))
        finally:
            graph._hover = None


def test_axis_maxima_land_on_readable_numbers():
    assert graphs.nice_maximum(0.42) == 0.5
    assert graphs.nice_maximum(1400) == 2000
    assert graphs.nice_maximum(0) == 1.0


# -- the tree -------------------------------------------------------------


def test_the_tree_lists_a_node_the_cluster_cannot_reach(window):
    wait_for_nodes(window)
    assert OFFLINE in window.sidebar.nodes, "an offline node vanished from the tree"
    row = window.sidebar._find_row(OFFLINE)
    assert row is not None, "the offline node has no row"
    label = window.sidebar.store.get_value(row, sidebar_mod.COL_LABEL)
    assert "offline" in label, f"the row does not say the node is down: {label!r}"


def test_a_collapsed_node_stays_collapsed_across_a_poll(window):
    """Selecting a row must not expand it, or it cannot be folded at all.

    The tree is rebuilt on every poll and the selection restored through
    select_key, so a select that expands is a row that springs open a second
    after every attempt to close it.
    """
    wait_for_nodes(window)
    sidebar = window.sidebar
    sidebar.select_key(NODE)
    pump(0.3)
    path = sidebar.store.get_path(sidebar._find_row(NODE))
    sidebar.view.collapse_row(path)
    assert not sidebar.view.row_expanded(path), "the row would not collapse at all"

    window.refresh()
    pump(1.5)
    path = sidebar.store.get_path(sidebar._find_row(NODE))
    assert not sidebar.view.row_expanded(path), (
        "a poll re-expanded the node that was just collapsed"
    )
    # ...and it is still selected, which is what the restore is there for.
    assert sidebar.selected_node_key() == NODE


def test_a_guest_under_a_collapsed_node_is_still_revealed_when_selected(window):
    """The ancestors do get expanded; only the row itself does not."""
    wait_for_nodes(window)
    sidebar = window.sidebar
    node_path = sidebar.store.get_path(sidebar._find_row(NODE))
    sidebar.view.collapse_row(node_path)
    pump(0.2)

    guest = next(k for k, g in sidebar.guests.items() if g.node == "pve-node-01")
    sidebar.select_key(guest)
    pump(0.3)
    assert sidebar.view.row_expanded(sidebar.store.get_path(sidebar._find_row(NODE))), (
        "selecting a guest did not open the node it is under"
    )


def test_an_online_node_is_not_coloured_like_a_running_guest(window):
    """A node being up is the ordinary state, not an event worth green."""
    wait_for_nodes(window)
    dark = window.sidebar._dark
    online = icons_mod.node_icon(window.sidebar.nodes[NODE], dark=dark)
    structural = icons_mod.IconCache().get(
        "computer-symbolic", icons_mod.palette_for(dark)["group"]
    )
    running = icons_mod.IconCache().get(
        "computer-symbolic", icons_mod.palette_for(dark)["running"]
    )
    assert online.get_pixels() == structural.get_pixels(), (
        "an online node is not wearing the structural colour"
    )
    assert online.get_pixels() != running.get_pixels()

    # Down is the state worth a colour.
    offline = icons_mod.node_icon(window.sidebar.nodes[OFFLINE], dark=dark)
    assert offline.get_pixels() != online.get_pixels(), (
        "an offline node looks exactly like a healthy one"
    )


def test_selecting_a_node_is_not_selecting_a_guest(window):
    wait_for_nodes(window)
    window.sidebar.select_key(NODE)
    pump(0.3)
    assert window.sidebar.selected_node_key() == NODE
    assert window.sidebar.selected_keys() == [], (
        "a node row is being reported as a selected guest"
    )


# -- the tab --------------------------------------------------------------


def test_activating_a_node_opens_a_tab_on_its_summary(window, closed_nodes):
    wait_for_nodes(window)
    pages = window.notebook.get_n_pages()
    window.sidebar.emit("node-activated", NODE)
    pump(0.5)
    assert NODE in window.node_tabs, "double-clicking a node opened nothing"
    tab = window.node_tabs[NODE]
    assert isinstance(tab, NodeTab)
    assert tab.view == SUMMARY
    assert window.notebook.get_n_pages() == pages + 1

    # Again brings the same tab forward rather than opening a second.
    window.sidebar.emit("node-activated", NODE)
    pump(0.4)
    assert window.notebook.get_n_pages() == pages + 1


def test_the_node_summary_has_no_picture_of_a_console(window, closed_nodes):
    """A guest's summary shows its screen; a node's has nothing to show."""
    wait_for_nodes(window)
    window.open_node(NODE)
    pump(0.4)
    summary = window.node_tabs[NODE].summary
    assert not hasattr(summary, "preview_image"), "the node page grew a screenshot"
    assert not hasattr(window.node_tabs[NODE], "capture_preview")


def test_the_meters_fill_in_from_the_node_status_call(window, closed_nodes):
    wait_for_nodes(window)
    window.open_node(NODE)
    summary = window.node_tabs[NODE].summary
    assert pump_until(lambda: summary.status is not None, 8), (
        "the node status call never came back"
    )
    for name in ("cpu", "wait", "load", "memory", "swap", "disk"):
        assert name in summary.meters, f"{name} has no meter"
        assert summary.meters[name].value.get_text() != "-", f"{name} reads nothing"
    assert summary.meters["memory"].bar.get_fraction() == pytest.approx(0.5)
    assert "1.24" in summary.meters["load"].value.get_text()
    assert "6.8.12" in summary.values["kernel"].get_text()
    assert "pve-manager" in summary.values["manager"].get_text()


def test_the_summary_counts_the_guests_on_the_node(window, closed_nodes):
    wait_for_nodes(window)
    window.open_node(NODE)
    pump(0.5)
    text = window.node_tabs[NODE].summary.values["guests"].get_text()
    on_node = [g for g in window.sidebar.guests.values() if g.node == "pve-node-01"]
    assert str(len(on_node)) in text, f"guest count reads {text!r}"


def test_the_graphs_take_the_history(window, closed_nodes):
    wait_for_nodes(window)
    window.open_node(NODE)
    summary = window.node_tabs[NODE].summary
    assert pump_until(lambda: summary.cpu_graph.has_data(), 8), (
        "the CPU graph never got any samples"
    )
    assert summary.memory_graph.has_data()
    assert summary.network_graph.has_data()
    # Two lines apiece: CPU with IO delay, memory used against total, and
    # traffic in against out.
    assert len(summary.cpu_graph.series) == 2
    assert len(summary.network_graph.series) == 2


def test_changing_the_range_asks_for_that_range(window, api, closed_nodes):
    wait_for_nodes(window)
    window.open_node(NODE)
    summary = window.node_tabs[NODE].summary
    assert pump_until(lambda: summary.cpu_graph.has_data(), 8)

    summary.timeframe_combo.set_active_id("week")
    assert pump_until(
        lambda: any(c[0] == "node-rrd" and c[2] == "week" for c in api.calls), 8
    ), "changing the range did not re-read the history"


def test_a_summary_describes_its_own_node(window, closed_nodes):
    wait_for_nodes(window)
    for key in (NODE, OTHER):
        window.open_node(key)
    pump(0.6)
    for key, name in ((NODE, "pve-node-01"), (OTHER, "pve-node-02")):
        title = window.node_tabs[key].summary.title.get_text()
        assert title == name, f"a node tab is describing {title!r}"


def test_closing_a_node_tab_takes_the_page_with_it(window, closed_nodes):
    wait_for_nodes(window)
    window.open_node(NODE)
    pump(0.4)
    pages = window.notebook.get_n_pages()
    window.close_node(NODE)
    pump(0.4)
    assert NODE not in window.node_tabs
    assert NODE not in window.node_consoles
    assert window.notebook.get_n_pages() == pages - 1


def test_an_offline_node_shows_no_figures_at_all(window, closed_nodes):
    """Stale meters on a dead node read as a machine that is fine."""
    wait_for_nodes(window)
    window.open_node(OFFLINE)
    pump(0.6)
    summary = window.node_tabs[OFFLINE].summary
    assert summary.values["status"].get_text() == "offline"
    for name, meter in summary.meters.items():
        assert meter.bar.get_fraction() == 0.0, f"{name} is still showing a reading"
        assert meter.value.get_text() == "-", f"{name} reads {meter.value.get_text()!r}"


def test_an_offline_node_will_not_open_a_shell(window, closed_nodes):
    wait_for_nodes(window)
    window.open_node(OFFLINE)
    pump(0.4)
    summary = window.node_tabs[OFFLINE].summary
    assert not summary.shell_button.get_sensitive(), (
        "an offline node is offering a shell"
    )
    window.open_node_shell(OFFLINE)
    pump(0.4)
    assert OFFLINE not in window.node_consoles, "a shell was dialled on a dead node"


# -- the shell ------------------------------------------------------------


def test_the_shell_asks_the_node_endpoint_not_a_guests(window, api, closed_nodes):
    """termproxy on /nodes/<node>, which is vmid=None all the way down."""
    wait_for_nodes(window)
    window.open_node_shell(NODE)
    assert pump_until(
        lambda: any(c[0] == "term" and c[1] is None for c in api.calls), 8
    ), f"no node-level termproxy request was made: {api.calls[-4:]}"


def test_the_shell_is_a_terminal_in_the_nodes_own_tab(window, closed_nodes):
    wait_for_nodes(window)
    window.open_node_shell(NODE)
    assert pump_until(
        lambda: isinstance(window.node_consoles.get(NODE), SerialConsole), 10
    ), "the shell never became a terminal"
    tab = window.node_tabs[NODE]
    assert tab.console is window.node_consoles[NODE]
    assert tab.view == CONSOLE, "the shell opened behind the summary"
    assert window.notebook.page_num(tab) >= 0, "the shell opened in a tab of its own"


def test_asking_twice_does_not_dial_a_second_session(window, closed_nodes):
    wait_for_nodes(window)
    window.open_node_shell(NODE)
    assert pump_until(
        lambda: isinstance(window.node_consoles.get(NODE), SerialConsole), 10
    )
    first = window.node_consoles[NODE]
    window.node_tabs[NODE].show_view(SUMMARY, by_user=True)
    window.open_node_shell(NODE)
    pump(0.6)
    assert window.node_consoles[NODE] is first, "a second shell replaced the first"
    assert window.node_tabs[NODE].view == CONSOLE


def test_the_tab_wears_the_nodes_icon_and_keeps_it(window, closed_nodes):
    """Opening a shell in a node's tab does not turn it into a terminal."""
    wait_for_nodes(window)
    window.open_node(NODE)
    pump(0.4)
    tab = window.node_tabs[NODE]
    label = window.notebook.get_tab_label(tab)
    before = label.icon.get_pixbuf()
    assert before is not None, "the node tab has no icon of its own"
    expected = icons_mod.node_icon(
        window.sidebar.nodes[NODE], dark=window.sidebar._dark
    )
    assert before.get_pixels() == expected.get_pixels(), (
        "the tab is not wearing the same icon as the tree"
    )

    window.open_node_shell(NODE)
    assert pump_until(
        lambda: isinstance(window.node_consoles.get(NODE), SerialConsole), 10
    )
    pump(0.4)
    after = label.icon.get_pixbuf()
    assert after is not None and after.get_pixels() == before.get_pixels(), (
        "the tab icon changed when the shell opened"
    )


def test_a_node_shell_is_not_offered_a_pop_out(window, closed_nodes):
    """A pop-out window is a guest's power controls around a console."""
    wait_for_nodes(window)
    window.open_node_shell(NODE)
    assert pump_until(
        lambda: isinstance(window.node_consoles.get(NODE), SerialConsole), 10
    )
    pump(0.4)
    assert not window.popout_item.get_sensitive(), (
        "Pop Out is live over a node shell, where it would do nothing"
    )


def test_a_node_tab_leaves_the_guest_actions_alone(window, closed_nodes):
    """Nothing on the VM menu applies to a node, and it must not aim at one."""
    wait_for_nodes(window)
    window.open_node(NODE)
    pump(0.5)
    assert window.context_guest() is None, (
        "the toolbar is still pointed at a guest while a node tab is in front"
    )


def test_the_session_remembers_open_nodes_without_their_shells(window, closed_nodes):
    wait_for_nodes(window)
    window.open_node(NODE)
    pump(0.4)
    window._save_session()
    assert NODE in (window.config.get("session_nodes") or []), (
        "an open node page was not recorded for the next session"
    )
    assert NODE not in (window.config.get("session_consoles") or [])


def test_the_page_still_lays_out_in_one_column_when_narrow(window, closed_nodes):
    """Six meters side by side stop being readable in a narrow tab."""
    wait_for_nodes(window)
    window.open_node(NODE)
    was = window.get_size()
    summary = window.node_tabs[NODE].summary
    try:
        window.resize(1500, 900)
        pump(0.8)
        assert summary._meter_columns == 2, "a wide page is stacking its meters"
        window.resize(700, 900)
        assert pump_until(lambda: summary._meter_columns == 1, 6), (
            "a narrow page kept two columns of meters"
        )
        # ...and every meter is still in the grid after the re-lay-out.
        assert len(summary._meter_grid.get_children()) == len(summary.METERS)
    finally:
        window.resize(*was)
        pump(0.4)


def test_the_node_page_is_scrollable(window, closed_nodes):
    """Meters, facts and three graphs do not fit a short window."""
    wait_for_nodes(window)
    window.open_node(NODE)
    pump(0.4)
    assert isinstance(window.node_tabs[NODE].summary, Gtk.ScrolledWindow)

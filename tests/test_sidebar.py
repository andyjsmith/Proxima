"""The guest tree: what it shows, how it sorts, and what it offers."""

import time

import pytest
from gi.repository import Gdk, Gtk

from proxima.api import notes as notes_mod
from proxima.ui import actions as action_defs
from proxima.ui import sidebar as sidebar_mod

from .conftest import (
    CONN_ID,
    SAMPLE,
    FakeAPI,
    key_for,
    pump,
    pump_until,
    sample_row,
)

RUNNING = key_for(100)
STOPPED = key_for(102)
CONTAINER = key_for(202, node="pve-node-02", kind="lxc")
TEMPLATE = key_for(900, node="pve-node-02")


def label_for(window, vmid):
    """The tree label of a guest row, wherever it sits."""
    store = window.sidebar.store
    found = []

    def walk(parent):
        row = store.iter_children(parent)
        while row is not None:
            if store.get_value(row, 0).endswith(f"/{vmid}"):
                found.append(store.get_value(row, 1))
            walk(row)
            row = store.iter_next(row)

    walk(None)
    return found[0] if found else ""


def guests_under(window, node_name):
    """The guest labels below a node, in tree order."""
    store = window.sidebar.store
    labels = []

    def walk(parent):
        row = store.iter_children(parent)
        while row is not None:
            if store.get_value(row, 4) == "node" and store.get_value(row, 5).endswith(
                f"/{node_name}"
            ):
                child = store.iter_children(row)
                while child is not None:
                    labels.append(store.get_value(child, 1))
                    child = store.iter_next(child)
            walk(row)
            row = store.iter_next(row)

    walk(None)
    return labels


def menu_labels(window, guest):
    menu = Gtk.Menu()
    window.sidebar._build_single_menu(menu, guest)
    return [
        c.get_label()
        for c in menu.get_children()
        if isinstance(c, Gtk.MenuItem) and c.get_label()
    ]


def test_sidebar_lists_every_guest(window):
    assert len(window.sidebar.guests) == len(SAMPLE)


def test_selecting_by_key_takes(window):
    window.sidebar.select_key(RUNNING)
    pump(0.4)
    selected = window.sidebar.selected_guest()
    assert selected is not None and selected.vmid == 100


def test_toolbar_follows_a_running_guest(window):
    window.sidebar.select_key(RUNNING)
    pump(0.4)
    assert not any(w.get_sensitive() for w in window._action_items["start"]), (
        "Power On is enabled for an already running guest"
    )
    assert all(w.get_sensitive() for w in window._action_items["stop"]), (
        "Power Off is disabled for a running guest"
    )


def test_toolbar_follows_a_stopped_guest(window):
    window.sidebar.select_key(STOPPED)
    pump(0.3)
    assert all(w.get_sensitive() for w in window._action_items["start"])
    assert not any(w.get_sensitive() for w in window._action_items["reset"])


def test_a_template_offers_no_power_actions(window):
    window.sidebar.select_key(TEMPLATE)
    pump(0.3)
    assert not any(
        any(w.get_sensitive() for w in widgets)
        for widgets in window._action_items.values()
    )
    assert not window.console_tool_item.get_sensitive()


def test_a_container_is_not_offered_reset(window):
    window.sidebar.select_key(CONTAINER)
    pump(0.3)
    assert not any(w.get_sensitive() for w in window._action_items["reset"])


def test_tree_is_one_unheaded_column(window):
    assert len(window.sidebar.view.get_columns()) == 1
    assert not window.sidebar.view.get_headers_visible()


def strongest_green(pixbuf):
    data = pixbuf.get_pixels()
    stride, channels = pixbuf.get_rowstride(), pixbuf.get_n_channels()
    greenest = (0, 0, 0)
    for y in range(pixbuf.get_height()):
        for x in range(pixbuf.get_width()):
            offset = y * stride + x * channels
            r, g, b = data[offset], data[offset + 1], data[offset + 2]
            alpha = data[offset + 3] if channels == 4 else 255
            if alpha > 200 and g > greenest[1]:
                greenest = (r, g, b)
    return greenest


def test_a_running_guest_gets_a_green_icon(window):
    store = window.sidebar.store
    icon = store.get_value(window.sidebar._find_row(RUNNING), sidebar_mod.COL_ICON)
    assert icon is not None, "running guest has no icon pixbuf"
    red, green, blue = strongest_green(icon)
    assert green > red + 30 and green > blue + 30, (
        f"running icon is not green, strongest pixel = {(red, green, blue)}"
    )


def test_stopped_and_running_icons_differ(window):
    store = window.sidebar.store
    running = store.get_value(window.sidebar._find_row(RUNNING), sidebar_mod.COL_ICON)
    stopped = store.get_value(window.sidebar._find_row(STOPPED), sidebar_mod.COL_ICON)
    assert running is not None and stopped is not None
    assert stopped.get_pixels() != running.get_pixels()


def test_icons_repaint_when_the_palette_flips(window):
    store = window.sidebar.store
    before = store.get_value(window.sidebar._find_row(RUNNING), sidebar_mod.COL_ICON)
    # The palette follows the system theme, so toggle away from whatever is
    # live rather than assuming light.
    was_dark = window.sidebar._palette is sidebar_mod.PALETTES[True]
    try:
        window.sidebar.set_dark(not was_dark)
        pump(0.2)
        after = store.get_value(window.sidebar._find_row(RUNNING), sidebar_mod.COL_ICON)
        assert after is not None
        assert after.get_pixels() != before.get_pixels(), (
            f"icons did not change when the palette flipped (was_dark={was_dark})"
        )
    finally:
        window.sidebar.set_dark(was_dark)
        pump(0.2)


def test_search_narrows_the_tree(window):
    window.sidebar.search_entry.set_text("pfsense")
    pump(0.8)
    try:
        visible = set(window.sidebar.visible_keys())
        assert len(visible) == 1 and any("201" in k for k in visible), (
            f"search left {len(visible)} guests: {sorted(visible)}"
        )
    finally:
        window.sidebar.search_entry.set_text("")
        pump(0.8)


def test_multi_term_search_matches_every_term(window):
    window.sidebar.search_entry.set_text("running qemu")
    pump(0.8)
    try:
        shown = window.sidebar.visible_guests()
        assert shown, "multi-term search matched nothing"
        assert all(g.running for g in shown), "multi-term search kept a stopped guest"
    finally:
        window.sidebar.search_entry.set_text("")
        pump(0.8)


def test_tree_shows_name_and_id_by_default(window):
    assert label_for(window, 100) == "web01 (100)"


def test_id_first_format_applies_live_and_sorts_by_id(window, config):
    config["tree_name_format"] = "id"
    window._apply_name_formats()
    pump(0.3)
    try:
        assert label_for(window, 100) == "100 (web01)"
        assert guests_under(window, "pve-node-01") == [
            "100 (web01)",
            "101 (db01)",
            "102 (build-runner)",
        ]
    finally:
        config["tree_name_format"] = "name"
        window._apply_name_formats()
        pump(0.3)


def test_name_first_format_sorts_by_name(window):
    assert guests_under(window, "pve-node-01") == [
        "build-runner (102)",
        "db01 (101)",
        "web01 (100)",
    ]


def test_a_poll_rebuild_does_not_destroy_an_open_rename(window):
    window.sidebar._editing_key = RUNNING
    window.sidebar.store.set_value(window.sidebar._find_row(RUNNING), 1, "being-edited")
    try:
        window.sidebar.rebuild()
        assert label_for(window, 100) == "being-edited"
    finally:
        window.sidebar._end_editing()
        pump(0.2)


# -- folders --------------------------------------------------------------


@pytest.fixture
def folder_view(window):
    window.sidebar.folder_view = True
    window.sidebar._update_view_button()
    yield window.sidebar
    window.sidebar.folder_view = False
    window.sidebar._update_view_button()
    window.sidebar.rebuild()
    pump(0.3)


def test_a_folder_is_written_into_the_guest_notes(window, folder_view):
    window.move_guest_to_folder(RUNNING, "Production/Customer A")
    pump_until(lambda: bool(FakeAPI.NOTES.get(100)), 5)
    stored = FakeAPI.NOTES.get(100, "")
    assert notes_mod.folder_of(stored) == ("Production", "Customer A"), (
        f"folder not written to notes: {stored!r}"
    )


def test_existing_notes_survive_a_folder_change(window, folder_view):
    FakeAPI.NOTES[101] = "Important: do not delete."
    window.move_guest_to_folder(key_for(101), "Staging")
    pump_until(lambda: "PROXIMA" in FakeAPI.NOTES.get(101, ""), 5)
    assert notes_mod.parse(FakeAPI.NOTES.get(101, ""))[1] == "Important: do not delete."
    assert notes_mod.folder_of(FakeAPI.NOTES[101]) == ("Staging",)


def test_folder_view_builds_the_tree(window, folder_view):
    window.move_guest_to_folder(RUNNING, "Production/Customer A")
    pump_until(lambda: bool(FakeAPI.NOTES.get(100)), 5)
    window.sidebar.rebuild()
    pump(0.3)

    labels = []

    def collect(parent):
        row = window.sidebar.store.iter_children(parent)
        while row is not None:
            if window.sidebar.store.get_value(row, sidebar_mod.COL_KIND) == "folder":
                labels.append(
                    window.sidebar.store.get_value(row, sidebar_mod.COL_TOOLTIP)
                )
            collect(row)
            row = window.sidebar.store.iter_next(row)

    collect(None)
    assert "Production" in labels and "Production/Customer A" in labels, (
        f"folder tree wrong: {labels}"
    )


def test_moving_to_the_root_clears_the_folder(window, folder_view):
    window.move_guest_to_folder(RUNNING, "Production/Customer A")
    pump_until(lambda: bool(notes_mod.folder_of(FakeAPI.NOTES.get(100, ""))), 5)
    window.move_guest_to_folder(RUNNING, "")
    pump_until(lambda: not notes_mod.folder_of(FakeAPI.NOTES.get(100, "")), 5)
    assert not notes_mod.folder_of(FakeAPI.NOTES.get(100, ""))


def test_folder_view_holds_guests_until_it_knows_where_they_go(window):
    FakeAPI.NOTES = {100: notes_mod.with_folder("", ["Production"])}
    for guest in window.sidebar.guests.values():
        guest.notes_loaded = False
        guest.folder = ()
    window.sidebar.folder_view = True
    window.sidebar._update_view_button()
    window.sidebar.rebuild()
    try:
        # Checked without pumping: the poll loop would kick off a folder scan
        # and, against a fake API, finish it before a pump returned.
        assert not window.sidebar.visible_keys(), (
            "folder view showed guests before their notes were read"
        )

        window._load_folders()
        pump_until(lambda: bool(window.sidebar.visible_keys()), 8)
        shown = window.sidebar.visible_keys()
        unloaded = [g.key for g in window.sidebar.guests.values() if not g.notes_loaded]
        assert shown, "folder view never showed any guests"
        assert not unloaded, f"guests shown while {len(unloaded)} still unread"
    finally:
        FakeAPI.NOTES = {}
        window.sidebar.folder_view = False
        window.sidebar._update_view_button()
        window.sidebar.rebuild()
        pump(0.3)


@pytest.mark.parametrize("style", ["name", "id"])
def test_folders_sort_case_insensitively_in_both_modes(window, style):
    paths = [("Zebra",), ("apps",), ("Apps", "beta"), ("Apps", "Alpha")]
    window.sidebar.name_format = style
    try:
        ordered = [
            "/".join(p) for p in sorted(paths, key=window.sidebar._folder_sort_key)
        ]
        assert ordered == ["apps", "Apps/Alpha", "Apps/beta", "Zebra"]
    finally:
        window.sidebar.name_format = "name"


# -- context menus --------------------------------------------------------


def test_a_template_menu_offers_only_what_a_template_can_do(window):
    template = window.sidebar.guests[TEMPLATE]
    entries = menu_labels(window, template)
    unwanted = [
        e
        for e in entries
        if any(
            word in e
            for word in (
                "Console",
                "Start",
                "Stop",
                "Shutdown",
                "Reset",
                "Snapshot",
                "Suspend",
                "Reboot",
            )
        )
    ]
    assert not unwanted, f"template menu offers {unwanted}"
    assert "Clone..." in entries and "Rename..." in entries


def test_a_guest_menu_has_no_snapshot_entries(window):
    entries = menu_labels(window, window.sidebar.guests[RUNNING])
    assert not any("Snapshot" in e for e in entries), (
        f"snapshots are still in the tree menu: {entries}"
    )
    assert "Rename..." in entries and "Open Console" in entries


def test_settings_sits_at_the_bottom_of_a_guest_menu(window):
    labels = menu_labels(window, window.sidebar.guests[RUNNING])
    assert labels and labels[-1] == "Settings"


def test_folder_view_offers_only_the_new_subfolder_entry(window, folder_view):
    entries = menu_labels(window, window.sidebar.guests[RUNNING])
    assert [e for e in entries if "older" in e] == ["Move to New Subfolder..."]


class _RightClick:
    type = Gdk.EventType.BUTTON_PRESS
    button = 3

    def __init__(self, x, y):
        self.x, self.y = x, y


@pytest.fixture
def right_click(window):
    """Right-click the first row of a given kind and return its menu labels."""
    sidebar = window.sidebar
    sidebar.rebuild()
    sidebar.view.expand_all()
    pump(0.3)

    popped = []
    real_popup = sidebar._popup
    sidebar._popup = lambda menu, event: popped.append(
        [
            c.get_label()
            for c in menu.get_children()
            if isinstance(c, Gtk.MenuItem) and c.get_label()
        ]
    )

    def click(kind):
        store = sidebar.store
        found = []

        def walk(parent):
            row = store.iter_children(parent)
            while row is not None and not found:
                if store.get_value(row, 4) == kind:
                    found.append(store.get_path(row))
                walk(row)
                row = store.iter_next(row)

        walk(None)
        if not found:
            return None
        area = sidebar.view.get_cell_area(found[0], sidebar.name_column)
        popped.clear()
        sidebar._on_button_press(
            sidebar.view, _RightClick(area.x + 4, area.y + area.height / 2)
        )
        return popped[0] if popped else []

    yield click
    sidebar._popup = real_popup
    sidebar.rebuild()
    pump(0.3)


def test_connect_is_on_the_server_row_not_on_nodes(window, right_click):
    # "Connect..." belongs to the server row and the empty space below the
    # tree; a node has nothing to do with adding a server.
    connection_menu = right_click("connection")
    node_menu = right_click("node")
    assert connection_menu is not None and node_menu is not None, (
        "could not find a connection and a node row to click"
    )
    assert any("Connect" in e for e in connection_menu), (
        f"the server row lost Connect...: {connection_menu}"
    )
    assert node_menu == [], f"a node row opened a menu: {node_menu}"


def test_a_folder_row_opens_no_menu(window, folder_view, right_click):
    window.move_guest_to_folder(RUNNING, "Production")
    pump_until(lambda: bool(FakeAPI.NOTES.get(100)), 5)
    window.sidebar.rebuild()
    window.sidebar.view.expand_all()
    pump(0.3)
    folder_menu = right_click("folder")
    if folder_menu is None:
        pytest.skip("no folder row to right-click")
    assert folder_menu == [], f"a folder row opened a menu: {folder_menu}"


# -- busy rows ------------------------------------------------------------
# A change that has been asked for spins until the cluster confirms it. The
# one unacceptable outcome is a row that spins for ever, so every way out is
# checked.


@pytest.fixture
def busy(window):
    FakeAPI.RENAME_DELAY = True
    yield window.sidebar.guests[STOPPED]
    FakeAPI.RENAME_DELAY = False
    window._clear_busy(STOPPED)
    sample_row(102)["name"] = "build-runner"
    sample_row(102)["status"] = "stopped"
    window.refresh()
    pump(0.6)


def test_a_rename_shows_its_new_name_at_once_and_stops_spinning(window, busy):
    window.rename_guest(STOPPED, "renamed-vm")
    pump(0.2)
    assert STOPPED in window.sidebar.busy, "a rename did not start a spinner"
    names = {k: v[1] for k, v in window.sidebar.busy.items()}
    assert names.get(STOPPED) == "renamed-vm", (
        f"the row does not show the new name: {names}"
    )
    label = window.sidebar.store.get_value(
        window.sidebar._find_row(STOPPED), sidebar_mod.COL_LABEL
    )
    assert "renamed-vm" in label, f"the tree still shows the old name: {label!r}"

    # The poll that finally reports the new name ends the wait. The fake
    # holds the rename back for two polls, so this is several seconds even
    # on an idle machine -- and the whole suite is not an idle machine.
    pump_until(lambda: STOPPED not in window.sidebar.busy, 25, step=0.3)
    assert STOPPED not in window.sidebar.busy, (
        "the rename spinner outlived the server confirming it"
    )
    assert window.sidebar._pulse_source is None, (
        "the pulse timer kept running with nothing spinning"
    )


def test_renaming_to_the_current_name_starts_no_spinner(window, busy):
    window._mark_busy(
        STOPPED,
        "name",
        "build-runner",
        "build-runner",
        "Renaming...",
        30,
        name="build-runner",
    )
    assert STOPPED not in window._busy


def test_a_change_that_never_arrives_gives_up_on_its_deadline(window, busy):
    # Undone in the web UI before we saw it, or a task that failed silently.
    window._mark_busy(
        STOPPED,
        "name",
        "build-runner",
        "never-lands",
        "Renaming...",
        0.4,
        name="never-lands",
    )
    assert STOPPED in window._busy, "a pending rename did not register"
    pump_until(lambda: STOPPED not in window._busy, 8, step=0.3)
    assert STOPPED not in window._busy, "a change that never arrived spun for ever"


def test_someone_else_changing_the_guest_ends_the_wait(window, busy):
    window._mark_busy(STOPPED, "status", "stopped", "paused", "Suspending...", 30)
    sample_row(102)["status"] = "running"  # not what we asked for
    pump_until(lambda: STOPPED not in window._busy, 8, step=0.3)
    assert STOPPED not in window._busy, (
        "an unexpected status change left the row spinning"
    )


def test_a_failed_action_clears_its_own_spinner(window, busy):
    window._mark_busy(STOPPED, "status", "stopped", "running", "Starting...", 30)
    # _action_failed also raises an error dialog, which would sit there modal
    # with nobody to dismiss it.
    real_error_dialog = window._error_dialog
    window._error_dialog = lambda *a, **k: None
    try:
        window._action_failed(action_defs.ACTIONS_BY_NAME["start"], busy, "nope")
    finally:
        window._error_dialog = real_error_dialog
    assert STOPPED not in window._busy


def test_a_reboot_acknowledges_briefly_rather_than_waiting(window, busy):
    # Rebooting has no status to wait for, so it must not wait for one.
    window._action_done(action_defs.ACTIONS_BY_NAME["reboot"], busy)
    change = window._busy.get(STOPPED)
    assert change is not None, "a reboot showed nothing at all"
    assert change.deadline - time.monotonic() <= window.REBOOT_ACK + 1, (
        "a reboot waits for a status change that never comes"
    )


# -- drag and drop --------------------------------------------------------


def test_the_drag_and_drop_switch_disarms_the_tree(window, config):
    assert window.sidebar.dnd_enabled, "drag and drop starts disabled"
    window._toggle_dnd()
    pump(0.2)
    try:
        assert not window.sidebar.dnd_enabled, (
            "the drag and drop switch did not reach the sidebar"
        )
        assert config.get("enable_dnd") is False, "the switch was not saved"
        assert window.dnd_icon.struck, "the icon is not struck through when off"
        # Unset rather than refused later: with no drag source the tree cannot
        # start a drag at all, which is the point of the switch.
        targets = window.sidebar.view.drag_dest_get_target_list()
        assert not (
            targets is not None
            and targets.find(Gdk.Atom.intern("proxima/guest", False))[0]
        ), "the tree still accepts guest drops with dnd off"
    finally:
        window._toggle_dnd()
        pump(0.2)
    assert window.sidebar.dnd_enabled and not window.dnd_icon.struck, (
        "drag and drop did not switch back on"
    )


# -- several servers at once ----------------------------------------------


def test_a_second_server_is_listed_separately(window, api):
    from proxima.api.connection import FAILED

    from .conftest import FakeAPI as _FakeAPI
    from .conftest import make_connection

    second = _FakeAPI()
    second_conn = make_connection(second)
    second_conn.host = "pve2.example.invalid"
    window.connections.add(second_conn)
    window._connection_ready(second_conn)
    pump(1.5)

    def roots(column):
        values = []
        row = window.sidebar.store.iter_children(None)
        while row is not None:
            values.append(window.sidebar.store.get_value(row, column))
            row = window.sidebar.store.iter_next(row)
        return values

    ids = roots(sidebar_mod.COL_ID)
    assert len(ids) == 2, f"tree shows {len(ids)} servers, expected 2: {ids}"

    # Keys are namespaced, so the same VMID on both servers stays distinct.
    keys = window.sidebar.visible_keys()
    second_100 = "pve2.example.invalid/pve-node-01/qemu/100"
    assert RUNNING in keys and second_100 in keys, (
        "identical VMIDs on two servers collided"
    )
    assert window.api_for(window.sidebar.guests[second_100]) is second, (
        "guest resolved to the wrong server's API"
    )

    # A failed server stays listed and does not take the others with it.
    second_conn.state = FAILED
    second_conn.error = "connection refused"
    window.sidebar.update(window.connections)
    pump(0.4)
    labels = roots(sidebar_mod.COL_LABEL)
    assert any("failed" in label for label in labels), (
        f"failed server not marked: {labels}"
    )
    assert window.sidebar.visible_keys(), "a failed server emptied the whole tree"

    window.disconnect_connection("pve2.example.invalid")
    pump(0.6)
    assert window.connections.get("pve2.example.invalid") is None, (
        "disconnect did not remove the server"
    )
    assert CONN_ID in str(window.sidebar.visible_keys())

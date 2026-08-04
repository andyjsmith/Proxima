"""Snapshot buttons, and the manager that draws the history."""

import pytest
from gi.repository import Gtk

from proxima.ui.snapshots import SnapshotManager

from .conftest import FakeAPI, key_for, pump

RUNNING = key_for(100)
STOPPED = key_for(102)
NO_SNAPSHOTS = key_for(101)
TEMPLATE = key_for(900, node="pve-node-02")


def test_snapshot_buttons_are_disabled_for_a_template(window):
    window.sidebar.select_key(TEMPLATE)
    pump(0.3)
    assert not any(i.get_sensitive() for i in window.snapshot_items.values())


def test_snapshot_buttons_are_enabled_for_a_guest(window):
    window.sidebar.select_key(STOPPED)
    pump(0.3)
    assert all(i.get_sensitive() for i in window.snapshot_items.values())


def test_revert_targets_the_newest_snapshot(window, api):
    window.sidebar.select_key(STOPPED)
    pump(0.3)
    real_confirm = window._confirm
    window._confirm = lambda *a, **k: True  # auto-approve
    try:
        window._snapshot_action("revert")
        pump(0.8)
    finally:
        window._confirm = real_confirm
    rollbacks = [c for c in api.calls if c[0] == "snap-rollback"]
    assert rollbacks, "revert did not issue a rollback"
    assert rollbacks[-1][2] == "before-upgrade", (
        f"revert rolled back to {rollbacks[-1][2]!r}, expected the newest snapshot"
    )


def test_the_revert_tooltip_names_the_snapshot(window):
    window.sidebar.select_key(RUNNING)
    pump(0.8)
    assert window.snapshot_items["revert"].get_sensitive(), (
        "revert disabled despite an existing snapshot"
    )
    # GtkToolItem stores its tooltip apart from the widget, so the tool
    # button's getter always returns None; read the menu item, which carries
    # the same text.
    tooltip = window.snapshot_menu_items["revert"].get_tooltip_text() or ""
    assert "before-upgrade" in tooltip and "ago" in tooltip, (
        f"revert tooltip reads {tooltip!r}"
    )


def test_revert_is_disabled_with_no_snapshots(window):
    saved = FakeAPI.SNAPSHOTS
    FakeAPI.SNAPSHOTS = []
    try:
        window.sidebar.select_key(NO_SNAPSHOTS)
        pump(0.8)
        assert not window.snapshot_items["revert"].get_sensitive(), (
            "revert enabled for a guest with no snapshots"
        )
        assert window.snapshot_items["take"].get_sensitive(), (
            "take disabled for a guest with no snapshots"
        )
    finally:
        FakeAPI.SNAPSHOTS = saved
        window.sidebar.select_key(STOPPED)
        pump(0.8)


@pytest.fixture
def manager(window, api):
    window.sidebar.select_key(STOPPED)
    pump(0.3)
    dialog = SnapshotManager(window, api, window.sidebar.selected_guest())
    pump(0.6)
    yield dialog
    dialog.destroy()
    pump(0.2)


def snapshot_rows(store, parent=None, depth=0):
    rows = []
    row = store.iter_children(parent)
    while row is not None:
        rows.append((depth, store.get_value(row, 0)))
        rows.extend(snapshot_rows(store, row, depth + 1))
        row = store.iter_next(row)
    return rows


def test_the_manager_draws_the_branching_history(manager):
    # A flat list cannot show this shape: clean-install has two children, and
    # the live state hangs off one of them.
    assert snapshot_rows(manager.store) == [
        (0, "clean-install"),
        (1, "experiment"),
        (1, "before-upgrade"),
        (2, "NOW"),
    ]


def test_the_now_row_is_not_a_snapshot(manager):
    # NOW is not a snapshot and must never be a rollback or delete target.
    manager.view.expand_all()
    manager.view.get_selection().select_path(Gtk.TreePath.new_from_string("0:1:0"))
    pump(0.2)
    assert manager.selected() is None, "the NOW row is selectable as a snapshot"
    assert not manager.rollback_button.get_sensitive(), (
        "roll back is enabled on the NOW row"
    )

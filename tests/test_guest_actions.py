"""Powering guests on and off, renaming, cloning and deleting them."""

import pytest
from gi.repository import Gtk

from proxima.ui import actions as action_defs
from proxima.ui import status_icons as icons_mod
from proxima.ui.clone import CloneDialog

from .conftest import key_for, pump, pump_until, sample_row

RUNNING = key_for(100)
STOPPED = key_for(102)
PAUSED = key_for(202, node="pve-node-02", kind="lxc")
TEMPLATE = key_for(900, node="pve-node-02")


def start_button_for(window, key):
    window.notebook.set_current_page(0)
    window.sidebar.select_key(key)
    pump(0.6)
    widget = window._action_items["start"][0]
    return widget.get_label(), widget.get_sensitive()


def test_resume_has_no_control_of_its_own(window):
    assert "resume" not in action_defs.TOOLBAR_ACTIONS, (
        "Resume still has its own toolbar button"
    )
    assert not window._action_items.get("resume"), "Resume still has its own menu entry"


@pytest.mark.parametrize(
    ("key", "label", "enabled"),
    [
        (STOPPED, "Start", True),
        (PAUSED, "Resume", True),
        (RUNNING, "Start", False),
    ],
)
def test_the_start_button_relabels_by_guest_state(window, key, label, enabled):
    assert start_button_for(window, key) == (label, enabled)


def test_the_combined_button_resumes_a_paused_guest(window, api):
    # Clicking it must call the API that applies, not always "start".
    window._run_action(PAUSED, "start", confirm=False)
    pump(0.8)
    powered = [c for c in api.calls if c[0] == "power" and c[1] == 202]
    assert powered, "the combined button issued no power action"
    assert powered[-1][2] == "resume", (
        f"paused guest got '{powered[-1][2]}', expected 'resume'"
    )


def test_a_requested_action_shows_before_proxmox_reports_it(window):
    window.open_console(RUNNING)
    pump(1.2)
    live = window.consoles.get(RUNNING)
    assert live is not None, "no console opened for the running guest"
    try:
        window._run_action(RUNNING, "stop", confirm=False)
        pump(1.0)
        assert RUNNING in window._pending_actions, (
            "no pending state recorded for the action"
        )
        assert live.status_panel.get_visible(), (
            "nothing shown while the action was in flight"
        )
        assert "Stopping" in live.status_panel.title.get_text(), (
            f"panel reads {live.status_panel.title.get_text()!r}, expected Stopping"
        )
        assert live.pending, "console not marked pending, so it is not greyed"

        # Once the guest really moves, the pending state must give way.
        sample_row(100)["status"] = "stopped"
        try:
            window.refresh()
            pump_until(lambda: RUNNING not in window._pending_actions, 6, step=0.3)
            assert RUNNING not in window._pending_actions, (
                "pending state outlived the status change"
            )
            assert "stopped" in live.status_panel.title.get_text(), (
                f"after stopping, panel reads {live.status_panel.title.get_text()!r}"
            )
        finally:
            sample_row(100)["status"] = "running"
            window.refresh()
            pump(0.5)
    finally:
        window.close_console(RUNNING)
        pump(0.5)


def test_a_reset_does_not_cover_the_console_it_has_already_finished(window):
    """Reset is instant, so the panel arrives too late to say anything.

    The guest is back at its firmware splash before the panel could draw,
    and the panel then greys out the one thing worth looking at.
    """
    window.open_console(RUNNING)
    pump(1.2)
    live = window.consoles.get(RUNNING)
    assert live is not None, "no console opened for the running guest"
    try:
        window._run_action(RUNNING, "reset", confirm=False)
        pump(1.0)
        assert RUNNING not in window._pending_actions, (
            "reset recorded a pending state, so it still covers the console"
        )
        assert not live.pending, "reset greyed out the console it just restarted"
        assert "Resetting" not in live.status_panel.title.get_text(), (
            "reset still puts a panel over the console"
        )
        # Only the console panel goes: the inventory really has not caught
        # up yet, so the row is still right to say so.
        assert RUNNING in window._busy, "the row stopped acknowledging a reset"
    finally:
        window.close_console(RUNNING)
        pump(0.5)


def test_a_pending_panel_does_not_outlive_the_rows_spinner(window):
    """The panel and the spinner are one wait, so they end together.

    A reboot ends at "running", where it began, so no status change ever
    contradicts the panel. It used to sit there for the full PENDING_TIMEOUT
    after the row had already stopped spinning.
    """
    window.open_console(RUNNING)
    pump(1.2)
    live = window.consoles.get(RUNNING)
    assert live is not None, "no console opened for the running guest"
    try:
        window._run_action(RUNNING, "reboot", confirm=False)
        pump(1.0)
        assert RUNNING in window._pending_actions, "the reboot said nothing at all"
        assert "Rebooting" in live.status_panel.title.get_text(), (
            f"panel reads {live.status_panel.title.get_text()!r}, expected Rebooting"
        )

        # End the row's wait, as its shorter deadline does. The guest is
        # still "running", so nothing else could release the panel.
        window._clear_busy(RUNNING)
        window.refresh()
        pump_until(lambda: RUNNING not in window._pending_actions, 6, step=0.3)
        assert RUNNING not in window._pending_actions, (
            "the panel outlived the row's spinner"
        )
        assert not live.pending, "the console stayed greyed out after the wait ended"
    finally:
        window.close_console(RUNNING)
        pump(0.5)


def test_the_row_and_the_console_wait_on_one_deadline(window):
    """Two deadlines for one action is what let them disagree on screen."""
    assert window.REBOOT_ACK < window.PENDING_TIMEOUT, (
        "the short acknowledgement is no shorter than the full timeout"
    )
    for name in ("reset", "reboot"):
        assert name not in action_defs.EXPECTED_STATUS, (
            f"{name} claims a target status; it ends where it began"
        )


def test_renaming_reaches_the_api_and_updates_the_tree(window, api):
    before = len(api.calls)
    window.rename_guest(RUNNING, "web01-renamed")
    pump(0.8)
    try:
        renames = [
            c for c in api.calls[before:] if isinstance(c, tuple) and c[0] == "rename"
        ]
        assert renames, "rename never reached the API"
        assert renames[0][1:] == (100, "web01-renamed", "qemu"), (
            f"rename sent the wrong parameters: {renames[0]}"
        )
        assert window.sidebar.guests[RUNNING].name == "web01-renamed", (
            "the tree did not take the new name"
        )
    finally:
        window.sidebar.guests[RUNNING].name = "web01"
        sample_row(100)["name"] = "web01"
        window.refresh()
        pump(0.5)


@pytest.fixture
def clone_dialog(window, api):
    dialog = CloneDialog(window, api, window.sidebar.guests[TEMPLATE])
    pump(0.6)
    yield dialog
    dialog.destroy()
    pump(0.2)


def test_the_clone_dialog_defaults_to_a_linked_clone(clone_dialog):
    name, vmid, target, full, storage = clone_dialog.values()
    assert vmid == 903, f"clone dialog did not take the next VMID: {vmid}"
    assert not full and storage is None, "clone does not default to a linked clone"
    assert target == "pve-node-02", f"clone dialog targets the wrong node: {target}"
    assert name


def test_the_clone_dialog_collects_a_full_clone(clone_dialog):
    clone_dialog.mode_combo.set_active_id("full")
    pump(0.2)
    assert clone_dialog.storage_combo.get_sensitive(), (
        "storage stays disabled for a full clone"
    )
    clone_dialog.storage_combo.set_active_id("ceph-pool")

    clone_dialog.name_entry.set_text("bad name")
    pump(0.1)
    assert not clone_dialog.ok_button.get_sensitive(), (
        "the clone dialog accepts an invalid name"
    )
    clone_dialog.name_entry.set_text("debian12-clone")
    pump(0.1)
    assert clone_dialog.ok_button.get_sensitive(), (
        "the clone dialog rejects a valid name"
    )
    assert clone_dialog.values() == (
        "debian12-clone",
        903,
        "pve-node-02",
        True,
        "ceph-pool",
    )


def delete_entry(window, guest):
    menu = Gtk.Menu()
    window.sidebar._build_single_menu(menu, guest)
    for child in menu.get_children():
        if isinstance(child, Gtk.MenuItem) and child.get_label() == "Delete...":
            return child
    return None


def test_delete_is_offered_for_a_stopped_guest(window):
    entry = delete_entry(window, window.sidebar.guests[STOPPED])
    assert entry is not None, "no Delete entry for a stopped guest"
    assert entry.get_sensitive(), (
        f"Delete disabled for a stopped guest: {entry.get_tooltip_text()}"
    )


def test_delete_is_refused_for_a_running_guest(window):
    entry = delete_entry(window, window.sidebar.guests[RUNNING])
    assert entry is None or not entry.get_sensitive(), (
        "Delete offered for a running guest"
    )


def test_a_protected_guest_cannot_be_deleted(window, api):
    template = window.sidebar.guests[TEMPLATE]
    # Protection is read from the config, so make sure it has been.
    window.sidebar.select_key(TEMPLATE)
    pump(0.8)
    assert template.protected is True, (
        "the protection flag was not read from the config"
    )

    entry = delete_entry(window, template)
    assert entry is not None, "no Delete entry for a template"
    assert not entry.get_sensitive(), "Delete offered for a protected template"
    assert "rotect" in (entry.get_tooltip_text() or ""), (
        f"protection not explained: {entry.get_tooltip_text()!r}"
    )

    # ...and it must be refused even if the menu is bypassed.
    before = len(api.calls)
    window.delete_guest(TEMPLATE)
    pump(0.6)
    assert not any(
        c[0] == "delete" for c in api.calls[before:] if isinstance(c, tuple)
    ), "a protected guest was deleted anyway"


# -- a guest stopped by a storage failure ---------------------------------
# Proxmox reports "io-error" and shows a yellow caution mark. The guest is
# not executing, but QEMU is up: the web UI still offers every control that
# acts on a live guest, and so must this.


@pytest.fixture
def io_error(window):
    sample_row(100)["status"] = "io-error"
    window.refresh()
    pump_until(lambda: window.sidebar.guests[RUNNING].status == "io-error", 8)
    yield window.sidebar.guests[RUNNING]
    sample_row(100)["status"] = "running"
    window.refresh()
    pump_until(lambda: window.sidebar.guests[RUNNING].status == "running", 8)


def test_an_io_error_guest_can_still_be_powered_off(window, io_error):
    """The one guest most likely to need Stop had every button greyed out."""
    offered = {
        action.name
        for action in action_defs.visible_actions(io_error)
        if io_error.status in action.states and io_error.kind in action.kinds
    }
    assert {"shutdown", "stop", "reset", "reboot", "suspend"} <= offered, (
        f"an io-error guest cannot be powered off: {sorted(offered)}"
    )


def test_an_io_error_guest_is_not_offered_a_start(window, io_error):
    """There is nothing to start, and resuming just faults again."""
    offered = {
        action.name
        for action in action_defs.visible_actions(io_error)
        if io_error.status in action.states
    }
    assert "start" not in offered and "resume" not in offered, (
        f"start/resume offered for an io-error guest: {sorted(offered)}"
    )


def test_an_io_error_guest_wears_a_warning_not_a_question_mark(window, io_error):
    icon = icons_mod.STATUS_ICONS[io_error.status]
    assert icon == "dialog-warning-symbolic", f"io-error drew {icon}"
    for dark in (False, True):
        colour = icons_mod.PALETTES[dark]["io-error"]
        assert colour != icons_mod.PALETTES[dark]["unknown"], (
            "io-error is drawn in the same grey as unknown"
        )


def test_an_io_error_guest_still_has_a_console(window, io_error):
    """QEMU is up and still serving the frame it froze on."""
    assert io_error.has_console, "an io-error guest was treated as powered off"


# -- a guest held before its first instruction -----------------------------
# "prelaunch" is QEMU up with its vCPUs never released: started with
# --paused, or held at the start of a migration or restore. The web UI
# offers every live-guest control plus Resume; Proxima greyed out all of it.


@pytest.fixture
def prelaunch(window):
    sample_row(100)["status"] = "prelaunch"
    window.refresh()
    pump_until(lambda: window.sidebar.guests[RUNNING].status == "prelaunch", 8)
    yield window.sidebar.guests[RUNNING]
    sample_row(100)["status"] = "running"
    window.refresh()
    pump_until(lambda: window.sidebar.guests[RUNNING].status == "running", 8)


def test_a_prelaunch_guest_offers_every_control_the_web_ui_does(window, prelaunch):
    offered = {
        action.name
        for action in action_defs.visible_actions(prelaunch)
        if action_defs.enabled_for(action, prelaunch)
    }
    # Hibernate is Proxmox-only for now, so it is not in the comparison.
    assert {"shutdown", "stop", "reset", "reboot", "suspend", "resume"} <= offered, (
        f"a prelaunch guest is still greyed out: {sorted(offered)}"
    )


def test_a_prelaunch_guest_is_resumed_not_started(window, prelaunch):
    """It is already up, so a start would be refused. Resume releases it."""
    assert action_defs.start_action_for(prelaunch).name == "resume", (
        "the combined button offered Start for a guest that is already up"
    )
    assert start_button_for(window, RUNNING) == ("Resume", True), (
        "the combined button is wrong for a prelaunch guest"
    )


def test_resuming_a_prelaunch_guest_calls_resume(window, api, prelaunch):
    window._run_action(RUNNING, "start", confirm=False)
    pump(0.8)
    powered = [c for c in api.calls if c[0] == "power" and c[1] == 100]
    assert powered, "no power action reached the API"
    assert powered[-1][2] == "resume", (
        f"prelaunch guest got '{powered[-1][2]}', expected 'resume'"
    )


def test_a_prelaunch_guest_has_a_console(window, prelaunch):
    """Every device including the display is up; only the vCPUs are held."""
    assert prelaunch.has_console, "a prelaunch guest was treated as powered off"


def test_a_prelaunch_guest_is_not_drawn_as_unknown(window, prelaunch):
    icon = icons_mod.STATUS_ICONS.get(prelaunch.status)
    assert icon and icon != icons_mod.STATUS_ICONS["unknown"], (
        f"prelaunch drew {icon!r}, the not-polled-yet question mark"
    )
    for dark in (False, True):
        palette = icons_mod.PALETTES[dark]
        assert "prelaunch" in palette, "prelaunch has no colour, so it draws grey"

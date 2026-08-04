"""Which console a guest gets, and how its tab behaves."""

import time

import pytest
from gi.repository import Gtk

from .conftest import (
    FakeAPI,
    FakeConsole,
    key_for,
    plan_protocol,
    pump,
    pump_until,
    sample_row,
)

RUNNING = key_for(100)
STOPPED = key_for(102)


@pytest.mark.parametrize(
    ("key", "expected", "vga"),
    [
        (key_for(100), "spice", "qxl"),
        (key_for(101), "vnc", "std"),
        (key_for(200, node="pve-node-02"), "spice", "virtio"),
        (key_for(201, node="pve-node-02"), "spice", "virtio-gl"),
    ],
)
def test_planning_picks_the_protocol_the_display_implies(window, key, expected, vga):
    assert plan_protocol(window, key) == expected, f"vga={vga}"


def test_an_unknown_display_type_attempts_spice(window, api):
    # Containers never get SPICE, so borrow the container row for the VM path.
    key = key_for(202, node="pve-node-02", kind="lxc")
    guest = window.sidebar.guests[key]
    guest.kind = "qemu"
    try:
        before = len([c for c in api.calls if c[0] == "spice"])
        plan_protocol(window, key)
        after = len([c for c in api.calls if c[0] == "spice"])
        assert after > before, "unknown display type did not attempt SPICE"
    finally:
        guest.kind = "lxc"


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        (key_for(200, node="pve-node-02"), "SPICE"),
        (key_for(101), "VNC"),
        (key_for(201, node="pve-node-02"), "SPICE"),
    ],
)
def test_the_summary_reports_the_console_it_settled_on(window, key, expected):
    # 'checking...' must not survive a completed lookup: an unrecognised
    # adapter reports None for spice_capable, which used to be
    # indistinguishable from "not looked up".
    window.sidebar.select_key(key)
    pump(0.6)
    text = window.summary.values["console"].get_text()
    display = window.summary.values["display"].get_text()
    assert "checking" not in text, f"{key}: summary stuck on 'checking...'"
    assert text.startswith(expected), (
        f"{key}: display {display!r} reported console {text!r}"
    )


def test_the_summary_holds_its_detail_across_polls(window):
    # Rebuilding the tree empties the selection for an instant. If that
    # reaches the summary it discards its detail and re-fetches, which reads
    # as agent/IP/OS blinking to "-" every refresh.
    window.notebook.set_current_page(0)
    window.sidebar.select_key(RUNNING)
    watched = ("agent", "address", "os")
    pump_until(
        lambda: window.summary.values["agent"].get_text() not in ("-", "checking..."),
        6,
    )
    populated = {f: window.summary.values[f].get_text() for f in watched}
    assert all(v not in ("-", "") for v in populated.values()), (
        f"summary never populated: {populated}"
    )

    spurious = []
    window.sidebar.connect("guest-selected", lambda _s, key: spurious.append(key))
    blanks = {f: 0 for f in watched}
    end = time.time() + 5
    while time.time() < end:
        pump(0.1)
        for field in watched:
            if window.summary.values[field].get_text() == "-":
                blanks[field] += 1
    assert not any(blanks.values()), f"summary blanked during polling: {blanks}"
    assert not spurious, f"tree rebuild emitted selection changes: {spurious}"


def test_a_stopped_guest_still_gets_a_placeholder_tab(window):
    tabs_before = window.notebook.get_n_pages()
    window.open_console(STOPPED)
    pump(0.6)
    try:
        console = window.consoles.get(STOPPED)
        assert console is not None, "no tab opened for a stopped guest"
        assert window.notebook.get_n_pages() == tabs_before + 1
        assert console.protocol == "offline", (
            f"stopped guest got a {console.protocol} console"
        )
        assert console.status_panel.get_visible(), (
            "stopped guest tab shows no explanation"
        )
    finally:
        window.close_console(STOPPED)
        pump(0.3)


def test_a_reconnect_replaces_the_console_and_keeps_the_tab(window):
    # Starting a stopped guest must not make its console vanish and come
    # back. The swap itself is what regressed, so it is exercised directly:
    # building a real console here would need a live SPICE or VNC server.
    window.notebook.set_current_page(0)
    window.open_console(STOPPED)
    pump(0.8)
    placeholder = window.consoles.get(STOPPED)
    assert placeholder is not None and placeholder.protocol == "offline", (
        "stopped guest did not get a placeholder console"
    )

    # A tab after it, so a rebuild that appended instead of replacing would
    # show up as a move to the end.
    trailing = FakeConsole("trailing")
    trailing.guest_key = "trailing"
    window.consoles["trailing"] = trailing
    window.panes.append(trailing, Gtk.Label(label="trailing"))
    pump(0.3)
    try:
        pages_before = window.notebook.get_n_pages()
        position_before = window.notebook.page_num(placeholder)

        rebuilt = FakeConsole(window.sidebar.guests[STOPPED].name)
        window._install_console(window.sidebar.guests[STOPPED], rebuilt)
        pump(0.4)

        assert window.consoles.get(STOPPED) is rebuilt, (
            "install did not take over the guest's console"
        )
        assert window.notebook.get_n_pages() == pages_before, (
            f"tab count changed on reconnect: {pages_before} -> "
            f"{window.notebook.get_n_pages()}"
        )
        assert window.notebook.page_num(rebuilt) == position_before, (
            "the console moved tab on reconnect"
        )
        assert window.notebook.page_num(placeholder) < 0, (
            "the replaced console is still in the notebook"
        )
    finally:
        window.close_console_widget(trailing)
        window.close_console(STOPPED)
        pump(0.3)


def test_a_console_pops_out_into_its_own_window_and_back(window):
    window.sidebar.select_key(STOPPED)
    pump(0.3)
    console = FakeConsole("popme")
    console.guest_key = STOPPED
    window.consoles[STOPPED] = console
    window.panes.append(console, Gtk.Label(label="p"))
    pump(0.4)
    try:
        tabs_before = window.notebook.get_n_pages()
        window.popout_console()
        pump(0.6)
        popout = window._popouts.get(STOPPED)
        assert popout is not None, "pop out did not create a window"
        assert window.notebook.get_n_pages() == tabs_before - 1, (
            "pop out did not remove the tab"
        )
        assert console.get_parent() is not None, "popped-out console lost its parent"
        assert popout._action_items["start"].get_sensitive(), (
            "pop-out toolbar did not follow guest state"
        )

        popout.return_to_tabs()
        pump(0.6)
        assert window.notebook.get_n_pages() == tabs_before, (
            "returning from pop out did not restore the tab"
        )
        assert not window._popouts, "pop-out window was not forgotten"
    finally:
        window.close_console(STOPPED)
        pump(0.3)


def test_the_toolbar_context_follows_the_front_tab(window):
    window.notebook.set_current_page(0)
    window.sidebar.select_key(RUNNING)
    pump(0.5)
    assert window.context_guest() is not None and window.context_guest().vmid == 100, (
        "summary page context is not the tree selection"
    )

    console = FakeConsole("ctx")
    console.guest_key = STOPPED
    window.consoles[STOPPED] = console
    window.panes.append(console, Gtk.Label(label="c"))
    pump(0.5)
    try:
        assert window.context_guest().vmid == 102, (
            "console tab did not take over the context"
        )
        # Clicking around the tree must not re-aim the toolbar.
        window.sidebar.select_key(RUNNING)
        pump(0.5)
        assert window.context_guest().vmid == 102, (
            f"tree selection overrode the console tab's context "
            f"(got {window.context_guest().vmid})"
        )
        assert not any(w.get_sensitive() for w in window._action_items["stop"]), (
            "Stop enabled for the stopped guest in the front tab"
        )
    finally:
        window.close_console(STOPPED)
        window.notebook.set_current_page(0)
        pump(0.4)


def test_console_preferences_are_saved_per_guest(window, config):
    console = FakeConsole("pref")
    console.guest_key = RUNNING
    window.consoles["pref"] = console
    window.panes.append(console, Gtk.Label(label="pref"))
    pump(0.4)
    try:
        window.scaling_item.set_active(True)
        pump(0.3)
        stored = (config.get("guest_prefs") or {}).get(RUNNING, {})
        assert stored.get("scale_to_fit"), (
            f"scale-to-fit not saved per guest (got {stored})"
        )
        assert window.guest_prefs(RUNNING)["scaling"] is True, (
            "stored guest preference does not read back"
        )
    finally:
        window.close_console("pref")
        window.notebook.set_current_page(0)
        config["guest_prefs"] = {}
        pump(0.3)


@pytest.fixture
def live_console(window):
    """A console tab for the running guest, once the real one has landed.

    open_console puts a placeholder up first and swaps it when the session
    connects, so a reference captured before that goes stale mid-test.
    """
    window.open_console(RUNNING)
    pump_until(
        lambda: (
            window.consoles.get(RUNNING) is not None
            and type(window.consoles[RUNNING]).__name__ != "PlaceholderConsole"
            and window.notebook.get_tab_label(window.consoles[RUNNING]) is not None
        ),
        10,
        step=0.1,
    )
    console = window.consoles.get(RUNNING)
    yield console
    window.close_console(RUNNING)
    pump(0.3)


def test_tab_titles_follow_the_name_id_both_setting(window, config, live_console):
    assert live_console is not None, "no console tab appeared for a running guest"
    assert window.notebook.get_tab_label(live_console) is not None
    titles = {}
    try:
        for style in ("name", "id", "both"):
            config["tab_title_format"] = style
            window._apply_name_formats()
            pump(0.1)
            # Re-read: a reconnect can swap the widget behind the tab.
            current = window.consoles.get(RUNNING)
            label = window.notebook.get_tab_label(current) if current else None
            if label is not None:
                titles[style] = label.label.get_text()
    finally:
        config["tab_title_format"] = "name"
        window._apply_name_formats()
    assert titles == {"name": "web01", "id": "100", "both": "web01 (100)"}


def test_a_spice_console_can_be_reopened_with_vnc(window, live_console):
    # The console itself is not rebuilt here: this fake has no vnc_ticket, on
    # purpose, so planning is what gets checked -- it is where the decision is
    # actually made.
    if live_console is None or live_console.protocol != "spice":
        pytest.skip(
            f"no live SPICE console (got {getattr(live_console, 'protocol', None)})"
        )

    window._sync_view_menu()
    assert window.switch_protocol_item.get_label() == "Reopen Console with VNC", (
        "the VM menu does not offer VNC on a SPICE tab"
    )
    assert window.switch_protocol_item.get_sensitive()

    window._force_vnc.add(RUNNING)
    assert plan_protocol(window, RUNNING) == "vnc", (
        "a forced console still planned SPICE"
    )
    # Forcing VNC is not allowed to look like evidence about the display, or
    # there would be no way back.
    assert window.sidebar.guests[RUNNING].spice_capable is True, (
        "forcing VNC lost the guest's SPICE capability"
    )

    class _VncTab:
        protocol = "vnc"
        guest_key = RUNNING

    window._sync_protocol_switch(_VncTab())
    assert window.switch_protocol_item.get_label() == "Reopen Console with SPICE", (
        "no way back to SPICE from a switched console"
    )
    assert window.switch_protocol_item.get_sensitive()


def test_closing_the_tab_forgets_the_vnc_choice(window):
    window.open_console(RUNNING)
    pump(1.0)
    window._force_vnc.add(RUNNING)
    window.close_console(RUNNING)
    pump(0.3)
    assert RUNNING not in window._force_vnc


def test_a_saved_console_reopens_and_gives_up_on_guests_that_never_appear(
    window, config
):
    window.open_console(RUNNING)
    pump(1.0)
    config["restore_session"] = True
    window._save_session()
    assert config["session_consoles"] == [RUNNING], (
        f"open console not saved: {config['session_consoles']}"
    )
    assert config["session_expanded"], "tree expansion was not saved"
    window.close_console(RUNNING)
    pump(0.3)

    window._restore_keys = [RUNNING]
    window._restore_until = time.monotonic() + 30
    window._resume_session()
    pump(1.0)
    assert RUNNING in window.consoles, "the saved console did not reopen"
    window.close_console(RUNNING)
    pump(0.3)

    window._restore_keys = ["gone.example.invalid/pve-node-01/qemu/999"]
    window._restore_until = time.monotonic() - 1
    window._resume_session()
    assert not window._restore_keys, "session restore never gives up on a missing guest"


# -- another client on the SPICE console ----------------------------------
# Nobody may be thrown off a console by accident, and "the monitor would not
# answer" must never be mistaken for "nobody is there".


@pytest.fixture
def occupied(window, api):
    window.close_console(RUNNING)
    window._recent_spice.pop(RUNNING, None)
    pump(0.3)
    yield window.sidebar.guests[RUNNING]
    FakeAPI.SPICE_CLIENTS = {}
    # The class attribute and the instance one both matter: a refused monitor
    # call latches itself on the instance.
    FakeAPI.monitor_available = True
    api.monitor_available = True
    window.consoles.pop(RUNNING, None)
    window._recent_spice.pop(RUNNING, None)
    window.close_console(RUNNING)
    pump(0.3)


def test_an_idle_console_is_not_reported_as_occupied(window, occupied):
    FakeAPI.SPICE_CLIENTS = {100: 0}
    assert window._spice_occupancy(occupied) == (0, [])


def test_another_client_is_detected_and_overridable(window, occupied):
    FakeAPI.SPICE_CLIENTS = {100: 1}
    occupancy = window._spice_occupancy(occupied)
    assert occupancy is not None and occupancy[0] == 1, (
        f"an occupied console was not detected: {occupancy}"
    )
    assert window._plan_console(occupied)["protocol"] == "occupied", (
        "planning ignored the other client"
    )
    assert window._plan_console(occupied, takeover=True)["protocol"] == "spice", (
        "take over did not skip the occupancy check"
    )


def test_our_own_session_is_not_another_client(window, occupied):
    FakeAPI.SPICE_CLIENTS = {100: 1}

    class _LiveSpice(FakeConsole):
        protocol = "spice"
        connected = True

    mine = _LiveSpice()
    mine.guest_key = RUNNING
    window.consoles[RUNNING] = mine
    assert window._spice_occupancy(occupied)[0] == 0, (
        "our own SPICE session counted as another client"
    )


def test_a_just_closed_session_is_not_another_client(window, occupied):
    # QEMU keeps counting a client for a moment after it goes, which is what a
    # reconnect looks like from here.
    FakeAPI.SPICE_CLIENTS = {100: 1}
    window.consoles.pop(RUNNING, None)
    window._recent_spice[RUNNING] = time.monotonic()
    assert window._spice_occupancy(occupied)[0] == 0


def test_a_stopped_guest_voids_our_claim_on_its_session(window, occupied):
    # Anything on the console after the guest went down is somebody else's.
    console = FakeConsole()
    console.guest_key = RUNNING
    window.consoles[RUNNING] = console
    window._recent_spice[RUNNING] = time.monotonic()
    sample_row(100)["status"] = "stopped"
    try:
        window.refresh()
        pump_until(lambda: RUNNING not in window._recent_spice, 6)
        assert RUNNING not in window._recent_spice, (
            "a stopped guest kept our stale session claim"
        )
    finally:
        sample_row(100)["status"] = "running"
        window.refresh()
        pump(0.5)


def test_a_refused_monitor_call_means_unknown_not_empty(window, api, occupied):
    FakeAPI.monitor_available = False
    api.monitor_available = False
    assert window._spice_occupancy(occupied) is None, (
        "a refused monitor call was read as an answer"
    )
    assert window._plan_console(occupied)["protocol"] == "spice", (
        "a refused monitor call blocked the console"
    )


def test_the_occupancy_check_honours_its_preference(window, config, occupied):
    config["spice_session_check"] = False
    FakeAPI.SPICE_CLIENTS = {100: 2}
    try:
        assert window._spice_occupancy(occupied) is None, (
            "the occupancy check ran while switched off"
        )
    finally:
        config["spice_session_check"] = True


def test_an_automatic_open_puts_take_over_or_vnc_on_the_tab(window, api, occupied):
    FakeAPI.SPICE_CLIENTS = {100: 1}
    window.open_console(RUNNING, automatic=True)
    pump_until(
        lambda: getattr(window.consoles.get(RUNNING), "last_status", "") == "choice",
        8,
        step=0.3,
    )
    held = window.consoles.get(RUNNING)
    assert getattr(held, "last_status", "") == "choice", (
        f"an automatic open did not put the choice on the tab "
        f"(tab shows {getattr(held, 'last_status', None)!r})"
    )
    assert len(held.status_panel._extra) == 2, (
        "the occupied tab does not offer both ways out"
    )
    assert not held.status_panel.reconnect_button.get_visible(), (
        "the occupied tab still offers a plain Reconnect"
    )

    # Choosing VNC leaves the other client alone. This fake has no vnc_ticket,
    # so the open fails after planning -- which is fine, the point is that
    # planning never went near spiceproxy.
    spice_calls = len([c for c in api.calls if c[0] == "spice"])
    held.status_panel._extra[1].clicked()
    pump_until(
        lambda: RUNNING in window._force_vnc and not window._poll_busy, 6, step=0.3
    )
    assert len([c for c in api.calls if c[0] == "spice"]) == spice_calls, (
        "choosing VNC still asked for a SPICE ticket"
    )

"""Who else is already on a guest's SPICE console.

Split out of test_console.py so it runs on its own worker: the checks here
need the console closed and the monitor reset around every test, which is a
different starting point from the tab tests next door.

Nobody may be thrown off a console by accident, and "the monitor would not
answer" must never be mistaken for "nobody is there".
"""

import time

import pytest

from .conftest import FakeAPI, FakeConsole, key_for, pump, pump_until, sample_row

RUNNING = key_for(100)


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

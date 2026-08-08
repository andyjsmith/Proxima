"""A server that answers slowly, and one that refuses outright."""

import time

import pytest

from .conftest import (
    FailingConsoleAPI,
    SlowAPI,
    SlowConsoleAPI,
    build_window,
    close_window,
    key_for,
    make_config,
    pump,
    pump_until,
    reset_fakes,
    wait_for_guests,
)

RUNNING = key_for(100)
SPICE_GUEST = key_for(200, node="pve-node-02")


@pytest.fixture(autouse=True)
def _fakes():
    reset_fakes()
    yield
    reset_fakes()


def test_a_slow_detail_fetch_survives_the_polls_that_land_on_it():
    # The "checking... forever" regression: the poll re-renders the summary
    # every few seconds, and that used to cancel the in-flight reply while the
    # cache still claimed the request had been made.
    api = SlowAPI(delay=1.2)
    window = build_window(api, make_config(poll_idle_seconds=1, poll_active_seconds=1))
    try:
        # The inventory has to be in before a guest can be opened, and this
        # server is slow enough that it does not always arrive in one pump.
        assert wait_for_guests(window), "slow window never listed any guests"
        window.sidebar.select_key(SPICE_GUEST)
        window.open_console(SPICE_GUEST)
        pump_until(lambda: SPICE_GUEST in window.tabs, 8)
        tab = window.tabs[SPICE_GUEST]
        # Watching the summary is the situation under test: it is the poll
        # re-rendering it that used to cancel the fetch in flight.
        tab.show_view("summary", by_user=True)
        window.panes.focus_page(tab)
        summary = tab.summary
        guest = window.sidebar.guests[SPICE_GUEST]
        summary.show_guest(guest, window.api_for(guest))
        pump_until(
            lambda: "checking" not in summary.values["console"].get_text(),
            12,
        )
        text = summary.values["console"].get_text()
        assert "checking" not in text, (
            f"summary never left 'checking...' under polling "
            f"(after {api.config_calls} config calls)"
        )
    finally:
        close_window(window)


def test_the_tab_opens_before_the_ticket_arrives():
    api = SlowConsoleAPI(delay=1.5)
    window = build_window(api, make_config())
    try:
        assert wait_for_guests(window), "slow-console window never listed any guests"
        tabs_before = window.notebook.get_n_pages()

        started = time.time()
        window.open_console(RUNNING)
        pump(0.3)
        elapsed = time.time() - started
        opening = window.consoles.get(RUNNING)

        assert window.notebook.get_n_pages() == tabs_before + 1, (
            "no tab appeared while the console was connecting"
        )
        assert elapsed <= 1.0, f"tab took {elapsed:.1f}s to appear"
        assert opening is not None and opening.protocol == "offline", (
            "connecting tab is not a placeholder"
        )
        assert "Connecting" in opening.status_panel.title.get_text(), (
            f"connecting tab reads {opening.status_panel.title.get_text()!r}"
        )

        pump_until(lambda: window.consoles.get(RUNNING) is not opening, 15, step=0.3)
        swapped = window.consoles.get(RUNNING)
        assert swapped is not opening, "the real console never replaced the placeholder"
        assert window.notebook.get_n_pages() == tabs_before + 1, (
            "swapping the real console in changed the tab count"
        )
    finally:
        close_window(window)


def test_a_console_that_cannot_open_keeps_its_tab_and_explains_itself():
    window = build_window(FailingConsoleAPI(), make_config())
    try:
        assert wait_for_guests(window), "failing-console window never listed any guests"
        window.open_console(RUNNING)
        pump_until(
            lambda: getattr(window.consoles.get(RUNNING), "last_status", "") == "error",
            8,
            step=0.3,
        )
        console = window.consoles.get(RUNNING)
        assert console is not None, "a failed console lost its tab"
        assert console.last_status == "error", (
            f"failed console shows {console.status_panel.title.get_text()!r}"
        )
        assert console.status_panel.reconnect_button.get_visible(), (
            "a failed console offers no way to retry"
        )
    finally:
        close_window(window)

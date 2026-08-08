"""The inventory poll runs at two speeds and picks between them itself.

At rest it is watching in case somebody else changes something, which does
not need to be quick. With a change asked for and not yet reported it is the
thing that ends the wait, so it hurries. Everything here is about the window
moving between those two states on its own.
"""

import time

import pytest

from proxima.config import Config

from .conftest import key_for, pump, pump_until

RUNNING = key_for(100)
STOPPED = key_for(102)


@pytest.fixture
def resting(window):
    """A window with nothing outstanding, put back that way afterwards."""
    window._active_until = 0.0
    window._busy.clear()
    window._pending_actions.clear()
    window._restore_keys = []
    window._restore_nodes = []
    window._restart_poll()
    yield window
    window._active_until = 0.0
    window._busy.clear()
    window._pending_actions.clear()
    window._restart_poll()


# -- the settings ---------------------------------------------------------


def test_the_two_cadences_are_configurable(config):
    for key in ("poll_idle_seconds", "poll_active_seconds", "poll_active_for"):
        assert key in config, f"{key} is not a setting"


def test_resting_is_never_faster_than_waiting(window, config):
    """Two numbers that can be put the wrong way round, so they are ordered.

    A window that polled harder for doing nothing than for waiting on
    something would be the wrong way up.
    """
    was = (config.get("poll_idle_seconds"), config.get("poll_active_seconds"))
    try:
        config["poll_idle_seconds"] = 1
        config["poll_active_seconds"] = 5
        active, idle = window.poll_intervals()
        assert active == 5
        assert idle >= active, f"resting ({idle}s) is faster than waiting ({active}s)"
    finally:
        config["poll_idle_seconds"], config["poll_active_seconds"] = was


def test_the_retired_interval_settings_are_ignored_and_dropped():
    """No migration: the old keys are simply not settings any more.

    A stored config only keeps keys the defaults know about, so a file
    written before the split loads with the new defaults and loses the old
    keys the next time it is saved. Nothing to clean up by hand.
    """
    stored = Config({"config_version": 1, "refresh_seconds": 1, "burst_seconds": 30})
    assert "refresh_seconds" not in stored
    assert "burst_seconds" not in stored
    assert stored["poll_idle_seconds"] == 6
    assert stored["poll_active_seconds"] == 2
    assert stored["poll_active_for"] == 15

    # And a fresh install gets the same.
    assert Config()["poll_idle_seconds"] == 6
    assert Config()["poll_active_seconds"] == 2


# -- choosing between them ------------------------------------------------


def test_an_idle_window_polls_at_the_resting_interval(resting):
    active, idle = resting.poll_intervals()
    assert active != idle, "the fixture needs two different intervals to tell apart"
    assert not resting._waiting_for_something()
    assert resting._poll_interval() == idle
    assert resting._poll_every == idle, "the timer is not on the resting interval"


def test_an_action_moves_it_to_the_faster_one(resting):
    _active, idle = resting.poll_intervals()
    assert resting._poll_every == idle

    resting.burst_poll(seconds=5)
    assert resting._waiting_for_something()
    assert resting._poll_every == resting.poll_intervals()[0], (
        "an action did not speed the poll up"
    )


def test_it_drops_back_once_nothing_is_outstanding(resting):
    active, idle = resting.poll_intervals()
    resting.burst_poll(seconds=1)
    assert resting._poll_every == active

    assert pump_until(lambda: resting._poll_every == idle, 8), (
        "the window kept polling fast with nothing left to wait for"
    )


def test_a_guest_waiting_on_a_change_holds_the_fast_cadence(resting):
    """Longer than the timer would: the wait is what matters, not a countdown.

    An action whose result has not landed keeps the poll quick for as long
    as it takes, which is the whole point of choosing by state rather than
    by stopwatch.
    """
    active, _idle = resting.poll_intervals()
    # No time left on the clock, but a change still outstanding.
    resting._mark_busy(RUNNING, "status", "running", "stopped", "stopping", timeout=30)
    resting._active_until = 0.0
    try:
        assert resting._waiting_for_something()
        assert resting._poll_interval() == active
        assert resting._poll_every == active, (
            "marking a guest busy did not speed the poll up"
        )
    finally:
        resting._clear_busy(RUNNING)

    assert not resting._waiting_for_something(), (
        "the wait outlived the change it was waiting for"
    )


def test_a_server_still_connecting_counts_as_waiting(resting):
    connection = resting.connections.all[0]
    was = connection.state
    connection.state = "connecting"
    try:
        assert resting._waiting_for_something(), (
            "a server that has not answered yet is not being waited on"
        )
    finally:
        connection.state = was


# -- what it costs --------------------------------------------------------


def test_an_idle_window_makes_far_fewer_requests(resting, api, config):
    """The point of the whole thing, measured.

    Counted against the resting interval rather than against a fixed
    number, so the test says "roughly one poll per interval" rather than
    encoding today's default.
    """
    was = (config.get("poll_idle_seconds"), config.get("poll_active_seconds"))
    try:
        config["poll_idle_seconds"] = 3
        config["poll_active_seconds"] = 1
        resting._restart_poll()
        pump(0.3)

        before = api.calls.count("guests")
        started = time.time()
        pump(6.5)
        elapsed = time.time() - started
        polls = api.calls.count("guests") - before

        allowed = int(elapsed / 3) + 2
        assert polls <= allowed, (
            f"{polls} inventory calls in {elapsed:.1f}s at a 3s resting "
            f"interval (at most {allowed} expected)"
        )
    finally:
        config["poll_idle_seconds"], config["poll_active_seconds"] = was
        resting._restart_poll()


def test_an_action_no_longer_runs_two_timers_at_once(resting):
    """There used to be a second timer laid over the first one.

    A burst added its own once-a-second source while the ordinary poll kept
    running, so an action was answered by both at the same time. There is
    one timer now, and it changes speed.
    """
    resting.burst_poll(seconds=3)
    assert not hasattr(resting, "_burst_source"), "the second poll timer is back"
    assert resting._poll_source is not None
    assert resting._poll_every == resting.poll_intervals()[0]

"""The one description of a guest with no picture, shared by five callers.

These were five copies of the same lookup tables. The tests that matter are
the ones a sixth copy would fail: every status covered, every template fully
substituted, and the two axes that legitimately vary actually varying.
"""

import pytest

from proxima.api.models import LIVE_STATUSES
from proxima.console import guest_state
from proxima.ui import actions as action_defs

# Everything a guest can be that means "no console, or not a live one".
DESCRIBED = ("stopped", "suspended", "paused", "prelaunch", "io-error", "connecting")


@pytest.mark.parametrize("status", DESCRIBED)
@pytest.mark.parametrize("noun", (guest_state.GUEST, guest_state.CONTAINER))
@pytest.mark.parametrize("reconnecting", (True, False))
def test_every_description_is_fully_substituted(status, noun, reconnecting):
    """A leftover {noun} or {verb} would ship straight to the panel."""
    state = guest_state.describe(status, noun=noun, reconnecting=reconnecting)
    for field in (state.title, state.detail):
        assert "{" not in field and "}" not in field, (
            f"{status} left a template field unfilled: {field!r}"
        )
    assert state.title, f"{status} has no title"
    assert state.icon, f"{status} has no icon"


@pytest.mark.parametrize("status", DESCRIBED)
def test_every_described_status_has_a_detail_line(status):
    """The panel looks broken with a title and nothing under it."""
    assert guest_state.describe(status).detail, f"{status} says nothing but its name"


def test_an_unknown_status_is_still_named():
    """Proxmox may invent a state; naming it beats a blank panel."""
    state = guest_state.describe("hibernating")
    assert "hibernating" in state.title, f"unknown status drew {state.title!r}"


def test_a_container_is_not_called_a_guest():
    state = guest_state.describe("stopped", noun=guest_state.CONTAINER)
    assert state.title.startswith("Container"), state.title
    assert "guest" not in state.detail.lower(), (
        f"a container console called it a guest: {state.detail!r}"
    )


def test_a_first_connection_does_not_call_itself_a_reconnection():
    """The placeholder tab has never had a console to reconnect to."""
    first = guest_state.describe("stopped", reconnecting=False)
    again = guest_state.describe("stopped", reconnecting=True)
    assert "reconnect" not in first.detail, first.detail
    assert "reconnect" in again.detail, again.detail


@pytest.mark.parametrize("status", ("prelaunch", "io-error"))
def test_a_guest_that_is_up_can_be_reconnected_to(status):
    """QEMU is serving in both, so there is something to connect to."""
    assert guest_state.describe(status).can_reconnect, (
        f"{status} offered no way back to a console that exists"
    )
    # And the two agree with the model about which those are.
    assert status in LIVE_STATUSES, f"{status} is described as up but is not LIVE"


@pytest.mark.parametrize("status", ("stopped", "suspended", "paused", "connecting"))
def test_a_guest_that_is_not_up_offers_no_reconnect(status):
    assert not guest_state.describe(status).can_reconnect, (
        f"{status} offered a Reconnect that could only fail"
    )


def test_a_resumable_guest_is_told_to_resume():
    """The panel's advice has to match the button the toolbar offers."""
    for status in action_defs.RESUMABLE:
        detail = guest_state.describe(status).detail
        assert "esume" in detail, f"{status} does not mention resuming: {detail!r}"

"""Each guest's tab flips between its console and its summary.

The flip belongs to the tab, not to the window: two tabs open at once are on
whichever view each was left on.
"""

import pytest
from gi.repository import Gdk

from proxima.api import notes as notes_mod
from proxima.ui.guest_tab import CONSOLE, SUMMARY, GuestTab

from .conftest import FakeAPI, key_for, pump, pump_until, sample_row


def press_event(button=1):
    """A left click, as GTK would deliver it."""
    event = Gdk.Event.new(Gdk.EventType.BUTTON_PRESS)
    event.button = button
    return event


RUNNING = key_for(100)
OTHER_RUNNING = key_for(101)
STOPPED = key_for(102)
TEMPLATE = key_for(900, node="pve-node-02")


@pytest.fixture
def closed_tabs(window):
    """Leave no tabs behind, whatever a test opened."""
    yield
    for key in list(window.tabs):
        window.close_console(key)
    pump(0.3)


def test_there_is_no_global_summary_page(window):
    # The tab strip starts empty: a guest you have not opened has no tab,
    # exactly as it has no console.
    assert not hasattr(window, "summary"), "the global summary page is still there"
    assert window.notebook.get_n_pages() == 0
    assert window.tabs == {}


def test_opening_a_guest_gives_it_one_tab(window, closed_tabs):
    window.open_console(RUNNING)
    pump(1.0)
    assert RUNNING in window.tabs
    assert window.notebook.get_n_pages() == 1
    assert isinstance(window.tabs[RUNNING], GuestTab)

    # Opening it again brings the tab forward rather than adding another.
    window.open_console(RUNNING)
    pump(0.5)
    assert window.notebook.get_n_pages() == 1


def test_a_running_guest_opens_on_its_console(window, closed_tabs):
    window.open_console(RUNNING)
    pump_until(lambda: window.tabs[RUNNING].console is not None, 8)
    assert window.tabs[RUNNING].view == CONSOLE


def test_a_stopped_guest_opens_on_its_summary(window, closed_tabs):
    window.open_console(STOPPED)
    pump(0.8)
    tab = window.tabs[STOPPED]
    assert tab.view == SUMMARY, "a stopped guest opened on a console with nothing in it"
    # The console side still exists behind it, so there is something to flip
    # to once the guest starts.
    assert tab.console is not None


def test_the_toolbar_button_flips_the_tab_in_front(window, closed_tabs):
    window.open_console(RUNNING)
    pump_until(lambda: window.tabs[RUNNING].console is not None, 8)
    tab = window.tabs[RUNNING]
    assert tab.view == CONSOLE
    assert not window.summary_tool_item.get_active()

    window.summary_tool_item.set_active(True)
    pump(0.3)
    assert tab.view == SUMMARY, "the Summary button did not flip the tab"

    window.summary_tool_item.set_active(False)
    pump(0.3)
    assert tab.view == CONSOLE


def test_the_button_shows_the_state_of_whichever_tab_is_in_front(window, closed_tabs):
    window.open_console(RUNNING)
    pump_until(lambda: window.tabs[RUNNING].console is not None, 8)
    window.open_console(OTHER_RUNNING)
    pump_until(lambda: window.tabs[OTHER_RUNNING].console is not None, 8)

    window.tabs[RUNNING].show_view(SUMMARY, by_user=True)
    window.panes.focus_page(window.tabs[RUNNING])
    pump(0.4)
    assert window.summary_tool_item.get_active(), (
        "the button does not show that this tab is on its summary"
    )

    window.panes.focus_page(window.tabs[OTHER_RUNNING])
    pump(0.4)
    assert not window.summary_tool_item.get_active(), (
        "the button kept the other tab's state"
    )


def test_flipping_one_tab_leaves_the_others_alone(window, closed_tabs):
    window.open_console(RUNNING)
    window.open_console(OTHER_RUNNING)
    pump_until(
        lambda: (
            window.tabs[RUNNING].console is not None
            and window.tabs[OTHER_RUNNING].console is not None
        ),
        10,
    )
    for key in (RUNNING, OTHER_RUNNING):
        window.tabs[key].show_view(CONSOLE)

    window.tabs[RUNNING].toggle()
    pump(0.3)
    assert window.tabs[RUNNING].view == SUMMARY
    assert window.tabs[OTHER_RUNNING].view == CONSOLE, "flipping one tab moved another"


def test_a_guest_powering_off_brings_its_summary_forward(window, closed_tabs):
    window.open_console(RUNNING)
    pump_until(lambda: window.tabs[RUNNING].console is not None, 8)
    tab = window.tabs[RUNNING]
    assert tab.view == CONSOLE

    sample_row(100)["status"] = "stopped"
    try:
        window.refresh()
        pump_until(lambda: tab.view == SUMMARY, 8)
        assert tab.view == SUMMARY, "a stopped guest was left staring at a dead console"
    finally:
        sample_row(100)["status"] = "running"
        window.refresh()
        pump(0.5)


def test_a_guest_powering_on_brings_its_console_back(window, closed_tabs):
    sample_row(102)["status"] = "stopped"
    window.open_console(STOPPED)
    pump(0.8)
    tab = window.tabs[STOPPED]
    assert tab.view == SUMMARY

    sample_row(102)["status"] = "running"
    try:
        window.refresh()
        pump_until(lambda: tab.view == CONSOLE, 10)
        assert tab.view == CONSOLE, "the console did not come back with the guest"
    finally:
        sample_row(102)["status"] = "stopped"
        window.refresh()
        pump(0.5)


def test_a_hand_picked_view_survives_a_poll(window, closed_tabs):
    """Choosing the summary on a running guest is not undone a second later."""
    window.open_console(RUNNING)
    pump_until(lambda: window.tabs[RUNNING].console is not None, 8)
    tab = window.tabs[RUNNING]
    tab.show_view(SUMMARY, by_user=True)

    window.refresh()
    pump(1.5)
    assert tab.view == SUMMARY, "a poll overrode the view the user chose"


def test_closing_the_tab_takes_the_summary_with_it(window, closed_tabs):
    window.open_console(RUNNING)
    pump(1.0)
    assert window.notebook.get_n_pages() == 1
    window.close_console(RUNNING)
    pump(0.4)
    assert RUNNING not in window.tabs
    assert RUNNING not in window.consoles
    assert window.notebook.get_n_pages() == 0


def test_the_summary_describes_its_own_guest(window, closed_tabs):
    window.open_console(RUNNING)
    window.open_console(OTHER_RUNNING)
    pump(1.5)
    for key, vmid in ((RUNNING, "100"), (OTHER_RUNNING, "101")):
        guest = window.sidebar.guests[key]
        summary = window.tabs[key].summary
        summary.show_guest(guest, window.api_for(guest))
        pump(0.2)
        assert summary.values["vmid"].get_text() == vmid, (
            "a tab's summary is describing another tab's guest"
        )


def test_the_summary_footer_reports_the_power_state(window, closed_tabs):
    window.open_console(STOPPED)
    pump(0.8)
    summary = window.tabs[STOPPED].summary
    guest = window.sidebar.guests[STOPPED]
    summary.show_guest(guest, window.api_for(guest))
    pump(0.2)
    assert "stopped" in summary.footer.get_text().lower()


def test_a_template_has_no_tab(window, closed_tabs):
    window.open_console(TEMPLATE)
    pump(0.5)
    assert TEMPLATE not in window.tabs, "a template was given a console tab"


def test_starting_a_guest_goes_to_the_console_at_once(window, api, closed_tabs):
    """Without waiting for the cluster to admit the guest is running."""
    sample_row(102)["status"] = "stopped"
    window.open_console(STOPPED)
    pump(0.8)
    tab = window.tabs[STOPPED]
    assert tab.view == SUMMARY

    try:
        window._run_action(STOPPED, "start", confirm=False)
        pump(0.8)
        # The inventory still says stopped -- that is the whole point.
        assert window.sidebar.guests[STOPPED].status == "stopped"
        assert tab.view == CONSOLE, (
            "start left the tab on the summary until the backend caught up"
        )
        assert "Start" in tab.console.status_panel.title.get_text(), (
            f"console reads {tab.console.status_panel.title.get_text()!r}"
        )
    finally:
        window._pending_actions.pop(STOPPED, None)
        window._clear_busy(STOPPED)
        window.refresh()
        pump(0.5)


def test_clicking_the_picture_goes_to_the_console(window, closed_tabs):
    window.open_console(RUNNING)
    pump_until(lambda: window.tabs[RUNNING].console is not None, 8)
    tab = window.tabs[RUNNING]
    tab.show_view(SUMMARY, by_user=True)
    pump(0.3)

    tab.summary.preview_button.emit("button-press-event", press_event())
    pump(0.3)
    assert tab.view == CONSOLE, "clicking the picture did not go to the console"


def test_clicking_the_picture_of_a_stopped_guest_does_nothing(window, closed_tabs):
    # There is nothing to switch to, and a blank console is not an answer.
    window.open_console(STOPPED)
    pump(0.8)
    tab = window.tabs[STOPPED]
    assert tab.view == SUMMARY

    tab.summary.preview_button.emit("button-press-event", press_event())
    pump(0.3)
    assert tab.view == SUMMARY


# -- notes ---------------------------------------------------------------


def test_the_notes_editor_hides_proximas_own_block(window, closed_tabs):
    FakeAPI.NOTES = {
        100: notes_mod.with_folder("Handwritten notes about this VM.", ["Production"])
    }
    window.open_console(RUNNING)
    pump_until(lambda: window.tabs[RUNNING].summary.notes_text() != "", 6)
    text = window.tabs[RUNNING].summary.notes_text()
    assert text == "Handwritten notes about this VM.", (
        f"the notes editor is showing Proxima's block: {text!r}"
    )
    assert "PROXIMA" not in text


def test_saving_notes_keeps_the_block_the_user_never_sees(window, closed_tabs):
    FakeAPI.NOTES = {100: notes_mod.with_folder("Original text.", ["Production"])}
    window.open_console(RUNNING)
    pump_until(lambda: window.tabs[RUNNING].summary.notes_text() != "", 6)

    summary = window.tabs[RUNNING].summary
    summary.notes_buffer.set_text("Rewritten by hand.")
    summary.save_notes()
    pump_until(lambda: "Rewritten" in FakeAPI.NOTES.get(100, ""), 6)

    written = FakeAPI.NOTES.get(100, "")
    assert "Rewritten by hand." in written
    assert notes_mod.folder_of(written) == ("Production",), (
        f"editing the notes lost the guest's folder: {written!r}"
    )
    # ...and what comes back to the editor is still only the user's half.
    assert "PROXIMA" not in notes_mod.parse(written)[1]


def test_a_poll_does_not_overwrite_notes_being_typed(window, closed_tabs):
    FakeAPI.NOTES = {100: "Original text."}
    window.open_console(RUNNING)
    pump_until(lambda: window.tabs[RUNNING].summary.notes_text() != "", 6)

    summary = window.tabs[RUNNING].summary
    summary.notes_buffer.set_text("half a sentence")
    # Whatever the server says now, the half-typed sentence stays.
    summary.set_notes(RUNNING, "Original text.")
    assert summary.notes_text() == "half a sentence"

"""Each guest's tab flips between its console and its summary.

The flip belongs to the tab, not to the window: two tabs open at once are on
whichever view each was left on.
"""

import pytest
from gi.repository import Gdk, GdkPixbuf, Gtk

from proxima.api import notes as notes_mod
from proxima.ui import sidebar as sidebar_mod
from proxima.ui import status_icons as icons_mod
from proxima.ui.guest_tab import CONSOLE, SUMMARY, GuestTab

from .conftest import (
    FakeAPI,
    FakeConsole,
    key_for,
    open_tab,
    pump,
    pump_until,
    sample_row,
)


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
    open_tab(window, RUNNING)
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
    tab = open_tab(window, STOPPED)
    assert tab.view == SUMMARY, "a stopped guest opened on a console with nothing in it"
    # The console side still exists behind it, so there is something to flip
    # to once the guest starts.
    assert tab.console is not None


def test_the_toolbar_button_flips_the_tab_in_front(window, closed_tabs):
    """One toggle, not a Console/Summary pair: down is the console."""
    tab = open_tab(window, RUNNING)
    assert tab.view == CONSOLE
    assert window.console_tool_item.get_active(), (
        "the Console button is up while the console is showing"
    )

    window.console_tool_item.set_active(False)
    pump(0.3)
    assert tab.view == SUMMARY, "releasing the Console button did not show the summary"

    window.console_tool_item.set_active(True)
    pump(0.3)
    assert tab.view == CONSOLE, "pressing the Console button did not show the console"


def test_there_is_no_separate_summary_button(window):
    """The pair was two names for one piece of state."""
    assert not hasattr(window, "summary_tool_item"), (
        "the Summary toolbar button is back alongside Console"
    )
    # The View menu keeps its entry, which is where the accelerator lives.
    assert window.summary_view_item is not None


def test_the_button_shows_the_state_of_whichever_tab_is_in_front(window, closed_tabs):
    open_tab(window, RUNNING)
    open_tab(window, OTHER_RUNNING)

    window.tabs[RUNNING].show_view(SUMMARY, by_user=True)
    window.panes.focus_page(window.tabs[RUNNING])
    pump(0.4)
    assert not window.console_tool_item.get_active(), (
        "the button does not show that this tab is on its summary"
    )

    window.panes.focus_page(window.tabs[OTHER_RUNNING])
    pump(0.4)
    assert window.console_tool_item.get_active(), (
        "the button kept the other tab's state"
    )


def test_flipping_one_tab_leaves_the_others_alone(window, closed_tabs):
    open_tab(window, RUNNING)
    open_tab(window, OTHER_RUNNING)
    for key in (RUNNING, OTHER_RUNNING):
        window.tabs[key].show_view(CONSOLE)

    window.tabs[RUNNING].toggle()
    pump(0.3)
    assert window.tabs[RUNNING].view == SUMMARY
    assert window.tabs[OTHER_RUNNING].view == CONSOLE, "flipping one tab moved another"


def test_a_guest_powering_off_brings_its_summary_forward(window, closed_tabs):
    tab = open_tab(window, RUNNING)
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
    tab = open_tab(window, STOPPED)
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
    tab = open_tab(window, RUNNING)
    tab.show_view(SUMMARY, by_user=True)

    window.refresh()
    pump(1.5)
    assert tab.view == SUMMARY, "a poll overrode the view the user chose"


def test_closing_the_tab_takes_the_summary_with_it(window, closed_tabs):
    open_tab(window, RUNNING)
    assert window.notebook.get_n_pages() == 1
    window.close_console(RUNNING)
    pump(0.4)
    assert RUNNING not in window.tabs
    assert RUNNING not in window.consoles
    assert window.notebook.get_n_pages() == 0


def test_the_summary_describes_its_own_guest(window, closed_tabs):
    open_tab(window, RUNNING)
    open_tab(window, OTHER_RUNNING)
    for key, vmid in ((RUNNING, "100"), (OTHER_RUNNING, "101")):
        guest = window.sidebar.guests[key]
        summary = window.tabs[key].summary
        summary.show_guest(guest, window.api_for(guest))
        pump(0.2)
        # The VMID lives in the heading now rather than in the grid: the
        # grid stopped repeating what the line above it already says.
        assert vmid in summary.subtitle.get_text(), (
            f"a tab's summary is describing another tab's guest: "
            f"{summary.subtitle.get_text()!r}"
        )


def test_the_summary_has_no_status_line_along_the_bottom(window, closed_tabs):
    """Every figure on it -- uptime, processors, memory, IP -- is a row."""
    tab = open_tab(window, STOPPED)
    assert not hasattr(tab.summary, "footer"), "the bottom status line is back"
    for field in ("uptime", "cpu", "memory", "address"):
        assert field in tab.summary.values, f"{field} is not in the grid either"


def test_a_template_has_no_tab(window, closed_tabs):
    window.open_console(TEMPLATE)
    pump(0.5)
    assert TEMPLATE not in window.tabs, "a template was given a console tab"


def test_starting_a_guest_goes_to_the_console_at_once(window, api, closed_tabs):
    """Without waiting for the cluster to admit the guest is running."""
    sample_row(102)["status"] = "stopped"
    tab = open_tab(window, STOPPED)
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
    tab = open_tab(window, RUNNING)
    tab.show_view(SUMMARY, by_user=True)
    pump(0.3)

    tab.summary.preview_button.emit("button-press-event", press_event())
    pump(0.3)
    assert tab.view == CONSOLE, "clicking the picture did not go to the console"


def test_clicking_the_picture_of_a_stopped_guest_does_nothing(window, closed_tabs):
    # There is nothing to switch to, and a blank console is not an answer.
    tab = open_tab(window, STOPPED)
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


# -- the summary's picture of the guest -----------------------------------


def big_pixbuf(width=1920, height=1080):
    """A frame the size a real console hands over."""
    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, width, height)
    pixbuf.fill(0x336699FF)
    return pixbuf


def test_the_preview_shrinks_with_the_window(window, closed_tabs):
    """A picture must never be able to make the page bigger than the window.

    A GtkImage will not go below its pixbuf's size, so a large frame used to
    push the summary's minimum width past the window: the page scrolled, the
    allocation stopped tracking the window, and the picture could only ever
    ratchet larger.
    """
    tab = open_tab(window, RUNNING)
    tab.show_view(SUMMARY, by_user=True)
    summary = tab.summary
    was = window.get_size()
    try:
        window.resize(1400, 900)
        pump(0.6)
        summary.set_preview(big_pixbuf())
        pump(0.6)
        wide = summary.preview_image.get_pixbuf()
        assert wide is not None, "the preview never took the frame"

        window.resize(760, 700)
        pump(1.2)
        narrow = summary.preview_image.get_pixbuf()
        assert narrow.get_width() < wide.get_width(), (
            f"the preview did not shrink with the window: "
            f"{wide.get_width()}px wide before, {narrow.get_width()}px after"
        )
    finally:
        window.resize(*was)
        pump(0.4)


def test_the_preview_never_outgrows_the_visible_page(window, closed_tabs):
    """The whole point of the height budget: no scrollbar on the summary."""
    tab = open_tab(window, RUNNING)
    tab.show_view(SUMMARY, by_user=True)
    summary = tab.summary
    was = window.get_size()
    try:
        # Wide and short, which is what makes a 16:9 frame scaled to the full
        # width taller than the page it sits on.
        window.resize(1600, 700)
        pump(0.6)
        summary.set_preview(big_pixbuf())
        pump(1.2)
        drawn = summary.preview_image.get_pixbuf()
        assert drawn is not None
        assert drawn.get_height() <= summary.get_allocated_height(), (
            f"the picture is {drawn.get_height()}px tall in a "
            f"{summary.get_allocated_height()}px page"
        )
    finally:
        window.resize(*was)
        pump(0.4)


def test_the_notes_sit_under_the_details_not_across_the_bottom(window, closed_tabs):
    """Full width, the notes were what pushed the page past the window."""
    tab = open_tab(window, RUNNING)
    summary = tab.summary
    # Walk up from the notes to see which column they landed in.
    parents = []
    widget = summary.notes_view
    while widget is not None:
        parents.append(widget)
        widget = widget.get_parent()
    assert summary._details_grid.get_parent() in parents, (
        "the notes are not in the same column as the details"
    )


def test_double_clicking_a_stopped_guest_lands_on_its_summary(window, closed_tabs):
    """There is no picture to look at, so the summary is the useful half.

    The tab already opens on the summary; what regressed is the second
    activation, which forced the console view on a guest that has none.
    """
    tab = open_tab(window, STOPPED)
    assert tab.view == SUMMARY

    # A double-click in the tree on a guest that already has a tab.
    window.sidebar.emit("guest-activated", STOPPED)
    pump(0.6)
    assert tab.view == SUMMARY, "double-clicking a stopped guest opened a dead console"
    assert not window.console_tool_item.get_active()


def test_double_clicking_a_running_guest_still_goes_to_its_console(window, closed_tabs):
    tab = open_tab(window, RUNNING)
    tab.show_view(SUMMARY, by_user=True)
    pump(0.3)

    window.sidebar.emit("guest-activated", RUNNING)
    pump(0.6)
    assert tab.view == CONSOLE, "double-clicking a running guest left the summary up"


def test_double_clicking_an_io_error_guest_goes_to_its_console(window, closed_tabs):
    """Its screen is frozen but present, and it is what you need to see."""
    tab = open_tab(window, RUNNING)
    tab.show_view(SUMMARY, by_user=True)
    pump(0.3)
    sample_row(100)["status"] = "io-error"
    try:
        window.refresh()
        pump_until(lambda: window.sidebar.guests[RUNNING].status == "io-error", 8)
        window.sidebar.emit("guest-activated", RUNNING)
        pump(0.6)
        assert tab.view == CONSOLE, "an io-error guest was sent to its summary"
    finally:
        sample_row(100)["status"] = "running"
        window.refresh()
        pump(0.5)


def test_the_preview_frame_follows_the_picture_it_holds(window, closed_tabs):
    """The frame must hug the picture, not sit at one fixed height.

    The scroller that lets the picture shrink does not pass its child's
    natural size up by default, which left the frame stuck at its minimum
    whatever was inside it.
    """
    tab = open_tab(window, RUNNING)
    tab.show_view(SUMMARY, by_user=True)
    summary = tab.summary
    was = window.get_size()
    frame = summary.preview_image.get_parent().get_parent()
    try:
        window.resize(1400, 950)
        pump(0.6)
        summary.set_preview(big_pixbuf())
        pump(1.2)
        drawn = summary.preview_image.get_pixbuf()
        gap = frame.get_allocated_height() - drawn.get_height()
        assert 0 <= gap <= 40, (
            f"the frame is {frame.get_allocated_height()}px around a "
            f"{drawn.get_height()}px picture"
        )
    finally:
        window.resize(*was)
        pump(0.4)


def watch_views(monkeypatch):
    """Every view a tab shows, from the moment it is built.

    Hooked on the property, not on show_view(): swapping a console used to
    change the visible child without going anywhere near show_view, so a spy
    on that method saw a clean run while the screen flashed.
    """
    seen = []
    real_init = GuestTab.__init__

    def spy(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        seen.append(self.get_visible_child_name())
        self.connect(
            "notify::visible-child-name",
            lambda stack, _p: seen.append(stack.get_visible_child_name()),
        )

    monkeypatch.setattr(GuestTab, "__init__", spy)
    return seen


def test_a_running_guest_opens_on_the_console_without_passing_the_summary(
    window, closed_tabs, monkeypatch
):
    """No flip to watch, including while the console itself is swapped in.

    A tab puts up a "Connecting..." placeholder and replaces it with the real
    console once the ticket lands. Both of those used to take the stack's
    visible child away, and a GtkStack with no visible child falls back to
    the summary -- so the tab crossfaded out and back twice on the way in.
    """
    seen = watch_views(monkeypatch)
    window.open_console(RUNNING)
    pump_until(lambda: window.tabs[RUNNING].console is not None, 8)
    pump(0.6)

    assert seen, "the tab never picked a view"
    assert SUMMARY not in seen, (
        f"the tab passed through the summary on its way to the console: {seen}"
    )
    assert window.tabs[RUNNING].view == CONSOLE


def test_swapping_a_console_leaves_the_view_alone(window, closed_tabs):
    """The mechanism behind the flash, on its own.

    Replacing the console is the one thing that must never move the tab: a
    reconnect happens under the user, and being thrown to the summary for it
    is exactly what the flicker was.
    """
    tab = open_tab(window, RUNNING)
    assert tab.view == CONSOLE
    seen = []
    tab.connect(
        "notify::visible-child-name",
        lambda stack, _p: seen.append(stack.get_visible_child_name()),
    )

    first, second = FakeConsole("first"), FakeConsole("second")
    tab.set_console(first)
    pump(0.3)
    tab.set_console(second)
    pump(0.3)

    assert tab.console is second
    assert tab.view == CONSOLE
    assert seen == [], f"swapping the console moved the tab: {seen}"


def test_a_stopped_guest_still_opens_straight_onto_its_summary(
    window, closed_tabs, monkeypatch
):
    """The same path the other way: no console flash for a guest with none."""
    seen = watch_views(monkeypatch)
    window.open_console(STOPPED)
    pump_until(lambda: STOPPED in window.tabs, 8)
    pump(0.6)

    assert seen, "the tab never picked a view"
    assert CONSOLE not in seen, (
        f"a stopped guest's tab flashed a console it does not have: {seen}"
    )
    assert window.tabs[STOPPED].view == SUMMARY


# -- the two buttons under the guest's name -------------------------------


def test_a_stopped_guest_offers_power_on_and_a_running_one_the_console(
    window, closed_tabs
):
    tab = open_tab(window, STOPPED)
    assert tab.summary.console_button.get_label() == "Power on"
    assert tab.summary.console_button.get_sensitive()

    running = open_tab(window, RUNNING)
    assert running.summary.console_button.get_label() == "Open Console"
    assert running.summary.console_button.get_sensitive()


def test_power_on_starts_the_guest_and_goes_to_the_console(window, api, closed_tabs):
    """The same thing the toolbar's Start does, from the summary."""
    sample_row(102)["status"] = "stopped"
    tab = open_tab(window, STOPPED)
    assert tab.view == SUMMARY

    # Counted, not compared: an earlier test in this file starts the same
    # guest, so the identical tuple is already in the list.
    def starts():
        return len([c for c in api.calls if c[0] == "power" and c[2] == "start"])

    before = starts()
    try:
        tab.summary.console_button.clicked()
        pump(0.8)
        assert starts() > before, (
            f"Power on asked Proxmox for nothing: {api.calls[-3:]}"
        )
        assert tab.view == CONSOLE, "Power on stayed on the summary"
    finally:
        window._pending_actions.pop(STOPPED, None)
        window._clear_busy(STOPPED)
        window.refresh()
        pump(0.5)


def test_edit_settings_opens_the_guests_settings(window, closed_tabs, monkeypatch):
    opened = []
    monkeypatch.setattr(
        type(window), "open_guest_settings", lambda self, key: opened.append(key)
    )
    tab = open_tab(window, RUNNING)
    assert tab.summary.settings_button.get_sensitive()
    tab.summary.settings_button.clicked()
    pump(0.3)
    assert opened == [RUNNING], f"Edit settings opened {opened}"


def test_the_grid_no_longer_repeats_the_heading(window, closed_tabs):
    """Name, VMID, node and type are all in the line above it."""
    tab = open_tab(window, RUNNING)
    for gone in ("name", "vmid", "node", "kind"):
        assert gone not in tab.summary.values, f"{gone} is still a row in the grid"


def test_a_stopped_guest_still_reports_its_processors_and_os(window, closed_tabs):
    """Both are in the guest's settings, so "-" was never the truth."""
    tab = open_tab(window, STOPPED)
    summary = tab.summary
    assert pump_until(lambda: summary.values["cpu"].get_text() != "-", 8), (
        "a stopped guest reports no processors"
    )
    cpu = summary.values["cpu"].get_text()
    assert "vCPU" in cpu, f"processors read {cpu!r}"
    assert pump_until(lambda: summary.values["os"].get_text() != "-", 8), (
        "a stopped guest reports no operating system"
    )
    assert "configured" in summary.values["os"].get_text()


def test_every_network_adapter_gets_a_row(window, api, closed_tabs):
    """One row per NIC, and the IP address field stays alongside them."""
    FakeAPI.HARDWARE[100] = {
        "net0": "virtio=BC:24:11:00:00:01,bridge=vmbr0,firewall=1",
        "net1": "e1000=BC:24:11:00:00:02,bridge=vmbr1,tag=42",
    }
    try:
        tab = open_tab(window, RUNNING)
        summary = tab.summary
        assert pump_until(lambda: len(summary._net_widgets) >= 2, 8), (
            f"only {len(summary._net_widgets)} network rows for two adapters"
        )
        texts = [value.get_text() for _name, value in summary._net_widgets]
        assert any("vmbr0" in t and "virtio" in t for t in texts), texts
        assert any("vmbr1" in t and "VLAN 42" in t for t in texts), texts
        assert "address" in summary.values, "IP address was dropped"
    finally:
        FakeAPI.HARDWARE.pop(100, None)


def test_the_picture_starts_level_with_the_guests_name(window, closed_tabs):
    """Two columns, nothing spanning above them.

    The heading and its buttons used to run the full width, which started
    the picture level with the Status row and gave away the tallest part of
    the space it had.
    """
    tab = open_tab(window, RUNNING)
    tab.show_view(SUMMARY, by_user=True)
    summary = tab.summary
    was = window.get_size()
    try:
        window.resize(1400, 900)
        pump(0.8)
        summary.set_preview(big_pixbuf())
        pump(0.8)
        picture = summary._preview_box.get_allocation()
        title = summary.title.get_allocation()
        grid = summary._details_grid.get_allocation()
        assert picture.y <= title.y, (
            f"the picture starts at y={picture.y}, below the name at y={title.y}"
        )
        assert picture.y < grid.y, (
            f"the picture starts at y={picture.y}, level with the details at y={grid.y}"
        )
        # ...and it really is the right-hand column, not stacked underneath.
        assert picture.x > grid.x, "the picture is not beside the details"
    finally:
        window.resize(*was)
        pump(0.4)


def test_the_action_buttons_space_their_icons_off_their_labels(window, closed_tabs):
    """GTK's own image-spacing is two pixels, which reads as one glyph.

    Measured on the image's allocation rather than the gap between the two:
    a CSS margin is drawn inside the widget's allocation, so the gap between
    the image and label boxes stays put while the icon moves left within it.
    """

    def image_in(widget):
        """The icon, wherever GTK put it inside the button."""
        if isinstance(widget, Gtk.Image):
            return widget
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                found = image_in(child)
                if found is not None:
                    return found
        return None

    tab = open_tab(window, RUNNING)
    tab.show_view(SUMMARY, by_user=True)
    pump(0.5)
    for name in ("console_button", "settings_button"):
        button = getattr(tab.summary, name)
        image = image_in(button)
        assert image is not None, f"{name} has no icon"
        assert button.get_style_context().has_class("labelled-icon"), name
        spaced = image.get_allocation().width
        button.get_style_context().remove_class("labelled-icon")
        # Waited for rather than slept on: a fixed pump is a bet that GTK
        # gets round to re-allocating within it, and on a loaded suite that
        # bet is lost often enough to fail a test about the CSS.
        pump_until(lambda i=image, w=spaced: i.get_allocation().width != w, 5, step=0.1)
        bare = image.get_allocation().width
        button.get_style_context().add_class("labelled-icon")
        pump_until(lambda i=image, w=spaced: i.get_allocation().width == w, 5, step=0.1)
        assert spaced - bare >= 4, (
            f"{name} gives its icon {spaced - bare}px of room beside the text"
        )


def test_nothing_around_the_preview_tries_to_scroll(window, closed_tabs):
    """The picture is sized to fit, so it must not sit in a scroller.

    One was used to stop the image dictating the page's minimum size, but a
    GtkScrolledWindow also swallows the wheel and paints GTK's overshoot
    glow -- so scrolling over a picture with nothing to scroll flashed the
    end-of-area indicators.
    """
    tab = open_tab(window, RUNNING)
    tab.show_view(SUMMARY, by_user=True)
    pump(0.4)
    summary = tab.summary

    scrollers = []
    widget = summary.preview_image
    while widget is not None and widget is not summary:
        if isinstance(widget, Gtk.ScrolledWindow):
            scrollers.append(type(widget).__name__)
        widget = widget.get_parent()
    assert not scrollers, (
        f"the preview is inside {scrollers}, which will try to scroll it"
    )
    # The summary page itself is still a scroller -- that one is wanted.
    assert isinstance(summary, Gtk.ScrolledWindow)


def test_the_notes_buttons_appear_only_when_there_is_an_edit(window, closed_tabs):
    """Nothing to save means no decision to explain, so no buttons."""
    FakeAPI.NOTES = {100: "Original text."}
    try:
        tab = open_tab(window, RUNNING)
        summary = tab.summary
        tab.show_view(SUMMARY, by_user=True)
        assert pump_until(lambda: summary.notes_text() != "", 8), "notes never loaded"
        # show_all() runs when a tab goes on screen; the buttons must not
        # come back with it.
        tab.show_all()
        pump(0.3)
        assert not summary.notes_save.get_visible(), "Save is showing with no edit"
        assert not summary.notes_revert.get_visible(), "Revert is showing with no edit"

        summary.notes_buffer.set_text("Edited by hand.")
        pump(0.3)
        assert summary.notes_save.get_visible(), "Save did not appear for an edit"
        assert summary.notes_revert.get_visible(), "Revert did not appear for an edit"

        summary._reset_notes()
        pump(0.3)
        assert not summary.notes_save.get_visible(), "Save outlived the edit"
        assert not summary.notes_revert.get_visible(), "Revert outlived the edit"
    finally:
        FakeAPI.NOTES = {}


def test_the_notes_do_not_shift_when_the_buttons_appear(window, closed_tabs):
    """A button is taller than the "Notes" label beside it."""
    FakeAPI.NOTES = {100: "Original text."}
    try:
        tab = open_tab(window, RUNNING)
        summary = tab.summary
        tab.show_view(SUMMARY, by_user=True)
        assert pump_until(lambda: summary.notes_text() != "", 8), "notes never loaded"
        pump(0.4)
        before = summary.notes_view.get_allocation().y

        summary.notes_buffer.set_text("Edited by hand.")
        pump(0.5)
        assert summary.notes_save.get_visible()
        after = summary.notes_view.get_allocation().y
        assert after == before, (
            f"the notes moved {after - before}px when the buttons appeared"
        )
    finally:
        FakeAPI.NOTES = {}


def test_there_is_no_unsaved_changes_caption(window, closed_tabs):
    """The buttons appearing already says it."""
    FakeAPI.NOTES = {100: "Original text."}
    try:
        tab = open_tab(window, RUNNING)
        summary = tab.summary
        assert pump_until(lambda: summary.notes_text() != "", 8), "notes never loaded"
        summary.notes_buffer.set_text("Edited by hand.")
        pump(0.4)
        assert "unsaved" not in summary.notes_status.get_text().lower(), (
            f"the caption is still there: {summary.notes_status.get_text()!r}"
        )
    finally:
        FakeAPI.NOTES = {}


def test_the_heading_wears_the_same_icon_as_the_tree(window, closed_tabs):
    """One source for what a guest looks like, not two opinions of it.

    Compared pixel for pixel against what the tree stores, at the tree's own
    size -- so the summary cannot drift to a different icon or a different
    green.
    """
    tab = open_tab(window, RUNNING)
    tab.show_view(SUMMARY, by_user=True)
    pump(0.4)
    drawn = tab.summary.status_icon.get_pixbuf()
    assert drawn is not None, "the heading has no status icon"

    guest = window.sidebar.guests[RUNNING]
    in_tree = window.sidebar.store.get_value(
        window.sidebar._find_row(RUNNING), sidebar_mod.COL_ICON
    )
    at_tree_size = icons_mod.guest_icon(
        guest, dark=window.sidebar._dark, size=icons_mod.ICON_SIZE
    )
    assert in_tree is not None and at_tree_size is not None
    assert at_tree_size.get_pixels() == in_tree.get_pixels(), (
        "the tree is not drawing the shared icon"
    )


def test_the_heading_icon_follows_the_guests_state(window, closed_tabs):
    tab = open_tab(window, RUNNING)
    tab.show_view(SUMMARY, by_user=True)
    pump(0.4)
    running = tab.summary.status_icon.get_pixbuf()
    sample_row(100)["status"] = "stopped"
    try:
        window.refresh()
        assert pump_until(
            lambda: (
                tab.summary.status_icon.get_pixbuf().get_pixels()
                != running.get_pixels()
            ),
            8,
        ), "the heading icon stayed on the running colour"
    finally:
        sample_row(100)["status"] = "running"
        window.refresh()
        pump(0.5)

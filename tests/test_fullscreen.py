"""Fullscreen hides every piece of chrome and gives it all back."""

import time

import pytest
from gi.repository import Gdk, Gtk

from proxima.ui import fullscreen as fullscreen_mod

from .conftest import FakeConsole, key_for, pump

CHROME = ("menubar", "toolbar", "statusbar_box", "sidebar")


def open_console(window, heads=1):
    """Put a console-shaped widget in a tab, so fullscreen has something to show."""
    window.sidebar.select_key(key_for(100))
    pump(0.3)
    widget = FakeConsole(heads=heads)
    window.consoles["fake"] = widget
    window.panes.append(widget, Gtk.Label(label="fake"))
    pump(0.4)
    return widget


def close_console(window):
    if window.fullscreen_control.active:
        window.fullscreen_control.leave()
        pump(0.3)
    window.close_console("fake")
    pump(0.3)


@pytest.fixture
def console(window):
    widget = open_console(window)
    yield widget
    close_console(window)


@pytest.fixture
def dual_console(window):
    """A console whose adapter can be asked for a second head, as QXL can."""
    widget = open_console(window, heads=2)
    yield widget
    close_console(window)


@pytest.fixture
def two_monitors(monkeypatch):
    """A desktop with a second monitor, whatever the machine really has."""
    monkeypatch.setattr(fullscreen_mod, "monitor_layout", lambda: [(0, 0), (1, 1920)])


def warp_pointer(window, dx, dy):
    """Park the real pointer, which is what drives the reveal."""
    gdk_window = window.get_window()
    if gdk_window is None:
        return False
    seat = Gdk.Display.get_default().get_default_seat()
    pointer = seat.get_pointer() if seat else None
    if pointer is None:
        return False
    origin = gdk_window.get_origin()
    pointer.warp(Gdk.Screen.get_default(), origin[1] + dx, origin[2] + dy)
    return True


def test_fullscreen_is_offered_only_with_a_console_open(window):
    window.sidebar.select_key(key_for(100))
    pump(0.3)
    assert not window.fullscreen_item.get_sensitive(), (
        "Full Screen is enabled with no console open"
    )


def test_entering_fullscreen_hides_every_piece_of_chrome(window, console):
    assert window.fullscreen_item.get_sensitive(), (
        "Full Screen is disabled with a console open"
    )
    window.fullscreen_control.enter()
    pump(0.4)
    assert window.fullscreen_control.active, "enter_fullscreen did not take"
    still_visible = [n for n in CHROME if getattr(window, n).get_visible()]
    assert not still_visible, f"still visible in fullscreen: {still_visible}"
    assert not window.notebook.get_show_tabs(), "notebook tabs still visible"
    assert window.fullscreen_control.revealer.get_reveal_child(), (
        "fullscreen bar not shown on entry"
    )
    assert window.fullscreen_control.title_label.get_text() == console.title


def test_the_bar_pins_open_and_auto_hides_when_unpinned(window, console):
    window.fullscreen_control.enter()
    pump(0.4)
    # The reveal is driven by the real pointer, so park it deliberately --
    # otherwise this passes or fails on where the mouse happens to be.
    warp_pointer(window, 400, 500)
    pump(0.3)

    window.fullscreen_control.pin.set_active(True)
    pump(0.3)
    assert window.fullscreen_control.revealer.get_reveal_child(), (
        "pinned bar was hidden"
    )

    window.fullscreen_control.pin.set_active(False)
    hidden = False
    for _attempt in range(4):
        # Keep nudging the pointer away rather than failing on a warp the
        # window manager ignored.
        warp_pointer(window, 400, 500)
        window.fullscreen_control._hide_at = time.monotonic() - 1
        pump(0.5)
        if not window.fullscreen_control.revealer.get_reveal_child():
            hidden = True
            break
    assert hidden, "unpinned bar did not auto-hide"


def test_the_bar_reveals_at_the_top_edge(window, console):
    window.fullscreen_control.enter()
    pump(0.4)
    if not warp_pointer(window, 400, 500):
        pytest.skip("pointer warp unavailable")
    pump(0.3)

    revealed = False
    for _attempt in range(4):
        warp_pointer(window, 400, 0)  # touch the top edge
        pump(0.5)
        if window.fullscreen_control.revealer.get_reveal_child():
            revealed = True
            break
    assert revealed, "bar did not reveal at the top edge"

    left_edge = False
    for _attempt in range(4):
        warp_pointer(window, 400, 500)
        pump(0.5)
        if not window.fullscreen_control.revealer.get_reveal_child():
            left_edge = True
            break
    assert left_edge, "bar did not hide again after leaving the edge"


def test_leaving_fullscreen_restores_the_chrome(window, console):
    window.fullscreen_control.enter()
    pump(0.5)
    window.fullscreen_control.leave()
    pump(0.4)
    assert not window.fullscreen_control.active, "leave_fullscreen did not take"
    missing = [n for n in CHROME if not getattr(window, n).get_visible()]
    assert not missing, f"not restored after fullscreen: {missing}"
    assert window.notebook.get_show_tabs(), "notebook tabs were not restored"
    assert window.fullscreen_control._poll_source is None, (
        "fullscreen poll timer still running"
    )


def test_all_monitors_is_refused_by_a_single_head_adapter(
    window, console, two_monitors
):
    """VirtIO-GPU, where asking for a second head would be asking for nothing."""
    window._sync_view_menu()
    assert not window.all_monitors_item.get_sensitive(), (
        "Use All Monitors is offered for a single-head adapter"
    )


def test_all_monitors_needs_a_second_monitor_to_use(window, dual_console, monkeypatch):
    monkeypatch.setattr(fullscreen_mod, "monitor_layout", lambda: [(0, 0)])
    window._sync_view_menu()
    assert not window.all_monitors_item.get_sensitive(), (
        "Use All Monitors is offered with only one monitor to use"
    )


def test_all_monitors_is_offered_when_a_head_can_be_asked_for(
    window, dual_console, two_monitors
):
    window._sync_view_menu()
    assert window.all_monitors_item.get_sensitive(), (
        "Use All Monitors is disabled for an adapter that can do it"
    )


def test_a_console_without_multi_monitor_support_never_spans(
    window, dual_console, two_monitors
):
    """VNC reports two heads in one framebuffer, so there is nothing to split."""
    dual_console.supports = dict(dual_console.supports, multi_monitor=False)
    window._sync_view_menu()
    assert not window.all_monitors_item.get_sensitive(), (
        "Use All Monitors is offered on a console that cannot do it"
    )

    window.fullscreen_control.enter(all_monitors=True)
    pump(0.4)
    assert not window.fullscreen_control.spanning, "spanned a console that cannot"
    assert not window.fullscreen_control._extra_windows, "opened an extra window anyway"


def test_a_second_head_gets_a_window_of_its_own(window, dual_console, two_monitors):
    window.fullscreen_control.enter(all_monitors=True)
    pump(0.5)
    assert window.fullscreen_control.spanning, "did not span monitors"
    extra = window.fullscreen_control._extra_windows
    assert len(extra) == 1, f"expected one extra window, got {len(extra)}"
    head, head_window = extra[0]
    assert head == 1, "the extra window is not showing the second head"
    assert head_window.get_visible(), "the extra window was never shown"
    assert 1 in dual_console.head_displays, "the console was never asked for the head"

    window.fullscreen_control.leave()
    pump(0.5)
    assert not window.fullscreen_control._extra_windows, (
        "extra window outlived fullscreen"
    )
    assert not dual_console.head_displays, "the extra head was never released"


def test_the_keyboard_shortcut_honours_the_preference(
    window, dual_console, two_monitors
):
    """Ctrl+Alt+Enter is handled inside the controller, not by the menu item.

    It used to toggle with no opinion about monitors, which meant the one
    way in that most people use ignored the setting entirely and always
    took a single screen.
    """
    window.config["fullscreen_all_monitors"] = True
    try:
        event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
        event.keyval = Gdk.KEY_Return
        event.state = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK
        handled = window.fullscreen_control.handle_key_press(event)
        pump(0.5)
        assert handled, "Ctrl+Alt+Enter was not taken"
        assert window.fullscreen_control.spanning, (
            "the shortcut went full screen on one monitor with the setting on"
        )
        assert window.fullscreen_control._extra_windows, "no window for the second head"
    finally:
        window.config["fullscreen_all_monitors"] = False


def test_every_window_opens_at_once_and_the_head_is_asked_for_again(
    window, dual_console, two_monitors
):
    """Ask immediately, then again once the guest has answered the resize.

    Both, not either. Some guests make the head on the first ask and lose
    it if the ask is delayed; others drop that first ask, because the
    message describing the first head's resize into full screen does not
    mention a head that has been asked for and not yet created. Asking
    twice suits both, and opening every window up front means none of them
    appears out of nowhere a second later.
    """
    settle = []
    retries = []
    dual_console.after_primary_settles = settle.append
    dual_console.retry_missing_heads = lambda: retries.append(True)

    window.fullscreen_control.enter(all_monitors=True)
    pump(0.4)
    assert window.fullscreen_control._extra_windows, (
        "the second monitor's window did not open with the first"
    )
    assert 1 in dual_console.head_displays, "the head was not asked for immediately"
    assert not retries, "asked again before the guest had answered"

    settle[0]()  # the guest answers the resize
    pump(0.3)
    assert retries, "the head was never asked for a second time"


def test_leaving_before_the_guest_answers_does_not_ask_again(
    window, dual_console, two_monitors
):
    settle = []
    retries = []
    dual_console.after_primary_settles = settle.append
    dual_console.retry_missing_heads = lambda: retries.append(True)

    window.fullscreen_control.enter(all_monitors=True)
    pump(0.3)
    window.fullscreen_control.leave()
    pump(0.3)

    settle[0]()  # the guest answers, too late to matter
    pump(0.3)
    assert not retries, "asked again after full screen was left"
    assert not window.fullscreen_control._extra_windows
    assert not dual_console.head_displays, "a head was left behind"


def test_full_screen_stays_on_one_monitor_when_not_asked(
    window, dual_console, two_monitors
):
    window.fullscreen_control.enter(all_monitors=False)
    pump(0.4)
    assert window.fullscreen_control.active, "did not go fullscreen"
    assert not window.fullscreen_control.spanning, (
        "spanned monitors without being asked"
    )
    assert not window.fullscreen_control._extra_windows, "opened an unasked-for window"


def test_closing_a_fullscreen_console_exits_fullscreen(window, console):
    window.notebook.set_current_page(window.notebook.page_num(console))
    pump(0.3)
    window.fullscreen_control.enter()
    pump(0.3)
    window.close_console("fake")
    pump(0.3)
    assert not window.fullscreen_control.active, (
        "closing the console left the window fullscreen"
    )

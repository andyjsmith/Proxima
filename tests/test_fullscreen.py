"""Fullscreen hides every piece of chrome and gives it all back."""

import time

import pytest
from gi.repository import Gdk, Gtk

from .conftest import FakeConsole, key_for, pump

CHROME = ("menubar", "toolbar", "statusbar_box", "sidebar")


@pytest.fixture
def console(window):
    """A console-shaped widget in a tab, so fullscreen has something to show."""
    window.sidebar.select_key(key_for(100))
    pump(0.3)
    widget = FakeConsole()
    window.consoles["fake"] = widget
    window.panes.append(widget, Gtk.Label(label="fake"))
    pump(0.4)
    yield widget
    if window.fullscreen_control.active:
        window.fullscreen_control.leave()
        pump(0.3)
    window.close_console("fake")
    pump(0.3)


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

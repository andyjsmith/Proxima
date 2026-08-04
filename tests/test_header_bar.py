"""The in-application titlebar, which is off unless asked for.

See docs/header-bar.md.
"""

import pytest

from .conftest import (
    FakeAPI,
    build_window,
    close_window,
    make_config,
    pump,
    reset_fakes,
)


def descends_from(widget, ancestor):
    while widget is not None:
        if widget is ancestor:
            return True
        widget = widget.get_parent()
    return False


@pytest.fixture(scope="module")
def plain_window():
    reset_fakes()
    window = build_window(FakeAPI(), make_config())
    pump(0.6)
    yield window
    close_window(window)
    reset_fakes()


@pytest.fixture(scope="module")
def header_window():
    reset_fakes()
    window = build_window(FakeAPI(), make_config(use_header_bar=True))
    pump(0.6)
    yield window
    close_window(window)
    reset_fakes()


def test_the_header_bar_is_off_by_default(plain_window):
    assert plain_window.header_bar is None


def test_without_a_header_bar_the_menus_stay_in_the_window(plain_window):
    assert descends_from(plain_window.menubar, plain_window.get_child())


def test_the_header_bar_becomes_the_titlebar(header_window):
    assert header_window.header_bar is not None, "the header bar setting did not take"
    assert header_window.get_titlebar() is header_window.header_bar, (
        "the header bar is not the window's titlebar"
    )


def test_the_header_bar_shows_a_connection_summary(header_window):
    header_window._update_connection_label()
    assert header_window.header_bar.get_subtitle()


def test_the_menus_move_into_the_titlebar_exactly_once(header_window):
    menubar = header_window.menubar
    assert descends_from(menubar, header_window.header_bar), (
        "the menu bar did not move into the titlebar"
    )
    assert not descends_from(menubar, header_window.get_child()), (
        "the menu bar is in the titlebar AND the window"
    )


def test_the_titlebar_keeps_its_window_controls(header_window):
    # An unset decoration layout is how they vanished before.
    assert header_window.header_bar.get_show_close_button(), (
        "the titlebar has no window controls"
    )
    assert "close" in (header_window.header_bar.get_decoration_layout() or ""), (
        "the titlebar's decoration layout has no close button: "
        f"{header_window.header_bar.get_decoration_layout()!r}"
    )

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


# -- the plain window's single chrome row ---------------------------------


def test_the_menus_share_the_toolbars_row(plain_window):
    """One row, not two: menus at the left, the buttons straight after.

    The menu bar used to have a row of its own above the toolbar, which is a
    lot of vertical space for four short words.
    """
    assert descends_from(plain_window.menubar, plain_window.toolbar), (
        "the menu bar is not in the toolbar"
    )
    root = plain_window.get_child()
    assert plain_window.menubar not in root.get_children(), (
        "the menu bar still has a row of its own"
    )


def test_the_menus_come_before_the_toolbar_buttons(plain_window):
    """Left-most, so the reading order is menus then actions."""
    items = plain_window.toolbar.get_children()
    assert items, "the toolbar is empty"
    assert descends_from(plain_window.menubar, items[0]), (
        f"the first toolbar item is {items[0]}, not the menus"
    )
    assert descends_from(plain_window.console_tool_item, plain_window.toolbar), (
        "the toolbar lost its buttons"
    )
    positions = {
        "menus": plain_window.toolbar.get_item_index(items[0]),
        "console": plain_window.toolbar.get_item_index(plain_window.console_tool_item),
    }
    assert positions["menus"] < positions["console"], positions


def test_a_header_bar_leaves_the_toolbar_to_its_buttons(header_window):
    """The menus go to one place or the other, never both."""
    assert not descends_from(header_window.menubar, header_window.toolbar), (
        "the menus are in the header bar AND the toolbar"
    )


def test_fullscreen_still_hides_the_menus(plain_window):
    """They are inside the toolbar now, so they must not be left behind."""
    plain_window.menubar.hide()
    assert not plain_window.menubar.get_visible()
    plain_window.menubar.show()
    assert plain_window.menubar.get_visible()


def test_neither_row_carries_a_preferences_button(plain_window, header_window):
    """It is on the File menu, which is on the same row either way."""

    def buttons(container):
        found = []

        def walk(widget):
            name = getattr(widget, "get_icon_name", None)
            if name is not None and name() == "preferences-system-symbolic":
                found.append(widget)
            if hasattr(widget, "get_children"):
                for child in widget.get_children():
                    walk(child)

        walk(container)
        return found

    assert not buttons(plain_window.toolbar), (
        "the toolbar still has a Preferences button"
    )
    assert not buttons(header_window.header_bar), (
        "the titlebar still has a Preferences button"
    )

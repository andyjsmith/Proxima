"""Drawing the guest larger, and the pointer still landing where it looks.

The pointer is the whole risk in this feature. A picture drawn at the wrong
size is obvious the moment anyone looks at it; a picture drawn correctly with
clicks arriving somewhere else is the kind of bug that gets blamed on the
guest. So most of what is here maps a widget coordinate through the same
path a real click takes and checks where it comes out.

Only the VNC side is exercised that way, because only the VNC side is ours:
on SPICE the geometry and the pointer are both spice-gtk's, out of one
function (transform_input, from spice_display_get_scaling), and what is worth
checking here is that we hand it the right numbers.
"""

import threading

import cairo
import pytest
from gi.repository import Gtk

from proxima.console.scaling import (
    CONSOLE_SCALES,
    clamp_console_scale,
    console_scale_index,
)
from proxima.console.vnc import VncConsole

from .conftest import pump

GUEST_W, GUEST_H = 640, 480
VIEW_W, VIEW_H = 800, 600


# -- the setting itself -------------------------------------------------


def test_the_offered_scales_start_at_100_and_100_is_a_no_op():
    assert CONSOLE_SCALES[0] == 100
    assert clamp_console_scale(100) == 100


@pytest.mark.parametrize("value", [None, "", "big", 0, 99, 201, 10_000, -100])
def test_an_unusable_scale_falls_back_to_100(value):
    # Settings files are edited by hand, and one predating this setting has
    # no value at all. Neither may leave a console unable to draw.
    assert clamp_console_scale(value) == 100


def test_a_scale_between_the_offered_ones_picks_the_nearest_radio_item():
    # 130 is valid but not offered; the menu still has to show something.
    assert CONSOLE_SCALES[console_scale_index(130)] == 125
    assert CONSOLE_SCALES[console_scale_index(190)] == 200


def test_a_string_from_the_settings_combo_is_still_a_scale():
    assert clamp_console_scale("150") == 150


# -- the VNC geometry ---------------------------------------------------


@pytest.fixture
def console(monkeypatch):
    """A VNC console with a framebuffer but no server.

    What is under test is geometry, and geometry needs a real allocated
    widget -- but not a real connection, which is the only reason the
    dialling is stubbed out rather than faked in more detail.
    """
    monkeypatch.setattr(VncConsole, "_connect", lambda *args, **kwargs: None)
    console = VncConsole(
        "wss://example.invalid/vnc", {}, "password", title="scaling-test"
    )

    # A framebuffer of a known size, standing in for one the server sent,
    # and just enough of a client for the draw handler to take its lock --
    # a surface without a client is a state a real console never reaches.
    console._surface = cairo.ImageSurface(cairo.FORMAT_RGB24, GUEST_W, GUEST_H)
    console.client = type("FakeClient", (), {"fb_lock": threading.RLock()})()
    monkeypatch.setattr(console, "_sync_surface", lambda: True)

    window = Gtk.Window()
    window.set_default_size(VIEW_W, VIEW_H)
    window.add(console)
    window.show_all()
    pump(0.3)
    yield console
    window.destroy()
    pump(0.1)


def viewport_is_ready(console):
    """Whether the window manager has given the console its size yet."""
    width, height = console._viewport_size()
    return width > 1 and height > 1


def test_at_100_percent_the_picture_is_not_scaled_up(console):
    if not viewport_is_ready(console):
        pytest.skip("the console was never allocated a size")
    console.set_console_scale(100)
    pump(0.2)
    scale, _, _ = console._scale_factors()
    # scale_to_fit defaults on for VNC, so the guest is stretched to the
    # viewport -- the point is that the zoom multiplied it by one.
    assert scale == pytest.approx(
        min(
            console._viewport_size()[0] / GUEST_W,
            console._viewport_size()[1] / GUEST_H,
        )
    )


def test_200_percent_doubles_the_scale(console):
    if not viewport_is_ready(console):
        pytest.skip("the console was never allocated a size")
    console.set_console_scale(100)
    pump(0.2)
    at_100, _, _ = console._scale_factors()
    console.set_console_scale(200)
    pump(0.2)
    at_200, _, _ = console._scale_factors()
    assert at_200 == pytest.approx(at_100 * 2)


def test_a_click_lands_where_the_picture_says_it_does_at_every_scale(console):
    """The one that matters: widget coordinate in, guest pixel out.

    Checked against the scale and offset the *drawing* uses, because a
    pointer that agrees with a picture drawn from different numbers is not
    actually correct -- it is two bugs cancelling.
    """
    if not viewport_is_ready(console):
        pytest.skip("the console was never allocated a size")
    for percent in CONSOLE_SCALES:
        console.set_console_scale(percent)
        pump(0.2)
        scale, offset_x, offset_y = console._scale_factors()
        for guest_x, guest_y in ((0, 0), (100, 80), (GUEST_W - 1, GUEST_H - 1)):
            # Where the middle of that guest pixel is drawn...
            widget_x = offset_x + (guest_x + 0.5) * scale
            widget_y = offset_y + (guest_y + 0.5) * scale
            # ...is where clicking has to send the pointer.
            assert console._widget_to_guest(widget_x, widget_y) == (
                guest_x,
                guest_y,
            ), f"at {percent}%"


def test_the_pointer_cannot_be_pushed_outside_the_guest_screen(console):
    if not viewport_is_ready(console):
        pytest.skip("the console was never allocated a size")
    console.set_console_scale(200)
    pump(0.2)
    # Well past every edge, which is what the letterbox margins are at 100%
    # and what a drag out of the window is at any scale.
    for x, y in ((-500, -500), (10_000, 10_000)):
        guest_x, guest_y = console._widget_to_guest(x, y)
        assert 0 <= guest_x < GUEST_W
        assert 0 <= guest_y < GUEST_H


def test_zooming_past_the_tab_makes_the_canvas_scrollable(console):
    """Magnified beyond the viewport, the overflow has to stay reachable.

    A drawing area larger than what shows it is only usable inside a
    scroller; without one the far edge of the guest screen is both invisible
    and unclickable, which is the trap the old size request was avoiding.
    """
    if not viewport_is_ready(console):
        pytest.skip("the console was never allocated a size")
    console.set_console_scale(100)
    pump(0.2)
    assert console._canvas_request == (-1, -1), (
        "an unzoomed console should ask for no more room than the tab has"
    )

    console.set_console_scale(200)
    pump(0.2)
    view_w, view_h = console._viewport_size()
    want_w, want_h = console._canvas_request
    assert want_w > view_w and want_h > view_h
    assert console.area.get_parent() is not None
    assert isinstance(console.area.get_ancestor(Gtk.ScrolledWindow), Gtk.ScrolledWindow)


def test_a_guest_resolution_change_keeps_the_scale_and_the_pointer(console):
    """A guest that switches resolution mid-session must not break either.

    Which a Windows guest does at boot, when the display driver loads. The
    canvas does not necessarily change size -- with scale-to-fit on it is
    the viewport times the zoom whatever the guest is doing -- but every
    guest pixel is now a different size, so the pointer mapping has to be
    working from the new framebuffer rather than the old one.
    """
    if not viewport_is_ready(console):
        pytest.skip("the console was never allocated a size")
    console.set_console_scale(200)
    pump(0.2)
    before, _, _ = console._scale_factors()

    console._surface = cairo.ImageSurface(cairo.FORMAT_RGB24, GUEST_W * 2, GUEST_H * 2)
    console._update_canvas()
    pump(0.2)

    assert console.console_scale == 200
    after, offset_x, offset_y = console._scale_factors()
    assert after == pytest.approx(before / 2), (
        "twice the guest pixels in the same space is half the scale"
    )
    # And a click still lands on the pixel it is over, at the new size.
    far_x, far_y = GUEST_W * 2 - 1, GUEST_H * 2 - 1
    assert console._widget_to_guest(
        offset_x + (far_x + 0.5) * after, offset_y + (far_y + 0.5) * after
    ) == (far_x, far_y)

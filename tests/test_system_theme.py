"""Handing the interface back to the desktop.

The whole risk here is the difference between *skipping* our theming and
*undoing* it. Everything the theme layer touches is global to the process --
the GTK theme name, the screen's font options, the stylesheet on the screen
-- so a run that has already applied them keeps them until something puts
them back. A setting that only takes effect at the next start would be a
setting that does nothing when you press it, which is exactly the report
these tests exist to prevent.
"""

import pytest
from gi.repository import Gdk, Gtk

from proxima import theme
from proxima.config import apply_environment

from .conftest import make_config, pump


@pytest.fixture
def screen():
    return Gdk.Screen.get_default()


@pytest.fixture
def restore_theme(screen):
    """Put the process back the way conftest's session fixture left it.

    Theming is global to the process, and this file is the only one that
    changes it -- so every test here has to hand it back, or the next file
    on this worker builds its windows against whatever was left behind.
    """
    yield
    theme.apply(make_config())
    pump(0.2)


def providers_on_screen():
    """How many stylesheets the theme layer currently has installed."""
    return len(theme._providers)


def test_our_stylesheet_is_installed_by_default(restore_theme):
    theme.apply(make_config(use_system_theme=False))
    pump(0.2)
    assert providers_on_screen() == 1
    assert Gtk.Settings.get_default().get_property("gtk-theme-name") == theme.THEME_NAME


def test_the_system_theme_removes_the_stylesheet(restore_theme):
    theme.apply(make_config(use_system_theme=False))
    pump(0.2)
    theme.apply(make_config(use_system_theme=True))
    pump(0.2)
    assert providers_on_screen() == 0, "the compact stylesheet is still on the screen"
    assert theme.system_theme_active() is True


def test_the_pinned_theme_is_given_back_not_merely_left_alone(restore_theme):
    """The desktop's own theme name has to be restored.

    Ours is set on every apply(), so by the time anyone asks for the
    original it has already gone -- which is why it is captured up front
    rather than read on demand.
    """
    settings = Gtk.Settings.get_default()
    original = settings.get_property("gtk-theme-name")

    theme.apply(make_config(use_system_theme=False))
    pump(0.2)
    theme.apply(make_config(use_system_theme=True))
    pump(0.2)

    assert settings.get_property("gtk-theme-name") == original


def test_font_options_are_handed_back(restore_theme, screen):
    """Ours are pushed onto the screen, so ours have to be taken off it."""
    theme.apply(make_config(use_system_theme=False, hint_metrics=True))
    pump(0.2)
    assert screen.get_font_options() is not None, "our own options were never applied"

    theme.apply(make_config(use_system_theme=True))
    pump(0.2)
    assert screen.get_font_options() is None, (
        "the screen still carries font options of ours"
    )


def test_turning_it_off_again_puts_everything_back(restore_theme):
    """The setting has to work in both directions, live."""
    theme.apply(make_config(use_system_theme=True))
    pump(0.2)
    theme.apply(make_config(use_system_theme=False))
    pump(0.2)

    assert providers_on_screen() == 1
    assert Gtk.Settings.get_default().get_property("gtk-theme-name") == theme.THEME_NAME
    assert theme.system_theme_active() is False


def test_the_compact_row_padding_follows_the_setting(restore_theme):
    """A cell renderer's padding is a property, not CSS.

    So it does not come back on its own when the stylesheet is dropped, and
    a tree left at the compact padding under the desktop's theme is the one
    part of the window that still looks like ours.
    """
    theme.apply(make_config(use_system_theme=False))
    assert theme.current_row_ypad() == theme.COMPACT_ROW_YPAD
    theme.apply(make_config(use_system_theme=True))
    assert theme.current_row_ypad() == theme.NATIVE_ROW_YPAD


def test_the_dark_flag_comes_from_the_desktop_not_from_our_setting(restore_theme):
    """Widgets that draw their own colours still have to be told which way.

    They have no theme to ask. In system mode our own light/dark choice is
    not applying to anything, so answering from it would leave custom-drawn
    icons light on a dark desktop.
    """
    theme.apply(make_config(use_system_theme=False, color_mode="dark"))
    assert theme.current_dark() is True

    theme.apply(make_config(use_system_theme=True, color_mode="dark"))
    assert theme.current_dark() == bool(theme.system.system_prefers_dark()), (
        "the dark flag still follows our own colour setting"
    )


def test_the_pango_backend_is_left_alone(monkeypatch):
    """The one part that cannot be undone, so it must never be done.

    Pango reads PANGOCAIRO_BACKEND once, when it builds its default fontmap,
    and never looks again -- so unlike everything else here this cannot be
    handed back later. It has to not be set in the first place.
    """
    monkeypatch.delenv("PANGOCAIRO_BACKEND", raising=False)
    apply_environment(make_config(use_system_theme=True, font_backend="fontconfig"))
    import os

    assert "PANGOCAIRO_BACKEND" not in os.environ

    apply_environment(make_config(use_system_theme=False, font_backend="fontconfig"))
    assert os.environ.get("PANGOCAIRO_BACKEND") == "fontconfig"

"""Theming: the compact stylesheet, light/dark, font rendering.

The GTK theme itself is not a choice. Adwaita is what the compact stylesheet
is written against, what the symbolic icons are drawn for, and the only one
a packaged build carries -- so it is pinned rather than inherited from the
desktop, which on Linux could otherwise be anything and would take the
layout with it. Light and dark remain a choice; both are Adwaita.
"""

import logging

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from . import css, fonts, native_chrome, system

log = logging.getLogger(__name__)

# Compiled into GTK, so it is always there and needs no discovery.
THEME_NAME = "Adwaita"

# The tree row padding the compact stylesheet is drawn around, and GTK's own,
# used when the desktop's theme is left in charge. A cell renderer's padding
# is a property rather than CSS, so it does not come back on its own when the
# stylesheet is dropped.
COMPACT_ROW_YPAD = css.ROW_YPAD
NATIVE_ROW_YPAD = 2

_providers = []
_dark = False
_system_theme = False

# What GTK had before anything here touched it, captured once. Restoring the
# desktop's own theme means knowing its name, and by the time anyone asks,
# apply() has already overwritten it.
_original = {}


def _remember(settings):
    if _original:
        return
    _original["gtk-theme-name"] = settings.get_property("gtk-theme-name")
    _original["gtk-application-prefer-dark-theme"] = settings.get_property(
        "gtk-application-prefer-dark-theme"
    )


def current_row_ypad():
    """Tree row padding matching the last apply()."""
    return NATIVE_ROW_YPAD if _system_theme else COMPACT_ROW_YPAD


def system_theme_active():
    """Whether the desktop's own theme is currently in charge."""
    return _system_theme


def _load(screen, stylesheet, label):
    """Add one stylesheet. A failure here must not affect the others."""
    provider = Gtk.CssProvider()
    try:
        provider.load_from_data(stylesheet.encode("utf-8"))
    except GLib.Error as exc:
        log.warning("%s CSS not applied: %s", label, exc.message)
        return False
    Gtk.StyleContext.add_provider_for_screen(
        screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _providers.append(provider)
    return True


def current_dark():
    """The dark flag from the last apply(), for windows created later."""
    return _dark


def keep_active(window):
    """Stop a window from washing out when it loses focus.

    GTK3 sets GTK_STATE_FLAG_BACKDROP on a toplevel that is not active and
    propagates it to every child; themes then draw that state in muted
    colours. There is no setting for it, so the flag itself is cleared as
    fast as it arrives.

    Clearing the flag rather than restyling the state is what makes this
    safe. Every theme rule keyed on :backdrop simply stops matching, so
    widgets keep their ordinary appearance -- including insensitive ones,
    whose own flag is untouched. Restating colours in CSS could not manage
    that: application CSS outranks the theme, so it took the disabled
    styling with it.

    **The clearing has to wait for an idle.** GTK emits state-flags-changed
    on the toplevel *before* it has finished walking the children. Clearing
    the flag from inside that handler starts a nested walk that does clear
    them -- and then the outer walk resumes and puts BACKDROP back on every
    child. The window then reads as active while everything inside it reads
    as backdrop, permanently, because the window's own flag is already
    right and nothing will disturb it again. That is worse than the
    dimming: it looks like the entire interface is disabled.

    Deferring means the flag is cleared once the propagation has finished,
    where the unset propagates properly. The idle runs above the redraw, so
    the frame that would have been drawn dimmed never is -- see schedule().
    """
    state = {"queued": False, "dead": False}

    def clear():
        state["queued"] = False
        # Checked here rather than when the handler was connected, so that
        # turning the setting on or off reaches windows that already exist.
        # A backdrop window really does dim on GNOME; leaving it to do so is
        # part of what "the desktop's own theme" means.
        if state["dead"] or _system_theme:
            return False
        if window.get_state_flags() & Gtk.StateFlags.BACKDROP:
            window.unset_state_flags(Gtk.StateFlags.BACKDROP)
        return False

    def schedule(*_args):
        if state["queued"] or state["dead"]:
            return
        state["queued"] = True
        # Above GDK_PRIORITY_REDRAW (HIGH_IDLE + 20), so the flag is gone
        # before the frame that would have shown it dimmed is painted. At the
        # default idle priority (200) the redraw ran first and the window was
        # drawn in backdrop colours for a frame -- which the theme's own CSS
        # transitions then stretched into a visible fade out and back. Still
        # a separate turn of the main loop, so the propagation problem the
        # docstring describes is still avoided.
        GLib.idle_add(clear, priority=GLib.PRIORITY_HIGH_IDLE)

    window.connect("state-flags-changed", schedule)
    # Belt and braces: the flag arrives with the window deactivating, and
    # this is the signal that says so in as many words.
    window.connect("notify::is-active", schedule)
    window.connect("destroy", lambda *_: state.update(dead=True))
    schedule()
    return window


def decorate(window):
    """Match a window's native titlebar to the theme, and keep it active.

    Every dialog is its own toplevel, so neither the main window's titlebar
    colour nor its backdrop handling reaches them -- each one has to be
    decorated as it is created. Windows that are not realised yet are
    handled on realize, since the HWND does not exist before then.
    """

    def apply_now(*_args):
        native_chrome.apply_dark_titlebar(window, dark=_dark)
        return False

    keep_active(window)
    if window.get_realized():
        apply_now()
    else:
        window.connect("realize", apply_now)
    return window


def decorate_all():
    """Re-decorate every live toplevel, e.g. after light/dark changes."""
    for window in Gtk.Window.list_toplevels():
        if window.get_realized():
            native_chrome.apply_dark_titlebar(window, dark=_dark)


def apply(config, root_widget=None):
    """Apply the stylesheet, font rendering and titlebar in one pass.

    Returns whether the result is dark, which is what callers have to pass
    on to anything that draws its own colours.

    With `use_system_theme` set this hands the interface back to the desktop
    instead: no stylesheet of ours, no pinned theme, no font overrides, and
    no backdrop clearing. Everything is undone rather than merely skipped,
    so the setting takes effect the moment it is changed rather than at the
    next start -- except the Pango backend, which is an environment variable
    its library reads once (see config.apply_environment).

    The dark flag still has to be answered, because widgets that draw their
    own colours have no theme to ask. In system mode it comes from the
    desktop rather than from our own light/dark setting, which is not
    applying to anything any more.
    """
    global _dark, _system_theme

    screen = Gdk.Screen.get_default()
    settings = Gtk.Settings.get_default()
    _remember(settings)

    _system_theme = bool(config.get("use_system_theme"))
    dark = (
        bool(system.system_prefers_dark())
        if _system_theme
        else system.resolve_dark(config.get("color_mode", "system"))
    )
    _dark = dark

    # Ours come off first either way: apply() is re-run on every settings
    # change, and a stylesheet added twice is a stylesheet applied twice.
    for existing in _providers:
        Gtk.StyleContext.remove_provider_for_screen(screen, existing)
    _providers.clear()

    if _system_theme:
        settings.set_property("gtk-theme-name", _original["gtk-theme-name"])
        settings.set_property(
            "gtk-application-prefer-dark-theme",
            _original["gtk-application-prefer-dark-theme"],
        )
        fonts.restore_font_options(screen, root_widget)
    else:
        settings.set_property("gtk-theme-name", THEME_NAME)
        # Adwaita's dark variant is selected by this flag rather than by name.
        settings.set_property("gtk-application-prefer-dark-theme", dark)
        _load(screen, css.build_css(dark), "compact")
        fonts.apply_font_options(screen, config, root_widget)

    # Covers the main window and any dialog that happens to be open.
    decorate_all()

    return dark


__all__ = [
    "COMPACT_ROW_YPAD",
    "NATIVE_ROW_YPAD",
    "THEME_NAME",
    "apply",
    "css",
    "current_dark",
    "current_row_ypad",
    "decorate",
    "decorate_all",
    "fonts",
    "keep_active",
    "native_chrome",
    "system",
    "system_theme_active",
]

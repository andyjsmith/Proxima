"""Theming: the compact stylesheet, base theme selection, font rendering."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib  # noqa: E402

from . import css, fonts, discovery, native_chrome, system  # noqa: E402

_providers = []
_dark = False


def _load(screen, stylesheet, label):
    """Add one stylesheet. A failure here must not affect the others."""
    provider = Gtk.CssProvider()
    try:
        provider.load_from_data(stylesheet.encode("utf-8"))
    except GLib.Error as exc:
        print(f"[theme] {label} CSS not applied: {exc.message}")
        return False
    Gtk.StyleContext.add_provider_for_screen(
        screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
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

    Unsetting re-emits the signal with the flag already gone, so the guard
    below terminates it rather than looping.
    """
    def on_state_change(widget, _old_flags):
        if widget.get_state_flags() & Gtk.StateFlags.BACKDROP:
            widget.unset_state_flags(Gtk.StateFlags.BACKDROP)

    window.connect("state-flags-changed", on_state_change)
    on_state_change(window, 0)
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
    """Re-decorate every live toplevel, e.g. after the theme changes."""
    for window in Gtk.Window.list_toplevels():
        if window.get_realized():
            native_chrome.apply_dark_titlebar(window, dark=_dark)


def apply(config, root_widget=None):
    """Apply theme, density CSS, font rendering and titlebar in one pass.

    Returns the resolved (gtk_theme_name, dark) so callers can report it.
    """
    global _dark

    screen = Gdk.Screen.get_default()
    settings = Gtk.Settings.get_default()

    dark = system.resolve_dark(config.get("color_mode", "system"))
    _dark = dark
    theme_name, available = discovery.resolve_theme(
        config.get("theme", "Adwaita"), dark)

    settings.set_property("gtk-theme-name", theme_name)
    settings.set_property("gtk-application-prefer-dark-theme", dark)

    for existing in _providers:
        Gtk.StyleContext.remove_provider_for_screen(screen, existing)
    _providers.clear()

    _load(screen, css.build_css(dark), "compact")

    fonts.apply_font_options(screen, config, root_widget)

    # Covers the main window and any dialog that happens to be open.
    decorate_all()

    if not available:
        print(f"[theme] {config.get('theme')} is not installed; "
              f"using {theme_name}")
    return theme_name, dark


__all__ = ["apply", "decorate", "decorate_all", "keep_active",
           "css", "fonts", "discovery", "native_chrome", "system"]

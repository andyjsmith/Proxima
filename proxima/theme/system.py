"""Whether the desktop is currently in dark mode, and when that changes.

Three sources, in the order they are trusted:

  * Windows: HKCU\\...\\Themes\\Personalize\\AppsUseLightTheme. This is the
    value File Explorer itself reads, and it updates the instant the user
    flips the setting.
  * The XDG settings portal's org.freedesktop.appearance color-scheme, which
    is the modern cross-desktop answer and works under Flatpak too.
  * GSettings org.gnome.desktop.interface color-scheme, then gtk-theme, as
    the fallback for desktops without a portal.

Polling is used rather than change signals: it costs one registry read or one
GSettings lookup every few seconds, and it avoids depending on a D-Bus main
loop integration that may not exist on Windows at all.
"""

import os

_POLL_SECONDS = 5


def _windows_dark():
    try:
        import winreg
    except ImportError:
        return None
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        with key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return value == 0
    except (OSError, FileNotFoundError):
        return None


def _portal_dark():
    """org.freedesktop.appearance color-scheme: 1 means prefer dark."""
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except Exception:
        return None
    try:
        proxy = Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SESSION,
            Gio.DBusProxyFlags.NONE,
            None,
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.Settings",
            None,
        )
        result = proxy.call_sync(
            "Read",
            GLib.Variant("(ss)", ("org.freedesktop.appearance", "color-scheme")),
            Gio.DBusCallFlags.NONE,
            1000,
            None,
        )
        value = result.unpack()[0]
        # Nested variants are common here depending on portal version.
        while isinstance(value, tuple) and value:
            value = value[0]
        return int(value) == 1
    except Exception:
        return None


def _gsettings_dark():
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio
    except Exception:
        return None

    try:
        source = Gio.SettingsSchemaSource.get_default()
        if source is None:
            return None
        schema = source.lookup("org.gnome.desktop.interface", True)
        if schema is None:
            return None
        settings = Gio.Settings.new("org.gnome.desktop.interface")
        # The schema's key list, not Gio.Settings.list_keys(): the latter is
        # deprecated, and the schema is already in hand from the lookup.
        keys = schema.list_keys()
        if "color-scheme" in keys:
            scheme = settings.get_string("color-scheme")
            if scheme == "prefer-dark":
                return True
            if scheme == "prefer-light":
                return False
        if "gtk-theme" in keys:
            return settings.get_string("gtk-theme").lower().endswith("-dark")
    except Exception:
        return None
    return None


def _env_dark():
    """Last resort: some desktops only advertise it through the environment."""
    for var in ("GTK_THEME",):
        value = os.environ.get(var, "")
        if ":dark" in value.lower() or value.lower().endswith("-dark"):
            return True
    return None


def system_prefers_dark():
    """True, False, or None when the platform will not say."""
    for probe in (_windows_dark, _portal_dark, _gsettings_dark, _env_dark):
        result = probe()
        if result is not None:
            return result
    return None


def resolve_dark(color_mode):
    """Turn the 'system' / 'light' / 'dark' setting into a boolean."""
    if color_mode == "dark":
        return True
    if color_mode == "light":
        return False
    detected = system_prefers_dark()
    return bool(detected)


class DarkModeWatcher:
    """Calls back on the GLib main loop whenever the system preference flips."""

    def __init__(self, on_change, interval=_POLL_SECONDS):
        from gi.repository import GLib

        self._glib = GLib
        self._on_change = on_change
        self._state = system_prefers_dark()
        self._source = GLib.timeout_add_seconds(interval, self._poll)

    def _poll(self):
        current = system_prefers_dark()
        if current is not None and current != self._state:
            self._state = current
            self._on_change(current)
        return True  # keep the timeout alive

    def stop(self):
        if self._source:
            self._glib.source_remove(self._source)
            self._source = None

"""The window icon, in a checkout as well as in a package.

GTK finds an application's icon through the icon theme, which only works once
the program is installed somewhere the theme looks. From a source checkout it
is nowhere, so the file is found by hand and set as the default for every
window the process opens.
"""

import logging
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

log = logging.getLogger(__name__)

ICON_NAME = "proxima.png"


def icon_file():
    """Wherever this copy's icon is, or None if it has none."""
    here = Path(__file__).resolve().parent
    executable = Path(sys.executable).resolve().parent
    for candidate in (
        # A source checkout: the icon lives with the packaging assets.
        here.parent.parent / "packaging" / ICON_NAME,
        # A bundle: installed into the icon theme it carries.
        executable / "share" / "icons" / "hicolor" / "256x256" / "apps" / ICON_NAME,
        executable / ICON_NAME,
        # An installed copy on Linux.
        Path("/usr/share/icons/hicolor/256x256/apps") / ICON_NAME,
    ):
        if candidate.is_file():
            return candidate
    return None


def apply_default_icon():
    """Give every window this process opens the application icon."""
    path = icon_file()
    if path is None:
        return None
    try:
        Gtk.Window.set_default_icon_from_file(str(path))
    except Exception as exc:  # a corrupt or unreadable file is not fatal
        log.warning("could not load %s: %s", path, exc)
        return None
    return path

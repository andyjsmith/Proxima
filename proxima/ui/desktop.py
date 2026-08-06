"""Handing a folder or a link to the desktop to open.

GIO is the portable way to do this and it is the wrong way on Windows. It
routes everything through g_app_info_launch_default_for_uri(), which looks
the target up in its own registry of handlers -- and on Windows that
registry has nothing for a directory at all:

    g-io-error-quark: No application is registered as handling this file

even for a folder that plainly exists and that Explorer opens on a
double click. Web links are fine, because a browser does register itself
for the http and https schemes, so the split below is not paranoia about
GIO in general: it is about the one case that does not work.

So folders go to the platform's own opener -- ShellExecute via
os.startfile on Windows, `open` on macOS, GIO then xdg-open elsewhere --
and links stay with GIO, which is what respects the user's chosen browser.
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk

log = logging.getLogger(__name__)


def _launch(command):
    try:
        subprocess.Popen(command, close_fds=True)
        return True
    except OSError as exc:
        log.warning("could not run %s: %s", command[0], exc)
        return False


def _gio_open(uri, parent=None):
    try:
        return bool(Gtk.show_uri_on_window(parent, uri, Gdk.CURRENT_TIME))
    except Exception as exc:
        log.info("GIO would not open %s: %s", uri, exc)
        return False


def open_folder(path, parent=None):
    """Show a directory in the file manager. Returns whether it opened."""
    path = Path(path)
    if not path.exists():
        log.warning("cannot open %s: it does not exist", path)
        return False

    if os.name == "nt":
        # ShellExecute, which is what a double click in Explorer does.
        try:
            os.startfile(str(path))  # noqa: S606 -- a directory we built
            return True
        except OSError as exc:
            log.warning("could not open %s: %s", path, exc)
            return False

    if sys.platform == "darwin":
        return _launch(["open", str(path)])

    # A Linux desktop registers a file manager properly, so GIO is right
    # here; xdg-open covers the ones that do not.
    #
    # as_uri() raises rather than returns for a relative path, and the path
    # comes from the caller -- so a failure to name it is one more reason to
    # try the next opener, which takes a plain path anyway.
    try:
        uri = path.as_uri()
    except ValueError:
        uri = None
    if uri is not None and _gio_open(uri, parent):
        return True
    return _launch(["xdg-open", str(path)])


def open_uri(uri, parent=None):
    """Open a link in the user's browser. Returns whether it opened."""
    if _gio_open(uri, parent):
        return True
    if os.name == "nt":
        try:
            os.startfile(uri)  # noqa: S606 -- a link we built
            return True
        except OSError as exc:
            log.warning("could not open %s: %s", uri, exc)
            return False
    if sys.platform == "darwin":
        return _launch(["open", uri])
    return _launch(["xdg-open", uri])

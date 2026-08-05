"""Getting at spice-gtk through introspection.

Split out from spice.py because the USB redirection code needs the same
namespaces and the same signal workaround, and importing the console module
to reach them would be a circle.
"""

import gi

gi.require_version("Gtk", "3.0")

from gi.repository import GObject


def _import_namespace(candidates):
    """Return the first typelib that loads, and the name it loaded under."""
    for name, version in candidates:
        try:
            gi.require_version(name, version)
            module = __import__("gi.repository", fromlist=[name])
            return getattr(module, name), name
        except Exception:
            continue
    return None, None


# The introspection namespace is spelled SpiceClientGLib in most builds but
# SpiceClientGlib in some, so probe rather than assume.
SpiceGLib, SPICE_GLIB_NS = _import_namespace(
    [("SpiceClientGLib", "2.0"), ("SpiceClientGlib", "2.0")]
)
SpiceGtk, SPICE_GTK_NS = _import_namespace([("SpiceClientGtk", "3.0")])

AVAILABLE = SpiceGLib is not None and SpiceGtk is not None


def connect_signal(obj, name, handler):
    """Always attach a GObject signal, never a shadowing Spice method.

    spice_session_connect/disconnect collide with GObject.Object's connect
    and disconnect, and which one wins depends on the PyGObject build.
    """
    return GObject.Object.connect(obj, name, handler)

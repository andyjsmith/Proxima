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


# What a session that cannot be built actually means, said in terms of the
# thing to go and fix rather than in terms of GObject's type system.
MISSING_LIBRARY = (
    "spice-gtk's typelib is present but its library is not, so no SPICE "
    "type could be created. A packaged build is missing "
    "libspice-client-glib; run 'proxima --diagnose' for the detail."
)


def selftest():
    """(ok, detail) for "can this installation actually build a session?".

    Importing the namespace is not the same question, and the difference is
    exactly the trap: a typelib with no library behind it imports perfectly
    and then fails to produce a single object. Construction is the only
    honest test, and it costs one throwaway object.
    """
    if SpiceGLib is None:
        return False, "the SpiceClientGLib typelib is not installed"
    try:
        SpiceGLib.Session()
    except TypeError as exc:
        # "could not get a reference to type class" -- the GType resolved to
        # void because g_typelib_symbol() found no library to ask.
        return False, f"{MISSING_LIBRARY} ({exc})"
    except Exception as exc:
        return False, f"could not create a SPICE session: {exc}"
    if SpiceGtk is None:
        return False, "the SpiceClientGtk typelib is not installed"
    if getattr(SpiceGtk, "Display", None) is None:
        return False, "spice-gtk has no Display widget"
    return True, "a session and a display widget can be created"

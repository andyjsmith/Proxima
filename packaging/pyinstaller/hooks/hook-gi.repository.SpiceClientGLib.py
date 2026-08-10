"""Collect the SPICE GLib typelib and the libraries it names.

PyInstaller ships hooks for the GNOME stack but not for spice-gtk, and
proxima/console/spicelib.py imports the namespace through importlib, so
nothing in the module graph names it either. GiModuleInfo does the same work
the stock gi hooks do: find the .typelib, find the shared libraries it names,
and pull in the namespaces it depends on.

Finding the libraries is the half worth spelling out. A typelib is data, not
code: SpiceClientGLib-2.0.typelib merely *names* libspice-client-glib, and
nothing in the program links against it. A bundle that carries the typelib
alone still imports -- so every "is SPICE available?" check says yes -- and
only when something asks for an actual GType does g_typelib_symbol() come up
empty, every type resolve to void, and constructing one raise

    TypeError: could not get a reference to type class

from somewhere with no obvious connection to a missing file. That is what
--diagnose's "SPICE session usable" line exists to catch.

The namespace is spelled SpiceClientGLib in most builds and SpiceClientGlib
in some, which spicelib.py probes for rather than assuming. Both spellings
are tried here rather than in two hook files, because macOS's filesystem is
case-insensitive and the two names would be one file anyway; PyInstaller
finds this file for either import for the same reason.
"""

from PyInstaller.utils.hooks.gi import GiModuleInfo

for _namespace in ("SpiceClientGLib", "SpiceClientGlib"):
    module_info = GiModuleInfo(_namespace, "2.0")
    if module_info.available:
        binaries, datas, hiddenimports = module_info.collect_typelib_data()
        break

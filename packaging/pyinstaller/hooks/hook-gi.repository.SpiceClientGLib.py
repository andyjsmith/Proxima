"""Collect the SPICE GLib typelib and the libraries it names.

PyInstaller ships hooks for the GNOME stack but not for spice-gtk, and
proxima/console/spicelib.py imports the namespace through importlib, so
nothing in the module graph names it either. GiModuleInfo does the same work
the stock gi hooks do: find the .typelib, find the shared libraries it names
-- see tools/bundle_deps.py's docstring for why a typelib without its library
fails so obscurely -- and pull in the namespaces it depends on.

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

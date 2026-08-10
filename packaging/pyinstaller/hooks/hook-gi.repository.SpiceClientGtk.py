"""Collect the SPICE GTK widget typelib and its libraries.

See hook-gi.repository.SpiceClientGLib.py for why this is not automatic.
"""

from PyInstaller.utils.hooks.gi import GiModuleInfo

module_info = GiModuleInfo("SpiceClientGtk", "3.0")
if module_info.available:
    binaries, datas, hiddenimports = module_info.collect_typelib_data()

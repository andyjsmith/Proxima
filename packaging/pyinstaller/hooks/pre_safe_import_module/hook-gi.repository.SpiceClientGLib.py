"""Make gi.repository.SpiceClientGLib a module the graph will admit exists.

A gi namespace is not a file on disk, so modulegraph records it as a
MissingModule and the standard hook beside this one is never reached --
"Hidden import 'gi.repository.SpiceClientGLib' not found", and the typelib
and its library are silently left out of the bundle. PyInstaller's own gi
hooks each carry one of these; spice-gtk gets no hook from PyInstaller, so
it needs ours.
"""


def pre_safe_import_module(api):
    api.add_runtime_module(api.module_name)

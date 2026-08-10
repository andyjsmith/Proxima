"""See hook-gi.repository.SpiceClientGLib.py beside this one."""


def pre_safe_import_module(api):
    api.add_runtime_module(api.module_name)

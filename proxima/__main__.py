"""python -m proxima

This module exists to get the ordering right: a couple of settings are read
by libraries exactly once, when they first initialise, so they have to be in
the environment before `gi` is imported anywhere. Nothing above the
apply_environment() call may import gi, directly or transitively.
"""

import sys


def _log_level(argv):
    """--log-level=debug, or --debug for the same thing in fewer letters."""
    for arg in argv:
        if arg.startswith("--log-level="):
            return arg.split("=", 1)[1]
    return "DEBUG" if "--debug" in argv else None


def main():
    from . import bundle, logs
    from .config import Config, apply_environment

    # Answered before the log file is opened, so that asking where the logs
    # go does not itself write one.
    if "--logs" in sys.argv:
        print(logs.log_dir())
        return 0

    # Before anything that could fail, so that it can be logged when it
    # does. A packaged Windows build has no stderr at all, which makes the
    # file this opens the only account of the run there will ever be.
    logs.setup(level=_log_level(sys.argv))
    logs.log_environment()

    # A packaged build has to be told where its own GTK data went. Does
    # nothing from a source checkout.
    bundle.apply()

    config = Config.load()

    # Command line switches win over the stored settings, which is what makes
    # them useful for one-off debugging.
    if "--fontconfig" in sys.argv:
        config["font_backend"] = "fontconfig"
    elif "--win32-fonts" in sys.argv:
        config["font_backend"] = "win32"
    if "--sw-decoders" in sys.argv:
        config["sw_decoders"] = True

    apply_environment(config)

    from .app import Application
    from .app import main as app_main

    if "--diagnose" in sys.argv:
        return app_main(sys.argv)
    return Application(config).run()


if __name__ == "__main__":
    sys.exit(main() or 0)

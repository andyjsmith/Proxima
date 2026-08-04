"""python -m proxima

This module exists to get the ordering right: a couple of settings are read
by libraries exactly once, when they first initialise, so they have to be in
the environment before `gi` is imported anywhere. Nothing above the
apply_environment() call may import gi, directly or transitively.
"""

import sys


def main():
    from . import bundle
    from .config import Config, apply_environment

    # First of all, and before the settings are even read: a packaged build
    # has to be told where its own GTK data went. Does nothing from a source
    # checkout.
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

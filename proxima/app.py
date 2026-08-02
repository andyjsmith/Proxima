"""Application entry point.

The window opens straight away with no connection. Saved connections are
dialled in the background afterwards, so a server that is slow or down delays
nothing and fails visibly in the tree rather than behind a modal dialog.
"""

import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from . import APP_NAME
from .config import Config
from .theme import apply as apply_theme
from .ui import MainWindow


class Application:
    def __init__(self, config=None):
        self.config = config or Config.load()
        self.window = None

    def run(self):
        GLib.set_application_name(APP_NAME)
        GLib.set_prgname("proxima")

        apply_theme(self.config)

        self.window = MainWindow(self.config)
        self.window.show_all()
        # Saved servers connect once the window is on screen.
        GLib.idle_add(self.window.connect_saved)
        Gtk.main()
        return 0


def main(argv=None):
    argv = sys.argv if argv is None else argv
    config = Config.load()

    if "--diagnose" in argv:
        from .console import SPICE_AVAILABLE, VNC_AVAILABLE
        from .console.decoders import gstreamer_report
        from .theme import discovery

        discovery.diagnose()
        print("--- GStreamer ---")
        for line in gstreamer_report():
            print(line)
        print(f"\nSPICE widget available: {SPICE_AVAILABLE}")
        print(f"VNC fallback available: {VNC_AVAILABLE}")
        return 0

    return Application(config).run()

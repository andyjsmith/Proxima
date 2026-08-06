"""Application entry point.

The window opens straight away with no connection. Saved connections are
dialled in the background afterwards, so a server that is slow or down delays
nothing and fails visibly in the tree rather than behind a modal dialog.
"""

import logging
import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from . import APP_NAME, __version__, logs
from .config import Config
from .theme import apply as apply_theme
from .ui import MainWindow
from .ui.appicon import apply_default_icon

log = logging.getLogger(__name__)


class Application:
    def __init__(self, config=None):
        self.config = config or Config.load()
        self.window = None

    def run(self):
        GLib.set_application_name(APP_NAME)
        GLib.set_prgname("proxima")

        # Now that gi is loaded: GTK, GStreamer and spice-gtk log through
        # GLib, and in a packaged build those messages have nowhere to go.
        logs.bridge_glib(verbose=logs.verbose())

        apply_theme(self.config)
        # Before any window is built: GTK reads the default icon when a
        # window is realised, not afterwards.
        apply_default_icon()

        self.window = MainWindow(self.config)
        self.window.show_all()
        # Saved servers connect once the window is on screen.
        GLib.idle_add(self.window.connect_saved)
        # And the update check after those, on a timer rather than an idle:
        # connecting is what the user is waiting for, and a release note
        # dialog opening over a half-drawn window is a poor greeting.
        GLib.timeout_add_seconds(3, self._check_for_updates)

        log.info("entering the main loop")
        Gtk.main()
        log.info("main loop finished")
        logging.shutdown()
        return 0

    def _check_for_updates(self):
        if self.window is not None:
            self.window.check_for_updates(automatic=True)
        return False  # once


def main(argv=None):
    argv = sys.argv if argv is None else argv
    config = Config.load()

    if "--diagnose" in argv:
        from . import bundle
        from .console import SPICE_AVAILABLE, VNC_AVAILABLE
        from .console.decoders import gstreamer_report
        from .theme import discovery

        # First, and with the interpreter: a bundle that cannot find its own
        # pyproject reports 0.0.0+unknown here, and the python it was built
        # against is what decides which standard library the bundle has.
        print(f"{APP_NAME} {__version__} (python {sys.version.split()[0]})")
        print(f"logs: {logs.current_log_file() or logs.log_dir()}")
        discovery.diagnose()
        for line in bundle.report():
            print(line)
        print()
        print("--- GStreamer ---")
        for line in gstreamer_report():
            print(line)
        print(f"\nSPICE widget available: {SPICE_AVAILABLE}")
        print(f"VNC fallback available: {VNC_AVAILABLE}")
        return 0

    return Application(config).run()

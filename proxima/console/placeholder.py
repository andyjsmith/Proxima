"""A console tab for a guest that is not running.

Opening a console used to be refused for a stopped guest, which meant the
set of tabs you could arrange was dictated by what happened to be powered on.
This stands in instead: the tab exists, says why there is no picture, and is
swapped for a real console as soon as the guest starts.
"""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from .status_panel import ConsoleStatusPanel

TITLES = {
    "stopped": "Guest is stopped",
    "suspended": "Guest is suspended",
    "paused": "Guest is paused",
    "connecting": "Connecting...",
}

DETAILS = {
    "stopped": "The console will connect when the guest starts.",
    "suspended": "Resume the guest to connect.",
    "paused": "Resume the guest to connect.",
    "connecting": "Fetching a console ticket from Proxmox.",
}

ICONS = {
    "paused": "media-playback-pause-symbolic",
    "suspended": "media-playback-pause-symbolic",
    "connecting": "content-loading-symbolic",
}

# States the user cannot act on, so no Reconnect button is offered.
NO_RECONNECT = ("stopped", "suspended", "paused", "connecting")


class PlaceholderConsole(Gtk.Box):
    """Holds a tab open for a guest with nothing to display yet."""

    protocol = "offline"
    agent_connected = False
    connected = False
    pending = False

    supports = {
        "auto_resize": False,
        "scaling": False,
        "codec": False,
        "compression": False,
        "refresh": False,
        "ctrl_alt_del": False,
    }

    def __init__(self, title="console", status="stopped", on_reconnect=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.title = title
        self.on_reconnect = on_reconnect or (lambda: None)
        self.last_status = ""

        self.status_panel = ConsoleStatusPanel(on_reconnect=lambda: self.on_reconnect())
        self.pack_start(self.status_panel, True, True, 0)
        self.show_guest_state(status)

    def show_pending_state(self, title, detail=""):
        self.pending = True
        self.status_panel.show_message(title, detail, can_reconnect=False, busy=True)

    def clear_pending_state(self):
        self.pending = False

    def show_guest_state(self, status):
        self.last_status = status
        self.status_panel.show_message(
            TITLES.get(status, f"Guest is {status}"),
            DETAILS.get(status, ""),
            icon=ICONS.get(status, "media-playback-stop-symbolic"),
            can_reconnect=status not in NO_RECONNECT,
            # "Connecting..." is a wait; "stopped" is a result.
            busy=status == "connecting",
        )

    def show_choice_state(self, title, detail, actions, icon="dialog-warning-symbolic"):
        """Something needs a decision before this tab can become a console.

        Used when another client already holds the guest's SPICE session:
        the tab explains itself and carries the ways out, rather than a
        dialog appearing over whatever the user is doing -- this state is
        usually reached by an automatic reconnect, not by a click.
        """
        self.last_status = "choice"
        self.status_panel.show_message(
            title, detail, icon=icon, can_reconnect=False, actions=actions
        )

    def show_error_state(self, message):
        """Opening failed. Say so in the tab and offer another go."""
        self.last_status = "error"
        self.status_panel.show_message(
            "Could not open the console",
            message,
            icon="dialog-error-symbolic",
            can_reconnect=True,
        )

    # -- the console interface, inert ----------------------------------

    def telemetry(self):
        return None

    def send_ctrl_alt_del(self):
        return False

    def grab_focus_display(self):
        pass

    def release_input(self):
        pass

    def screenshot(self, _path):
        return False

    def shutdown(self):
        pass

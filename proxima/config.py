"""Persistent settings.

Deliberately dependency-free and importable *before* gi, because a couple of
the values it holds (the Pango backend in particular) only take effect if they
are pushed into the environment before GTK loads.
"""

import copy
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# Bumped when a stored setting needs rewriting rather than merely defaulting.
CONFIG_VERSION = 1

DEFAULTS = {
    "config_version": CONFIG_VERSION,
    # -- connection ----------------------------------------------------
    "host": "",
    "username": "root",
    "realm": "pam",
    # host:port -> the certificate fingerprint approved for it. There is no
    # setting for "do not check": an unrecognised certificate is shown to
    # the user once and pinned from then on. See api/certs.py.
    "trusted_certs": {},
    "save_credentials": False,  # ticket only; never the password
    # -- appearance ----------------------------------------------------
    # The GTK theme is not a setting: the stylesheet and the icons are drawn
    # for Adwaita and it is the only one a packaged build carries. Light and
    # dark are still a choice, and both are Adwaita.
    "color_mode": "system",  # "system" | "light" | "dark"
    # Draw the titlebar ourselves (GtkHeaderBar) instead of letting the OS
    # do it. Off by default, and read once at startup -- GTK will not swap a
    # window's decorations after it is on screen. See docs/header-bar.md.
    "use_header_bar": False,
    # -- font rendering ------------------------------------------------
    # FreeType is the default because it is the only backend that honours
    # cairo hint styles; GDI does its own hinting and ignores them, so the
    # hinting settings would otherwise do nothing on Windows. "default"
    # leaves PANGOCAIRO_BACKEND alone. Needs a restart either way.
    "font_backend": "fontconfig",
    "font_name": "",  # "" means leave the theme's font alone
    "antialias": "grayscale",  # grayscale | subpixel | none | default
    "hint_style": "slight",  # slight | full | medium | none
    "hint_metrics": False,
    # -- console -------------------------------------------------------
    "enable_audio": True,
    "sw_decoders": False,
    "auto_resize": True,
    "scale_to_fit": False,
    "prefer_vnc": False,  # force VNC even when SPICE is available
    # Ask QEMU whether anyone is already watching before opening SPICE.
    # QEMU serves one SPICE client at a time, so connecting without asking
    # silently throws whoever is there off their session.
    "spice_session_check": True,
    # Offer a USB device to the guest when it is plugged in, the way VMware
    # Workstation does. On by default: the alternative is that redirection
    # is a menu you have to remember exists.
    "usb_autoprompt": True,
    # -- confirmations -------------------------------------------------
    # Which destructive power actions stop to ask first. The two that cut
    # power without telling the guest are on by default; pausing is
    # reversible in one click, so it is not.
    "confirm_stop": True,
    "confirm_shutdown": True,
    "confirm_reset": True,
    "confirm_pause": False,
    # -- startup -------------------------------------------------------
    # Ask GitHub for the latest release a few seconds after the window
    # opens. Only a packaged build does this: a source checkout reports
    # whatever pyproject says, which is routinely behind the tree it
    # describes, so the answer would be noise.
    "check_updates": True,
    # Reopen the consoles that were open when the app last closed, and put
    # the tree back the way it was expanded.
    "restore_session": True,
    "session_consoles": [],  # guest keys, in tab order
    "session_expanded": [],  # sidebar row identities
    # -- naming --------------------------------------------------------
    "tab_title_format": "name",  # "name" | "id" | "both"
    "tree_name_format": "name",  # "name" | "id"
    # Templates are not guests you use, they are things you clone from, so
    # by default they sit together at the foot of each group rather than
    # interleaved with the running estate.
    "templates_last": True,
    # -- layout --------------------------------------------------------
    # The size is what the window returns to when it is unmaximised, so it
    # is recorded separately from the maximised flag rather than being
    # overwritten by the size of a maximised window.
    "window_width": 1280,
    "window_height": 800,
    "window_maximized": False,
    "sidebar_width": 280,
    # Both panes are toggled from the toolbar. The tree is open by default
    # and the task list is not, which is where each of them starts.
    "sidebar_visible": True,
    # Dragging a guest between folders. Worth being able to switch off: in a
    # tree you click around all day, a slipped drag silently rewrites a
    # guest's notes on the server.
    "enable_dnd": True,
    # Polling. The inventory drives everything the tree and toolbar show,
    # so it is the one worth tuning; the burst is what covers the few
    # seconds Proxmox takes to report a power action.
    "refresh_seconds": 2,
    "task_refresh_seconds": 5,
    "burst_seconds": 15,
    # Per-guest console settings, keyed by "<node>/<kind>/<vmid>". Consoles
    # differ enough (a 4K desktop vs a serial-ish text console) that one
    # global scaling choice is the wrong answer for at least one of them.
    "guest_prefs": {},
    # Saved servers, reconnected at startup. Passwords are stored through
    # secrets.encode(), which is DPAPI on Windows and obfuscation elsewhere.
    "connections": [],
    # How the inventory tree groups guests: by node, by client-side folder,
    # or by the tags Proxmox keeps. Changed from the button beside the
    # search box or from Preferences; both write here.
    "tree_view": "node",  # "node" | "folder" | "tag"
}


def config_dir():
    # An explicit override keeps tests and portable installs away from the
    # real user settings.
    override = os.environ.get("PROXIMA_CONFIG_DIR")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "Proxima"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "proxima"


def config_file():
    return config_dir() / "settings.json"


class Config(dict):
    """Settings dict that knows how to load and save itself."""

    def __init__(self, data=None):
        super().__init__(copy.deepcopy(DEFAULTS))
        if data:
            # Read the stored version before merging: the defaults already
            # carry the current one, so afterwards every config would look
            # up to date and no migration would ever run.
            version = data.get("config_version", 0)
            # Only accept keys we know about; a stale config should never
            # smuggle in surprises.
            self.update({k: v for k, v in data.items() if k in DEFAULTS})
            self._migrate(version)

    def _migrate(self, version):
        """Rewrite settings whose default changed after they were saved."""
        # FreeType became the default in version 1. A stored "default"
        # predates the choice existing, so it is not a deliberate preference.
        if version < 1 and self.get("font_backend") == "default":
            self["font_backend"] = "fontconfig"
        self["config_version"] = CONFIG_VERSION

    @classmethod
    def load(cls):
        try:
            with open(config_file(), encoding="utf-8") as handle:
                return cls(json.load(handle))
        except (OSError, ValueError):
            return cls()

    def save(self):
        try:
            config_dir().mkdir(parents=True, exist_ok=True)
            tmp = config_file().with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(dict(self), handle, indent=2, sort_keys=True)
            os.replace(tmp, config_file())
            return True
        except OSError as exc:
            log.warning("could not save: %s", exc)
            return False


def apply_environment(config):
    """Push the settings that must exist before GTK/Pango initialise.

    Pango builds its default fontmap lazily on first use and never rebuilds
    it, so PANGOCAIRO_BACKEND is read exactly once, very early. Same story for
    GStreamer feature ranks, which Gst.init() snapshots.
    """
    backend = config.get("font_backend", "default")
    if backend == "fontconfig":
        os.environ["PANGOCAIRO_BACKEND"] = "fontconfig"
    elif backend == "win32":
        os.environ["PANGOCAIRO_BACKEND"] = "win32"

    if config.get("sw_decoders"):
        from .console.decoders import demote_hardware_decoders

        demote_hardware_decoders()

"""One guest, one status icon -- shared by the tree and the summary.

The tree owns the look of a guest's state: a symbolic icon recoloured to
say what the guest is doing. The summary shows the same guest, so it shows
the same icon rather than a second opinion about what green means.
"""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk

ICON_SIZE = 16

STATUS_ICONS = {
    "running": "media-playback-start-symbolic",
    "stopped": "media-playback-stop-symbolic",
    "paused": "media-playback-pause-symbolic",
    "suspended": "media-playback-pause-symbolic",
    # The caution mark Proxmox shows for a guest stopped by a storage
    # failure. "unknown" keeps the question mark: that one also means "not
    # polled yet", and a new row must not flash a warning.
    "io-error": "dialog-warning-symbolic",
    "unknown": "dialog-question-symbolic",
}

PALETTES = {
    False: {  # light
        "running": "#26a269",
        "stopped": "#77767b",
        "paused": "#c88800",
        "suspended": "#c88800",
        "io-error": "#e5a50a",
        "unknown": "#77767b",
        "template": "#77767b",
        "group": "#3d3846",
        "failed": "#e01b24",
        "pending": "#c88800",
    },
    True: {  # dark
        "running": "#57e389",
        "stopped": "#9a9996",
        "paused": "#f8e45c",
        "suspended": "#f8e45c",
        "io-error": "#f9f06b",
        "unknown": "#9a9996",
        "template": "#9a9996",
        "group": "#deddda",
        "failed": "#ff7b63",
        "pending": "#f8e45c",
    },
}


class IconCache:
    """Recoloured symbolic icons, cached so rows do not re-render them."""

    def __init__(self):
        self._cache = {}

    def clear(self):
        self._cache.clear()

    def get(self, name, colour, size=ICON_SIZE):
        key = (name, colour, size)
        if key in self._cache:
            return self._cache[key]

        pixbuf = None
        info = Gtk.IconTheme.get_default().lookup_icon(
            name, size, Gtk.IconLookupFlags.FORCE_SIZE
        )
        if info is not None:
            rgba = Gdk.RGBA()
            if rgba.parse(colour):
                try:
                    pixbuf = info.load_symbolic(rgba, None, None, None)[0]
                except Exception:
                    pixbuf = None
            if pixbuf is None:
                try:
                    pixbuf = info.load_icon()
                except Exception:
                    pixbuf = None

        self._cache[key] = pixbuf
        return pixbuf


# Shared by every caller that has no reason to keep its own. The tree keeps
# one of its own so it can clear it on a theme change without throwing away
# anybody else's.
_ICONS = IconCache()


def palette_for(dark):
    return PALETTES[bool(dark)]


def node_icon(node, dark=False, size=ICON_SIZE, cache=None):
    """The icon for a cluster node, in the structural colour unless it is down.

    Deliberately not the green a running guest gets. A node being up is the
    ordinary state of affairs and colouring it as an event makes the whole
    tree look like a status board; a node is furniture, so it wears the same
    colour the server row does. What is worth a colour is a node that has
    dropped out of the cluster, which is red.
    """
    icons = cache if cache is not None else _ICONS
    palette = palette_for(dark)
    status = getattr(node, "status", "unknown")
    down = status not in ("online", "unknown", "")
    return icons.get("computer-symbolic", palette["failed" if down else "group"], size)


def guest_icon(guest, dark=False, size=ICON_SIZE, cache=None):
    """The status icon for a guest, recoloured for the palette in use.

    The single source for "what does this guest look like right now": a
    template, or its power state, in the colour the tree draws it.
    """
    icons = cache if cache is not None else _ICONS
    palette = palette_for(dark)
    if guest.template:
        return icons.get("document-properties-symbolic", palette["template"], size)
    name = STATUS_ICONS.get(guest.status, STATUS_ICONS["unknown"])
    colour = palette.get(guest.status, palette["unknown"])
    return icons.get(name, colour, size)

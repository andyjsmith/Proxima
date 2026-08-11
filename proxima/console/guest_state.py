"""What a console says when there is no picture to show.

Five places needed this text: the placeholder tab that stands in for a guest
which is not up, the three live consoles when the inventory says the guest
went away underneath them, and the window's own handler for a console that
lost its connection. Each carried its own copy of the same lookup tables,
which is how "prelaunch" came to be missing from all of them at once, and
how one sentence about a failed disk turned into four slightly different
ones.

Only two things legitimately differ between the callers, so only two things
are parameters: what the guest is called -- the serial console only ever
serves containers -- and whether the console is connecting for the first
time or reconnecting.
"""

from collections import namedtuple

from .status_panel import CONNECTING_ICON, CONNECTING_TITLE

STOPPED_ICON = "media-playback-stop-symbolic"
HELD_ICON = "media-playback-pause-symbolic"
WARNING_ICON = "dialog-warning-symbolic"

GUEST = "Guest"
CONTAINER = "Container"

# status -> (title, detail, icon), as templates. {noun} is what the thing is
# called at the start of a sentence, {it} names it again mid-sentence, and
# {verb} is "connect" or "reconnect".
_STATES = {
    "stopped": (
        "{noun} is stopped",
        "The console will {verb} when {it} starts.",
        STOPPED_ICON,
    ),
    "suspended": ("{noun} is suspended", "Resume {it} to {verb}.", HELD_ICON),
    "paused": ("{noun} is paused", "Resume {it} to {verb}.", HELD_ICON),
    # Up with every device initialised, including the display, but never yet
    # allowed to execute. Held rather than stopped, so it reads like paused.
    "prelaunch": (
        "{noun} has not been started yet",
        "It is up and waiting to run its first instruction. "
        "Resume {it} to let it boot.",
        HELD_ICON,
    ),
    "io-error": (
        "{noun} stopped on an I/O error",
        "Proxmox stopped it because its storage stopped answering. "
        "Fix the storage, then reset or stop {it}.",
        WARNING_ICON,
    ),
    # Not a guest status but a console state. It reaches the same panel by
    # the same lookup, so keeping it out would only mean a second lookup.
    "connecting": (
        CONNECTING_TITLE,
        "Fetching a console ticket from Proxmox.",
        CONNECTING_ICON,
    ),
}

# States the user cannot act on, so no Reconnect button is offered. The two
# absentees are deliberate: an io-error guest and a prelaunch one both still
# have QEMU up and serving, so there is something there to connect to.
NO_RECONNECT = ("stopped", "suspended", "paused", "connecting")

GuestState = namedtuple("GuestState", "title detail icon can_reconnect")


def describe(status, noun=GUEST, reconnecting=True):
    """Title, detail, icon and whether Reconnect is worth offering.

    An unrecognised status still gets a panel rather than an empty one:
    Proxmox is free to invent a state we have never heard of, and naming it
    is more use than a blank.
    """
    entry = _STATES.get(status)
    can_reconnect = status not in NO_RECONNECT
    if entry is None:
        return GuestState(f"{noun} is {status}", "", STOPPED_ICON, can_reconnect)
    title, detail, icon = entry
    fields = {
        "noun": noun,
        "it": "it" if noun == CONTAINER else "the guest",
        "verb": "reconnect" if reconnecting else "connect",
    }
    return GuestState(
        title.format(**fields), detail.format(**fields), icon, can_reconnect
    )

"""Guest power actions, shared by the toolbar, the menu bar and the sidebar
context menu.

Labels match the Proxmox action names. Actions that do not apply to the
selected guest are disabled rather than hidden.
"""

from collections import namedtuple

Action = namedtuple("Action", "name label icon tooltip confirm states kinds")


POWER_ACTIONS = [
    # A suspended guest is resumed with start, not resume: resume is for a
    # guest paused in memory, start is what brings back one suspended to
    # disk. They never apply at the same time, so the UI shows one button.
    Action(
        "start",
        "Start",
        "media-playback-start-symbolic",
        "Start",
        None,
        ("stopped", "suspended"),
        ("qemu", "lxc"),
    ),
    Action(
        "shutdown",
        "Shutdown",
        "system-shutdown-symbolic",
        "ACPI shutdown",
        "Shut down {name}?",
        ("running", "paused"),
        ("qemu", "lxc"),
    ),
    Action(
        "stop",
        "Stop",
        "media-playback-stop-symbolic",
        "Immediate power off",
        "Stop {name}? The guest OS will not shut down cleanly.",
        ("running", "paused", "suspended"),
        ("qemu", "lxc"),
    ),
    Action(
        "reset",
        "Reset",
        "view-refresh-symbolic",
        "Hard reset",
        "Reset {name}? The guest OS will not shut down cleanly.",
        ("running",),
        ("qemu",),
    ),
    Action(
        "reboot",
        "Reboot",
        "system-reboot-symbolic",
        "ACPI reboot",
        None,
        ("running",),
        ("qemu", "lxc"),
    ),
    Action(
        "suspend",
        "Suspend",
        "media-playback-pause-symbolic",
        "Pause, keep memory",
        "Pause {name}? The guest stops running until it is resumed.",
        ("running",),
        ("qemu", "lxc"),
    ),
    Action(
        "resume",
        "Resume",
        "media-playback-start-symbolic",
        "Resume",
        None,
        ("paused",),
        ("qemu", "lxc"),
    ),
]

# The subset shown on the toolbar, in order. The rest live in the menus.
# "start" is the combined Start/Resume control -- see start_action_for().
TOOLBAR_ACTIONS = ("start", "shutdown", "stop", "reset", "suspend")

ACTIONS_BY_NAME = {action.name: action for action in POWER_ACTIONS}


# What to show over a console while an action is still taking effect.
# Purely client side: it is cleared as soon as the guest's real status moves,
# and a console opened later simply never sees it.
IN_PROGRESS = {
    "start": "Starting",
    "shutdown": "Shutting down",
    "stop": "Stopping",
    "reset": "Resetting",
    "reboot": "Rebooting",
    "suspend": "Suspending",
    "resume": "Resuming",
}


# The status each action is trying to reach, where there is one. Used to
# stop waiting the moment the inventory shows it.
#
# reboot and reset are absent on purpose: both end where they began, at
# "running", so there is no target status that would mean they finished.
# Waiting on those falls back to "any change, or the deadline" -- which is
# the honest answer, since from the outside a reboot is indistinguishable
# from a guest that never stopped running.
EXPECTED_STATUS = {
    "start": "running",
    "resume": "running",
    "shutdown": "stopped",
    "stop": "stopped",
    "suspend": "paused",
}


def start_action_for(guest):
    """Whichever of Start/Resume applies to a guest.

    They are mutually exclusive, so they share one button that relabels
    itself rather than sitting side by side with one always dead.
    """
    if guest is not None and guest.status == "paused":
        return ACTIONS_BY_NAME["resume"]
    return ACTIONS_BY_NAME["start"]


def visible_actions(guest):
    """Power actions to offer for a guest, Start and Resume collapsed."""
    result = []
    for action in POWER_ACTIONS:
        if action.name == "resume":
            continue  # folded into the start entry
        result.append(start_action_for(guest) if action.name == "start" else action)
    return result


def resolve(action_name, guest):
    """Map a UI action name to the one that actually applies to a guest."""
    if action_name == "start":
        return start_action_for(guest)
    return ACTIONS_BY_NAME.get(action_name)


def enabled_for(action, guest):
    """Whether an action applies to the currently selected guest."""
    if guest is None or guest.template:
        return False
    if guest.kind not in action.kinds:
        return False
    if guest.lock:
        return False  # locked: a task is already running on it
    return guest.status in action.states


# Which actions can be told not to ask, and the setting that says so. An
# action missing from here either has nothing to confirm (start, resume) or
# is not destructive enough to be worth a prompt either way (reboot, which
# asks the guest OS and can be refused by it).
CONFIRM_SETTINGS = {
    "stop": "confirm_stop",
    "shutdown": "confirm_shutdown",
    "reset": "confirm_reset",
    "suspend": "confirm_pause",
}


def confirms(action_name, config=None):
    """Whether this action should stop and ask, per the user's settings."""
    key = CONFIRM_SETTINGS.get(action_name)
    if key is None:
        return True  # nothing configurable about it
    if config is None:
        return True
    return bool(config.get(key, key != "confirm_pause"))


def confirmation_text(action, guest, config=None):
    if not action.confirm or not confirms(action.name, config):
        return None
    return action.confirm.format(name=guest.label)

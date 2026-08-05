"""The controls the main window and a popped-out console both carry.

Both windows offer the same power actions and the same three snapshot
buttons, enabled by the same rules -- a popped-out console is still a way to
manage the guest, not just a picture of it. Only what happens on a click
differs, so that is all either window passes in.

Keeping the definitions here means a new action, or a change to when one is
offered, lands in both windows at once rather than in whichever was
remembered.
"""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from . import actions as action_defs
from .snapshots import describe_revert

# Name, label, icon, tooltip. The names are the ones the windows use to talk
# about these buttons afterwards.
SNAPSHOT_BUTTONS = (
    ("manage", "Manage", "document-open-recent-symbolic", "Manage snapshots"),
    ("take", "Snapshot", "appointment-new-symbolic", "Take a snapshot"),
    (
        "revert",
        "Revert",
        "document-revert-symbolic",
        "Roll back to the most recent snapshot",
    ),
)


def tool_button(label, icon, tooltip, important=False, sensitive=False):
    item = Gtk.ToolButton()
    item.set_label(label)
    item.set_icon_name(icon)
    item.set_tooltip_text(tooltip)
    item.set_is_important(important)
    item.set_sensitive(sensitive)
    return item


def add_power_buttons(bar, on_click, important=()):
    """Insert the power actions into a toolbar. Returns {name: widget}.

    on_click is called with the action name.
    """
    items = {}
    for name in action_defs.TOOLBAR_ACTIONS:
        action = action_defs.ACTIONS_BY_NAME[name]
        item = tool_button(
            action.label,
            action.icon,
            action.tooltip,
            important=name in important,
        )
        item.connect("clicked", lambda _b, which=name: on_click(which))
        items[name] = item
        bar.insert(item, -1)
    return items


def add_snapshot_buttons(bar, on_click, important=()):
    """Insert Snapshot/Revert/Manage. Returns {name: widget}.

    on_click is called with "take", "revert" or "manage".
    """
    items = {}
    for name, label, icon, tooltip in SNAPSHOT_BUTTONS:
        item = tool_button(label, icon, tooltip, important=name in important)
        item.connect("clicked", lambda _b, which=name: on_click(which))
        items[name] = item
        bar.insert(item, -1)
    return items


def _widgets(entry):
    """A button may be shared with a menu entry, so take one or many."""
    if isinstance(entry, (list, tuple, set)):
        return entry
    return (entry,)


def apply_power_state(items, guest):
    """Enable each power control for the guest, if it applies to it.

    items maps an action name to a widget, or to several -- the toolbar
    button and the menu entry that does the same thing.
    """
    for name, entry in items.items():
        # "start" is the shared Start/Resume control, so it takes its label
        # from whichever of the two currently applies.
        action = action_defs.resolve(name, guest)
        enabled = action_defs.enabled_for(action, guest)
        for widget in _widgets(entry):
            widget.set_sensitive(enabled)
            if name == "start":
                widget.set_label(action.label)
                widget.set_tooltip_text(action.tooltip)


def apply_snapshot_state(items, guest):
    """Snapshots work on stopped guests too; only templates are excluded.

    Revert additionally needs a snapshot to exist, and says which one it
    would roll back to.
    """
    can_snapshot = guest is not None and not guest.template
    latest = guest.latest_snapshot if guest else None

    for name in ("take", "manage"):
        for widget in _widgets(items.get(name, ())):
            widget.set_sensitive(can_snapshot)

    tooltip = describe_revert(latest)
    for widget in _widgets(items.get("revert", ())):
        widget.set_sensitive(bool(can_snapshot and latest))
        widget.set_tooltip_text(tooltip)

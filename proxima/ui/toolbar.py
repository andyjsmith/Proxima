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

from ..console import keys as console_keys
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


SEND_KEY_ICON = "input-keyboard-symbolic"
SEND_KEY_TOOLTIP = (
    "Send Ctrl+Alt+Del to the guest. The arrow has the other combinations "
    "this computer would otherwise swallow."
)


def send_key_menu(on_send):
    """A fresh Send Key menu, calling on_send(keysyms) for each entry.

    Fresh every time on purpose: a GtkMenu belongs to one place, so the
    main window and a popped-out console cannot share one instance.
    """
    menu = Gtk.Menu()
    for entry in console_keys.SEND_KEYS:
        if entry is None:
            menu.append(Gtk.SeparatorMenuItem())
            continue
        label, keysyms = entry
        item = Gtk.MenuItem(label=label)
        item.connect("activate", lambda _i, keys=keysyms: on_send(keys))
        menu.append(item)
    menu.show_all()
    return menu


def send_key_button(on_send):
    """Ctrl+Alt+Del as a button, with the rest of the keys behind its arrow.

    The click is the one people came for -- Ctrl+Alt+Del is most of the
    reason this control exists -- and the menu is there for the twelve
    virtual terminals and the ones the window manager eats.
    """
    item = Gtk.MenuToolButton()
    item.set_label("Ctrl+Alt+Del")
    item.set_icon_name(SEND_KEY_ICON)
    item.set_tooltip_text(SEND_KEY_TOOLTIP)
    item.set_arrow_tooltip_text("Send another key combination")
    item.set_menu(send_key_menu(on_send))
    item.set_sensitive(False)
    item.connect("clicked", lambda *_: on_send(console_keys.CTRL_ALT_DEL))
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

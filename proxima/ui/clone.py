"""The clone dialog, for turning a template into a new guest.

Mirrors what the Proxmox web UI asks for, in the same order: name, where it
lands, and how the disks are copied. The node and storage lists are fetched
in the background -- the dialog opens filled in with sensible defaults and
becomes more specific a moment later, rather than blocking on two API calls
before it will draw.
"""

import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from ..api.models import valid_guest_name
from ..theme import decorate as theme_decorate

# What a clone of each guest type needs its target storage to hold.
CONTENT_FOR_KIND = {"qemu": "images", "lxc": "rootdir"}

# The storage combo's "wherever the source lives" entry. Parentheses cannot
# appear in a Proxmox storage ID, so this can never collide with a real one.
# A NUL-prefixed sentinel would be the obvious choice and is not usable here:
# GTK truncates IDs at the first NUL, so it arrives back as an empty string.
SAME_STORAGE = "(source storage)"


class CloneDialog(Gtk.Dialog):
    """Collects the parameters for one clone. Read them back with values()."""

    def __init__(self, parent, api, guest):
        super().__init__(title=f"Clone {guest.name}", transient_for=parent,
                         modal=True)
        self.api = api
        self.guest = guest
        self._closed = False

        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.ok_button = self.add_button("Clone", Gtk.ResponseType.OK)
        self.ok_button.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_default_size(440, -1)

        content = self.get_content_area()
        content.set_spacing(6)
        content.set_border_width(12)

        grid = Gtk.Grid(row_spacing=6, column_spacing=10)
        content.pack_start(grid, False, False, 0)

        grid.attach(self._label("Name"), 0, 0, 1, 1)
        self.name_entry = Gtk.Entry()
        self.name_entry.set_hexpand(True)
        self.name_entry.set_activates_default(True)
        self.name_entry.set_text(self._default_name())
        self.name_entry.set_tooltip_text(
            "Letters, digits, hyphens and dots; no leading or trailing hyphen")
        self.name_entry.connect("changed", lambda *_: self._validate())
        grid.attach(self.name_entry, 1, 0, 1, 1)

        grid.attach(self._label("VMID"), 0, 1, 1, 1)
        self.vmid_entry = Gtk.Entry()
        self.vmid_entry.set_activates_default(True)
        self.vmid_entry.set_placeholder_text("next free ID")
        self.vmid_entry.connect("changed", lambda *_: self._validate())
        grid.attach(self.vmid_entry, 1, 1, 1, 1)

        grid.attach(self._label("Target node"), 0, 2, 1, 1)
        self.node_combo = Gtk.ComboBoxText()
        self.node_combo.append(guest.node, guest.node)
        self.node_combo.set_active_id(guest.node)
        self.node_combo.connect("changed", lambda *_: self._load_storages())
        grid.attach(self.node_combo, 1, 2, 1, 1)

        grid.attach(self._label("Mode"), 0, 3, 1, 1)
        self.mode_combo = Gtk.ComboBoxText()
        self.mode_combo.append("linked", "Linked clone")
        self.mode_combo.append("full", "Full clone")
        # Linked is what a template is for: instant, and it costs no space
        # until the clone writes something.
        self.mode_combo.set_active_id("linked")
        self.mode_combo.set_tooltip_text(
            "A linked clone shares the template's disks and cannot outlive "
            "it. A full clone is an independent copy.")
        self.mode_combo.connect("changed", lambda *_: self._sync_mode())
        grid.attach(self.mode_combo, 1, 3, 1, 1)

        self.storage_label = self._label("Target storage")
        grid.attach(self.storage_label, 0, 4, 1, 1)
        self.storage_combo = Gtk.ComboBoxText()
        self.storage_combo.append(SAME_STORAGE, "Same as source")
        self.storage_combo.set_active_id(SAME_STORAGE)
        self.storage_combo.set_tooltip_text(
            "Full clones only. Leave as the source to copy in place.")
        grid.attach(self.storage_combo, 1, 4, 1, 1)

        self.message = Gtk.Label(xalign=0.0)
        self.message.set_line_wrap(True)
        self.message.get_style_context().add_class("dim")
        content.pack_start(self.message, False, False, 0)

        theme_decorate(self)
        self.show_all()
        self._sync_mode()
        self._validate()
        self.name_entry.grab_focus()
        self.name_entry.select_region(0, -1)

        self._load_nextid()
        self._load_nodes()
        self._load_storages()

    # -- construction helpers ------------------------------------------

    @staticmethod
    def _label(text):
        label = Gtk.Label(label=text, xalign=1.0)
        label.get_style_context().add_class("dim")
        return label

    def _default_name(self):
        base = (self.guest.name or "clone").removesuffix("-template")
        return f"{base}-clone"

    def _background(self, name, work, apply_result):
        """Run one API call off the main loop and apply it if we are still up."""
        def worker():
            try:
                result = work()
            except Exception:
                # A list that will not load costs the user a typed value at
                # worst; it is not worth an error dialog over a dialog.
                return
            GLib.idle_add(lambda: (self._closed or apply_result(result),
                                   False)[1])

        threading.Thread(target=worker, daemon=True, name=name).start()

    # -- background loads ----------------------------------------------

    def _load_nextid(self):
        def apply(vmid):
            if vmid and not self.vmid_entry.get_text().strip():
                self.vmid_entry.set_text(str(vmid))
            self._validate()

        self._background("clone-nextid", self.api.next_vmid, apply)

    def _load_nodes(self):
        def apply(nodes):
            names = sorted(node.name for node in nodes
                           if node.status == "online")
            if not names:
                return
            current = self.node_combo.get_active_id()
            self.node_combo.remove_all()
            for name in names:
                self.node_combo.append(name, name)
            self.node_combo.set_active_id(
                current if current in names else self.guest.node)

        self._background("clone-nodes", self.api.nodes, apply)

    def _load_storages(self):
        node = self.node_combo.get_active_id() or self.guest.node
        content = CONTENT_FOR_KIND.get(self.guest.kind, "images")

        def apply(rows):
            names = sorted(row.get("storage") for row in rows
                           if row.get("storage"))
            current = self.storage_combo.get_active_id()
            self.storage_combo.remove_all()
            self.storage_combo.append(SAME_STORAGE, "Same as source")
            for name in names:
                self.storage_combo.append(name, name)
            self.storage_combo.set_active_id(
                current if current in names else SAME_STORAGE)

        self._background(
            "clone-storages",
            lambda: self.api.node_storages(node, content), apply)

    # -- state ---------------------------------------------------------

    def _sync_mode(self):
        """Storage is a full-clone question only; Proxmox rejects it on a
        linked clone rather than ignoring it."""
        full = self.mode_combo.get_active_id() == "full"
        self.storage_combo.set_sensitive(full)
        self.storage_label.set_sensitive(full)
        if not full:
            self.storage_combo.set_active_id(SAME_STORAGE)

    def _validate(self):
        name = self.name_entry.get_text().strip()
        vmid = self.vmid_entry.get_text().strip()
        problem = ""
        if not valid_guest_name(name):
            problem = ("Names may hold letters, digits, hyphens and dots, "
                       "and cannot start or end with a hyphen.")
        elif vmid and not vmid.isdigit():
            problem = "The VMID must be a number."
        elif vmid and int(vmid) < 100:
            problem = "Proxmox reserves VMIDs below 100."
        self.message.set_text(problem)
        self.ok_button.set_sensitive(not problem and bool(vmid))
        return not problem

    def values(self):
        """(name, vmid, target node, full, storage or None)."""
        vmid = self.vmid_entry.get_text().strip()
        storage = self.storage_combo.get_active_id()
        return (
            self.name_entry.get_text().strip(),
            int(vmid) if vmid.isdigit() else None,
            self.node_combo.get_active_id() or self.guest.node,
            self.mode_combo.get_active_id() == "full",
            storage if storage and storage != SAME_STORAGE else None,
        )

    def destroy(self):
        self._closed = True
        super().destroy()

"""Per-VM settings, stored on the server.

Three tabs, matching how Proxmox itself divides a guest's configuration.
Hardware and Options are placeholders for now; Proxmox Manager holds the
settings this client owns.

Two things separate these from the status bar's clipboard and audio buttons,
and from Reopen with VNC:

  * These are the guest's settled configuration and live in its notes, so
    every machine running this client sees the same answer.
  * Those are temporary helpers for the session in front of you. Clicking
    them never writes here -- an experiment is not a decision.

Because saving means a round trip that rewrites the guest's description,
nothing is written until Apply or Save is pressed.
"""

import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from ..api import devices
from ..api import notes as notes_meta
from ..theme import decorate as theme_decorate

RESPONSE_APPLY = 1

# Display adapters worth offering, and what Proxmox calls them. The first
# entry is the empty value: Proxmox treats an absent 'vga' as std.
VGA_CHOICES = [
    ("", "Default  -  Standard VGA, no SPICE"),
    ("qxl", "SPICE (QXL)"),
    ("virtio", "VirtIO-GPU  -  SPICE"),
    ("virtio-gl", "VirGL (VirtIO-GPU, 3D)  -  SPICE"),
    ("std", "Standard VGA"),
    ("vmware", "VMware compatible"),
    ("cirrus", "Cirrus  -  legacy guests only"),
    ("serial0", "Serial terminal 0"),
    ("none", "None"),
]

NIC_MODEL_CHOICES = [
    ("virtio", "VirtIO (paravirtualised)"),
    ("e1000", "Intel E1000"),
    ("e1000e", "Intel E1000E"),
    ("rtl8139", "Realtek RTL8139"),
    ("vmxnet3", "VMware vmxnet3"),
]

# Config keys Proxmox cannot change under a running guest without parking
# the change as "pending". The dialog refuses to offer them instead, which
# is the whole point of not reproducing the web UI's behaviour here.
NEEDS_STOPPED = ("cores", "sockets", "memory", "balloon", "vga", "agent")

# spice-gtk's clipboard switch is a single boolean covering both directions,
# so there is nothing honest to offer between "on" and "off". If a future
# spice-gtk grows per-direction control, add the entries here and teach
# SpiceConsole._apply_clipboard about them; the stored value already passes
# through normalise_settings() untouched.
CLIPBOARD_CHOICES = [
    ("enabled", "Enabled  -  shared both ways"),
    ("disabled", "Disabled"),
]

AUDIO_CHOICES = [
    ("enabled", "Enabled"),
    ("disabled", "Disabled"),
]

PROTOCOL_CHOICES = [
    ("default", "Default  -  SPICE when the display supports it"),
    ("vnc", "VNC only"),
]


class VMSettingsDialog(Gtk.Dialog):
    """Hardware / Options / Proxmox Manager for one guest."""

    def __init__(self, parent, api, guest, on_saved=None):
        super().__init__(title=f"Settings - {guest.label}",
                         transient_for=parent, modal=True)
        self.api = api
        self.guest = guest
        self.on_saved = on_saved or (lambda settings: None)
        self._loading = True
        self._saving = False

        self.set_default_size(620, 520)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.apply_button = self.add_button("Apply", RESPONSE_APPLY)
        self.save_button = self.add_button("Save", Gtk.ResponseType.OK)
        self.save_button.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)

        # The settings as they are on the server, so Apply knows whether
        # there is anything to send and Cancel has something to mean.
        self.saved = notes_meta.normalise_settings(guest.settings)
        self.values = dict(self.saved)

        # The guest config as it was read, the keys edited since, and the
        # checksum that lets Proxmox refuse the write if somebody else got
        # there first.
        self.config = dict(guest.config or {})
        self.digest = self.config.get("digest")
        self.edits = {}                 # config key -> new value
        self.running = bool(guest.running)
        self.nets = self._load_nets()

        notebook = Gtk.Notebook()
        notebook.set_border_width(8)
        notebook.append_page(self._hardware_page(),
                             Gtk.Label(label="Hardware"))
        notebook.append_page(self._options_page(),
                             Gtk.Label(label="Options"))
        notebook.append_page(self._manager_page(),
                             Gtk.Label(label="Proxmox Manager"))

        content = self.get_content_area()
        content.pack_start(notebook, True, True, 0)

        self.message = Gtk.Label(xalign=0.0)
        self.message.get_style_context().add_class("dim")
        self.message.set_line_wrap(True)
        self.message.set_margin_start(12)
        self.message.set_margin_end(12)
        self.message.set_margin_bottom(6)
        content.pack_start(self.message, False, False, 0)

        self._loading = False
        self._alive = True
        self._sync_buttons()
        theme_decorate(self)
        self.show_all()
        self.connect("response", self._on_response)
        # The bridge lookup runs on its own thread and can land after the
        # dialog is gone; this is how it knows.
        self.connect("destroy", lambda *_: setattr(self, "_alive", False))

    # -- pages ---------------------------------------------------------

    @staticmethod
    def _page():
        grid = Gtk.Grid(row_spacing=8, column_spacing=12)
        grid.set_border_width(12)
        return grid

    @staticmethod
    def _heading(text):
        label = Gtk.Label(xalign=0.0)
        label.set_markup(f"<b>{GLib.markup_escape_text(text)}</b>")
        label.set_margin_top(8)
        return label

    @staticmethod
    def _caption(text):
        label = Gtk.Label(label=text, xalign=1.0)
        label.get_style_context().add_class("dim")
        return label

    def _scrolled(self, child):
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(child)
        return scroll

    # -- hardware ------------------------------------------------------

    def _config_int(self, key, default):
        try:
            return int(str(self.config.get(key, default)).strip())
        except (TypeError, ValueError):
            return default

    def _spin(self, grid, row, label, key, lower, upper, default,
              tooltip=None, live=False):
        grid.attach(self._caption(label), 0, row, 1, 1)
        adjustment = Gtk.Adjustment(
            value=float(self._config_int(key, default)),
            lower=lower, upper=upper, step_increment=1, page_increment=8)
        spin = Gtk.SpinButton(adjustment=adjustment, digits=0)
        spin.set_hexpand(True)
        spin.set_numeric(True)
        if tooltip:
            spin.set_tooltip_text(tooltip)
        self._gate(spin, key, live)
        spin.connect("value-changed",
                     lambda w: self._edit(key, str(int(w.get_value())),
                                          str(self._config_int(key, default))))
        grid.attach(spin, 1, row, 1, 1)
        return spin

    def _gate(self, widget, key, live):
        """Disable a field the guest is not currently able to accept."""
        if live or not self.running or key not in NEEDS_STOPPED:
            return
        widget.set_sensitive(False)
        widget.set_tooltip_text(
            "Stop the VM to change this. Proxmox would otherwise hold it as "
            "a pending change until the next boot.")

    def _hardware_page(self):
        grid = self._page()
        row = 0

        if self.running:
            warning = Gtk.Label(xalign=0.0)
            warning.set_line_wrap(True)
            warning.set_markup(
                "<span foreground='#e0913a'>The VM is running. Processors, "
                "memory and display can only be changed while it is stopped."
                "</span>")
            grid.attach(warning, 0, row, 2, 1)
            row += 1

        grid.attach(self._heading("Processors"), 0, row, 2, 1)
        row += 1
        self._spin(grid, row, "Sockets", "sockets", 1, 4, 1)
        row += 1
        self._spin(grid, row, "Cores per socket", "cores", 1, 128, 1)
        row += 1

        grid.attach(self._heading("Memory"), 0, row, 2, 1)
        row += 1
        self._spin(grid, row, "Memory (MiB)", "memory", 16, 4194304, 512)
        row += 1
        self._spin(
            grid, row, "Minimum (MiB)", "balloon", 0, 4194304, 0,
            tooltip="Ballooning floor. 0 turns the balloon driver off, so "
                    "the guest keeps all of its memory.")
        row += 1

        grid.attach(self._heading("Display"), 0, row, 2, 1)
        row += 1
        grid.attach(self._caption("Graphics card"), 0, row, 1, 1)
        vga_pairs = devices.parse_pairs(self.config.get("vga", ""))
        self._vga_pairs = vga_pairs
        current, _ = self._vga_type(vga_pairs)
        combo = Gtk.ComboBoxText()
        for value, text in VGA_CHOICES:
            combo.append(value or "default", text)
        combo.set_active_id(current or "default")
        if combo.get_active_id() is None:
            combo.append(current, f"{current}  (kept as configured)")
            combo.set_active_id(current)
        combo.set_hexpand(True)
        combo.set_tooltip_text(
            "SPICE needs QXL or VirtIO-GPU. Anything else opens on VNC.")
        self._gate(combo, "vga", live=False)
        combo.connect("changed", self._on_vga_changed)
        grid.attach(combo, 1, row, 1, 1)
        row += 1

        grid.attach(self._heading("Network devices"), 0, row, 2, 1)
        row += 1
        self.net_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        grid.attach(self.net_box, 0, row, 2, 1)
        row += 1

        add = Gtk.Button(label="Add network device")
        add.set_halign(Gtk.Align.START)
        add.connect("clicked", lambda *_: self._add_net())
        grid.attach(add, 0, row, 2, 1)
        row += 1

        self.net_note = Gtk.Label(xalign=0.0)
        self.net_note.set_line_wrap(True)
        self.net_note.get_style_context().add_class("dim")
        if self.running and not devices.network_hotplug(self.config):
            self.net_note.set_markup(
                "<span foreground='#e0913a'>This VM does not hot-plug "
                "network changes (see its 'hotplug' setting), so they will "
                "wait for the next boot.</span>")
        else:
            self.net_note.set_text(
                "Bridge, VLAN and firewall changes apply to a running VM.")
        grid.attach(self.net_note, 0, row, 2, 1)

        self._rebuild_nets()
        self._bridges = []
        self._load_bridges()
        return self._scrolled(grid)

    @staticmethod
    def _vga_type(pairs):
        """(type, remaining pairs) for a vga line."""
        for index, (key, value) in enumerate(pairs):
            if key is None:
                return value, pairs[:index] + pairs[index + 1:]
            if key == "type":
                return value, pairs[:index] + pairs[index + 1:]
        return "", list(pairs)

    def _on_vga_changed(self, combo):
        if self._loading:
            return
        chosen = combo.get_active_id() or ""
        if chosen == "default":
            chosen = ""
        _, rest = self._vga_type(self._vga_pairs)
        # Everything else on the line -- a QXL memory size, most likely --
        # is carried over rather than dropped on the floor.
        pairs = ([(None, chosen)] if chosen else []) + rest
        self._edit("vga", devices.render_pairs(pairs),
                   str(self.config.get("vga", "")))

    # -- network devices -----------------------------------------------

    def _load_nets(self):
        rows = []
        for slot in devices.nic_slots(self.config):
            rows.append({"slot": slot,
                         "pairs": devices.parse_pairs(self.config[slot]),
                         "new": False})
        return rows

    def _rebuild_nets(self):
        for child in self.net_box.get_children():
            self.net_box.remove(child)
        for entry in self.nets:
            self.net_box.pack_start(self._net_row(entry), False, False, 0)
        if not self.nets:
            empty = Gtk.Label(xalign=0.0)
            empty.get_style_context().add_class("dim")
            empty.set_text("No network devices.")
            self.net_box.pack_start(empty, False, False, 0)
        self.net_box.show_all()

    def _net_row(self, entry):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        model, mac = devices.nic_model(entry["pairs"])

        name = Gtk.Label(xalign=0.0)
        name.set_markup(f"<b>{entry['slot'] or 'new'}</b>")
        name.set_width_chars(5)
        box.pack_start(name, False, False, 0)

        model_combo = Gtk.ComboBoxText()
        for value, text in NIC_MODEL_CHOICES:
            model_combo.append(value, value)
            model_combo.set_tooltip_text(text)
        if model and model_combo.get_active_id() is None:
            if model not in [v for v, _ in NIC_MODEL_CHOICES]:
                model_combo.append(model, model)
        model_combo.set_active_id(model or "virtio")
        model_combo.connect("changed", self._on_net_model, entry)
        box.pack_start(model_combo, False, False, 0)

        # An editable combo, because a bridge that exists but was not in the
        # node's list -- or a node we could not ask -- must still be usable.
        bridge = Gtk.ComboBoxText.new_with_entry()
        bridge.get_child().set_width_chars(9)
        current_bridge = devices.get_pair(entry["pairs"], "bridge", "")
        bridge.get_child().set_text(current_bridge)
        bridge.get_child().connect("changed", self._on_net_bridge, entry)
        entry["bridge_widget"] = bridge
        box.pack_start(bridge, False, False, 0)

        vlan = Gtk.Entry()
        vlan.set_width_chars(5)
        vlan.set_placeholder_text("VLAN")
        vlan.set_text(devices.get_pair(entry["pairs"], "tag", "") or "")
        vlan.set_tooltip_text("VLAN tag, or empty for untagged")
        vlan.connect("changed", self._on_net_vlan, entry)
        box.pack_start(vlan, False, False, 0)

        firewall = Gtk.CheckButton(label="Firewall")
        firewall.set_active(
            str(devices.get_pair(entry["pairs"], "firewall", "0")) == "1")
        firewall.connect("toggled", self._on_net_firewall, entry)
        box.pack_start(firewall, False, False, 0)

        if mac:
            address = Gtk.Label(xalign=0.0)
            address.get_style_context().add_class("dim")
            address.set_text(mac)
            address.set_tooltip_text("MAC address")
            box.pack_start(address, False, False, 0)

        remove = Gtk.Button()
        remove.set_relief(Gtk.ReliefStyle.NONE)
        remove.add(Gtk.Image.new_from_icon_name("list-remove-symbolic",
                                                Gtk.IconSize.MENU))
        remove.set_tooltip_text("Remove this network device")
        remove.connect("clicked", lambda *_: self._remove_net(entry))
        box.pack_end(remove, False, False, 0)
        return box

    def _on_net_model(self, combo, entry):
        if self._loading:
            return
        chosen = combo.get_active_id()
        if not chosen:
            return
        model, mac = devices.nic_model(entry["pairs"])
        if chosen == model:
            return
        # Replace the model token in place, keeping the MAC with it.
        pairs = [(chosen, mac) if key == model or (key is None and value == model)
                 else (key, value) for key, value in entry["pairs"]]
        entry["pairs"] = pairs
        self._sync_buttons()

    def _on_net_bridge(self, editable, entry):
        if self._loading:
            return
        value = editable.get_text().strip()
        entry["pairs"] = devices.set_pair(entry["pairs"], "bridge",
                                          value or None)
        self._sync_buttons()

    def _on_net_vlan(self, entry_widget, entry):
        if self._loading:
            return
        text = entry_widget.get_text().strip()
        if text and not text.isdigit():
            # Silently refuse anything that is not a tag rather than letting
            # Proxmox reject the whole write later.
            entry_widget.set_text("".join(c for c in text if c.isdigit()))
            return
        entry["pairs"] = devices.set_pair(entry["pairs"], "tag", text or None)
        self._sync_buttons()

    def _on_net_firewall(self, check, entry):
        if self._loading:
            return
        entry["pairs"] = devices.set_pair(
            entry["pairs"], "firewall", "1" if check.get_active() else "0")
        self._sync_buttons()

    def _add_net(self):
        slot = devices.free_nic_slot(
            self.config, [e["slot"] for e in self.nets if e["slot"]])
        if slot is None:
            self.message.set_text("All 32 network slots are in use.")
            return
        bridge = self._bridges[0] if self._bridges else "vmbr0"
        self.nets.append({"slot": slot,
                          "pairs": devices.parse_pairs(
                              devices.new_nic(bridge=bridge)),
                          "new": True})
        self._rebuild_nets()
        self._sync_buttons()

    def _remove_net(self, entry):
        self.nets.remove(entry)
        self._rebuild_nets()
        self._sync_buttons()

    def _load_bridges(self):
        """Fill the bridge dropdowns from the node, without blocking."""
        guest = self.guest

        def worker():
            try:
                names = self.api.node_bridges(guest.node)
            except Exception:
                names = []
            GLib.idle_add(self._apply_bridges, names)

        threading.Thread(target=worker, daemon=True,
                         name=f"bridges-{guest.node}").start()

    def _apply_bridges(self, names):
        if not getattr(self, "_alive", True):
            return False
        self._bridges = list(names)
        for entry in self.nets:
            widget = entry.get("bridge_widget")
            if widget is None:
                continue
            current = widget.get_child().get_text()
            widget.remove_all()
            for name in names:
                widget.append_text(name)
            # remove_all() clears the entry as well, so put it back.
            widget.get_child().set_text(current)
        return False

    # -- options -------------------------------------------------------

    def _flag(self, grid, row, label, key, tooltip, truth=None, live=True,
              encode=None):
        check = Gtk.CheckButton(label=label)
        raw = str(self.config.get(key, "0")).strip()
        current = truth(raw) if truth else raw not in ("0", "", "none")
        check.set_active(current)
        check.set_tooltip_text(tooltip)
        self._gate(check, key, live)
        write = encode or (lambda active: "1" if active else "0")
        check.connect(
            "toggled",
            lambda w: self._edit(key, write(w.get_active()), raw))
        grid.attach(check, 0, row, 2, 1)
        return check

    def _encode_agent(self, active):
        """The agent line with only its enabled flag changed.

        It can carry more than a boolean -- fstrim_cloned_disks and the
        agent type live on the same line -- and writing a bare 1 or 0 would
        throw those away.
        """
        pairs = devices.parse_pairs(self.config.get("agent", ""))
        if len(pairs) <= 1 and all(key is None for key, _ in pairs):
            return "1" if active else "0"
        return devices.render_pairs(
            devices.set_pair(pairs, "enabled", "1" if active else "0"))

    def _options_page(self):
        grid = self._page()
        row = 0

        self._flag(grid, row, "Start at boot", "onboot",
                   "Start this VM when the node boots.")
        row += 1
        self._flag(
            grid, row, "QEMU Guest Agent", "agent",
            "Lets Proxmox read the guest's IP addresses, run commands in it "
            "and shut it down cleanly. The agent must also be installed "
            "inside the guest.",
            truth=lambda raw: raw.split(",")[0] in ("1", "enabled=1"),
            encode=self._encode_agent)
        row += 1
        self._flag(grid, row, "Protection", "protection",
                   "Refuse to delete this VM or its disks.")
        row += 1

        note = Gtk.Label(xalign=0.0)
        note.get_style_context().add_class("dim")
        note.set_line_wrap(True)
        note.set_margin_top(8)
        note.set_text(
            "Start at boot and protection apply straight away. The guest "
            "agent setting is part of the VM's hardware, so it needs the VM "
            "stopped.")
        grid.attach(note, 0, row, 2, 1)
        return grid

    # -- edits ---------------------------------------------------------

    def _edit(self, key, value, original):
        """Record a config change, or drop it when it matches the server."""
        if self._loading:
            return
        if value == original:
            self.edits.pop(key, None)
        else:
            self.edits[key] = value
        self._sync_buttons()

    def _config_edits(self):
        """(changes, deletes) for the guest config, networks included."""
        changes = dict(self.edits)
        deletes = []

        listed = {entry["slot"] for entry in self.nets if entry["slot"]}
        for slot in devices.nic_slots(self.config):
            if slot not in listed:
                deletes.append(slot)
        for entry in self.nets:
            rendered = devices.render_pairs(entry["pairs"])
            if entry["new"] or rendered != self.config.get(entry["slot"]):
                changes[entry["slot"]] = rendered
        return changes, deletes

    def _manager_page(self):
        grid = self._page()
        container = self.guest.is_container

        self._combo(
            grid, 0, "Clipboard", CLIPBOARD_CHOICES, "clipboard",
            tooltip=("Share the clipboard between the host and the guest. "
                     "Needs a SPICE console and spice-vdagent running in "
                     "the guest."),
            sensitive=not container)
        self._combo(
            grid, 1, "Audio", AUDIO_CHOICES, "audio",
            tooltip=("Play the guest's sound on this machine. Needs a SPICE "
                     "console and a SPICE audio device on the VM."),
            sensitive=not container)
        self._combo(
            grid, 2, "Protocol", PROTOCOL_CHOICES, "protocol",
            tooltip=("Which console protocol to open. VNC only is worth "
                     "setting for a guest whose SPICE display misbehaves."))

        note = Gtk.Label(xalign=0.0)
        note.get_style_context().add_class("dim")
        note.set_line_wrap(True)
        note.set_margin_top(8)
        text = ("Stored in this guest's notes on the server, so every "
                "machine running Proxmox Manager sees the same settings.\n\n"
                "The clipboard and audio buttons in the status bar, and "
                "Reopen Console with VNC, only change the console in front "
                "of you for as long as it is open. They never change what "
                "is set here.")
        if container:
            text = ("Containers have no SPICE console, so clipboard and "
                    "audio do not apply to them.\n\n" + text)
        note.set_text(text)
        grid.attach(note, 0, 3, 2, 1)
        return grid

    def _combo(self, grid, row, label, choices, name, tooltip=None,
               sensitive=True):
        caption = Gtk.Label(label=label, xalign=1.0)
        caption.get_style_context().add_class("dim")
        grid.attach(caption, 0, row, 1, 1)

        combo = Gtk.ComboBoxText()
        for value, text in choices:
            combo.append(value, text)
        combo.set_active_id(self.values.get(name))
        if combo.get_active_id() is None:
            # A value this build does not know about. Show it rather than
            # silently rewriting it to the default on the next save.
            stored = str(self.values.get(name, ""))
            combo.append(stored, f"{stored}  (not supported here)")
            combo.set_active_id(stored)
        combo.set_hexpand(True)
        combo.set_sensitive(sensitive)
        if tooltip:
            combo.set_tooltip_text(tooltip)
        combo.connect("changed", self._on_combo_changed, name)
        grid.attach(combo, 1, row, 1, 1)
        return combo

    # -- state ---------------------------------------------------------

    def _on_combo_changed(self, combo, name):
        if self._loading:
            return
        value = combo.get_active_id()
        if value is None:
            return
        self.values[name] = value
        self._sync_buttons()

    @property
    def dirty(self):
        if self.values != self.saved:
            return True
        changes, deletes = self._config_edits()
        return bool(changes or deletes)

    def _sync_buttons(self):
        if self._loading:
            return
        self.apply_button.set_sensitive(self.dirty and not self._saving)
        self.save_button.set_sensitive(not self._saving)

    # -- saving --------------------------------------------------------

    def _on_response(self, _dialog, response):
        if response == RESPONSE_APPLY:
            if self.dirty and not self._saving:
                self._save(close=False)
            return
        if response == Gtk.ResponseType.OK:
            if self._saving:
                return
            if self.dirty:
                self._save(close=True)
            else:
                self.destroy()
            return
        # Cancel, Escape or the window's own close button.
        if not self._saving:
            self.destroy()

    def _save(self, close):
        """Write both halves of the dialog, on a worker thread.

        The guest config goes first. If it fails there is nothing to undo,
        whereas writing the notes first and then failing on the config would
        leave the two halves disagreeing about what was saved.

        The notes are read-modify-written rather than blindly replaced: they
        are the user's own text with a block of ours inside them, and
        clobbering somebody's description to store a clipboard setting would
        be indefensible.
        """
        self._saving = True
        self._sync_buttons()
        self.message.set_text("Saving...")
        guest = self.guest
        wanted = dict(self.values)
        changes, deletes = self._config_edits()
        notes_changed = wanted != self.saved

        def worker():
            try:
                if changes or deletes:
                    self.api.set_guest_config(
                        guest.node, guest.vmid, changes, deletes,
                        guest.kind, digest=self.digest)
            except Exception as exc:
                GLib.idle_add(self._save_failed, f"{exc}")
                return

            updated = None
            if notes_changed:
                try:
                    current = self.api.guest_notes(guest.node, guest.vmid,
                                                   guest.kind)
                    updated = notes_meta.with_settings(current, wanted)
                    self.api.set_guest_notes(guest.node, guest.vmid, updated,
                                             guest.kind)
                except Exception as exc:
                    GLib.idle_add(self._save_failed, f"{exc}",
                                  bool(changes or deletes))
                    return

            # Re-read so the dialog's idea of the guest -- and its digest --
            # match what is now on the server, rather than what we believe
            # we sent.
            try:
                config = self.api.guest_config(guest.node, guest.vmid,
                                               guest.kind)
            except Exception:
                config = None
            GLib.idle_add(self._save_done, wanted, updated, config, close)

        threading.Thread(target=worker, daemon=True,
                         name=f"vm-settings-{guest.vmid}").start()

    def _save_done(self, wanted, updated, config, close):
        if not getattr(self, "_alive", True):
            return False
        self._saving = False
        self.saved = dict(wanted)
        self.guest.settings = dict(wanted)
        self.guest.settings_loaded = True
        if config:
            self.config = dict(config)
            self.digest = self.config.get("digest")
            self.guest.config = dict(config)
        elif updated is not None and isinstance(self.guest.config, dict):
            # The description just changed on the server; keep the cached
            # config in step so the next reader does not see the old text.
            self.guest.config["description"] = updated
        self.edits = {}
        self._loading = True
        try:
            self.nets = self._load_nets()
            self._rebuild_nets()
        finally:
            self._loading = False
        self.on_saved(dict(wanted))
        if close:
            self.destroy()
            return False
        self.message.set_text("Saved.")
        self._sync_buttons()
        return False

    def _save_failed(self, error, config_written=False):
        self._saving = False
        prefix = "Could not save"
        if config_written:
            # Say which half landed. "Could not save" over a hardware change
            # that did go through would send somebody looking for a problem
            # that is not there.
            prefix = ("The hardware changes were saved, but the Proxmox "
                      "Manager settings could not be")
        self.message.set_markup(
            f"<span foreground='#e01b24'>{prefix}: "
            f"{GLib.markup_escape_text(error)}</span>")
        self._sync_buttons()
        return False

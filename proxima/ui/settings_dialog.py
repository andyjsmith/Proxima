"""Preferences.

Everything applies live except the font backend and the decoder ranking,
which their libraries read once at startup. Those are labelled as needing a
restart rather than appearing to take effect.
"""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from ..theme import decorate as theme_decorate
from ..theme import discovery, fonts

THEMES = [
    ("Adwaita", "Adwaita"),
    ("Fluent", "Fluent"),
]

COLOR_MODES = [
    ("system", "Follow system"),
    ("light", "Light"),
    ("dark", "Dark"),
]

TAB_TITLE_FORMATS = [
    ("name", "Name"),
    ("id", "ID"),
    ("both", "Name and ID"),
]

TREE_NAME_FORMATS = [
    ("name", "Name first  -  webserver (101)"),
    ("id", "ID first  -  101 (webserver)"),
]

# Power actions that can be told not to ask, in the order they appear on the
# toolbar. The wording names the consequence, since that is what decides
# whether you want to be asked.
CONFIRM_ACTIONS = [
    ("confirm_stop", "Stop"),
    ("confirm_shutdown", "Shutdown"),
    ("confirm_reset", "Reset"),
    ("confirm_pause", "Pause"),
]


class SettingsDialog(Gtk.Dialog):
    """Edits a Config in place; emits a callback whenever something changes."""

    def __init__(self, parent, config, on_change=None):
        super().__init__(title="Preferences", transient_for=parent, modal=True)
        self.config = config
        self.on_change = on_change or (lambda: None)
        self._loading = True

        self.add_button("Close", Gtk.ResponseType.CLOSE)
        self.set_default_size(520, -1)

        notebook = Gtk.Notebook()
        notebook.set_border_width(8)
        notebook.append_page(self._appearance_page(), Gtk.Label(label="Appearance"))
        notebook.append_page(self._font_page(), Gtk.Label(label="Text"))
        notebook.append_page(self._console_page(), Gtk.Label(label="Console"))
        notebook.append_page(self._behaviour_page(), Gtk.Label(label="Behaviour"))
        notebook.append_page(self._polling_page(), Gtk.Label(label="Polling"))
        self.get_content_area().pack_start(notebook, True, True, 0)

        self._loading = False
        theme_decorate(self)
        self.show_all()
        self.connect("response", lambda *_: self._save_and_close())

    # -- page builders -------------------------------------------------

    @staticmethod
    def _page():
        grid = Gtk.Grid(row_spacing=8, column_spacing=12)
        grid.set_border_width(12)
        return grid

    @staticmethod
    def _label(text):
        label = Gtk.Label(label=text, xalign=1.0)
        label.get_style_context().add_class("dim")
        return label

    def _combo(self, grid, row, label, choices, key, tooltip=None, annotate=None):
        grid.attach(self._label(label), 0, row, 1, 1)
        combo = Gtk.ComboBoxText()
        for value, text in choices:
            if annotate:
                text = annotate(value, text)
            combo.append(value, text)
        combo.set_active_id(str(self.config.get(key)))
        if combo.get_active_id() is None:
            combo.set_active(0)
        combo.set_hexpand(True)
        if tooltip:
            combo.set_tooltip_text(tooltip)
        combo.connect("changed", self._on_combo_changed, key)
        grid.attach(combo, 1, row, 1, 1)
        return combo

    def _check(self, grid, row, label, key, tooltip=None):
        check = Gtk.CheckButton(label=label)
        check.set_active(bool(self.config.get(key)))
        if tooltip:
            check.set_tooltip_text(tooltip)
        check.connect("toggled", self._on_check_toggled, key)
        grid.attach(check, 1, row, 1, 1)
        return check

    def _appearance_page(self):
        grid = self._page()
        installed = discovery.installed_themes()

        def annotate(value, text):
            if value == "Adwaita":
                return text
            _, available = discovery.resolve_theme(value, False, installed)
            return text if available else f"{text}  (not installed)"

        self._combo(
            grid,
            0,
            "Theme",
            THEMES,
            "theme",
            annotate=annotate,
            tooltip="Fluent must be installed separately. See README.",
        )
        self._combo(
            grid,
            1,
            "Colours",
            COLOR_MODES,
            "color_mode",
            tooltip="Tracks the system light/dark setting",
        )

        self._check(
            grid,
            2,
            "Draw the titlebar in the application",
            "use_header_bar",
            tooltip="Replaces the system titlebar with one GTK draws, so it "
            "matches the theme. Restart required.",
        )

        detected = discovery.installed_themes()
        note = Gtk.Label(xalign=0.0)
        note.get_style_context().add_class("dim")
        note.set_line_wrap(True)
        note.set_text("Installed GTK themes: " + ", ".join(detected))
        grid.attach(note, 0, 3, 2, 1)
        return grid

    def _font_page(self):
        grid = self._page()

        backend, hinting_ok = fonts.font_backend()

        self._combo(
            grid,
            0,
            "Font backend",
            fonts.FONT_BACKEND_CHOICES,
            "font_backend",
            tooltip="Restart required",
        )

        font_choices = [(name, name or "Theme default") for name in fonts.FONT_CHOICES]
        self._combo(grid, 1, "Interface font", font_choices, "font_name")

        self._combo(
            grid,
            2,
            "Antialiasing",
            fonts.ANTIALIAS_CHOICES,
            "antialias",
            tooltip="Grayscale avoids colour fringing",
        )

        self.hint_combo = self._combo(
            grid, 3, "Hinting", fonts.HINT_STYLE_CHOICES, "hint_style"
        )
        self._check(
            grid,
            4,
            "Hint metrics",
            "hint_metrics",
            tooltip="Snap glyph advances to whole pixels",
        )

        status = Gtk.Label(xalign=0.0)
        status.set_line_wrap(True)
        if hinting_ok:
            status.get_style_context().add_class("dim")
            status.set_text(f"Backend: {backend}. Hinting active.")
        else:
            status.set_markup(
                f"<span foreground='#e0913a'>Backend: {backend}. Hinting "
                "ignored - select FreeType and restart.</span>"
            )
            self.hint_combo.set_sensitive(False)
        grid.attach(status, 0, 5, 2, 1)
        return grid

    def _console_page(self):
        grid = self._page()
        self._check(
            grid, 0, "Enable audio", "enable_audio", tooltip="SPICE audio playback"
        )
        self._check(
            grid,
            1,
            "Software video decoding only",
            "sw_decoders",
            tooltip="Demotes D3D/NVDEC/Vulkan decoders. Restart required.",
        )
        self._check(
            grid,
            2,
            "Auto-resize guest",
            "auto_resize",
            tooltip="SPICE only. Requires spice-vdagent.",
        )
        self._check(grid, 3, "Scale console to fit", "scale_to_fit")
        self._check(
            grid,
            4,
            "Always use VNC",
            "prefer_vnc",
            tooltip="Ignore SPICE even when available",
        )
        self._check(
            grid,
            5,
            "Check for other SPICE clients",
            "spice_session_check",
            tooltip="QEMU serves one SPICE client at a time. Ask before "
            "opening, so connecting does not disconnect somebody "
            "else without warning. Needs the VM.Monitor privilege.",
        )

        return grid

    def _behaviour_page(self):
        """Confirmations, what happens at startup, and how guests are named."""
        grid = self._page()
        row = 0

        grid.attach(self._heading("Ask before"), 0, row, 2, 1)
        row += 1
        for key, label in CONFIRM_ACTIONS:
            self._check(grid, row, label, key)
            row += 1

        note = Gtk.Label(xalign=0.0)
        note.get_style_context().add_class("dim")
        note.set_line_wrap(True)
        note.set_text("Acting on more than one guest at once always asks.")
        grid.attach(note, 1, row, 1, 1)
        row += 1

        grid.attach(self._heading("Startup"), 0, row, 2, 1)
        row += 1
        self._check(
            grid,
            row,
            "Restore the last session",
            "restore_session",
            tooltip="Reopen the consoles that were open when the "
            "app last closed, and expand the tree the same "
            "way",
        )
        row += 1

        grid.attach(self._heading("Names"), 0, row, 2, 1)
        row += 1
        self._combo(grid, row, "Console tabs", TAB_TITLE_FORMATS, "tab_title_format")
        row += 1
        self._combo(grid, row, "Inventory tree", TREE_NAME_FORMATS, "tree_name_format")
        row += 1
        self._check(
            grid,
            row,
            "Group templates at the bottom",
            "templates_last",
            tooltip="Templates sort together below the guests, in the same "
            "order as the guests themselves",
        )
        return grid

    @staticmethod
    def _heading(text):
        label = Gtk.Label(xalign=0.0)
        label.set_markup(f"<b>{text}</b>")
        label.set_margin_top(6)
        return label

    def _polling_page(self):
        grid = self._page()
        self._spin(
            grid,
            0,
            "Inventory",
            "refresh_seconds",
            1,
            120,
            "How often guest status is polled, in seconds",
        )
        self._spin(
            grid,
            1,
            "Task list",
            "task_refresh_seconds",
            1,
            120,
            "How often the task pane refreshes while it is open",
        )
        self._spin(
            grid,
            2,
            "Fast refresh",
            "burst_seconds",
            0,
            120,
            "After a power or snapshot action, poll every second for this many seconds",
        )

        note = Gtk.Label(xalign=0.0)
        note.get_style_context().add_class("dim")
        note.set_line_wrap(True)
        note.set_text(
            "Proxmox takes a few seconds to report a power action. Fast "
            "refresh shortens the wait; the console says what was asked for "
            "in the meantime."
        )
        grid.attach(note, 0, 3, 2, 1)
        return grid

    def _spin(self, grid, row, label, key, lower, upper, tooltip=None):
        grid.attach(self._label(label), 0, row, 1, 1)
        adjustment = Gtk.Adjustment(
            value=float(self.config.get(key, lower)),
            lower=lower,
            upper=upper,
            step_increment=1,
            page_increment=5,
        )
        spin = Gtk.SpinButton(adjustment=adjustment, digits=0)
        spin.set_hexpand(True)
        if tooltip:
            spin.set_tooltip_text(tooltip)
        spin.connect("value-changed", self._on_spin_changed, key)
        grid.attach(spin, 1, row, 1, 1)
        return spin

    # -- change handling -----------------------------------------------

    def _on_combo_changed(self, combo, key):
        if self._loading:
            return
        value = combo.get_active_id()
        if value is None:
            return
        self.config[key] = value
        self._changed()

    def _on_check_toggled(self, check, key):
        if self._loading:
            return
        self.config[key] = check.get_active()
        self._changed()

    def _on_spin_changed(self, spin, key):
        if self._loading:
            return
        self.config[key] = int(spin.get_value())
        self._changed()

    def _changed(self):
        self.on_change()

    def _save_and_close(self):
        self.config.save()
        self.destroy()

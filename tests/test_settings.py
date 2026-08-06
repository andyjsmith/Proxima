"""Preferences, per-VM settings, and the window state that is remembered."""

import pytest
from gi.repository import Gdk, GLib, Gtk

from proxima.api import devices as dev_mod
from proxima.api import notes as notes_mod
from proxima.ui import actions as action_defs
from proxima.ui.settings_dialog import SettingsDialog
from proxima.ui.vm_settings import RESPONSE_APPLY, VMSettingsDialog

from .conftest import (
    FakeAPI,
    FakeEditable,
    key_for,
    plan_protocol,
    pump,
    pump_until,
)

RUNNING = key_for(100)
STOPPED = key_for(102)


def notebook_tabs(container):
    notebook = container.get_children()[0]
    return [notebook.get_tab_label_text(c) for c in notebook.get_children()]


def label_texts(widget):
    """Every label on a page, headings included, however deeply nested."""
    found = []
    if isinstance(widget, Gtk.Label):
        found.append(widget.get_text())
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            found.extend(label_texts(child))
    return found


@pytest.mark.parametrize(
    "key", ["refresh_seconds", "task_refresh_seconds", "burst_seconds"]
)
def test_the_polling_intervals_are_configurable(config, key):
    assert key in config


def test_preferences_has_a_polling_page(window, config):
    dialog = SettingsDialog(window, config)
    pump(0.3)
    try:
        titles = notebook_tabs(dialog.get_children()[0])
    finally:
        dialog.destroy()
    assert "Polling" in titles, f"no Polling page in preferences: {titles}"


def test_the_settings_dialog_builds(window, config):
    dialog = SettingsDialog(window, config, on_change=window.apply_appearance)
    pump(0.3)
    dialog.destroy()


def test_stop_shutdown_and_reset_ask_by_default_but_pause_does_not(window, config):
    guest = window.sidebar.guests[RUNNING]
    asks = {
        name: bool(
            action_defs.confirmation_text(
                action_defs.ACTIONS_BY_NAME[name], guest, config
            )
        )
        for name in ("stop", "shutdown", "reset", "suspend")
    }
    assert asks == {"stop": True, "shutdown": True, "reset": True, "suspend": False}


def test_the_confirmation_toggles_are_honoured(window, config):
    guest = window.sidebar.guests[RUNNING]
    config["confirm_stop"] = False
    config["confirm_pause"] = True
    try:
        flipped = {
            name: bool(
                action_defs.confirmation_text(
                    action_defs.ACTIONS_BY_NAME[name], guest, config
                )
            )
            for name in ("stop", "suspend")
        }
        assert flipped == {"stop": False, "suspend": True}
    finally:
        config["confirm_stop"] = True
        config["confirm_pause"] = False


# -- per-VM settings ------------------------------------------------------


@pytest.fixture
def settings_guest(window):
    guest = window.sidebar.guests[RUNNING]
    FakeAPI.NOTES = {100: "Handwritten notes about this VM."}
    guest.settings_loaded = False
    guest.config_loaded = False
    yield guest
    FakeAPI.NOTES = {}
    guest.settings = {}
    window._clear_session_choices(RUNNING)


def test_settings_opens_for_a_guest_whose_config_was_never_read(window, settings_guest):
    opened = []
    real_show = window._show_guest_settings
    window._show_guest_settings = lambda g, a: opened.append((g, a))
    try:
        window.open_guest_settings(RUNNING)
        pump_until(lambda: bool(opened), 6)
    finally:
        window._show_guest_settings = real_show
    assert opened, "Settings never opened for a guest with no config read"


def test_saving_vm_settings_keeps_the_user_text_in_the_notes(
    window, api, settings_guest
):
    dialog = VMSettingsDialog(
        window,
        api,
        settings_guest,
        on_saved=lambda s: window._guest_settings_saved(RUNNING, s),
    )
    pump(0.3)
    try:
        assert notebook_tabs(dialog.get_content_area()) == [
            "Hardware",
            "Options",
            "Proxmox Manager",
        ]
        assert not dialog.apply_button.get_sensitive(), (
            "Apply is offered before anything has changed"
        )

        dialog.values["protocol"] = "vnc"
        dialog.values["audio"] = "disabled"
        dialog._sync_buttons()
        assert dialog.apply_button.get_sensitive(), (
            "Apply stayed disabled after a change"
        )
        dialog.emit("response", RESPONSE_APPLY)
        pump_until(lambda: not dialog._saving, 6)
        pump(0.3)

        written = FakeAPI.NOTES.get(100, "")
        assert "Handwritten notes" in written, (
            f"saving settings damaged the notes: {written!r}"
        )
        assert notes_mod.settings_of(written)["protocol"] == "vnc", (
            f"protocol not stored: {notes_mod.settings_of(written)}"
        )
        assert settings_guest.settings.get("audio") == "disabled", (
            "the guest did not take the saved settings"
        )
        assert not dialog.apply_button.get_sensitive(), (
            "Apply stayed live after a successful save"
        )
    finally:
        dialog.destroy()
        pump(0.2)

    # The stored protocol has to steer the next console, and the switches have
    # to follow the stored audio value.
    window.notebook.set_current_page(0)
    pump(0.3)
    assert plan_protocol(window, RUNNING) == "vnc", (
        "a VM set to VNC only still planned SPICE"
    )
    assert window._guest_switch(settings_guest, "audio") is False, (
        "the stored audio setting did not reach the switch"
    )

    # Reopen with SPICE is a temporary helper and must overrule the setting.
    window._force_spice.add(RUNNING)
    assert plan_protocol(window, RUNNING) == "spice", (
        "Reopen with SPICE could not overrule the VM setting"
    )


def test_settings_reset_to_the_defaults_leave_the_notes_clean(
    window, api, settings_guest
):
    dialog = VMSettingsDialog(window, api, settings_guest)
    pump(0.3)
    dialog.values.update(notes_mod.SETTINGS_DEFAULTS)
    dialog.emit("response", Gtk.ResponseType.OK)
    pump_until(lambda: not dialog._saving, 6)
    pump(0.3)
    assert "settings" not in notes_mod.parse(FakeAPI.NOTES.get(100, ""))[0], (
        "resetting to the defaults left a settings block"
    )


# -- hardware and options -------------------------------------------------


@pytest.fixture
def hardware(window, api):
    FakeAPI.HARDWARE = {}
    guest = window.sidebar.guests[STOPPED]
    guest.config = api.guest_config(guest.node, guest.vmid)
    dialog = VMSettingsDialog(window, api, guest)
    pump(0.4)
    yield dialog
    dialog.destroy()
    pump(0.3)
    FakeAPI.HARDWARE = {}


def test_the_hardware_page_reads_the_config_with_no_phantom_edits(hardware):
    assert not hardware.running, "the stopped guest was treated as running"
    assert hardware.nets and hardware.nets[0]["slot"] == "net0", (
        f"network devices were not read: {hardware.nets}"
    )
    assert not hardware.dirty, "a freshly opened settings dialog is already dirty"


def test_editing_a_nic_keeps_the_settings_the_dialog_does_not_show(hardware):
    # The rate limit is the one that is not on screen.
    hardware._on_net_bridge(FakeEditable("vmbr1"), hardware.nets[0])
    hardware._on_net_vlan(FakeEditable("42"), hardware.nets[0])
    hardware.nets[0]["pairs"] = dev_mod.set_pair(
        hardware.nets[0]["pairs"], "firewall", "0"
    )
    changes, _deletes = hardware._config_edits()
    rendered = changes.get("net0", "")
    assert "rate=10" in rendered, (
        f"editing a NIC dropped its other settings: {rendered}"
    )
    assert "bridge=vmbr1" in rendered and "tag=42" in rendered, (
        f"NIC edits did not take: {rendered}"
    )
    assert "BC:24:11:00:00:01" in rendered, f"editing a NIC lost its MAC: {rendered}"


def test_nics_can_be_added_and_removed(hardware):
    hardware._add_net()
    added = [e for e in hardware.nets if e["new"]]
    assert len(added) == 1 and added[0]["slot"] == "net1", (
        f"adding a NIC picked the wrong slot: {hardware.nets}"
    )
    changes, _deletes = hardware._config_edits()
    assert "net1" in changes, "the added NIC was not in the changes"

    hardware._remove_net(added[0])
    hardware._remove_net(hardware.nets[0])
    changes, deletes = hardware._config_edits()
    assert deletes == ["net0"], f"removing a NIC did not delete it: {deletes}"
    assert "net0" not in changes, "a removed NIC was both written and deleted"


def test_applying_hardware_changes_sends_the_digest(hardware, api):
    # A VM changed underneath is then refused rather than silently overwritten.
    hardware._remove_net(hardware.nets[0])
    hardware.emit("response", RESPONSE_APPLY)
    pump_until(lambda: not hardware._saving, 6)
    pump(0.3)
    written = [c for c in api.calls if c[0] == "set-config"]
    assert written, "applying hardware changes wrote nothing"
    assert written[-1][4] == "digest-102", (
        f"the config digest was not sent: {written[-1]}"
    )
    assert "net0" in written[-1][3], f"the removed NIC was not deleted: {written[-1]}"


def test_a_running_vm_hides_the_fields_proxmox_would_park_as_pending(window, api):
    guest = window.sidebar.guests[RUNNING]
    guest.config = api.guest_config(guest.node, guest.vmid)
    dialog = VMSettingsDialog(window, api, guest)
    pump(0.4)
    try:
        assert dialog.running, "the running guest was treated as stopped"
        gated = {}

        def collect(widget):
            if isinstance(widget, Gtk.Container):
                for child in widget.get_children():
                    collect(child)
            if "Stop the VM to change" in (widget.get_tooltip_text() or ""):
                gated[widget] = widget.get_sensitive()

        collect(dialog.get_content_area())
        assert gated, "nothing was gated on a running VM"
        assert not any(gated.values()), (
            "a stopped-only field was editable while running"
        )
    finally:
        dialog.destroy()
        pump(0.3)


# -- appearance and window state ------------------------------------------


@pytest.mark.parametrize("mode", ["light", "dark", "system"])
def test_every_colour_mode_applies(window, config, mode):
    config["color_mode"] = mode
    window.apply_appearance()
    pump(0.1)


def test_the_text_settings_sit_on_the_appearance_page(window, config):
    """The Text tab is gone -- its contents moved, rather than going with it.

    Two headings on one page, because a tab holding one combo box and a
    tickbox is a place to go looking rather than a place to find things.
    """
    dialog = SettingsDialog(window, config)
    pump(0.3)
    try:
        titles = notebook_tabs(dialog.get_children()[0])
        assert "Text" not in titles, f"the Text tab is still there: {titles}"
        assert titles[0] == "Appearance", titles

        notebook = dialog.get_children()[0].get_children()[0]
        appearance = label_texts(notebook.get_nth_page(0))
        for heading in ("Window", "Text"):
            assert heading in appearance, f"no {heading!r} heading: {appearance}"
        for moved in ("Font backend", "Interface font", "Antialiasing", "Hinting"):
            assert moved in appearance, f"{moved!r} was lost with the tab"
        assert "Colours" in appearance, appearance
        # The hinting combo is still reachable, since the backend check
        # disables it when FreeType is not in use.
        assert dialog.hint_combo is not None
    finally:
        dialog.destroy()
        pump(0.2)


def test_the_gtk_theme_is_not_a_setting(window, config):
    """Adwaita is pinned: the stylesheet and icons are drawn for it.

    Inheriting the desktop's theme instead would let anything from Yaru to
    Breeze redraw the layout the compact CSS was measured against.
    """
    from gi.repository import Gtk

    from proxima import theme as theme_mod

    assert "theme" not in config, "the theme setting is back"
    window.apply_appearance()
    pump(0.1)
    assert (
        Gtk.Settings.get_default().get_property("gtk-theme-name")
        == theme_mod.THEME_NAME
    )


@pytest.mark.parametrize("antialias", ["grayscale", "subpixel", "none", "default"])
@pytest.mark.parametrize("hint", ["slight", "full", "medium", "none"])
def test_every_antialias_and_hinting_combination_applies(
    window, config, antialias, hint
):
    config["antialias"] = antialias
    config["hint_style"] = hint
    window.apply_appearance()
    pump(0.05)


class _StateEvent:
    def __init__(self, state):
        self.new_window_state = state


@pytest.fixture
def sized(window):
    """An ordinary window that has just been resized."""
    window._maximized = False
    window._fullscreen_state = False
    window._normal_size = (1111, 777)
    window._on_configure()
    return window._normal_size


def test_maximising_does_not_overwrite_the_restore_size(window, sized):
    window._on_window_state(window, _StateEvent(Gdk.WindowState.MAXIMIZED))
    window._on_configure()
    assert window._maximized, "maximising was not noticed"
    assert window._normal_size == sized, (
        f"maximising overwrote the restore size: {window._normal_size}"
    )


def test_fullscreen_leaves_the_saved_window_state_alone(window, sized):
    # Fullscreen is a console mode, never a saved window preference.
    window._on_window_state(window, _StateEvent(Gdk.WindowState.MAXIMIZED))
    window._on_window_state(window, _StateEvent(Gdk.WindowState.FULLSCREEN))
    window._on_configure()
    assert window._maximized, "fullscreen cleared the maximised flag"
    assert window._normal_size == sized, "fullscreen overwrote the restore size"


def test_a_maximised_window_saves_the_size_to_restore_to(window, config, sized):
    window._on_window_state(window, _StateEvent(Gdk.WindowState.MAXIMIZED))
    window._save_layout()
    assert config["window_maximized"], "maximised state was not saved"
    assert (config["window_width"], config["window_height"]) == sized


def test_an_unmaximised_window_saves_the_size_it_has(window, config, sized):
    window._on_window_state(window, _StateEvent(Gdk.WindowState.MAXIMIZED))
    window._save_layout()
    window._on_window_state(window, _StateEvent(0))
    window._normal_size = (900, 640)
    window._save_layout()
    assert not config["window_maximized"], "unmaximising did not clear the saved flag"
    assert (config["window_width"], config["window_height"]) == (900, 640)


# -- an unfocused window keeps its colours --------------------------------


def test_the_backdrop_flag_is_gone_before_the_window_is_repainted(window):
    """Losing focus must not cost even one dimmed frame.

    theme.keep_active() clears GTK's BACKDROP flag rather than restyling the
    state, but it has to wait for an idle to do it. At the default idle
    priority the redraw got there first: the window was painted in backdrop
    colours for a frame, and the theme's CSS transitions turned that one
    frame into a visible fade out and back.
    """
    seen = []
    window.set_state_flags(Gtk.StateFlags.BACKDROP, False)
    GLib.idle_add(
        lambda: (
            seen.append(bool(window.get_state_flags() & Gtk.StateFlags.BACKDROP)),
            False,
        )[1],
        # GDK_PRIORITY_REDRAW: what paints the frame.
        priority=GLib.PRIORITY_HIGH_IDLE + 20,
    )
    assert pump_until(lambda: bool(seen), 3), "the redraw never came round"
    assert not seen[0], (
        "the window still carried BACKDROP when it came to be painted, "
        "so it is drawn dimmed for a frame"
    )


def bridge_options(entry):
    """What a NIC row's bridge dropdown actually offers."""
    combo = entry["bridge_widget"]
    model = combo.get_model()
    return [row[0] for row in model] if model is not None else []


def test_a_nic_added_later_can_still_choose_its_bridge(hardware):
    """The dropdown is filled when the row is built, not only once.

    The bridge list arrives on a worker thread and used to be written into
    the rows that existed at that moment. A NIC added afterwards got an
    empty model -- and GTK draws the button of an empty combo insensitive,
    so it looked like the bridge could not be changed at all.
    """
    assert bridge_options(hardware.nets[0]) == ["vmbr0", "vmbr1"], (
        "the first row never got the bridge list"
    )

    hardware._add_net()
    pump(0.3)
    added = hardware.nets[-1]
    assert bridge_options(added) == ["vmbr0", "vmbr1"], (
        "a NIC added after the lookup landed has an empty bridge list"
    )
    assert added["bridge_widget"].get_child().get_text(), (
        "the new NIC was not given a bridge to start on"
    )


def test_removing_a_nic_leaves_the_others_editable(hardware):
    """Removal rebuilds every row, so every row has to be refilled."""
    hardware._add_net()
    pump(0.3)
    hardware._remove_net(hardware.nets[-1])
    pump(0.3)
    for entry in hardware.nets:
        assert bridge_options(entry) == ["vmbr0", "vmbr1"], (
            f"{entry['slot']} lost its bridge list when another was removed"
        )


def test_refreshing_the_bridge_list_keeps_what_each_nic_is_on(hardware):
    """remove_all() clears the entry too, which would blank the bridge."""
    before = hardware.nets[0]["bridge_widget"].get_child().get_text()
    assert before, "the first NIC has no bridge to preserve"
    hardware._apply_bridges(["vmbr0", "vmbr1", "vnet-dmz"])
    pump(0.2)
    assert hardware.nets[0]["bridge_widget"].get_child().get_text() == before
    assert "vnet-dmz" in bridge_options(hardware.nets[0]), "the refresh did not land"

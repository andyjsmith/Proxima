"""The indicators along the bottom, and the panes the toolbar toggles."""

import pytest
from gi.repository import Gtk

from .conftest import FakeConsole, key_for, pump, pump_until

RUNNING = key_for(100)
NO_AGENT = key_for(101)
CONTAINER = key_for(202, node="pve-node-02", kind="lxc")


# -- the microphone -------------------------------------------------------
# SPICE's record channel, carrying this machine's microphone into the guest.
# Unlike audio it moves live, because it is a channel rather than a property
# of the session -- and unlike every other switch it defaults to off.

SPICE_AUDIO = "device=ich9-intel-hda,driver=spice"


@pytest.fixture
def audio_guest(window, api):
    """The running guest, with a SPICE audio device on it."""
    api.HARDWARE[100] = {"audio0": SPICE_AUDIO}
    window.refresh()
    pump_until(
        lambda: (
            (window.sidebar.guests[RUNNING].config or {}).get("audio0") == SPICE_AUDIO
        ),
        8,
    )
    yield window.sidebar.guests[RUNNING]
    api.HARDWARE.pop(100, None)
    window.refresh()
    pump(0.4)


def test_the_microphone_defaults_to_off(window, audio_guest, switch_console):
    """Nothing else defaults off. This one has to."""
    assert window._guest_switch(audio_guest, "microphone") is False, (
        "the microphone was on for a guest that never asked for it"
    )
    assert switch_console.capture_audio is False, (
        "a console was built with the microphone already open"
    )
    window._update_microphone_indicator(switch_console)
    pump(0.2)
    assert window.mic_icon.struck, "the microphone icon is not struck when off"


def test_the_microphone_toggles_live_and_needs_no_reconnect(
    window, config, audio_guest, switch_console
):
    window._toggle_microphone()
    status = window.status_label_main.get_text()
    try:
        assert switch_console.capture_audio is True, (
            "the microphone switch did not reach the console"
        )
        assert RUNNING not in window._reconnecting, (
            f"turning the microphone on rebuilt the console, said {status!r}"
        )
        assert not window.mic_icon.struck, "icon stayed struck with the mic on"
        assert (config.get("guest_prefs") or {}).get(RUNNING, {}) == {}, (
            "the microphone button wrote a saved preference"
        )

        window._toggle_microphone()
        pump(0.2)
        assert switch_console.capture_audio is False, "the microphone would not go off"
        assert window.mic_icon.struck
    finally:
        config["guest_prefs"] = {}


def test_the_microphone_is_not_offered_while_audio_is_off(
    window, audio_guest, switch_console
):
    """spice-gtk builds playback and record together or not at all."""
    # Set directly rather than through _toggle_audio: that one rebuilds the
    # console, and the replacement would outlive this test.
    window._session_switches[(RUNNING, "audio")] = False
    try:
        window._update_microphone_indicator(switch_console)
        pump(0.2)
        assert not window.mic_icon.can_toggle, (
            "the microphone offered itself with the audio backend switched off"
        )
        assert "Audio" in (window.mic_icon.get_tooltip_text() or ""), (
            f"no explanation given: {window.mic_icon.get_tooltip_text()!r}"
        )
    finally:
        window._session_switches.pop((RUNNING, "audio"), None)


def test_a_guest_with_no_audio_input_cannot_turn_the_microphone_on(
    window, audio_guest, switch_console
):
    """No record channel means no reconnect would help, so none is tried."""
    switch_console.has_record_channel = False
    window._update_microphone_indicator(switch_console)
    pump(0.2)
    assert not window.mic_icon.can_toggle, (
        "offered a microphone the guest never provided an input for"
    )

    window._toggle_microphone()
    status = window.status_label_main.get_text()
    assert RUNNING not in window._reconnecting, (
        f"rebuilt the console for a channel that does not exist, said {status!r}"
    )


def test_spice_declares_the_microphone_contract():
    """The fake console in conftest mirrors this; keep them honest."""
    from proxima.console.spice import SpiceConsole

    assert SpiceConsole.supports["microphone"] is True
    assert "audio" in SpiceConsole.RECONNECT_SWITCHES, (
        "audio no longer asks for a rebuild"
    )
    assert "microphone" not in SpiceConsole.RECONNECT_SWITCHES, (
        "the microphone asks for a rebuild it does not need"
    )


def test_the_status_bar_follows_the_tab_it_switches_to(window, audio_guest):
    """Switching tabs must not leave the indicators describing the old one.

    The tree selection made at the end of a page switch refreshes the
    indicators with no console of its own to go on, so it read the notebook
    -- which mid-switch still reports the tab being *left*. The incoming tab
    ended up described by the outgoing one: a SPICE console with sound
    claiming to be VNC, or reporting the other guest's missing audio device.
    """
    # Let anything already in flight for these guests land first.
    pump(1.0)
    window._console_offline.pop(RUNNING, None)

    spice = FakeConsole("with-audio")
    spice.guest_key = RUNNING
    window.consoles[RUNNING] = spice
    window.panes.append(spice, Gtk.Label(label="spice"))

    vnc = FakeConsole("no-audio")
    vnc.guest_key = NO_AGENT
    vnc.protocol = "vnc"
    vnc.supports = dict(FakeConsole.supports, audio=False, microphone=False)
    window.consoles[NO_AGENT] = vnc
    window.panes.append(vnc, Gtk.Label(label="vnc"))
    pump(0.5)
    try:
        window.notebook.set_current_page(window.notebook.page_num(vnc))
        pump(0.5)
        assert window.current_console() is vnc, "the VNC tab did not come forward"

        window.notebook.set_current_page(window.notebook.page_num(spice))
        pump(0.5)
        assert window.current_console() is spice, "the SPICE tab did not come forward"

        tip = window.audio_icon.get_tooltip_text() or ""
        assert "VNC" not in tip, f"audio indicator describes the other tab: {tip!r}"
        assert "no device" not in tip, (
            f"audio indicator describes the other guest: {tip!r}"
        )
        assert SPICE_AUDIO in tip, f"the audio device is not reported: {tip!r}"
        assert window.audio_icon.can_toggle, (
            "audio cannot be switched on the SPICE tab it belongs to"
        )
    finally:
        window.close_console_widget(spice)
        window.close_console_widget(vnc)
        pump(0.4)


def test_the_agent_menu_is_enabled_when_the_agent_answers(window):
    window.sidebar.select_key(RUNNING)
    pump(0.8)
    assert window._agent_ok, "guest agent indicator did not go green"
    assert all(i.get_sensitive() for i in window.agent_items.values()), (
        "guest agent menu is disabled despite a live agent"
    )


def test_the_agent_menu_is_disabled_when_the_agent_is_absent(window, api):
    api.agent_available = False
    try:
        window.sidebar.select_key(NO_AGENT)
        pump(0.8)
        assert not window._agent_ok, "agent indicator stayed on with no agent"
        assert not any(i.get_sensitive() for i in window.agent_items.values()), (
            "guest agent menu enabled with no agent"
        )
    finally:
        api.agent_available = True


def test_the_agent_indicator_is_dimmed_for_a_container(window):
    # A container has no QEMU guest agent at all.
    window.sidebar.select_key(CONTAINER)
    pump(0.5)
    assert window.qga_icon.get_opacity() <= 0.3


def test_telemetry_reports_size_rate_and_frame_rate(window):
    window.sidebar.select_key(RUNNING)
    pump(0.3)
    console = FakeConsole()
    console.guest_key = "telemetry"
    window.consoles["telemetry"] = console
    window.panes.append(console, Gtk.Label(label="t"))
    pump(0.3)
    try:
        window._sample_telemetry()
        # On the protocol label's tooltip, not a label of its own: a readout
        # that redraws every second does not belong in the corner of the eye.
        text = window.protocol_label.get_tooltip_text() or ""
        assert "1920x1080" in text and "MB/s" in text and "fps" in text, (
            f"the protocol tooltip reads {text!r}"
        )
        assert window.vdagent_icon.get_opacity() >= 0.9, (
            "vdagent indicator dim despite a connected agent"
        )
    finally:
        window.close_console("telemetry")
        pump(0.2)


@pytest.fixture
def switch_console(window):
    """A stand-in console for the running guest, for the status bar switches."""
    # Let anything already in flight for this guest land first, or it will
    # replace the stand-in halfway through and the assertions below would be
    # reading a widget that is no longer on screen.
    pump(1.0)
    window._console_offline.pop(RUNNING, None)
    console = FakeConsole()
    console.guest_key = RUNNING
    window.consoles[RUNNING] = console
    window.panes.append(console, Gtk.Label(label="s"))
    pump(0.4)
    yield console
    window.close_console(RUNNING)
    window.notebook.set_current_page(0)
    pump(0.3)


def test_clipboard_toggles_live_and_saves_nothing(window, config, switch_console):
    guest = window.sidebar.guests[RUNNING]
    assert window._guest_switch(guest, "clipboard") is True, (
        "clipboard does not default to on"
    )

    window._toggle_clipboard()
    pump(0.3)
    try:
        stored = (config.get("guest_prefs") or {}).get(RUNNING, {})
        assert "clipboard" not in stored, (
            f"the clipboard button wrote a saved preference: {stored}"
        )
        live = window.consoles.get(RUNNING)
        assert getattr(live, "share_clipboard", None) is False, (
            "clipboard switch did not reach the console"
        )
        assert window.vdagent_icon.struck, (
            "clipboard icon is not struck through when off"
        )

        window._toggle_clipboard()
        pump(0.3)
        live = window.consoles.get(RUNNING)
        assert not window.vdagent_icon.struck
        assert getattr(live, "share_clipboard", False), (
            "clipboard did not toggle back on"
        )
    finally:
        config["guest_prefs"] = {}


def test_audio_reconnects_rather_than_claiming_success(window, config, switch_console):
    # Audio cannot be changed on a live SPICE session, so the toggle has to
    # rebuild the console.
    window._toggle_audio()
    # Read before pumping: the rebuilt console's own status messages overwrite
    # the status bar a moment later.
    status = window.status_label_main.get_text()
    # The rebuild is recorded per guest, with a deadline. Nothing has to
    # clear it first any more: the guard only holds off the poll's own
    # reconnects, and flipping a switch is not the poll.
    reconnecting = RUNNING in window._reconnecting
    stored = (config.get("guest_prefs") or {}).get(RUNNING, {})
    try:
        assert "audio" not in stored, (
            f"the audio button wrote a saved preference: {stored}"
        )
        # Deliberately the console the toggle acted on, not whatever is
        # installed now: audio needs a rebuild, so a placeholder is already
        # standing in while the replacement connects.
        assert switch_console.play_audio is False, (
            "audio switch did not reach the console"
        )
        assert reconnecting, f"audio toggle did not reconnect, said {status!r}"
        pump(0.5)

        # The session switch has to reach a console built afterwards, or the
        # reconnect would bring the sound straight back.
        guest = window.sidebar.guests[RUNNING]
        assert window._guest_switch(guest, "audio") is False, (
            "a new console would not see the audio switch"
        )

        # ...and closing the tab has to forget it, because the button is only
        # ever about the console in front of you.
        window.close_console(RUNNING)
        pump(0.3)
        assert window._guest_switch(guest, "audio") is True, (
            "the audio switch outlived its console"
        )
    finally:
        config["guest_prefs"] = {}


def test_neither_switch_is_clickable_on_a_vnc_console(window):
    class _VncConsoleStub(Gtk.Box):
        protocol = "vnc"
        supports = {
            "auto_resize": False,
            "scaling": True,
            "codec": False,
            "compression": False,
            "refresh": True,
            "ctrl_alt_del": True,
            "clipboard": False,
            "audio": False,
        }

        def __init__(self):
            super().__init__()
            self.title = "vnc-stub"
            self.guest_key = RUNNING
            self.pack_start(Gtk.Label(label="vnc"), True, True, 0)

        def shutdown(self):
            pass

    stub = _VncConsoleStub()
    window.consoles[RUNNING] = stub
    window.panes.append(stub, Gtk.Label(label="v"))
    pump(0.4)
    try:
        assert not window.audio_icon.can_toggle, "audio is toggleable on a VNC console"
        assert not window.vdagent_icon.can_toggle, (
            "clipboard is toggleable on a VNC console"
        )
    finally:
        window.close_console(RUNNING)
        pump(0.3)


def test_no_console_leaves_the_switches_dimmed_not_struck(window):
    # Nothing open at all: with no global summary page, "no console" means
    # an empty tab strip rather than a tab that happens not to be one.
    for page in window.panes.all_pages():
        window.close_console_widget(page)
    pump(0.3)
    assert window.notebook.get_n_pages() == 0
    assert not window.vdagent_icon.struck, "clipboard icon struck with no console open"
    assert not window.vdagent_icon.can_toggle and not window.audio_icon.can_toggle, (
        "switches are clickable with no console open"
    )


def test_the_tree_toggle_opens_and_closes_the_sidebar(window, config):
    assert window.tree_tool_item.get_active() and window.sidebar.get_visible(), (
        "the tree starts hidden"
    )
    window.tree_tool_item.set_active(False)
    pump(0.3)
    try:
        assert not window.sidebar.get_visible(), (
            "the tree toggle did not hide the sidebar"
        )
        assert config.get("sidebar_visible") is False, (
            "hiding the tree was not remembered"
        )
    finally:
        window.tree_tool_item.set_active(True)
        pump(0.3)
    assert window.sidebar.get_visible(), (
        "the tree toggle did not bring the sidebar back"
    )


def test_the_tasks_toggle_opens_and_closes_the_task_pane(window):
    window.tasks_tool_item.set_active(True)
    pump(0.5)
    assert window.task_feed.get_visible(), "the tasks toggle did not open the pane"
    window.tasks_tool_item.set_active(False)
    pump(0.3)
    assert not window.task_feed.get_visible(), "the tasks toggle did not close the pane"


def test_closing_the_task_pane_releases_its_toolbar_button(window):
    window.tasks_tool_item.set_active(True)
    pump(0.4)
    window.task_feed.close()  # the pane's own X
    pump(0.3)
    assert not window.tasks_tool_item.get_active(), (
        "closing the pane left its toolbar button pressed in"
    )


# -- smartcard redirection -------------------------------------------------


def test_smartcard_defaults_off_and_needs_a_rebuild(window, config, switch_console):
    """A smartcard is somebody's identity, so nothing shares it by accident."""
    guest = window.sidebar.guests[RUNNING]
    assert window._guest_switch(guest, "smartcard") is False, (
        "a guest that never asked was offering the card reader"
    )
    window._update_smartcard_indicator(switch_console)
    pump(0.2)
    assert window.smartcard_icon.struck, "the icon is not struck when off"
    assert window.smartcard_icon.can_toggle, (
        "with a reader here and a channel offered, it should be switchable"
    )

    window._toggle_smartcard()
    # Read before pumping: the rebuild finishes and clears the mark, exactly
    # as in the audio test above.
    reconnecting = RUNNING in window._reconnecting
    pump(0.3)
    try:
        assert switch_console.share_smartcard is True, "the switch did not reach it"
        # enable-smartcard is read when the session is built.
        assert reconnecting, "turning it on did not reconnect"
        assert (config.get("guest_prefs") or {}).get(RUNNING, {}) == {}, (
            "the status bar switch wrote a saved preference"
        )
    finally:
        config["guest_prefs"] = {}


def test_no_reader_on_this_machine_is_said_plainly(window, switch_console):
    switch_console.readers = []
    window._update_smartcard_indicator(switch_console)
    pump(0.2)
    assert not window.smartcard_icon.can_toggle, (
        "offered to share a reader this machine does not have"
    )
    assert "reader" in (window.smartcard_icon.get_tooltip_text() or "").lower(), (
        f"no explanation: {window.smartcard_icon.get_tooltip_text()!r}"
    )


def test_a_guest_with_no_ccid_device_says_so_once_it_is_on(window, switch_console):
    """The usual case on Proxmox, which adds no CCID device by default."""
    switch_console.has_smartcard_channel = False
    window._session_switches[(RUNNING, "smartcard")] = True
    try:
        window._update_smartcard_indicator(switch_console)
        pump(0.2)
        tip = window.smartcard_icon.get_tooltip_text() or ""
        assert "CCID" in tip, f"the reason was not given: {tip!r}"
    finally:
        window._session_switches.pop((RUNNING, "smartcard"), None)


def test_spice_declares_the_smartcard_contract():
    from proxima.console.spice import SpiceConsole

    assert SpiceConsole.supports["smartcard"] is True
    assert "smartcard" in SpiceConsole.RECONNECT_SWITCHES, (
        "enable-smartcard is read at session build, so it needs a rebuild"
    )


# -- file transfer --------------------------------------------------------
# Files dragged onto a console, which spice-gtk sends to the guest. The
# switch is the display widget's drop target, so it applies live and never
# needs the session rebuilt.


def test_file_transfer_is_on_by_default(window, switch_console):
    guest = window.context_guest(switch_console)
    assert window._guest_switch(guest, "file_transfer") is True, (
        "file transfer defaulted to off"
    )
    assert switch_console.allow_file_transfer is True, (
        "the console was built refusing dropped files"
    )
    window._update_file_transfer_indicator(switch_console)
    pump(0.2)
    assert not window.transfer_icon.struck, (
        "the file transfer icon is struck through while it is on"
    )


def test_file_transfer_toggles_live(window, config, switch_console):
    window._toggle_file_transfer()
    status = window.status_label_main.get_text()
    try:
        assert switch_console.allow_file_transfer is False, (
            "the file transfer switch did not reach the console"
        )
        assert RUNNING not in window._reconnecting, (
            f"turning file transfer off rebuilt the console, said {status!r}"
        )
        assert window.transfer_icon.struck, "the icon is not struck when off"

        window._toggle_file_transfer()
        pump(0.2)
        assert switch_console.allow_file_transfer is True, (
            "file transfer would not go back on"
        )
        assert not window.transfer_icon.struck
    finally:
        config["guest_prefs"] = {}


def test_file_transfer_needs_the_guest_agent(window, switch_console):
    """The agent writes the file, so without one there is nothing to receive
    it. Still clickable: the switch is ours, the agent is the guest's."""
    switch_console.agent_connected = False
    try:
        window._update_file_transfer_indicator(switch_console)
        pump(0.2)
        tooltip = window.transfer_icon.get_tooltip_text() or ""
        assert "spice-vdagent" in tooltip, (
            f"the indicator does not say the agent is missing, said {tooltip!r}"
        )
        assert not window.transfer_icon.struck, (
            "a missing agent read as somebody switching the feature off"
        )
    finally:
        switch_console.agent_connected = True

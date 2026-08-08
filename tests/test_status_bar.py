"""The indicators along the bottom, and the panes the toolbar toggles."""

import pytest
from gi.repository import Gtk

from .conftest import FakeConsole, key_for, pump

RUNNING = key_for(100)
NO_AGENT = key_for(101)
CONTAINER = key_for(202, node="pve-node-02", kind="lxc")


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
        text = window.telemetry_label.get_text()
        assert "1920x1080" in text and "MB/s" in text and "fps" in text, (
            f"telemetry label reads {text!r}"
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

"""Which console a guest gets, and how its tab behaves."""

import time

import pytest
from gi.repository import Gtk

from proxima.console import keys as keys_mod
from proxima.console import status_panel as status_panel_mod
from proxima.ui import split as split_mod

from .conftest import (
    FakeConsole,
    key_for,
    plan_protocol,
    pump,
    pump_until,
)

RUNNING = key_for(100)
STOPPED = key_for(102)


def open_summary(window, key, seconds=6):
    """Open a guest's tab on its summary, and wait for the detail to land."""
    window.open_console(key)
    pump(0.6)
    tab = window.tabs[key]
    tab.show_view("summary", by_user=True)
    guest = window.sidebar.guests[key]
    tab.summary.show_guest(guest, window.api_for(guest))
    pump_until(
        lambda: "checking" not in tab.summary.values["console"].get_text(), seconds
    )
    return tab.summary


@pytest.mark.parametrize(
    ("key", "expected", "vga"),
    [
        (key_for(100), "spice", "qxl"),
        (key_for(101), "vnc", "std"),
        (key_for(200, node="pve-node-02"), "spice", "virtio"),
        (key_for(201, node="pve-node-02"), "spice", "virtio-gl"),
    ],
)
def test_planning_picks_the_protocol_the_display_implies(window, key, expected, vga):
    assert plan_protocol(window, key) == expected, f"vga={vga}"


def test_an_unknown_display_type_attempts_spice(window, api):
    # Containers never get SPICE, so borrow the container row for the VM path.
    key = key_for(202, node="pve-node-02", kind="lxc")
    guest = window.sidebar.guests[key]
    guest.kind = "qemu"
    try:
        before = len([c for c in api.calls if c[0] == "spice"])
        plan_protocol(window, key)
        after = len([c for c in api.calls if c[0] == "spice"])
        assert after > before, "unknown display type did not attempt SPICE"
    finally:
        guest.kind = "lxc"


# -- containers, which choose between serial and VNC -----------------------

CONTAINER = key_for(202, node="pve-node-02", kind="lxc")


@pytest.fixture
def container(window):
    """The container, with every per-session override cleared afterwards.

    The window is shared by the whole file, so a test that forces a protocol
    and walks away decides the answer for the next one.
    """
    guest = window.sidebar.guests[CONTAINER]
    yield guest
    window._force_vnc.discard(CONTAINER)
    window._force_serial.discard(CONTAINER)
    window.config["prefer_vnc"] = False
    guest.settings = {}
    guest.console_note = ""


def test_a_container_opens_on_the_serial_console(window, container):
    assert plan_protocol(window, CONTAINER) == "serial"


def test_reopen_with_vnc_overrules_the_serial_default(window, container):
    window._force_vnc.add(CONTAINER)
    assert plan_protocol(window, CONTAINER) == "vnc"


def test_the_container_protocol_setting_is_honoured(window, container):
    container.settings = {"protocol": "vnc"}
    assert plan_protocol(window, CONTAINER) == "vnc"

    # And "serial only" holds against the global preference, which is the
    # only thing that setting is for.
    container.settings = {"protocol": "serial"}
    window.config["prefer_vnc"] = True
    assert plan_protocol(window, CONTAINER) == "serial"


def test_always_use_vnc_covers_containers_too(window, container):
    window.config["prefer_vnc"] = True
    assert plan_protocol(window, CONTAINER) == "vnc"


def test_a_container_falls_back_to_vnc_when_termproxy_refuses(
    window, container, monkeypatch
):
    from proxima.api import ProxmoxError

    def refuse(*_args, **_kwargs):
        raise ProxmoxError("termproxy refused: no permission")

    monkeypatch.setattr(window.api_for(container), "term_ticket", refuse)
    assert plan_protocol(window, CONTAINER) == "vnc", (
        "a refused termproxy ticket did not fall back to VNC"
    )
    assert "termproxy refused" in container.console_note, (
        "the fallback did not say why it happened"
    )


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        (key_for(200, node="pve-node-02"), "SPICE"),
        (key_for(101), "VNC"),
        (key_for(201, node="pve-node-02"), "SPICE"),
    ],
)
def test_the_summary_reports_the_console_it_settled_on(window, key, expected):
    # 'checking...' must not survive a completed lookup: an unrecognised
    # adapter reports None for spice_capable, which used to be
    # indistinguishable from "not looked up".
    summary = open_summary(window, key)
    try:
        text = summary.values["console"].get_text()
        display = summary.values["display"].get_text()
        assert "checking" not in text, f"{key}: summary stuck on 'checking...'"
        assert text.startswith(expected), (
            f"{key}: display {display!r} reported console {text!r}"
        )
    finally:
        window.close_console(key)
        pump(0.3)


def test_the_summary_holds_its_detail_across_polls(window, config, api):
    # Rebuilding the tree empties the selection for an instant. If that
    # reaches the summary it discards its detail and re-fetches, which reads
    # as agent/IP/OS blinking to "-" every refresh.
    summary = open_summary(window, RUNNING)
    was = (config.get("poll_idle_seconds"), config.get("poll_active_seconds"))
    handler = None
    try:
        watched = ("agent", "address", "os")
        pump_until(
            lambda: summary.values["agent"].get_text() not in ("-", "checking..."), 6
        )
        populated = {f: summary.values[f].get_text() for f in watched}
        assert all(v not in ("-", "") for v in populated.values()), (
            f"summary never populated: {populated}"
        )

        spurious = []
        handler = window.sidebar.connect(
            "guest-selected", lambda _s, key: spurious.append(key)
        )
        # Polls, not seconds: what is under test is what a rebuild does to
        # the summary, so the measure is how many rebuilds it survives. At
        # the default four-second cadence a five-second watch caught barely
        # one; driving the poll at a second covers three in less time.
        config["poll_idle_seconds"] = 1
        config["poll_active_seconds"] = 1
        window._restart_poll()
        first = api.calls.count("guests")
        blanks = {f: 0 for f in watched}
        deadline = time.time() + 15
        while api.calls.count("guests") - first < 3 and time.time() < deadline:
            pump(0.1)
            for field in watched:
                if summary.values[field].get_text() == "-":
                    blanks[field] += 1
        assert api.calls.count("guests") - first >= 3, "the window stopped polling"
        assert not any(blanks.values()), f"summary blanked during polling: {blanks}"
        assert not spurious, f"tree rebuild emitted selection changes: {spurious}"
    finally:
        if handler is not None:
            window.sidebar.disconnect(handler)
        config["poll_idle_seconds"], config["poll_active_seconds"] = was
        window._restart_poll()
        window.close_console(RUNNING)
        pump(0.3)


def test_a_stopped_guest_still_gets_a_placeholder_tab(window):
    tabs_before = window.notebook.get_n_pages()
    window.open_console(STOPPED)
    pump(0.6)
    try:
        console = window.consoles.get(STOPPED)
        assert console is not None, "no tab opened for a stopped guest"
        assert window.notebook.get_n_pages() == tabs_before + 1
        assert console.protocol == "offline", (
            f"stopped guest got a {console.protocol} console"
        )
        assert console.status_panel.get_visible(), (
            "stopped guest tab shows no explanation"
        )
    finally:
        window.close_console(STOPPED)
        pump(0.3)


def test_a_reconnect_replaces_the_console_and_keeps_the_tab(window):
    # Starting a stopped guest must not make its console vanish and come
    # back. The swap itself is what regressed, so it is exercised directly:
    # building a real console here would need a live SPICE or VNC server.
    window.notebook.set_current_page(0)
    window.open_console(STOPPED)
    pump(0.8)
    placeholder = window.consoles.get(STOPPED)
    assert placeholder is not None and placeholder.protocol == "offline", (
        "stopped guest did not get a placeholder console"
    )

    # A tab after it, so a rebuild that appended instead of replacing would
    # show up as a move to the end.
    trailing = FakeConsole("trailing")
    trailing.guest_key = "trailing"
    window.consoles["trailing"] = trailing
    window.panes.append(trailing, Gtk.Label(label="trailing"))
    pump(0.3)
    try:
        pages_before = window.notebook.get_n_pages()
        position_before = window.notebook.page_num(placeholder)

        rebuilt = FakeConsole(window.sidebar.guests[STOPPED].name)
        window._install_console(window.sidebar.guests[STOPPED], rebuilt)
        pump(0.4)

        assert window.consoles.get(STOPPED) is rebuilt, (
            "install did not take over the guest's console"
        )
        assert window.notebook.get_n_pages() == pages_before, (
            f"tab count changed on reconnect: {pages_before} -> "
            f"{window.notebook.get_n_pages()}"
        )
        assert window.notebook.page_num(rebuilt) == position_before, (
            "the console moved tab on reconnect"
        )
        assert window.notebook.page_num(placeholder) < 0, (
            "the replaced console is still in the notebook"
        )
    finally:
        window.close_console_widget(trailing)
        window.close_console(STOPPED)
        pump(0.3)


def test_a_console_pops_out_into_its_own_window_and_back(window):
    window.sidebar.select_key(STOPPED)
    pump(0.3)
    console = FakeConsole("popme")
    console.guest_key = STOPPED
    window.consoles[STOPPED] = console
    window.panes.append(console, Gtk.Label(label="p"))
    pump(0.4)
    try:
        tabs_before = window.notebook.get_n_pages()
        window.popout_console()
        pump(0.6)
        popout = window._popouts.get(STOPPED)
        assert popout is not None, "pop out did not create a window"
        assert window.notebook.get_n_pages() == tabs_before - 1, (
            "pop out did not remove the tab"
        )
        assert console.get_parent() is not None, "popped-out console lost its parent"
        assert popout._action_items["start"].get_sensitive(), (
            "pop-out toolbar did not follow guest state"
        )

        popout.return_to_tabs()
        pump(0.6)
        assert window.notebook.get_n_pages() == tabs_before, (
            "returning from pop out did not restore the tab"
        )
        assert not window._popouts, "pop-out window was not forgotten"
    finally:
        window.close_console(STOPPED)
        pump(0.3)


def test_the_toolbar_context_follows_the_front_tab(window):
    window.notebook.set_current_page(0)
    window.sidebar.select_key(RUNNING)
    pump(0.5)
    assert window.context_guest() is not None and window.context_guest().vmid == 100, (
        "summary page context is not the tree selection"
    )

    console = FakeConsole("ctx")
    console.guest_key = STOPPED
    window.consoles[STOPPED] = console
    window.panes.append(console, Gtk.Label(label="c"))
    pump(0.5)
    try:
        assert window.context_guest().vmid == 102, (
            "console tab did not take over the context"
        )
        # Clicking around the tree must not re-aim the toolbar.
        window.sidebar.select_key(RUNNING)
        pump(0.5)
        assert window.context_guest().vmid == 102, (
            f"tree selection overrode the console tab's context "
            f"(got {window.context_guest().vmid})"
        )
        assert not any(w.get_sensitive() for w in window._action_items["stop"]), (
            "Stop enabled for the stopped guest in the front tab"
        )
    finally:
        window.close_console(STOPPED)
        window.notebook.set_current_page(0)
        pump(0.4)


def test_console_preferences_are_saved_per_guest(window, config):
    console = FakeConsole("pref")
    console.guest_key = RUNNING
    window.consoles["pref"] = console
    window.panes.append(console, Gtk.Label(label="pref"))
    pump(0.4)
    try:
        window.scaling_item.set_active(True)
        pump(0.3)
        stored = (config.get("guest_prefs") or {}).get(RUNNING, {})
        assert stored.get("scale_to_fit"), (
            f"scale-to-fit not saved per guest (got {stored})"
        )
        assert window.guest_prefs(RUNNING)["scaling"] is True, (
            "stored guest preference does not read back"
        )
    finally:
        window.close_console("pref")
        window.notebook.set_current_page(0)
        config["guest_prefs"] = {}
        pump(0.3)


@pytest.fixture
def live_console(window):
    """A console tab for the running guest, once the real one has landed.

    open_console puts a placeholder up first and swaps it when the session
    connects, so a reference captured before that goes stale mid-test.
    """
    window.open_console(RUNNING)
    # The page in the notebook is the guest's tab, not the console inside it,
    # so the console is asked about through window.tabs. Waiting on
    # get_tab_label(console) instead looks like a live console never arriving:
    # it is always None, and the wait always ran to its full timeout.
    landed = pump_until(
        lambda: (
            window.consoles.get(RUNNING) is not None
            and type(window.consoles[RUNNING]).__name__ != "PlaceholderConsole"
            and window.tabs.get(RUNNING) is not None
            and window.notebook.page_num(window.tabs[RUNNING]) != -1
        ),
        10,
        step=0.1,
    )
    assert landed, "no live console tab appeared for a running guest"
    console = window.consoles.get(RUNNING)
    yield console
    window.close_console(RUNNING)
    pump(0.3)


def test_tab_titles_follow_the_name_id_both_setting(window, config, live_console):
    assert live_console is not None, "no console tab appeared for a running guest"
    # The page is the guest's tab, not the console inside it: a reconnect
    # swaps the console and the tab keeps its place and its label.
    tab = window.tabs[RUNNING]
    assert window.notebook.get_tab_label(tab) is not None
    titles = {}
    try:
        for style in ("name", "id", "both"):
            config["tab_title_format"] = style
            window._apply_name_formats()
            pump(0.1)
            label = window.notebook.get_tab_label(tab)
            if label is not None:
                titles[style] = label.label.get_text()
    finally:
        config["tab_title_format"] = "name"
        window._apply_name_formats()
    assert titles == {"name": "web01", "id": "100", "both": "web01 (100)"}


def test_a_spice_console_can_be_reopened_with_vnc(window, live_console):
    # The console itself is not rebuilt here: this fake has no vnc_ticket, on
    # purpose, so planning is what gets checked -- it is where the decision is
    # actually made.
    if live_console is None or live_console.protocol != "spice":
        pytest.skip(
            f"no live SPICE console (got {getattr(live_console, 'protocol', None)})"
        )

    window._sync_view_menu()
    assert window.switch_protocol_item.get_label() == "Reopen Console with VNC", (
        "the VM menu does not offer VNC on a SPICE tab"
    )
    assert window.switch_protocol_item.get_sensitive()

    window._force_vnc.add(RUNNING)
    assert plan_protocol(window, RUNNING) == "vnc", (
        "a forced console still planned SPICE"
    )
    # Forcing VNC is not allowed to look like evidence about the display, or
    # there would be no way back.
    assert window.sidebar.guests[RUNNING].spice_capable is True, (
        "forcing VNC lost the guest's SPICE capability"
    )

    class _VncTab:
        protocol = "vnc"
        guest_key = RUNNING

    window._sync_protocol_switch(_VncTab())
    assert window.switch_protocol_item.get_label() == "Reopen Console with SPICE", (
        "no way back to SPICE from a switched console"
    )
    assert window.switch_protocol_item.get_sensitive()


def test_reopen_works_twice_in_a_row(window, live_console, monkeypatch):
    """Two switches, one after the other, as somebody trying them out does.

    The second used to do nothing at all. reconnect_console marked a rebuild
    as in flight and only a timer cleared the mark, so a click inside the
    next eight seconds returned without opening anything -- after the status
    bar had already said "Reopening on SPICE...". Nothing else wrote to the
    status bar afterwards, so the window sat on that line having done
    nothing, and clicking again did nothing again.
    """
    if live_console is None or live_console.protocol != "spice":
        pytest.skip(
            f"no live SPICE console (got {getattr(live_console, 'protocol', None)})"
        )
    api = window.api_for(window.sidebar.guests[RUNNING])
    monkeypatch.setattr(
        api,
        "vnc_ticket",
        lambda node, vmid, kind="qemu": {"port": "5900", "ticket": "PVEVNC:fake"},
        raising=False,
    )
    monkeypatch.setattr(
        api,
        "vnc_websocket_url",
        lambda node, vmid, port, ticket, kind="qemu": (
            f"wss://{api.host}:{api.port}/api2/json"
            f"/nodes/{node}/{kind}/{vmid}/vncwebsocket"
        ),
        raising=False,
    )

    def protocol():
        return getattr(window.consoles.get(RUNNING), "protocol", None)

    try:
        window._switch_console_protocol()
        assert pump_until(lambda: protocol() == "vnc", 8), (
            f"the first switch did not reach VNC (on {protocol()})"
        )

        # Straight away, with no pause: this is the one that was swallowed.
        window._switch_console_protocol()
        assert pump_until(lambda: protocol() == "spice", 8), (
            f"the second switch was ignored -- still on {protocol()}, and the "
            f"status bar reads {window.status_label_main.get_text()!r}"
        )
        assert "Reopening" not in window.status_label_main.get_text(), (
            "the status bar was left saying it was reopening something"
        )
    finally:
        window._force_vnc.discard(RUNNING)
        window._force_spice.discard(RUNNING)


def test_an_automatic_reconnect_waits_but_a_click_never_does(window, monkeypatch):
    """The guard is for the poll, which asks again every few seconds."""
    asked = []
    monkeypatch.setattr(
        type(window),
        "open_console",
        lambda self, key, **kwargs: asked.append((key, kwargs.get("automatic", False))),
    )
    window._reconnecting.pop(RUNNING, None)
    try:
        window.reconnect_console(RUNNING)
        window.reconnect_console(RUNNING)
        assert len(asked) == 2, f"a second click was dropped: {asked}"

        # The poll's own attempts, while a rebuild is still in flight, are
        # what the guard exists for.
        window.reconnect_console(RUNNING, automatic=True)
        assert len(asked) == 2, f"an automatic reconnect piled on: {asked}"

        # ...and once the rebuild finishes, the poll may ask again.
        window._reconnect_finished(RUNNING)
        window.reconnect_console(RUNNING, automatic=True)
        assert len(asked) == 3, "the guard outlived the rebuild it was guarding"
    finally:
        window._reconnecting.pop(RUNNING, None)


def test_closing_the_tab_forgets_the_vnc_choice(window):
    window.open_console(RUNNING)
    pump(1.0)
    window._force_vnc.add(RUNNING)
    window.close_console(RUNNING)
    pump(0.3)
    assert RUNNING not in window._force_vnc


def test_a_saved_console_reopens_and_gives_up_on_guests_that_never_appear(
    window, config
):
    window.open_console(RUNNING)
    pump(1.0)
    config["restore_session"] = True
    window._save_session()
    assert config["session_consoles"] == [RUNNING], (
        f"open console not saved: {config['session_consoles']}"
    )
    assert config["session_expanded"], "tree expansion was not saved"
    window.close_console(RUNNING)
    pump(0.3)

    window._restore_keys = [RUNNING]
    window._restore_until = time.monotonic() + 30
    window._resume_session()
    pump(1.0)
    assert RUNNING in window.consoles, "the saved console did not reopen"
    window.close_console(RUNNING)
    pump(0.3)

    window._restore_keys = ["gone.example.invalid/pve-node-01/qemu/999"]
    window._restore_until = time.monotonic() - 1
    window._resume_session()
    assert not window._restore_keys, "session restore never gives up on a missing guest"


def test_a_real_console_keeps_the_tabs_connecting_panel_up(window, live_console):
    """The handover from placeholder to console must not be visible.

    The tab puts up "Connecting..." while it fetches a ticket, then swaps in
    the real console. That console used to start with a bare label in its
    display area, so the rich panel vanished and a line of small text took
    its place a moment before the picture arrived -- which read as the tab
    going backwards.
    """
    if type(live_console).__name__ == "PlaceholderConsole":
        pytest.skip("no SPICE widget here, so no real console to hand over to")
    panel = live_console.status_panel
    # Either still connecting with the same panel up, or already connected
    # and the panel gone -- never a bare label in between.
    if not live_console.connected:
        assert panel.get_visible(), (
            "the console dropped the tab's panel while connecting"
        )
        assert panel.title.get_text() == status_panel_mod.CONNECTING_TITLE, (
            f"the console reworded the wait: {panel.title.get_text()!r}"
        )
    placeholder = getattr(live_console, "placeholder", None)
    if placeholder is not None:
        assert placeholder.get_text() == "", (
            f"the console shows a bare {placeholder.get_text()!r} label"
        )


def test_the_split_button_needs_two_tabs_and_nothing_else(window):
    """Two tabs is the whole rule.

    It used to depend on which pane held what and on the console in front
    rather than on the tabs, which left it grey at moments when splitting
    was plainly reasonable.
    """
    window.open_console(STOPPED)
    pump_until(lambda: STOPPED in window.tabs, 6)
    pump(0.4)
    try:
        window._sync_view_menu()
        assert not window.split_item_tb.get_sensitive(), (
            "the split button is live with one tab open"
        )

        window.open_console(key_for(101))
        pump_until(lambda: key_for(101) in window.tabs, 6)
        pump(0.4)
        assert window.panes.total_pages() >= 2, "the two tabs did not both open"
        window._sync_view_menu()
        assert window.split_item.get_sensitive(), (
            "the split entry is dead with two tabs open in one pane"
        )
        assert window.split_item_tb.get_sensitive(), "the split button is dead too"
    finally:
        window.panes.set_split_mode(split_mod.SPLIT_NONE)
        window.close_console(key_for(101))
        window.close_console(STOPPED)
        pump(0.4)


def test_the_split_button_cycles_through_the_three_arrangements(window):
    """One pane, side by side, one above the other, and back again."""
    window.open_console(STOPPED)
    pump_until(lambda: STOPPED in window.tabs, 6)
    window.open_console(key_for(101))
    pump_until(lambda: key_for(101) in window.tabs, 6)
    pump(0.4)
    try:
        assert window.panes.split_mode() == split_mod.SPLIT_NONE

        window._cycle_split()
        pump(0.5)
        assert window.panes.split_mode() == split_mod.SPLIT_SIDE_BY_SIDE, (
            "the first press did not put the panes side by side"
        )
        assert window.panes.pane_count() == 2
        assert window.panes.notebooks[1].get_visible(), (
            "pane 1 is not the right-hand one"
        )

        window._cycle_split()
        pump(0.5)
        assert window.panes.split_mode() == split_mod.SPLIT_STACKED, (
            "the second press did not stack the panes"
        )
        assert window.panes.pane_count() == 2
        assert window.panes.notebooks[2].get_visible(), "pane 2 is not the lower one"

        window._cycle_split()
        pump(0.5)
        assert window.panes.split_mode() == split_mod.SPLIT_NONE, (
            "the third press did not close the split"
        )
        assert window.panes.pane_count() == 1
        assert window.panes.total_pages() == 2, "cycling lost a tab"
    finally:
        window.panes.set_split_mode(split_mod.SPLIT_NONE)
        window.close_console(key_for(101))
        window.close_console(STOPPED)
        pump(0.4)


def test_the_active_pane_is_marked_on_its_tab(window):
    """Split, and which console the toolbar is aimed at has to be visible."""
    window.open_console(STOPPED)
    pump_until(lambda: STOPPED in window.tabs, 6)
    window.open_console(key_for(101))
    pump_until(lambda: key_for(101) in window.tabs, 6)
    pump(0.4)
    try:
        window._cycle_split()
        pump(0.5)
        assert window.panes.pane_count() == 2, "the split did not open"

        def marked():
            found = []
            for notebook in window.panes.visible_notebooks():
                for page in notebook.get_children():
                    label = notebook.get_tab_label(page)
                    if getattr(label, "_current", False):
                        found.append(page)
            return found

        assert marked() == [window.panes.current_page()], (
            "the tab the window is acting on is not the one marked"
        )

        # Clicking into the other pane moves the mark, without switching tabs.
        other = next(
            n for n in window.panes.visible_notebooks() if n is not window.panes.active
        )
        window.panes.set_active(other)
        pump(0.3)
        assert marked() == [window.panes.current_page()], (
            "the mark did not follow the pane that was clicked into"
        )

        # But the focus alone does not. A SPICE console grabs the keyboard
        # when the pointer crosses it, so a window that followed the focus
        # changed which guest the toolbar acted on as the pointer passed
        # over a pane on its way to a button.
        was = window.panes.active
        elsewhere = next(n for n in window.panes.visible_notebooks() if n is not was)
        page = elsewhere.get_nth_page(elsewhere.get_current_page())
        window.set_focus(page)
        pump(0.3)
        assert window.panes.active is was, (
            "moving the focus took the pane over without a click"
        )
    finally:
        window.panes.set_split_mode(split_mod.SPLIT_NONE)
        window.close_console(key_for(101))
        window.close_console(STOPPED)
        pump(0.4)


class _FakeDisplay:
    """Just enough SpiceDisplay to answer "who has the keyboard?"."""

    def __init__(self):
        self.properties = {"grab-keyboard": True, "grab-mouse": True}
        self.focused = False
        self.ungrabs = 0

    def set_property(self, name, value):
        self.properties[name] = value

    def get_property(self, name):
        return self.properties[name]

    def grab_focus(self):
        self.focused = True

    def keyboard_ungrab(self):
        self.ungrabs += 1


class _FakeToplevel:
    def __init__(self, active):
        self.active = active

    def is_active(self):
        return self.active


def test_hovering_an_inactive_window_does_not_take_the_keyboard():
    """Hover grabs the keyboard, but only for the window you are using.

    spice-gtk's grab is a low-level keyboard hook on Windows, so a console
    that grabs while its window is in the background swallows what is being
    typed into whatever *is* in the foreground.
    """
    from proxima.console.spice import SpiceConsole

    console = SpiceConsole.__new__(SpiceConsole)
    console._closed = False
    console.connected = True
    console._pointer_inside = False
    display = _FakeDisplay()
    console._display = display
    # No extra heads: this console is not in fullscreen across monitors.
    console._head_displays = {}
    console._head_windows = {}
    console._toplevel = _FakeToplevel(active=False)

    console._apply_keyboard_grab()
    assert display.properties["grab-keyboard"] is False, (
        "spice-gtk was left free to grab from a background window"
    )
    assert display.ungrabs == 1, "an inactive window kept the keyboard grabbed"

    console._on_display_enter(display, None)
    assert not display.focused, "hovering a background console took the keyboard"
    assert console._pointer_inside, "the pointer position was not remembered"

    # The same hover, once the window is the one being used.
    console._toplevel.active = True
    console._apply_keyboard_grab()
    assert display.properties["grab-keyboard"] is True
    console._on_display_enter(display, None)
    assert display.focused, "hovering the active console did not take the keyboard"


def test_returning_to_the_window_regrabs_under_the_pointer():
    """Alt-tabbing back with the pointer already on the console.

    spice-gtk only reconsiders the grab on a crossing or focus event, and
    neither one is coming -- the pointer never moved.
    """
    from proxima.console.spice import SpiceConsole

    console = SpiceConsole.__new__(SpiceConsole)
    console._closed = False
    console.connected = True
    console._pointer_inside = True
    display = _FakeDisplay()
    console._display = display
    # No extra heads: this console is not in fullscreen across monitors.
    console._head_displays = {}
    console._head_windows = {}
    console._toplevel = _FakeToplevel(active=True)

    console._on_window_active_changed()
    assert display.focused, "coming back left the guest without the keyboard"

    display.focused = False
    console._toplevel.active = False
    console._on_window_active_changed()
    assert not display.focused, "leaving the window handed the keyboard back over"
    assert display.properties["grab-keyboard"] is False


# Where head N is, when the guest has not made head N yet. A client asks for
# a head and the guest then creates it, so the address has to be worked out
# before there is anything at it -- and QXL puts the extra heads in a
# different place depending on how many devices the VM was given.


def _head_console(heads, channel_ids):
    from proxima.console.spice import SpiceConsole

    console = SpiceConsole.__new__(SpiceConsole)
    console._heads = heads
    console._display_channels = dict.fromkeys(channel_ids, object())
    return console


def test_an_unmade_head_is_another_monitor_on_a_single_device():
    """'vga: qxl' -- one QXL device that splits itself into four monitors."""
    console = _head_console([(0, 0)], [0])
    assert console.head_address(1) == (0, 1)
    assert console.head_address(3) == (0, 3)


def test_an_unmade_head_is_another_channel_when_the_vm_has_several_devices():
    """'vga: qxl2' -- a device each, so a head each on its own channel."""
    console = _head_console([(0, 0), (1, 0)], [0, 1])
    assert console.head_address(1) == (1, 0), "head 2 is not the second channel"
    assert console.head_address(2) == (2, 0)


class _FakeHeadDisplay:
    """A SpiceDisplay bound to one channel and monitor."""

    def __init__(self, channel_id, monitor_id=0):
        self._properties = {"channel-id": channel_id, "monitor-id": monitor_id}

    def get_property(self, name):
        return self._properties[name]


def _channel_console(heads, channel_ids, shown_channel=0):
    console = _head_console(heads, channel_ids)
    console._display = _FakeHeadDisplay(shown_channel)
    return console


def test_the_tabs_own_head_is_not_assumed_to_be_head_zero():
    """A two-device guest can announce channel 1 first, and the tab takes it.

    The index of a head is its display id -- the number the guest's agent
    uses for it -- so the list stays in channel order and the tab's head is
    found in it rather than being put at the front. Renumbering around the
    tab is how switching off "the second head" switched off the one on
    screen, leaving the console black until it went full screen again.
    """
    console = _channel_console([(0, 0), (1, 0)], [0, 1], shown_channel=1)
    assert console.primary_head_index() == 1, "the tab's head was not found"
    # Which leaves display 0 as the one with a monitor to spare.
    assert console.head_address(0) == (0, 0)


def test_the_head_the_tab_is_showing_is_never_switched_off():
    """The last line of defence, whatever the numbering says."""
    console = _channel_console([(0, 0), (1, 0)], [0, 1], shown_channel=1)
    console._main_channel = object()
    console._head_sizes = {}
    console._reasserts = {}
    assert console.set_head_enabled(1, False) is False, (
        "the console switched off the head it is showing"
    )


def test_a_head_that_has_connected_is_taken_from_the_channels_themselves():
    console = _head_console([(0, 0), (0, 1), (2, 0)], [0, 2])
    assert console.head_address(1) == (0, 1)
    assert console.head_address(2) == (2, 0), "a connected head was guessed at instead"


def test_emptying_a_split_pane_gives_the_window_back(window):
    """Close the last tab on one side and the split closes with it.

    Pane 0 used to be pinned visible, so closing its last tab while the
    other pane still had one left half the window as a permanent blank --
    a split with nothing on one side of it.
    """
    window.open_console(STOPPED)
    pump_until(lambda: STOPPED in window.tabs, 6)
    window.open_console(key_for(101))
    pump_until(lambda: key_for(101) in window.tabs, 6)
    pump(0.4)
    try:
        window._cycle_split()
        pump(0.5)
        assert window.panes.pane_count() == 2, "the split did not open"

        # Whichever guest stayed behind in pane 0 is the one to close.
        primary = window.panes.primary
        remaining = primary.get_children()
        assert len(remaining) == 1, "expected one tab left in the first pane"
        stayed = remaining[0]
        moved = [p for p in window.panes.all_pages() if p is not stayed][0]

        window.close_console_widget(stayed)
        pump(0.5)

        assert window.panes.pane_count() == 1, "emptying one side left the split open"
        assert not primary.get_visible(), "the empty pane is still taking up room"
        assert window.panes.notebook_of(moved) is not None, (
            "the surviving console lost its pane"
        )
        assert window.panes.current_page() is moved, (
            "the window cannot see the console that is actually on screen"
        )

        # And the last one closing puts the default pane back, so the next
        # console has somewhere to land.
        window.close_console_widget(moved)
        pump(0.5)
        assert primary.get_visible(), "closing everything left no pane to open into"
    finally:
        for key in list(window.tabs):
            window.close_console(key)
        window.panes.set_split_mode(split_mod.SPLIT_NONE)
        pump(0.4)


# -- sending keys the host would otherwise eat ---------------------------


def send_key_items(menu_item):
    """The labelled entries of a Send Key submenu, in order."""
    return {
        child.get_label(): child
        for child in menu_item.get_submenu().get_children()
        if isinstance(child, Gtk.MenuItem) and child.get_label()
    }


def test_the_send_key_menu_covers_what_the_host_swallows(window):
    """Alt+Tab and the virtual terminals never reach the guest by being
    pressed -- the window manager takes them first -- so the menu is the
    only way to send one."""
    console = FakeConsole("keys")
    console.guest_key = STOPPED
    window.consoles[STOPPED] = console
    window.panes.append(console, Gtk.Label(label="k"))
    pump(0.4)
    try:
        window._sync_view_menu()
        assert window.send_key_item.get_sensitive()
        assert window.send_key_item_tb.get_sensitive(), "the toolbar button is dead"

        items = send_key_items(window.send_key_item)
        for label in ("Ctrl+Alt+Del", "Alt+Tab", "PrintScreen", "Ctrl+Alt+F1"):
            assert label in items, f"{label} is not on the menu: {sorted(items)}"
        # All twelve virtual terminals, not the two somebody felt like typing.
        for number in range(1, 13):
            assert f"Ctrl+Alt+F{number}" in items

        items["Ctrl+Alt+F2"].activate()
        pump(0.2)
        assert console.keys_sent == [(keys_mod.CONTROL_L, keys_mod.ALT_L, 0xFFBF)], (
            f"wrong keysyms for Ctrl+Alt+F2: {console.keys_sent}"
        )

        console.keys_sent.clear()
        items["PrintScreen"].activate()
        pump(0.2)
        assert console.keys_sent == [(keys_mod.PRINT,)]
    finally:
        window.close_console_widget(console)
        pump(0.3)


def test_the_toolbar_button_sends_ctrl_alt_del(window):
    """The click is the common case; the arrow is for everything else."""
    console = FakeConsole("cad")
    console.guest_key = STOPPED
    window.consoles[STOPPED] = console
    window.panes.append(console, Gtk.Label(label="c"))
    pump(0.4)
    try:
        window._sync_view_menu()
        window.send_key_item_tb.emit("clicked")
        pump(0.2)
        assert console.keys_sent == [keys_mod.CTRL_ALT_DEL], console.keys_sent
    finally:
        window.close_console_widget(console)
        pump(0.3)


def test_sending_keys_is_dead_without_a_console(window):
    for page in list(window.panes.all_pages()):
        window.close_console_widget(page)
    pump(0.3)
    window._sync_view_menu()
    assert not window.send_key_item.get_sensitive()
    assert not window.send_key_item_tb.get_sensitive()
    assert not window.ctrl_alt_del_item.get_sensitive()


# Wrong keysyms do not raise anywhere: the guest simply receives a different
# key, or nothing at all, which is a miserable thing to debug from the far
# end of a console. So they are pinned against the X11 names they came from.


def test_the_function_keys_are_the_whole_consecutive_run():
    assert keys_mod.function_key(1) == 0xFFBE, "F1 is not XK_F1"
    assert keys_mod.function_key(12) == 0xFFC9, "F12 is not XK_F12"
    assert [keys_mod.function_key(n) for n in range(1, 13)] == list(
        range(0xFFBE, 0xFFCA)
    )


@pytest.mark.parametrize("number", [0, 13, -1])
def test_a_key_that_does_not_exist_is_refused(number):
    with pytest.raises(ValueError):
        keys_mod.function_key(number)


def test_every_send_key_entry_is_a_real_combination():
    labelled = [entry for entry in keys_mod.SEND_KEYS if entry is not None]
    seen = set()
    for label, keysyms in labelled:
        assert label not in seen, f"{label} is on the menu twice"
        seen.add(label)
        assert keysyms, f"{label} sends nothing"
        # Every keysym in the function/modifier block, which is where the
        # non-printing keys live. A stray ASCII value here would mean a
        # literal character being typed at the guest instead.
        assert all(isinstance(k, int) for k in keysyms), label
        assert all(0xFF00 <= k <= 0xFFFF for k in keysyms), f"{label}: {keysyms}"

    assert labelled[0] == ("Ctrl+Alt+Del", keys_mod.CTRL_ALT_DEL), (
        "Ctrl+Alt+Del is not the first thing on the menu"
    )
    assert seen >= {f"Ctrl+Alt+F{n}" for n in range(1, 13)}, "a terminal is missing"
    assert {"Alt+Tab", "PrintScreen"} <= seen

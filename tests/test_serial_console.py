"""The termproxy client and the serial console widget.

The websocket is faked rather than dialled: what is worth checking here is
the framing Proxmox expects and what the widget does with what comes back,
neither of which needs a server.
"""

import queue
import threading

import pytest
from gi.repository import Gdk, Gtk, Pango

from proxima.console import serial as serial_mod
from proxima.console.serial import (
    DEFAULT_FONT_SIZE,
    MAX_FONT_SIZE,
    MIN_FONT_SIZE,
    SerialConsole,
)
from proxima.console.termproxy import TermProxyClient, TermProxyError

from .conftest import pump, pump_until


class FakeStream:
    """A websocket stand-in: what the client wrote, and what it will read."""

    def __init__(self, reply=b"OK"):
        self.written = []
        self.incoming = queue.Queue()
        self.closed = False
        self._lock = threading.Lock()
        if reply:
            self.incoming.put(reply)

    def read_some(self, limit=65536):
        if self.closed:
            return b""
        data = self.incoming.get()
        if data is None:  # the way a test says "the far side hung up"
            return b""
        return data[:limit]

    def write(self, data):
        with self._lock:
            self.written.append(bytes(data))

    def close(self):
        self.closed = True
        self.incoming.put(None)

    # -- helpers for tests ------------------------------------------------

    def send(self, data):
        self.incoming.put(data)

    def joined(self):
        with self._lock:
            return b"".join(self.written)


def start_client(stream, **kwargs):
    client = TermProxyClient(stream, "root@pam", "PVEVNC:ticket", **kwargs)
    client.start()
    return client


# -- the protocol ----------------------------------------------------------


def test_the_ticket_is_offered_as_its_own_line():
    """The websocket handshake authorises the API call, not the console."""
    stream = FakeStream()
    received = queue.Queue()
    client = start_client(stream, on_data=received.put)
    stream.send(b"hello")
    assert received.get(timeout=4) == b"hello"

    assert stream.written[0] == b"root@pam:PVEVNC:ticket\n"
    client.stop()


def test_a_refused_ticket_is_reported_rather_than_hung_on():
    stream = FakeStream(reply=b"NOPE")
    closed = queue.Queue()
    start_client(stream, on_closed=closed.put)
    reason = closed.get(timeout=4)
    assert "refused the ticket" in reason


def test_keystrokes_are_framed_with_a_byte_length():
    stream = FakeStream()
    client = start_client(stream)
    pump_until(lambda: client.authenticated, 4)

    client.send_input("ls\r")
    assert b"0:3:ls\r" in stream.joined()

    # A length in bytes, not characters: the server reads that many bytes
    # off the socket and would lose sync on anything non-ASCII otherwise.
    client.send_input("é")
    assert b"0:2:\xc3\xa9" in stream.joined()
    client.stop()


def test_a_resize_is_sent_once_and_only_when_it_changes():
    stream = FakeStream()
    client = start_client(stream, cols=80, rows=24)
    pump_until(lambda: client.authenticated, 4)

    assert client.send_resize(120, 40) is True
    assert b"1:120:40:" in stream.joined()
    assert client.send_resize(120, 40) is False, "an unchanged size was sent again"
    client.stop()


def test_the_far_side_closing_is_reported_as_a_closure_not_a_fault():
    stream = FakeStream()
    closed = queue.Queue()
    client = start_client(stream, on_closed=closed.put)
    pump_until(lambda: client.authenticated, 4)
    stream.send(None)
    assert "closed" in closed.get(timeout=4).lower()
    client.stop()


def test_an_error_before_authentication_is_named():
    stream = FakeStream(reply=None)
    stream.send(None)
    closed = queue.Queue()
    start_client(stream, on_closed=closed.put)
    assert "authenticat" in closed.get(timeout=4).lower()


def test_friendly_reason_does_not_show_a_winerror():
    from proxima.console.termproxy import friendly_reason

    assert "connection to the console" in friendly_reason(OSError(10054, "reset"))
    assert friendly_reason(TermProxyError("the ticket expired")) == "the ticket expired"


# -- the widget ------------------------------------------------------------


@pytest.fixture
def console(monkeypatch):
    """A SerialConsole wired to a fake stream, in a realised window."""
    streams = []

    def fake_stream(url, headers=None, fingerprint=None, **kwargs):
        stream = FakeStream()
        streams.append(stream)
        return stream

    monkeypatch.setattr(serial_mod, "WebSocketStream", fake_stream)

    window = Gtk.Window()
    window.set_default_size(720, 400)
    widget = SerialConsole(
        "wss://pve.example.invalid/fake",
        {},
        "root@pam",
        "PVEVNC:ticket",
        title="ct-test",
    )
    window.add(widget)
    window.show_all()
    pump_until(lambda: streams and widget.connected, 6)

    widget.stream = streams[0]
    yield widget

    widget.shutdown()
    window.destroy()
    pump(0.2)


def test_output_reaches_the_screen(console):
    console.stream.send(b"root@ct:~# uname\r\nLinux\r\n")
    pump_until(lambda: "Linux" in console.text(), 4)
    assert "root@ct:~#" in console.text()


def test_the_widget_tells_the_far_side_how_big_it_is(console):
    # The tab is 720px wide, so the terminal is not the 80x24 the protocol
    # starts at -- and a shell that is not told wraps its prompt in the
    # wrong place.
    pump_until(lambda: b"1:" in console.stream.joined(), 4)
    assert console.terminal.screen.cols > 20
    assert f"1:{console.terminal.screen.cols}:".encode() in console.stream.joined()


def test_typing_is_framed_and_sent(console):
    console.client.send_input("x")
    pump(0.2)
    assert b"0:1:x" in console.stream.joined()


def test_a_selection_rejoins_a_line_that_only_wrapped(console):
    cols = console.terminal.screen.cols
    console.stream.send(b"A" * (cols + 5) + b"\r\n")
    pump_until(lambda: console.terminal.screen.cursor_y > 0, 4)

    console._selection = ((0, 0), (1, 5))
    assert console.selected_text() == "A" * (cols + 5), (
        "a wrapped line was copied with a newline through the middle of it"
    )


def test_a_selection_across_real_lines_keeps_the_break(console):
    console.stream.send(b"one\r\ntwo\r\n")
    pump_until(lambda: "two" in console.text(), 4)
    console._selection = ((0, 0), (1, 3))
    assert console.selected_text() == "one\ntwo"


def test_scrollback_is_reachable_and_snaps_back_on_output(console):
    rows = console.terminal.screen.rows
    console.stream.send(b"\r\n".join(f"line{n}".encode() for n in range(rows + 20)))
    pump_until(lambda: len(console.terminal.screen.scrollback) > 5, 4)

    console.scroll_by(5)
    assert console._scroll_offset == 5

    console.stream.send(b"more\r\n")
    pump_until(lambda: console._scroll_offset == 0, 4)


def test_the_font_size_can_be_changed_and_is_reported(console):
    sizes = []
    console.on_font_size = sizes.append
    before = console._cell_height
    assert console.set_font_size(console._font_size + 4) is True
    pump(0.2)
    assert sizes == [console._font_size]
    assert console._cell_height > before, "a bigger font did not make a bigger cell"


def test_text_is_drawn_at_exactly_the_width_of_the_cells_it_sits_in(console):
    """The grid and the glyphs have to come from one set of metrics.

    Everything on this widget except the text -- the cursor, the selection,
    the background of a coloured run -- is drawn at `column * cell_width`.
    So a font drawn at any other advance walks away from the grid, a
    fraction of a cell per column, and the cursor ends up under the wrong
    character.

    That is what a layout from PangoCairo.create_layout(cr) did: it carries
    no resolution, so Pango used its default 96 dpi, while the cells were
    measured from the widget's context -- 96 dpi on Linux and Windows, and
    72 on macOS, where `monospace 12` measured 7px and drew 10px.
    """
    layout = console._text_layout(console.area)
    for size in (MIN_FONT_SIZE, DEFAULT_FONT_SIZE, MAX_FONT_SIZE):
        console.set_font_size(size)
        layout = console._text_layout(console.area)
        columns = 80
        layout.set_text("M" * columns, -1)
        drawn = layout.get_size()[0] / Pango.SCALE
        assert drawn == pytest.approx(columns * console._cell_width, abs=1.0), (
            f"at font size {size}, {columns} characters are drawn "
            f"{drawn:.1f}px wide but occupy {columns * console._cell_width}px "
            "of cells"
        )


def test_a_screenshot_of_a_terminal_is_a_picture_of_the_text(console, tmp_path):
    console.stream.send(b"visible\r\n")
    pump_until(lambda: "visible" in console.text(), 4)
    path = tmp_path / "shot.png"
    assert console.screenshot(str(path)) is True
    assert path.stat().st_size > 0


def test_pasting_goes_through_the_real_clipboard(console):
    """Not paste_text directly: the bug worth catching was in the callback.

    request_text answers asynchronously and hands the callback a different
    number of arguments depending on whether user_data was supplied, so a
    test that skips the round trip proves nothing about pasting.
    """
    Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text("echo hello", -1)
    pump(0.3)
    console._paste()
    pump_until(lambda: b"echo hello" in console.stream.joined(), 4)
    assert b"0:10:echo hello" in console.stream.joined()


def test_a_pasted_newline_becomes_a_carriage_return(console):
    Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text("one\ntwo\n", -1)
    pump(0.3)
    console._paste()
    pump_until(lambda: b"one" in console.stream.joined(), 4)
    # A bare LF at a shell prompt does nothing; the shell wants Enter.
    assert b"one\rtwo\r" in console.stream.joined()
    assert b"\n" not in console.stream.joined().split(b"one")[-1]


def test_a_paste_is_bracketed_when_the_far_side_asked_for_it(console):
    console.terminal.feed(b"\x1b[?2004h")
    Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text("text", -1)
    pump(0.3)
    console._paste()
    pump_until(lambda: b"200~" in console.stream.joined(), 4)
    assert b"\x1b[200~text\x1b[201~" in console.stream.joined()


def test_ctrl_alt_del_is_refused_rather_than_faked(console):
    assert console.send_ctrl_alt_del() is False
    assert "does not apply" in console.last_status


def test_the_view_menu_capabilities_say_what_a_terminal_cannot_do(console):
    assert console.supports["send_keys"] is True
    assert console.supports["ctrl_alt_del"] is False
    assert console.supports["scaling"] is False


def test_losing_the_connection_shows_the_panel(console):
    console.stream.send(None)
    pump_until(lambda: not console.connected, 4)
    assert console.status_panel.get_visible()

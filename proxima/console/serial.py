"""GTK widget for a Proxmox termproxy console.

The third console, and the only text one. A container's VNC console is a
picture of a terminal: the node runs vncterm, draws characters into a
framebuffer, and ships the pixels. Everything a terminal is actually good
for is lost on the way -- you cannot select a path out of it, the font is
whatever the server picked, and a 200-column window still gets 80 columns of
text. This talks to the real thing instead, so the text stays text.

Layout mirrors vnc.py deliberately: a drawing area under an overlay with the
same status panel, the same connect-on-a-worker-thread shape, and the same
console interface the window drives (protocol, supports, telemetry,
screenshot, shutdown). Anything the window can do to a VNC tab it can do to
this one.

Drawing is cell-based. The emulator in vt.py owns the grid; this reads it,
splits each row into runs that share a style, and draws each run as one
Pango layout. Runs rather than cells because a screen of ordinary output is
a handful of runs per row and two thousand cells.
"""

import contextlib
import logging
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
# Named explicitly because this module is the first in the package to import
# Gdk; without it PyGObject picks a version and warns about having done so.
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo

try:
    import cairo
except ImportError:  # pragma: no cover
    cairo = None

from .status_panel import (
    CONNECTING_ICON,
    CONNECTING_TITLE,
    ConsoleStatusPanel,
    draw_offline_effect,
)
from .termproxy import TermProxyClient
from .vt import CONTINUATION, DEFAULT, Terminal
from .vtkeys import key_sequence, menu_sequence
from .wsclient import WebSocketStream

log = logging.getLogger(__name__)

AVAILABLE = cairo is not None

# -- colour ----------------------------------------------------------------

# Tango, which is what GNOME Terminal ships and what most people's mental
# image of "terminal colours" actually is. The bright half is not simply the
# dark half lightened: 8 is a grey, because that is what programs that use it
# for dimmed text expect.
ANSI = [
    (0x2E, 0x34, 0x36),
    (0xCC, 0x00, 0x00),
    (0x4E, 0x9A, 0x06),
    (0xC4, 0xA0, 0x00),
    (0x34, 0x65, 0xA4),
    (0x75, 0x50, 0x7B),
    (0x06, 0x98, 0x9A),
    (0xD3, 0xD7, 0xCF),
    (0x55, 0x57, 0x53),
    (0xEF, 0x29, 0x29),
    (0x8A, 0xE2, 0x34),
    (0xFC, 0xE9, 0x4F),
    (0x72, 0x9F, 0xCF),
    (0xAD, 0x7F, 0xA8),
    (0x34, 0xE2, 0xE2),
    (0xEE, 0xEE, 0xEC),
]

DEFAULT_FG = (0xD3, 0xD7, 0xCF)
DEFAULT_BG = (0x1C, 0x1C, 0x1C)
CURSOR_COLOUR = (0xD3, 0xD7, 0xCF)
SELECTION_BG = (0x3D, 0x5A, 0x80)


def _build_palette():
    """The xterm 256-colour palette: 16 named, a 6x6x6 cube, then 24 greys."""
    palette = list(ANSI)
    steps = (0, 95, 135, 175, 215, 255)
    for red in steps:
        for green in steps:
            for blue in steps:
                palette.append((red, green, blue))
    for level in range(24):
        value = 8 + level * 10
        palette.append((value, value, value))
    return palette


PALETTE = _build_palette()

MIN_FONT_SIZE = 6
MAX_FONT_SIZE = 32
DEFAULT_FONT_SIZE = 11


def resolve(colour, default):
    if colour is DEFAULT:
        return default
    if isinstance(colour, tuple):
        return colour
    return PALETTE[colour & 0xFF] if 0 <= colour < len(PALETTE) else default


def _rgb(context, colour):
    red, green, blue = colour
    context.set_source_rgb(red / 255.0, green / 255.0, blue / 255.0)


class SerialConsole(Gtk.Box):
    """A serial console tab: terminal view, scrollback, status."""

    protocol = "serial"
    agent_connected = False
    pending = False

    # A terminal has no framebuffer, so everything the view menu offers for a
    # graphical console is meaningless here -- there is nothing to scale, no
    # codec to choose and no frame to refresh. What it does have, and the
    # other two do not, is text: selecting and pasting are handled in this
    # widget rather than through the guest.
    supports = {
        "auto_resize": False,
        "scaling": False,
        "codec": False,
        "compression": False,
        "refresh": False,
        # Ctrl+Alt+Del is a keyboard controller event; there is no keyboard
        # controller on the far end of a pty. Typing works regardless, which
        # is what "send_keys" says.
        "ctrl_alt_del": False,
        "send_keys": True,
        "clipboard": False,
        "audio": False,
        "usb": False,
    }

    def __init__(
        self,
        url,
        headers,
        user,
        ticket,
        title="console",
        on_status=None,
        fingerprint=None,
        font_size=DEFAULT_FONT_SIZE,
        on_disconnect=None,
        on_reconnect=None,
        on_font_size=None,
        scrollback=5000,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        self.title = title
        self.on_status = on_status or (lambda text: None)
        self.on_disconnect = on_disconnect or (lambda reason: None)
        self.on_reconnect = on_reconnect or (lambda: None)
        self.on_font_size = on_font_size or (lambda size: None)
        self.last_status = ""
        self.connected = False
        self.client = None

        self.terminal = Terminal(scrollback=scrollback, on_response=self._respond)
        self._closed = False
        self._drawn_revision = -1
        self._pending_draw = False
        self._scroll_offset = 0
        self._font_size = font_size or DEFAULT_FONT_SIZE
        self._cell_width = 8
        self._cell_height = 16
        self._baseline = 12
        self._selection = None
        self._selecting = False
        self._resize_source = None
        self._last_sample = None
        self._last_bytes = 0

        if not AVAILABLE:
            self.pack_start(
                Gtk.Label(
                    label="pycairo is required for the serial console.\n"
                    "Install mingw-w64-ucrt-x86_64-python-cairo"
                ),
                True,
                True,
                0,
            )
            return

        self.area = Gtk.DrawingArea()
        self.area.set_can_focus(True)
        self.area.set_hexpand(True)
        self.area.set_vexpand(True)
        self.area.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.SCROLL_MASK
            | Gdk.EventMask.SMOOTH_SCROLL_MASK
            | Gdk.EventMask.KEY_PRESS_MASK
            | Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.FOCUS_CHANGE_MASK
        )
        self.area.connect("draw", self._on_draw)
        self.area.connect("size-allocate", self._on_allocate)
        self.area.connect("key-press-event", self._on_key)
        self.area.connect("button-press-event", self._on_button_press)
        self.area.connect("button-release-event", self._on_button_release)
        self.area.connect("motion-notify-event", self._on_motion)
        self.area.connect("scroll-event", self._on_scroll)
        self.area.connect("enter-notify-event", lambda w, e: (w.grab_focus(), False)[1])
        # The block cursor is drawn filled while the terminal has the
        # keyboard and hollow while it does not, which is the only cue on
        # screen that typing will go somewhere else.
        self.area.connect("focus-in-event", lambda *_: self._invalidate())
        self.area.connect("focus-out-event", lambda *_: self._invalidate())

        self._measure_font()

        self.scrollbar = Gtk.Scrollbar(orientation=Gtk.Orientation.VERTICAL)
        self.adjustment = self.scrollbar.get_adjustment()
        self.adjustment.connect("value-changed", self._on_scrollbar)
        self._updating_adjustment = False

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        row.pack_start(self.area, True, True, 0)
        row.pack_start(self.scrollbar, False, False, 0)

        self.overlay = Gtk.Overlay()
        self.overlay.add(row)
        self.status_panel = ConsoleStatusPanel(on_reconnect=lambda: self.on_reconnect())
        self.overlay.add_overlay(self.status_panel)
        self.pack_start(self.overlay, True, True, 0)
        self.status_panel.show_message(
            CONNECTING_TITLE,
            "Opening the console session.",
            icon=CONNECTING_ICON,
            can_reconnect=False,
            busy=True,
        )

        self._status("connecting...")
        self._connect(url, headers, user, ticket, fingerprint)

    # -- setup -------------------------------------------------------------

    def _connect(self, url, headers, user, ticket, fingerprint=None):
        def worker():
            try:
                stream = WebSocketStream(url, headers=headers, fingerprint=fingerprint)
            except Exception as exc:
                GLib.idle_add(self._disconnected, f"{exc}")
                return
            client = TermProxyClient(
                stream,
                user,
                ticket,
                on_data=self._on_data,
                on_status=lambda text: GLib.idle_add(self._on_client_status, text),
                on_closed=lambda reason: GLib.idle_add(self._disconnected, reason),
                cols=self.terminal.screen.cols,
                rows=self.terminal.screen.rows,
                name=self.title,
            )
            self.client = client
            client.start()

        threading.Thread(
            target=worker, daemon=True, name=f"serial-connect-{self.title}"
        ).start()

    def _on_client_status(self, text):
        if self._closed:
            return False
        if text == "connected":
            self.connected = True
            self.status_panel.hide_message()
            self._invalidate()
        self._status(text)
        return False

    # -- font and geometry -------------------------------------------------

    def _measure_font(self):
        """Work out the cell box from the font, once per size change.

        Every position on screen is derived from these two numbers, so they
        are measured rather than guessed: a monospace font's advance is not
        its em, and a cell built from the wrong one drifts a pixel per column
        until the right-hand side of the screen is visibly wrong.
        """
        self.font = Pango.FontDescription.from_string(f"monospace {self._font_size}")
        context = self.area.get_pango_context()
        metrics = context.get_metrics(self.font, None)
        scale = Pango.SCALE
        self._cell_width = max(1, int(metrics.get_approximate_digit_width() / scale))
        ascent = metrics.get_ascent() / scale
        descent = metrics.get_descent() / scale
        self._cell_height = max(1, int(ascent + descent))
        self._baseline = ascent

    def set_font_size(self, size):
        size = max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, int(size)))
        if size == self._font_size:
            return False
        self._font_size = size
        self._measure_font()
        self.on_font_size(size)
        # The window did not change, so the number of cells that fit did.
        self._apply_size(self.area.get_allocation())
        self._invalidate()
        return True

    def _on_allocate(self, _widget, allocation):
        self._apply_size(allocation)

    def _apply_size(self, allocation):
        cols = max(1, allocation.width // self._cell_width)
        rows = max(1, allocation.height // self._cell_height)
        if (cols, rows) == (self.terminal.screen.cols, self.terminal.screen.rows):
            return

        self.terminal.resize(cols, rows)
        self._sync_adjustment()
        self._invalidate()

        # Coalesced: dragging a window edge produces an allocation per frame,
        # and telling the far side about every one of them makes the shell
        # redraw its prompt dozens of times for one resize.
        if self._resize_source is not None:
            GLib.source_remove(self._resize_source)
        self._resize_source = GLib.timeout_add(120, self._flush_resize)

    def _flush_resize(self):
        self._resize_source = None
        if self.client is not None and not self._closed:
            with contextlib.suppress(Exception):
                self.client.send_resize(
                    self.terminal.screen.cols, self.terminal.screen.rows
                )
        return False

    # -- incoming data -----------------------------------------------------

    def _on_data(self, data):
        """Called from the reader thread; parse there, draw on the main loop.

        The emulator runs on the reader thread on purpose. A container's boot
        log arrives in one burst of tens of kilobytes, and feeding that
        through the parser in an idle callback would block the main loop --
        the window would stop repainting for as long as it took.
        """
        if self._closed:
            return
        self.terminal.feed(data)
        # Any output means the person wants to see the bottom, which is what
        # every terminal does and why `tail -f` is usable at all.
        self._scroll_offset = 0
        self._queue_draw()

    def _respond(self, data):
        """A reply the emulator owes the far side (cursor position, DA)."""
        if self.client is not None and not self._closed:
            with contextlib.suppress(Exception):
                self.client.send_input(data)

    def _queue_draw(self):
        if self._pending_draw or self._closed:
            return
        self._pending_draw = True
        GLib.idle_add(self._flush_draw)

    def _flush_draw(self):
        self._pending_draw = False
        if self._closed:
            return False
        if self.terminal.revision != self._drawn_revision:
            self._sync_adjustment()
            self.area.queue_draw()
        return False

    def _invalidate(self):
        self._drawn_revision = -1
        if not self._closed and getattr(self, "area", None) is not None:
            self.area.queue_draw()

    # -- scrollback --------------------------------------------------------

    def _sync_adjustment(self):
        screen = self.terminal.screen
        history = len(screen.scrollback)
        self._updating_adjustment = True
        try:
            self.adjustment.configure(
                history - self._scroll_offset,
                0,
                history + screen.rows,
                1,
                max(1, screen.rows - 1),
                screen.rows,
            )
        finally:
            self._updating_adjustment = False
        self.scrollbar.set_visible(history > 0)

    def _on_scrollbar(self, adjustment):
        if self._updating_adjustment or self._closed:
            return
        history = len(self.terminal.screen.scrollback)
        offset = max(0, min(history, history - int(adjustment.get_value())))
        if offset != self._scroll_offset:
            self._scroll_offset = offset
            self._invalidate()

    def scroll_by(self, lines):
        """Move the view through the history. Positive is back in time."""
        history = len(self.terminal.screen.scrollback)
        offset = max(0, min(history, self._scroll_offset + lines))
        if offset != self._scroll_offset:
            self._scroll_offset = offset
            self._sync_adjustment()
            self._invalidate()
        return True

    def _on_scroll(self, _widget, event):
        state = event.state
        if state & Gdk.ModifierType.CONTROL_MASK:
            direction = self._scroll_direction(event)
            if direction:
                self.set_font_size(self._font_size + (1 if direction < 0 else -1))
            return True
        direction = self._scroll_direction(event)
        if direction:
            return self.scroll_by(-direction * 3)
        return False

    @staticmethod
    def _scroll_direction(event):
        """-1 for up, 1 for down, 0 for neither. Smooth or stepped."""
        if event.direction == Gdk.ScrollDirection.SMOOTH:
            _, _, delta_y = event.get_scroll_deltas()
            if delta_y < 0:
                return -1
            return 1 if delta_y > 0 else 0
        # Horizontal wheels and anything unrecognised report neither, rather
        # than falling through to "down" and scrolling the history sideways.
        if event.direction == Gdk.ScrollDirection.UP:
            return -1
        return 1 if event.direction == Gdk.ScrollDirection.DOWN else 0

    # -- drawing -----------------------------------------------------------

    def _runs(self, line):
        """Split a row into (column, text, style) runs sharing one style.

        Styles are shared objects, so identity settles most comparisons
        without looking at ten attributes.
        """
        runs = []
        start = 0
        current = None
        text = []
        for column, cell in enumerate(line.cells):
            char, style = cell
            if cell is CONTINUATION:
                # The right half of a wide character. Its glyph was drawn
                # with the left half and it has no text of its own; skipping
                # it keeps the run's text and its column count in step.
                continue
            if current is None:
                start, current = column, style
            elif style is not current and style != current:
                runs.append((start, "".join(text), current))
                start, current, text = column, style, []
            text.append(char or " ")
        if current is not None and text:
            runs.append((start, "".join(text), current))
        return runs

    @staticmethod
    def _colours(style, selected=False):
        """Foreground and background after reverse, dim and selection."""
        foreground = resolve(style.fg, DEFAULT_FG)
        background = resolve(style.bg, DEFAULT_BG)
        if style.bold and isinstance(style.fg, int) and style.fg < 8:
            # Bold has meant "the bright half of the palette" for as long as
            # there has been a bright half, and plenty of prompts still rely
            # on it for their colour.
            foreground = PALETTE[style.fg + 8]
        if style.reverse:
            foreground, background = background, foreground
        if style.dim:
            foreground = tuple(value // 2 for value in foreground)
        if style.hidden:
            foreground = background
        if selected:
            background = SELECTION_BG
        return foreground, background

    def _on_draw(self, widget, context):
        if self._closed:
            return False
        allocation = widget.get_allocation()
        _rgb(context, DEFAULT_BG)
        context.paint()

        screen = self.terminal.screen
        lines = screen.display(self._scroll_offset)
        layout = PangoCairo.create_layout(context)
        layout.set_font_description(self.font)
        selection = self._selection_span()

        for row, line in enumerate(lines):
            y = row * self._cell_height
            if y > allocation.height:
                break
            absolute = self._absolute_row(row)
            for column, text, style in self._runs(line):
                self._draw_run(
                    context, layout, column, y, text, style, absolute, selection
                )

        self._draw_cursor(context, layout)
        self._drawn_revision = self.terminal.revision

        if not self.connected or self.pending:
            draw_offline_effect(context, allocation.width, allocation.height)
        return False

    def _draw_run(self, context, layout, column, y, text, style, absolute, selection):
        """One run, split further only where a selection cuts through it."""
        for start, chunk, selected in self._split_selection(
            column, text, absolute, selection
        ):
            foreground, background = self._colours(style, selected)
            x = start * self._cell_width
            width = len(chunk) * self._cell_width

            if background != DEFAULT_BG:
                _rgb(context, background)
                context.rectangle(x, y, width, self._cell_height)
                context.fill()

            if not chunk.strip():
                continue

            layout.set_text(chunk, -1)
            attrs = Pango.AttrList()
            if style.bold:
                attrs.insert(Pango.attr_weight_new(Pango.Weight.BOLD))
            if style.italic:
                attrs.insert(Pango.attr_style_new(Pango.Style.ITALIC))
            if style.underline:
                attrs.insert(Pango.attr_underline_new(Pango.Underline.SINGLE))
            if style.strike:
                attrs.insert(Pango.attr_strikethrough_new(True))
            layout.set_attributes(attrs)

            _rgb(context, foreground)
            context.move_to(x, y)
            PangoCairo.show_layout(context, layout)

    def _draw_cursor(self, context, layout):
        screen = self.terminal.screen
        if not screen.cursor_visible or self._scroll_offset or not self.connected:
            return
        x = screen.cursor_x * self._cell_width
        y = screen.cursor_y * self._cell_height
        focused = self.area.has_focus()

        _rgb(context, CURSOR_COLOUR)
        if not focused:
            # Hollow: the terminal is still there, but the keyboard is not.
            context.set_line_width(1)
            context.rectangle(
                x + 0.5, y + 0.5, self._cell_width - 1, self._cell_height - 1
            )
            context.stroke()
            return

        context.rectangle(x, y, self._cell_width, self._cell_height)
        context.fill()

        # The emulator runs on the reader thread, so the cursor can move --
        # or the grid can be rebuilt under it by a resize -- between the
        # position being read above and the cell being read here. A frame
        # with the cursor a column out is not worth an exception in a draw
        # handler, which PyGObject would swallow to a stderr that a packaged
        # build does not have.
        try:
            char, _ = screen.lines[screen.cursor_y].cells[screen.cursor_x]
        except IndexError:
            return
        if char and char.strip():
            layout.set_attributes(Pango.AttrList())
            layout.set_text(char, -1)
            _rgb(context, DEFAULT_BG)
            context.move_to(x, y)
            PangoCairo.show_layout(context, layout)

    # -- selection ---------------------------------------------------------

    def _absolute_row(self, row):
        """A display row as an index into scrollback-plus-screen.

        Selections are held in this space rather than in screen rows so that
        scrolling the view does not move what is selected.
        """
        history = len(self.terminal.screen.scrollback)
        return history - self._scroll_offset + row

    def _position(self, x, y):
        row = max(0, int(y // self._cell_height))
        column = max(0, int(x // self._cell_width))
        return self._absolute_row(row), min(column, self.terminal.screen.cols)

    def _selection_span(self):
        """The selection as ((row, col), (row, col)) in reading order."""
        if self._selection is None:
            return None
        start, end = self._selection
        return (start, end) if start <= end else (end, start)

    def _split_selection(self, column, text, absolute, selection):
        """Cut a run where the selection starts or ends inside it."""
        if selection is None:
            return [(column, text, False)]
        (start_row, start_col), (end_row, end_col) = selection
        if not start_row <= absolute <= end_row:
            return [(column, text, False)]

        low = start_col if absolute == start_row else 0
        high = end_col if absolute == end_row else self.terminal.screen.cols
        end = column + len(text)
        if high <= column or low >= end:
            return [(column, text, False)]

        pieces = []
        cut_low = max(column, low)
        cut_high = min(end, high)
        if cut_low > column:
            pieces.append((column, text[: cut_low - column], False))
        pieces.append((cut_low, text[cut_low - column : cut_high - column], True))
        if cut_high < end:
            pieces.append((cut_high, text[cut_high - column :], False))
        return [piece for piece in pieces if piece[1]]

    def _on_button_press(self, widget, event):
        widget.grab_focus()
        if event.button == 2:
            self._paste(Gdk.SELECTION_PRIMARY)
            return True
        if event.button != 1:
            return False
        if event.type != Gdk.EventType.BUTTON_PRESS:
            # Double click selects a word, triple selects the line. Cheap to
            # add and the first thing anyone tries when copying a path.
            self._select_word_or_line(event)
            return True
        position = self._position(event.x, event.y)
        self._selection = (position, position)
        self._selecting = True
        self._invalidate()
        return True

    def _on_motion(self, _widget, event):
        if not self._selecting:
            return False
        start, _ = self._selection
        self._selection = (start, self._position(event.x, event.y))
        self._invalidate()
        return True

    def _on_button_release(self, _widget, event):
        if event.button != 1 or not self._selecting:
            return False
        self._selecting = False
        text = self.selected_text()
        if text:
            # X11's primary selection, which middle click pastes. Harmless on
            # Windows, where nothing reads it.
            Gtk.Clipboard.get(Gdk.SELECTION_PRIMARY).set_text(text, -1)
        else:
            self._selection = None
            self._invalidate()
        return True

    def _select_word_or_line(self, event):
        row, column = self._position(event.x, event.y)
        lines = self.terminal.screen.display(self._scroll_offset)
        index = row - self._absolute_row(0)
        if not 0 <= index < len(lines):
            return
        cells = lines[index].cells

        if event.type == Gdk.EventType._3BUTTON_PRESS:
            self._selection = ((row, 0), (row, len(cells)))
        else:
            separators = " \t\"'`()[]{}<>|;,"
            column = min(column, len(cells) - 1)
            start = end = column
            while start > 0 and cells[start - 1][0] not in separators:
                start -= 1
            while end < len(cells) and cells[end][0] not in separators:
                end += 1
            self._selection = ((row, start), (row, end))

        self._invalidate()
        text = self.selected_text()
        if text:
            Gtk.Clipboard.get(Gdk.SELECTION_PRIMARY).set_text(text, -1)

    def selected_text(self):
        """The selection as text, joining rows that wrapped rather than ended.

        The join is the point. A command long enough to fold onto a second
        row is one command, and pasting it back with a newline in the middle
        runs half of it.
        """
        span = self._selection_span()
        if span is None:
            return ""
        (start_row, start_col), (end_row, end_col) = span

        screen = self.terminal.screen
        history = list(screen.scrollback)
        everything = history + screen.lines
        parts = []
        pending = ""
        for row in range(max(0, start_row), min(end_row, len(everything) - 1) + 1):
            line = everything[row]
            low = start_col if row == start_row else 0
            high = end_col if row == end_row else len(line.cells)
            text = "".join(char for char, _ in line.cells[low:high])
            if row != end_row:
                text = text.rstrip() if not line.wrapped else text
                pending += text
                if not line.wrapped:
                    parts.append(pending)
                    pending = ""
            else:
                pending += text.rstrip()
                parts.append(pending)
                pending = ""
        if pending:
            parts.append(pending)
        return "\n".join(parts)

    def copy_selection(self):
        text = self.selected_text()
        if not text:
            return False
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)
        self._status("copied the selection")
        return True

    def _paste(self, selection=None):
        clipboard = Gtk.Clipboard.get(selection or Gdk.SELECTION_CLIPBOARD)
        clipboard.request_text(self._on_clipboard_text)
        return True

    def _on_clipboard_text(self, _clipboard, text, *_user_data):
        """The clipboard answered. Asynchronous: X11 has to ask the owner.

        The trailing *args is not defensive padding -- PyGObject passes the
        user_data argument through to this callback only when request_text
        was given one, so a handler with a fixed third parameter raises
        rather than pastes.
        """
        self.paste_text(text)

    def paste_text(self, text):
        if not text or self.client is None:
            return False
        # Newlines become carriage returns: a pasted line is a line the shell
        # should see as Enter, and a bare LF at a prompt does nothing useful.
        text = text.replace("\r\n", "\r").replace("\n", "\r")
        if self.terminal.screen.bracketed_paste:
            # The far side asked to be told this was a paste, which is how an
            # editor knows not to auto-indent it into a staircase.
            self.client.send_input("\x1b[200~" + text + "\x1b[201~")
        else:
            self.client.send_input(text)
        self._scroll_offset = 0
        return True

    # -- input -------------------------------------------------------------

    def _on_key(self, _widget, event):
        if self.client is None:
            return False
        state = event.state
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        alt = bool(state & Gdk.ModifierType.MOD1_MASK)
        keyval = event.keyval

        if ctrl and shift:
            # The terminal's own bindings. Ctrl+Shift rather than Ctrl,
            # because Ctrl+C has a job here that predates the clipboard.
            name = Gdk.keyval_name(Gdk.keyval_to_lower(keyval)) or ""
            if name == "c":
                self.copy_selection()
                return True
            if name in ("v", "insert"):
                self._paste()
                return True

        if shift and keyval in (0xFF55, 0xFF56):  # Page Up / Page Down
            return self.scroll_by(
                self.terminal.screen.rows // 2 * (1 if keyval == 0xFF55 else -1)
            )
        if ctrl and keyval in (0x2B, 0x3D, 0xFFAB):  # plus, equal, KP_Add
            return self.set_font_size(self._font_size + 1) or True
        if ctrl and keyval in (0x2D, 0xFFAD):  # minus, KP_Subtract
            return self.set_font_size(self._font_size - 1) or True
        if ctrl and keyval == 0x30:  # zero
            return self.set_font_size(DEFAULT_FONT_SIZE) or True

        # A modifier pressed on its own maps to no character at all, and
        # NUL is a character -- turning "nothing" into chr(0) would put a
        # stray byte on the wire every time somebody reached for Shift.
        codepoint = Gdk.keyval_to_unicode(keyval) if keyval else 0

        data = key_sequence(
            keyval,
            char=chr(codepoint) if codepoint else "",
            ctrl=ctrl,
            alt=alt,
            shift=shift,
            cursor_app=self.terminal.screen.cursor_keys_app,
        )
        if data is None:
            return False

        # Typing means the bottom of the buffer, and means the selection is
        # no longer about anything on screen.
        self._scroll_offset = 0
        if self._selection is not None:
            self._selection = None
            self._invalidate()
        with contextlib.suppress(Exception):
            self.client.send_input(data)
        return True

    def send_keys(self, keyvals):
        """A Send Key menu combination, where it means anything here."""
        if self.client is None:
            self._status("not connected")
            return False
        data = menu_sequence(keyvals)
        if data is None:
            self._status("that combination has no meaning on a serial console")
            return False
        with contextlib.suppress(Exception):
            self.client.send_input(data)
        return True

    def send_ctrl_alt_del(self):
        self._status("Ctrl+Alt+Del does not apply to a serial console")
        return False

    def grab_focus_display(self):
        if getattr(self, "area", None) is not None:
            self.area.grab_focus()

    def release_input(self):
        toplevel = self.get_toplevel()
        if isinstance(toplevel, Gtk.Window):
            toplevel.set_focus(None)

    # -- the console interface ---------------------------------------------

    def set_scaling(self, _enabled):
        """Nothing to scale. Kept so the view menu can call it blindly."""
        return False

    def telemetry(self):
        """Throughput and the grid size. No frame rate -- there are no frames."""
        if self.client is None:
            return None
        now = time.monotonic()
        total = self.client.bytes_in
        rate = None
        if self._last_sample is not None and now > self._last_sample:
            rate = (total - self._last_bytes) / (now - self._last_sample)
        self._last_sample, self._last_bytes = now, total
        screen = self.terminal.screen
        return {
            "rate": rate,
            "fps": None,
            "codec": "",
            "size": f"{screen.cols}x{screen.rows}",
        }

    def screenshot(self, path):
        """Draw the current screen into a PNG, as the other consoles do."""
        if self._closed or cairo is None:
            return False
        # Sized to the grid rather than to the widget, so the margin left
        # over when the tab is not an exact multiple of the cell is not in
        # the picture. The draw path reads the widget's own allocation, and
        # the two differ by less than one cell.
        width = max(1, self.terminal.screen.cols * self._cell_width)
        height = max(1, self.terminal.screen.rows * self._cell_height)
        surface = cairo.ImageSurface(cairo.FORMAT_RGB24, width, height)
        context = cairo.Context(surface)
        # Reuses the draw path rather than a second renderer, so a screenshot
        # cannot drift out of step with what is on screen.
        saved, self.pending = self.pending, False
        try:
            self._on_draw(self.area, context)
        finally:
            self.pending = saved
        surface.flush()
        surface.write_to_png(path)
        return True

    def text(self, include_scrollback=True):
        """Everything the terminal holds, as text. Used by tests and logs."""
        return self.terminal.screen.text(include_scrollback)

    # -- lifecycle ---------------------------------------------------------

    def _status(self, text):
        self.last_status = text
        self.on_status(text)
        log.info("%s: %s", self.title, text)
        return False

    def show_pending_state(self, title, detail=""):
        if self._closed or getattr(self, "status_panel", None) is None:
            return
        self.pending = True
        self.status_panel.show_message(title, detail, can_reconnect=False, busy=True)
        self._invalidate()

    def clear_pending_state(self):
        if not self.pending:
            return
        self.pending = False
        if self.connected:
            self.status_panel.hide_message()
        self._invalidate()

    def show_guest_state(self, status):
        if self._closed or getattr(self, "status_panel", None) is None:
            return
        self.connected = False
        titles = {
            "stopped": "Container is stopped",
            "io-error": "Container stopped on an I/O error",
            "suspended": "Container is suspended",
            "paused": "Container is paused",
        }
        details = {
            "stopped": "Start it to reconnect.",
            "io-error": "Proxmox stopped it because its storage stopped answering. Fix the storage, then reset or stop it.",
            "suspended": "Resume it to reconnect.",
            "paused": "Resume it to reconnect.",
        }
        icons = {
            "io-error": "dialog-warning-symbolic",
            "paused": "media-playback-pause-symbolic",
            "suspended": "media-playback-pause-symbolic",
        }
        self.status_panel.show_message(
            titles.get(status, f"Guest is {status}"),
            details.get(status, ""),
            icon=icons.get(status, "media-playback-stop-symbolic"),
            can_reconnect=False,
        )
        self._invalidate()

    def _disconnected(self, reason):
        if self._closed or getattr(self, "status_panel", None) is None:
            return False
        was_connected, self.connected = self.connected, False
        self.status_panel.show_message("Connection closed", reason)
        self._invalidate()
        self._status(reason)
        if was_connected:
            self.on_disconnect(reason)
        return False

    def shutdown(self):
        self._closed = True
        if self._resize_source is not None:
            GLib.source_remove(self._resize_source)
            self._resize_source = None
        if self.client is not None:
            self.client.stop()
            self.client = None

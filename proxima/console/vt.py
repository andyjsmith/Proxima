"""A VT100/xterm terminal emulator: escape sequences in, screen buffer out.

Proxmox's third console is a real character terminal rather than a picture of
one, and nothing on the way to the screen will emulate it for us. VTE is the
obvious answer on Linux and does not exist on Windows -- MSYS2 ships no vte3
for any mingw target, because VTE is built around POSIX ptys -- so the
emulator is here, in the same spirit as rfb.py: the protocol, written out,
with no dependency to install.

Nothing in this module touches GTK. It turns bytes into a grid of styled
cells and answers questions about that grid; serial.py draws it. That split
is what makes the awkward half -- the escape sequences -- testable without a
display.

The dialect is xterm's, which is what `TERM=xterm-256color` promises and what
Proxmox sets. That means 256-colour and truecolor SGR, the DEC private modes
that full-screen programs actually use (autowrap, origin, alternate screen,
application cursor keys), and the line-drawing charset -- without which
anything ncurses draws a box with, `dialog` and `whiptail` included, comes
out as a fence of lowercase letters.
"""

import codecs
import unicodedata
from collections import deque

# -- styling ---------------------------------------------------------------

# A colour is None for "whatever the theme calls default", an int 0-255 for a
# palette entry, or an (r, g, b) triple for a truecolor SGR. Keeping the
# palette as indices rather than resolving to RGB here is what lets the
# widget theme the 16 ANSI colours without the emulator knowing about it.

DEFAULT = None


class Style:
    """One cell's appearance. Immutable, shared, and compared by value.

    Immutable because every cell holds a reference to one: a run of text in
    the same colour is the same object repeated, so a screen full of ordinary
    output costs one Style rather than two thousand. That also makes the
    renderer's job easy -- it splits a row into runs by identity.
    """

    __slots__ = (
        "fg",
        "bg",
        "bold",
        "dim",
        "italic",
        "underline",
        "blink",
        "reverse",
        "hidden",
        "strike",
        "_key",
    )

    def __init__(
        self,
        fg=DEFAULT,
        bg=DEFAULT,
        bold=False,
        dim=False,
        italic=False,
        underline=False,
        blink=False,
        reverse=False,
        hidden=False,
        strike=False,
    ):
        self.fg = fg
        self.bg = bg
        self.bold = bold
        self.dim = dim
        self.italic = italic
        self.underline = underline
        self.blink = blink
        self.reverse = reverse
        self.hidden = hidden
        self.strike = strike
        self._key = (
            fg,
            bg,
            bold,
            dim,
            italic,
            underline,
            blink,
            reverse,
            hidden,
            strike,
        )

    def replace(self, **changes):
        values = {name: getattr(self, name) for name in self.__slots__[:-1]}
        values.update(changes)
        return Style(**values)

    def __eq__(self, other):
        return isinstance(other, Style) and self._key == other._key

    def __hash__(self):
        return hash(self._key)

    def __repr__(self):  # pragma: no cover - debugging aid
        set_flags = [
            name
            for name in ("bold", "dim", "italic", "underline", "blink", "reverse")
            if getattr(self, name)
        ]
        return f"Style(fg={self.fg}, bg={self.bg}, {'+'.join(set_flags) or 'plain'})"


PLAIN = Style()

# A cell is (text, style). Text rather than a single character because a
# combining mark belongs to the cell before it -- "e" then U+0301 is one cell
# holding "é", not two cells, and anything that splits them draws an
# accent in the wrong column.
BLANK = (" ", PLAIN)

# The second half of a double-width character. It holds no text of its own:
# the renderer skips it, and the emulator overwrites both halves together so
# a pair can never be left with one side of it.
CONTINUATION = ("", PLAIN)


class Line:
    """A row of cells, and whether it ran into the next one.

    'wrapped' is only ever read when copying a selection out. A shell prompt
    long enough to fold is one line as far as the person reading it is
    concerned, and pasting it back with a newline in the middle would run
    half a command.
    """

    __slots__ = ("cells", "wrapped")

    def __init__(self, cols, wrapped=False):
        self.cells = [BLANK] * cols
        self.wrapped = wrapped

    def text(self, start=0, end=None):
        """The row as text, with the trailing blanks dropped."""
        cells = self.cells[start : end if end is not None else len(self.cells)]
        return "".join(text for text, _ in cells).rstrip()

    def resize(self, cols):
        current = len(self.cells)
        if cols > current:
            self.cells.extend([BLANK] * (cols - current))
        elif cols < current:
            del self.cells[cols:]
            self.wrapped = False


# -- character measurement -------------------------------------------------


def char_width(char):
    """Cells a character occupies: 0 for a combining mark, 2 for wide, else 1.

    unicodedata rather than a table copied out of wcwidth: it is already in
    the standard library, it tracks the Unicode version Python was built
    against, and being a version or two behind on an emoji block is a
    cosmetic problem in a console that mostly shows shell output.
    """
    if unicodedata.combining(char) or unicodedata.category(char) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


# The DEC Special Graphics set, which is how every ncurses program on earth
# draws a box. Selected with ESC ( 0 and left in force until ESC ( B, so a
# terminal that ignores it does not merely lose the borders -- it prints
# "lqqqqk" where the top of the dialog should be.
GRAPHICS = {
    "_": " ",
    "`": "◆",
    "a": "▒",
    "b": "␉",
    "c": "␌",
    "d": "␍",
    "e": "␊",
    "f": "°",
    "g": "±",
    "h": "␤",
    "i": "␋",
    "j": "┘",
    "k": "┐",
    "l": "┌",
    "m": "└",
    "n": "┼",
    "o": "⎺",
    "p": "⎻",
    "q": "─",
    "r": "⎼",
    "s": "⎽",
    "t": "├",
    "u": "┤",
    "v": "┴",
    "w": "┬",
    "x": "│",
    "y": "≤",
    "z": "≥",
    "{": "π",
    "|": "≠",
    "}": "£",
    "~": "·",
}


# -- the screen ------------------------------------------------------------

DEFAULT_SCROLLBACK = 5000


class Screen:
    """The grid, the cursor, and the modes that govern what writing does."""

    def __init__(self, cols=80, rows=24, scrollback=DEFAULT_SCROLLBACK):
        self.cols = max(1, cols)
        self.rows = max(1, rows)
        self.scrollback = deque(maxlen=scrollback)
        # Bumped on every change. The widget compares it against what it last
        # drew, so an escape sequence that turns out to change nothing -- a
        # cursor-position report, a repeated SGR -- costs no redraw.
        self.revision = 0
        self.title = ""
        self.bell_count = 0
        self.reset()

    # -- state ------------------------------------------------------------

    def reset(self):
        self.lines = [Line(self.cols) for _ in range(self.rows)]
        self.alternate = None
        self.style = PLAIN
        self.cursor_x = 0
        self.cursor_y = 0
        self.cursor_visible = True
        self.top = 0
        self.bottom = self.rows - 1
        self.autowrap = True
        self.origin_mode = False
        self.insert_mode = False
        self.cursor_keys_app = False
        self.keypad_app = False
        self.bracketed_paste = False
        self.mouse_tracking = 0
        self._wrap_pending = False
        self._saved = None
        self._graphics = False
        self._charsets = ["B", "B"]
        self.tabstops = {x for x in range(self.cols) if x % 8 == 0 and x}
        self.touch()

    def touch(self):
        self.revision += 1

    @property
    def in_alternate(self):
        return self.alternate is not None

    # -- geometry ---------------------------------------------------------

    def resize(self, cols, rows):
        """Fit the grid to a new size, keeping as much content as possible.

        No reflow. Rewrapping the scrollback to a new width is a large amount
        of machinery for a case that resolves itself: the far side is told
        the new size, and the shell redraws its prompt at it. What matters is
        that nothing is lost off the top and the cursor stays on the grid.
        """
        cols, rows = max(1, cols), max(1, rows)
        if (cols, rows) == (self.cols, self.rows):
            return False

        for line in self.lines:
            line.resize(cols)
        for line in self.scrollback:
            line.resize(cols)

        if rows < self.rows:
            # Take the surplus off the top, not the bottom: the bottom is
            # where the prompt and the cursor are. Rows that were never
            # written to are dropped rather than filling the scrollback with
            # blanks, which is what a shrink after a `clear` would otherwise
            # do.
            surplus = self.rows - rows
            keep_from = min(surplus, max(0, self.cursor_y - rows + 1))
            for line in self.lines[:keep_from]:
                if line.text() and not self.in_alternate:
                    self.scrollback.append(line)
            del self.lines[:keep_from]
            del self.lines[rows:]
            self.cursor_y -= keep_from
        elif rows > self.rows:
            self.lines.extend(Line(cols) for _ in range(rows - self.rows))

        self.cols, self.rows = cols, rows
        self.tabstops = {x for x in range(cols) if x % 8 == 0 and x}
        self.top, self.bottom = 0, rows - 1
        self.cursor_x = min(self.cursor_x, cols - 1)
        self.cursor_y = min(max(self.cursor_y, 0), rows - 1)
        self._wrap_pending = False
        self.touch()
        return True

    # -- the grid ---------------------------------------------------------

    def line(self, y):
        return self.lines[y]

    def display(self, offset=0):
        """The rows to draw, counting `offset` lines back into the scrollback.

        Returns exactly `rows` Lines whatever the offset, so the renderer
        never has to think about running off the end of the history.
        """
        offset = max(0, min(offset, len(self.scrollback)))
        if not offset:
            return list(self.lines)
        history = list(self.scrollback)[len(self.scrollback) - offset :]
        return (history + list(self.lines))[: self.rows]

    def _blank_line(self):
        return Line(self.cols)

    def scroll_up(self, count=1):
        """Move the scroll region's contents up, as new output does."""
        for _ in range(count):
            line = self.lines.pop(self.top)
            # Only the primary screen keeps history. The alternate screen is
            # what full-screen programs draw on, and letting vim's redraws
            # into the scrollback is how a terminal ends up with a history
            # full of half-drawn editor frames.
            if self.top == 0 and not self.in_alternate:
                self.scrollback.append(line)
            self.lines.insert(self.bottom, self._blank_line())
        self.touch()

    def scroll_down(self, count=1):
        for _ in range(count):
            self.lines.pop(self.bottom)
            self.lines.insert(self.top, self._blank_line())
        self.touch()

    # -- writing ----------------------------------------------------------

    def write(self, text):
        """Put printable text at the cursor, wrapping and scrolling as needed."""
        for char in text:
            width = char_width(char)

            if width == 0:
                # A combining mark joins the cell to the left, which is not
                # necessarily the one before the cursor: after a wide
                # character the cursor is two columns along.
                self._combine(char)
                continue

            if self._wrap_pending and self.autowrap:
                self.line(self.cursor_y).wrapped = True
                self.cursor_x = 0
                self._index()
                self._wrap_pending = False

            if width == 2 and self.cursor_x == self.cols - 1:
                # No room for both halves. Wrapping is what xterm does; the
                # alternative is drawing half a character.
                if self.autowrap:
                    self.line(self.cursor_y).wrapped = True
                    self.cursor_x = 0
                    self._index()
                else:
                    continue

            if self._graphics:
                char = GRAPHICS.get(char, char)

            line = self.line(self.cursor_y)
            if self.insert_mode:
                shift = width
                line.cells[self.cursor_x + shift :] = line.cells[
                    self.cursor_x : self.cols - shift
                ]

            self._clear_pair(line, self.cursor_x)
            line.cells[self.cursor_x] = (char, self.style)
            if width == 2:
                self._clear_pair(line, self.cursor_x + 1)
                line.cells[self.cursor_x + 1] = CONTINUATION

            if self.cursor_x + width >= self.cols:
                # Deferred: a character in the last column leaves the cursor
                # on it, not past it. Only the *next* character wraps. Get
                # this wrong and every line exactly as wide as the terminal
                # gains a blank line after it.
                self.cursor_x = self.cols - 1
                self._wrap_pending = True
            else:
                self.cursor_x += width
        self.touch()

    def _clear_pair(self, line, x):
        """Overwriting half a wide character must clear the other half."""
        if 0 <= x < self.cols and line.cells[x] is CONTINUATION and x:
            line.cells[x - 1] = BLANK
        elif 0 <= x + 1 < self.cols and line.cells[x + 1] is CONTINUATION:
            line.cells[x + 1] = BLANK

    def _combine(self, char):
        line = self.line(self.cursor_y)
        x = self.cursor_x - 1
        while x >= 0 and line.cells[x] is CONTINUATION:
            x -= 1
        if x < 0:
            return
        text, style = line.cells[x]
        line.cells[x] = (text + char, style)

    # -- cursor -----------------------------------------------------------

    def _bounds(self):
        """The rows the cursor may address, which origin mode narrows."""
        if self.origin_mode:
            return self.top, self.bottom
        return 0, self.rows - 1

    def move_to(self, x=None, y=None):
        if y is not None:
            low, high = self._bounds()
            if self.origin_mode:
                y += self.top
            self.cursor_y = min(max(y, low), high)
        if x is not None:
            self.cursor_x = min(max(x, 0), self.cols - 1)
        self._wrap_pending = False
        self.touch()

    def move_by(self, dx=0, dy=0):
        if dy:
            # Vertical movement stops at the scroll region rather than
            # scrolling it, which is what keeps a status line at the bottom
            # of a full-screen program still.
            low = self.top if self.cursor_y >= self.top else 0
            high = self.bottom if self.cursor_y <= self.bottom else self.rows - 1
            self.cursor_y = min(max(self.cursor_y + dy, low), high)
        if dx:
            self.cursor_x = min(max(self.cursor_x + dx, 0), self.cols - 1)
        self._wrap_pending = False
        self.touch()

    def _index(self):
        """Down one row, scrolling when already at the bottom of the region."""
        if self.cursor_y == self.bottom:
            self.scroll_up()
        elif self.cursor_y < self.rows - 1:
            self.cursor_y += 1

    def _reverse_index(self):
        if self.cursor_y == self.top:
            self.scroll_down()
        elif self.cursor_y > 0:
            self.cursor_y -= 1

    def save_cursor(self):
        self._saved = (
            self.cursor_x,
            self.cursor_y,
            self.style,
            self.origin_mode,
            self._graphics,
            list(self._charsets),
            self.autowrap,
        )

    def restore_cursor(self):
        if self._saved is None:
            self.move_to(0, 0)
            return
        (
            self.cursor_x,
            self.cursor_y,
            self.style,
            self.origin_mode,
            self._graphics,
            charsets,
            self.autowrap,
        ) = self._saved
        self._charsets = list(charsets)
        self.cursor_x = min(self.cursor_x, self.cols - 1)
        self.cursor_y = min(self.cursor_y, self.rows - 1)
        self._wrap_pending = False
        self.touch()

    # -- erasing and editing ----------------------------------------------

    def erase_in_display(self, mode=0):
        if mode == 2 or mode == 3:
            if mode == 3:
                self.scrollback.clear()
            elif not self.in_alternate:
                # `clear` at a prompt should push the screen into the history
                # rather than destroy it -- that is where the scrollback of a
                # terminal session actually comes from.
                for line in self.lines:
                    if line.text():
                        self.scrollback.append(line)
            self.lines = [self._blank_line() for _ in range(self.rows)]
        elif mode == 0:
            self.erase_in_line(0)
            for y in range(self.cursor_y + 1, self.rows):
                self.lines[y] = self._blank_line()
        elif mode == 1:
            self.erase_in_line(1)
            for y in range(self.cursor_y):
                self.lines[y] = self._blank_line()
        self._wrap_pending = False
        self.touch()

    def _erased(self):
        """A blank in the current background, which is what erasing leaves.

        Only the background travels: a `clear` under a program that set a
        blue background fills the screen blue, but the bold and underline it
        also had are not visible on a space and would surprise whatever
        writes there next.
        """
        if self.style.bg is DEFAULT and not self.style.reverse:
            return BLANK
        return (" ", Style(bg=self.style.bg, reverse=self.style.reverse))

    def erase_in_line(self, mode=0):
        line = self.line(self.cursor_y)
        blank = self._erased()
        if mode == 0:
            line.cells[self.cursor_x :] = [blank] * (self.cols - self.cursor_x)
            line.wrapped = False
        elif mode == 1:
            line.cells[: self.cursor_x + 1] = [blank] * (self.cursor_x + 1)
        elif mode == 2:
            line.cells[:] = [blank] * self.cols
            line.wrapped = False
        self._wrap_pending = False
        self.touch()

    def erase_chars(self, count=1):
        line = self.line(self.cursor_y)
        end = min(self.cursor_x + count, self.cols)
        line.cells[self.cursor_x : end] = [self._erased()] * (end - self.cursor_x)
        self.touch()

    def insert_lines(self, count=1):
        if not self.top <= self.cursor_y <= self.bottom:
            return
        for _ in range(min(count, self.bottom - self.cursor_y + 1)):
            self.lines.pop(self.bottom)
            self.lines.insert(self.cursor_y, self._blank_line())
        self.cursor_x = 0
        self.touch()

    def delete_lines(self, count=1):
        if not self.top <= self.cursor_y <= self.bottom:
            return
        for _ in range(min(count, self.bottom - self.cursor_y + 1)):
            self.lines.pop(self.cursor_y)
            self.lines.insert(self.bottom, self._blank_line())
        self.cursor_x = 0
        self.touch()

    def insert_chars(self, count=1):
        line = self.line(self.cursor_y)
        count = min(count, self.cols - self.cursor_x)
        line.cells[self.cursor_x + count :] = line.cells[
            self.cursor_x : self.cols - count
        ]
        line.cells[self.cursor_x : self.cursor_x + count] = [self._erased()] * count
        self.touch()

    def delete_chars(self, count=1):
        line = self.line(self.cursor_y)
        count = min(count, self.cols - self.cursor_x)
        del line.cells[self.cursor_x : self.cursor_x + count]
        line.cells.extend([self._erased()] * count)
        self.touch()

    def repeat_last(self, count=1):
        """REP: the preceding character again, which some ncurses builds use."""
        x = self.cursor_x - 1
        line = self.line(self.cursor_y)
        if x < 0:
            return
        text, _ = line.cells[x]
        if text:
            self.write(text * count)

    # -- tabs -------------------------------------------------------------

    def tab(self, count=1):
        for _ in range(count):
            stops = [x for x in self.tabstops if x > self.cursor_x]
            self.cursor_x = min(stops) if stops else self.cols - 1
        self._wrap_pending = False
        self.touch()

    def back_tab(self, count=1):
        for _ in range(count):
            stops = [x for x in self.tabstops if x < self.cursor_x]
            self.cursor_x = max(stops) if stops else 0
        self.touch()

    # -- alternate screen -------------------------------------------------

    def set_alternate(self, enabled):
        if enabled == self.in_alternate:
            return
        if enabled:
            self.alternate = self.lines
            self.lines = [self._blank_line() for _ in range(self.rows)]
        else:
            self.lines = self.alternate
            self.alternate = None
            for line in self.lines:
                line.resize(self.cols)
            while len(self.lines) < self.rows:
                self.lines.append(self._blank_line())
            del self.lines[self.rows :]
        self.top, self.bottom = 0, self.rows - 1
        self.cursor_x = min(self.cursor_x, self.cols - 1)
        self.cursor_y = min(self.cursor_y, self.rows - 1)
        self.touch()

    # -- reading it back --------------------------------------------------

    def text(self, include_scrollback=False):
        """The screen as plain text, one row per line."""
        lines = (list(self.scrollback) if include_scrollback else []) + self.lines
        return "\n".join(line.text() for line in lines)


# -- SGR -------------------------------------------------------------------


def apply_sgr(style, params):
    """Fold a Select Graphic Rendition parameter list into a Style."""
    if not params:
        params = [0]

    index = 0
    while index < len(params):
        code = params[index]
        index += 1

        if code == 0:
            style = PLAIN
        elif code == 1:
            style = style.replace(bold=True)
        elif code == 2:
            style = style.replace(dim=True)
        elif code == 3:
            style = style.replace(italic=True)
        elif code == 4:
            style = style.replace(underline=True)
        elif code in (5, 6):
            style = style.replace(blink=True)
        elif code == 7:
            style = style.replace(reverse=True)
        elif code == 8:
            style = style.replace(hidden=True)
        elif code == 9:
            style = style.replace(strike=True)
        elif code == 21:
            style = style.replace(bold=False)
        elif code == 22:
            style = style.replace(bold=False, dim=False)
        elif code == 23:
            style = style.replace(italic=False)
        elif code == 24:
            style = style.replace(underline=False)
        elif code == 25:
            style = style.replace(blink=False)
        elif code == 27:
            style = style.replace(reverse=False)
        elif code == 28:
            style = style.replace(hidden=False)
        elif code == 29:
            style = style.replace(strike=False)
        elif 30 <= code <= 37:
            style = style.replace(fg=code - 30)
        elif 40 <= code <= 47:
            style = style.replace(bg=code - 40)
        elif code == 39:
            style = style.replace(fg=DEFAULT)
        elif code == 49:
            style = style.replace(bg=DEFAULT)
        elif 90 <= code <= 97:
            style = style.replace(fg=code - 90 + 8)
        elif 100 <= code <= 107:
            style = style.replace(bg=code - 100 + 8)
        elif code in (38, 48):
            colour, index = _extended_colour(params, index)
            if colour is not False:
                style = style.replace(**{"fg" if code == 38 else "bg": colour})
    return style


def _extended_colour(params, index):
    """Read a 38/48 colour argument. Returns (colour, next index).

    Both the sub-parameter form (38:2:...) and the historical semicolon form
    (38;2;...) arrive here already flattened into one list, which is why this
    counts arguments rather than trusting a separator.
    """
    if index >= len(params):
        return False, index
    kind = params[index]
    index += 1
    if kind == 5 and index < len(params):
        return params[index] & 0xFF, index + 1
    if kind == 2 and index + 2 < len(params):
        red, green, blue = params[index : index + 3]
        return (red & 0xFF, green & 0xFF, blue & 0xFF), index + 3
    return False, index


# -- the parser ------------------------------------------------------------

GROUND, ESCAPE, CSI, OSC, STRING, CHARSET = range(6)

# C0 controls that end a string sequence, plus the ones handled in ground.
BELL = "\x07"


class Terminal:
    """Bytes in, screen changes out, plus the replies the far side expects.

    Replies matter more than they look: a program asking "where is the
    cursor?" (DSR) or "what are you?" (DA) waits for an answer, and a
    terminal that stays silent leaves it hanging. `on_response` is how those
    get back to the socket.
    """

    def __init__(
        self, cols=80, rows=24, scrollback=DEFAULT_SCROLLBACK, on_response=None
    ):
        self.screen = Screen(cols, rows, scrollback)
        self.on_response = on_response or (lambda data: None)
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._state = GROUND
        self._params = ""
        self._private = ""
        self._intermediate = ""
        self._string = ""
        self._string_kind = ""
        self._charset_slot = 0

    # -- feeding ----------------------------------------------------------

    def feed(self, data):
        """Consume a chunk of output. Any number of bytes, split anywhere.

        Incremental on both levels: a UTF-8 sequence split across two reads
        is held by the decoder, and an escape sequence split across two reads
        is held by the state machine. A terminal that assumed whole sequences
        per read would corrupt roughly whenever the network felt like it.
        """
        text = self._decoder.decode(data)
        if not text:
            return
        pending = []
        for char in text:
            if self._state == GROUND:
                if char < " " or char == "\x7f":
                    if pending:
                        self.screen.write("".join(pending))
                        pending = []
                    self._control(char)
                else:
                    pending.append(char)
                continue

            if pending:  # pragma: no cover - states other than GROUND clear it
                self.screen.write("".join(pending))
                pending = []

            if self._state == ESCAPE:
                self._escape(char)
            elif self._state == CSI:
                self._csi(char)
            elif self._state in (OSC, STRING):
                self._string_char(char)
            elif self._state == CHARSET:
                self._designate(char)

        if pending:
            self.screen.write("".join(pending))

    # -- C0 ---------------------------------------------------------------

    def _control(self, char):
        screen = self.screen
        if char == "\x1b":
            self._state = ESCAPE
            self._params = self._private = self._intermediate = ""
        elif char == "\r":
            screen.move_to(x=0)
        elif char in "\n\v\f":
            screen._index()
            screen._wrap_pending = False
            screen.touch()
        elif char == "\b":
            # Back up over the deferred wrap first: after a character in the
            # last column the cursor is still on it, and a backspace there
            # means "leave that column", not "go to the one before it".
            if screen._wrap_pending:
                screen._wrap_pending = False
            elif screen.cursor_x:
                screen.cursor_x -= 1
            screen.touch()
        elif char == "\t":
            screen.tab()
        elif char == BELL:
            screen.bell_count += 1
        elif char == "\x0e":
            self._charset_slot = 1
            screen._graphics = screen._charsets[1] == "0"
        elif char == "\x0f":
            self._charset_slot = 0
            screen._graphics = screen._charsets[0] == "0"
        # Everything else in C0 is either meaningless here (NUL padding, XON)
        # or belongs to a protocol we do not speak.

    # -- ESC --------------------------------------------------------------

    def _escape(self, char):
        screen = self.screen
        if char == "[":
            self._state = CSI
            self._params = self._private = self._intermediate = ""
            return
        if char == "]":
            self._state = OSC
            self._string = ""
            self._string_kind = "]"
            return
        if char in "P^_X":
            # DCS, PM, APC and SOS. Nothing here answers any of them, but
            # they have to be swallowed to the terminator or their payload
            # gets printed.
            self._state = STRING
            self._string = ""
            self._string_kind = char
            return
        if char in "()*+":
            self._state = CHARSET
            self._charset_slot = "()*+".index(char)
            return

        self._state = GROUND
        if char == "7":
            screen.save_cursor()
        elif char == "8":
            screen.restore_cursor()
        elif char == "D":
            screen._index()
            screen.touch()
        elif char == "E":
            screen._index()
            screen.move_to(x=0)
        elif char == "M":
            screen._reverse_index()
            screen.touch()
        elif char == "H":
            screen.tabstops.add(screen.cursor_x)
        elif char == "c":
            screen.reset()
        elif char == "=":
            screen.keypad_app = True
        elif char == ">":
            screen.keypad_app = False

    def _designate(self, char):
        """ESC ( <char> and friends: which character set a slot holds."""
        self._state = GROUND
        slot = self._charset_slot if self._charset_slot < 2 else 0
        self.screen._charsets[slot] = char
        # Only the slot currently shifted in changes what gets drawn, and
        # nothing outside a DEC terminal ever shifts to G1.
        self.screen._graphics = self.screen._charsets[0] == "0"

    # -- strings ----------------------------------------------------------

    def _string_char(self, char):
        # Terminated by BEL (xterm's shortcut) or by ST, which is ESC \. The
        # ESC is caught here and the backslash discarded on the next char.
        if char == BELL:
            self._end_string()
        elif char == "\x1b":
            self._string += "\x1b"
        elif char == "\\" and self._string.endswith("\x1b"):
            self._string = self._string[:-1]
            self._end_string()
        elif char < " " and char != "\x1b":
            # A stray control byte means the string was never terminated;
            # abandoning it beats swallowing the rest of the session.
            self._end_string()
            self._control(char)
        else:
            self._string += char

    def _end_string(self):
        if self._string_kind == "]":
            self._osc(self._string)
        self._state = GROUND
        self._string = ""

    def _osc(self, payload):
        """Operating System Command. In practice: the window title."""
        code, _, text = payload.partition(";")
        # 0 sets icon name and title, 2 sets the title. 1 is the icon name
        # alone, which has nowhere to go here.
        if code in ("0", "2"):
            self.screen.title = text
            self.screen.touch()

    # -- CSI --------------------------------------------------------------

    def _csi(self, char):
        if "0" <= char <= "9" or char in ";:":
            self._params += char
            return
        if "<" <= char <= "?":
            self._private += char
            return
        if " " <= char <= "/":
            self._intermediate += char
            return

        self._state = GROUND
        # Sub-parameters (the colon form of SGR) are flattened to the same
        # list as the semicolon form; nothing here distinguishes them.
        params = [
            int(part) if part.isdigit() else 0
            for part in self._params.replace(":", ";").split(";")
        ]
        self._dispatch(char, params, self._private, self._intermediate)
        self._params = self._private = self._intermediate = ""

    def _dispatch(self, final, params, private, intermediate):
        screen = self.screen

        def arg(index=0, default=1):
            value = params[index] if index < len(params) else 0
            return value or default

        if private:
            # A private prefix makes it a different sequence, not a variant
            # of the same one, and there is no overlap between the two
            # tables. Falling through to the standard table below is how
            # ESC[>c -- vim asking what terminal this is -- used to be
            # answered with a primary device-attributes report, which vim
            # cannot match against the secondary form it asked for. The
            # unmatched reply stayed in its input buffer and was read as
            # keystrokes: ESC left insert mode and 'c' started a change.
            self._private_dispatch(final, params, private)
            return
        if intermediate:
            # Only DECSCUSR (cursor shape) is at all common, and its effect
            # is cosmetic here, so intermediates are dropped rather than
            # guessed at.
            return

        if final == "@":
            screen.insert_chars(arg())
        elif final == "A":
            screen.move_by(dy=-arg())
        elif final == "B":
            screen.move_by(dy=arg())
        elif final == "C":
            screen.move_by(dx=arg())
        elif final == "D":
            screen.move_by(dx=-arg())
        elif final == "E":
            screen.move_by(dy=arg())
            screen.move_to(x=0)
        elif final == "F":
            screen.move_by(dy=-arg())
            screen.move_to(x=0)
        elif final in "G`":
            screen.move_to(x=arg() - 1)
        elif final in "Hf":
            screen.move_to(x=arg(1) - 1, y=arg(0) - 1)
        elif final == "I":
            screen.tab(arg())
        elif final == "J":
            screen.erase_in_display(arg(0, 0))
        elif final == "K":
            screen.erase_in_line(arg(0, 0))
        elif final == "L":
            screen.insert_lines(arg())
        elif final == "M":
            screen.delete_lines(arg())
        elif final == "P":
            screen.delete_chars(arg())
        elif final == "S":
            screen.scroll_up(arg())
        elif final == "T":
            screen.scroll_down(arg())
        elif final == "X":
            screen.erase_chars(arg())
        elif final == "Z":
            screen.back_tab(arg())
        elif final == "b":
            screen.repeat_last(arg())
        elif final == "d":
            screen.move_to(y=arg() - 1)
        elif final == "c":
            # "I am a VT102." Enough to satisfy anything that asks, and a
            # smaller promise than claiming to be a VT220 with options.
            self.on_response(b"\x1b[?6c")
        elif final == "g":
            if arg(0, 0) == 3:
                screen.tabstops.clear()
            else:
                screen.tabstops.discard(screen.cursor_x)
        elif final in "hl":
            self._set_modes(params, final == "h")
        elif final == "m":
            screen.style = apply_sgr(screen.style, params)
        elif final == "n":
            if arg(0, 0) == 6:
                row = screen.cursor_y + 1
                if screen.origin_mode:
                    row -= screen.top
                self.on_response(f"\x1b[{row};{screen.cursor_x + 1}R".encode("ascii"))
            elif arg(0, 0) == 5:
                self.on_response(b"\x1b[0n")
        elif final == "r":
            top = arg(0) - 1
            bottom = arg(1, screen.rows) - 1
            if 0 <= top < bottom < screen.rows:
                screen.top, screen.bottom = top, bottom
                screen.move_to(0, 0)
        elif final == "s":
            screen.save_cursor()
        elif final == "u":
            screen.restore_cursor()
        elif final == "t":
            # XTWINOPS. The window-manipulation half is refused by every
            # terminal worth using, but the two size *questions* are asked
            # in earnest -- by anything trying to work out how big it can
            # draw -- and go unanswered at the cost of a startup stall.
            if arg(0, 0) == 18:
                self.on_response(f"\x1b[8;{screen.rows};{screen.cols}t".encode("ascii"))
            elif arg(0, 0) == 14:
                # In pixels, which we do not know here: the cell size lives
                # in the widget. Reporting zero is the documented way to say
                # so, and is what a terminal without a window reports.
                self.on_response(b"\x1b[4;0;0t")

    def _private_dispatch(self, final, params, private):
        """CSI sequences carrying a private prefix: ?, >, < or =.

        Deliberately a short list that ends in silence. Most of what arrives
        here is a program negotiating a feature this terminal does not have
        -- modifyOtherKeys, the kitty keyboard protocol, focus reporting --
        and the right answer to those is to do nothing, not to guess.

        The queries are the exception. A program that asks a question waits
        for an answer, so anything with a well-defined reply gets one in the
        form it asked for; silence there costs a startup delay at best and
        leaves the asker parsing our output as keystrokes at worst.
        """
        if private == "?" and final in "hl":
            self._set_private_modes(params, final == "h")
        elif private == ">" and final == "c":
            # Secondary device attributes: "what are you, and what version?"
            # Answering as an unremarkable VT220 keeps programs from
            # enabling extensions on the strength of a version number we
            # invented. The '>' in the reply is what makes it match the
            # question -- that is the whole bug this method exists for.
            self.on_response(b"\x1b[>0;10;1c")
        elif private == "=" and final == "c":
            # Tertiary DA. No unit ID to report, so an empty DECRPTUI.
            self.on_response(b"\x1bP!|00000000\x1b\\")
        elif private == "?" and final == "n" and params and params[0] == 6:
            # DECXCPR: the same answer as DSR 6, marked as the private form
            # so the asker can tell which question it answers.
            row = self.screen.cursor_y + 1
            if self.screen.origin_mode:
                row -= self.screen.top
            self.on_response(f"\x1b[?{row};{self.screen.cursor_x + 1}R".encode("ascii"))

    def _set_modes(self, params, enabled):
        for code in params:
            if code == 4:
                self.screen.insert_mode = enabled

    def _set_private_modes(self, params, enabled):
        screen = self.screen
        for code in params:
            if code == 1:
                screen.cursor_keys_app = enabled
            elif code == 6:
                screen.origin_mode = enabled
                screen.move_to(0, 0)
            elif code == 7:
                screen.autowrap = enabled
            elif code == 25:
                screen.cursor_visible = enabled
                screen.touch()
            elif code in (9, 1000, 1002, 1003, 1005, 1006, 1015):
                # Remembered, not acted on: whether the far side wants mouse
                # reports decides whether a click should be sent to it or
                # used to select text locally.
                screen.mouse_tracking = code if enabled else 0
            elif code in (47, 1047, 1049):
                if code == 1049 and enabled:
                    screen.save_cursor()
                screen.set_alternate(enabled)
                if code == 1049:
                    if enabled:
                        screen.erase_in_display(2)
                    else:
                        screen.restore_cursor()
            elif code == 1048:
                screen.save_cursor() if enabled else screen.restore_cursor()
            elif code == 2004:
                screen.bracketed_paste = enabled

    # -- convenience ------------------------------------------------------

    def resize(self, cols, rows):
        return self.screen.resize(cols, rows)

    @property
    def revision(self):
        return self.screen.revision

"""Keystrokes to terminal bytes.

The other two consoles hand the guest a keysym and let it do its own
keyboard handling. A terminal has no keyboard: it has a byte stream, and
every key that is not a letter has to be spelled out as an escape sequence
before it means anything. Getting this table wrong is what makes arrow keys
print "^[[A" at a prompt.

Keyvals here are X11 keysyms, which is what GDK keyvals are, so the numbers
match Gdk.KEY_* exactly -- and spelling them out rather than importing Gdk
keeps this half testable without a display, the same trade rfb.py makes.

The dialect is xterm's, because that is what TERM says we are.
"""

# -- keysyms ---------------------------------------------------------------

BACKSPACE = 0xFF08
TAB = 0xFF09
LINEFEED = 0xFF0A
RETURN = 0xFF0D
ESCAPE = 0xFF1B
ISO_LEFT_TAB = 0xFE20

HOME = 0xFF50
LEFT = 0xFF51
UP = 0xFF52
RIGHT = 0xFF53
DOWN = 0xFF54
PAGE_UP = 0xFF55
PAGE_DOWN = 0xFF56
END = 0xFF57
INSERT = 0xFF63
DELETE = 0xFFFF

KP_ENTER = 0xFF8D
KP_HOME = 0xFF95
KP_LEFT = 0xFF96
KP_UP = 0xFF97
KP_RIGHT = 0xFF98
KP_DOWN = 0xFF99
KP_PAGE_UP = 0xFF9A
KP_PAGE_DOWN = 0xFF9B
KP_END = 0xFF9C
KP_INSERT = 0xFF9E
KP_DELETE = 0xFF9F

F1 = 0xFFBE

# The keypad's arrows and friends are the same keys as the main block as far
# as anything reading the stream is concerned.
KEYPAD_ALIASES = {
    KP_ENTER: RETURN,
    KP_HOME: HOME,
    KP_LEFT: LEFT,
    KP_UP: UP,
    KP_RIGHT: RIGHT,
    KP_DOWN: DOWN,
    KP_PAGE_UP: PAGE_UP,
    KP_PAGE_DOWN: PAGE_DOWN,
    KP_END: END,
    KP_INSERT: INSERT,
    KP_DELETE: DELETE,
}

# -- the tables ------------------------------------------------------------

# Keys that change shape with DECCKM: normal form uses CSI, application form
# uses SS3. A shell with readline turns application mode on, so this is not
# an obscure corner -- it is what arrow keys do at every bash prompt.
CURSOR_KEYS = {
    UP: "A",
    DOWN: "B",
    RIGHT: "C",
    LEFT: "D",
    HOME: "H",
    END: "F",
}

# Keys that are always CSI <number> ~.
TILDE_KEYS = {
    INSERT: 2,
    DELETE: 3,
    PAGE_UP: 5,
    PAGE_DOWN: 6,
}

# F1-F4 are SS3 in xterm, the rest are tilde sequences, and the numbers skip
# 16 and 22 for reasons that are now purely historical.
FUNCTION_SS3 = {0: "P", 1: "Q", 2: "R", 3: "S"}
FUNCTION_TILDE = {4: 15, 5: 17, 6: 18, 7: 19, 8: 20, 9: 21, 10: 23, 11: 24}

SIMPLE = {
    RETURN: b"\r",
    LINEFEED: b"\r",
    TAB: b"\t",
    ESCAPE: b"\x1b",
    ISO_LEFT_TAB: b"\x1b[Z",
    # DEL, not BS. xterm has sent 0x7f for the backspace key for decades and
    # every stty default on a Linux guest expects it; sending 0x08 instead
    # gets you "^H" in bash.
    BACKSPACE: b"\x7f",
}


def modifier_code(shift=False, alt=False, ctrl=False):
    """xterm's modifier parameter: 1 plus a bitmask, or None for none held."""
    mask = (1 if shift else 0) | (2 if alt else 0) | (4 if ctrl else 0)
    return mask + 1 if mask else None


def _csi(final, modifier=None, app=False):
    if modifier is not None:
        return f"\x1b[1;{modifier}{final}".encode("ascii")
    return (f"\x1bO{final}" if app else f"\x1b[{final}").encode("ascii")


def _tilde(number, modifier=None):
    if modifier is not None:
        return f"\x1b[{number};{modifier}~".encode("ascii")
    return f"\x1b[{number}~".encode("ascii")


def control_byte(char):
    """The control character Ctrl+<char> produces, if it produces one.

    Ctrl+C is the reason a console is worth having, so this is not a detail:
    the whole range from Ctrl+@ through Ctrl+_ has to work, not just the
    letters.
    """
    if not char:
        return None
    code = ord(char[0].upper() if char[0].isalpha() else char[0])
    if 0x40 <= code <= 0x5F:  # @ A-Z [ \ ] ^ _
        return bytes([code & 0x1F])
    if char[0] == " ":
        return b"\x00"
    if char[0] == "?":
        return b"\x7f"
    if char[0] == "/":
        # Ctrl+/ is Ctrl+_ on a US layout, which is undo in readline.
        return b"\x1f"
    return None


def key_sequence(
    keyval,
    char="",
    ctrl=False,
    alt=False,
    shift=False,
    cursor_app=False,
):
    """The bytes a key press should put on the wire, or None to ignore it.

    `char` is the character the key would type on its own, which the caller
    gets from Gdk.keyval_to_unicode -- taking it as an argument rather than
    deriving it here is what keeps the layout question (where is @ on a
    German keyboard?) with GDK, which actually knows.
    """
    keyval = KEYPAD_ALIASES.get(keyval, keyval)
    modifier = modifier_code(shift=shift, alt=alt, ctrl=ctrl)

    if keyval in CURSOR_KEYS:
        final = CURSOR_KEYS[keyval]
        # Home and End have no application form worth sending: xterm's SS3 H
        # is not what a modern terminfo expects, and CSI H works everywhere.
        app = cursor_app and keyval not in (HOME, END)
        return _csi(final, modifier, app)

    if keyval in TILDE_KEYS:
        return _tilde(TILDE_KEYS[keyval], modifier)

    if F1 <= keyval <= F1 + 11:
        number = keyval - F1
        if number in FUNCTION_SS3 and modifier is None:
            return f"\x1bO{FUNCTION_SS3[number]}".encode("ascii")
        if number in FUNCTION_SS3:
            return f"\x1b[1;{modifier}{FUNCTION_SS3[number]}".encode("ascii")
        return _tilde(FUNCTION_TILDE[number], modifier)

    if keyval in SIMPLE:
        sequence = SIMPLE[keyval]
        # Ctrl+Backspace is word-erase; readline wants 0x08 for it, which is
        # the one place the backspace key does send BS.
        if keyval == BACKSPACE and ctrl:
            sequence = b"\x08"
        return b"\x1b" + sequence if alt else sequence

    if ctrl:
        control = control_byte(char)
        if control is not None:
            return b"\x1b" + control if alt else control
        return None

    if not char:
        return None

    data = char.encode("utf-8")
    # Alt as Meta, which is what every terminal has done since the key stopped
    # being called Meta: prefix with ESC rather than setting the high bit.
    return b"\x1b" + data if alt else data


# -- the Send Key menu -----------------------------------------------------

# The menu offers combinations aimed at a graphical guest, and most of them
# mean nothing to a character terminal. Rather than fail silently, the ones
# that do have a terminal equivalent are translated and the rest are refused
# by name, so the status bar can say why nothing happened.
MENU_EQUIVALENTS = {
    (0xFFE3, 0xFF1B): b"\x1b",  # Ctrl+Esc -> just Escape
}


def menu_sequence(keysyms):
    """What a Send Key menu combination means here, if anything.

    Ctrl+Alt+F<n> switches virtual terminals on a machine with a console
    driver; there is no such thing on the far end of a termproxy socket, and
    inventing a sequence for it would be worse than admitting it does not
    apply.
    """
    keysyms = tuple(keysyms)
    if keysyms in MENU_EQUIVALENTS:
        return MENU_EQUIVALENTS[keysyms]

    # A single ordinary key, or one modifier and a key, can be expressed.
    modifiers = {0xFFE3: "ctrl", 0xFFE4: "ctrl", 0xFFE9: "alt", 0xFFEA: "alt"}
    held = {modifiers[k] for k in keysyms if k in modifiers}
    rest = [k for k in keysyms if k not in modifiers and k not in (0xFFE1, 0xFFE2)]
    if len(rest) != 1:
        return None

    keyval = rest[0]
    if held == {"ctrl", "alt"} and F1 <= keyval <= F1 + 11:
        # Ctrl+Alt+F<n> switches virtual terminals, which is a thing a
        # console driver does. There is no console driver behind a pty, and
        # sending the function key alone would run whatever F2 happens to be
        # bound to in the program on screen -- a different action, silently.
        return None

    char = chr(keyval) if 0x20 <= keyval <= 0x7E else ""
    return key_sequence(
        keyval,
        char=char,
        ctrl="ctrl" in held,
        alt="alt" in held,
    )

"""X11 keysyms, and the combinations the Send Key menu offers.

Both protocols speak keysyms -- spice_display_send_keys() takes them as
integers and RFB's KeyEvent carries them on the wire -- so the table lives
here rather than twice over in the two console modules.

What the menu is *for* is the combinations the host will not let through.
Alt+Tab switches windows on this computer; Ctrl+Alt+F2 switches virtual
terminals on this computer if it is a Linux desktop; PrintScreen is grabbed
by half the screenshot tools ever written. None of them reach the guest by
being pressed, so the only way to send one is to ask for it by name.
"""

# Modifiers and the odd keys out.
CONTROL_L = 0xFFE3
ALT_L = 0xFFE9
SHIFT_L = 0xFFE1
DELETE = 0xFFFF
BACKSPACE = 0xFF08
TAB = 0xFF09
ESCAPE = 0xFF1B
PRINT = 0xFF61

# F1 through F12 are consecutive, which is what makes the loop below honest
# rather than twelve lines of magic numbers.
F1 = 0xFFBE
FUNCTION_KEYS = 12

CTRL_ALT_DEL = (CONTROL_L, ALT_L, DELETE)


def function_key(number):
    """The keysym for F<number>, counting from 1."""
    if not 1 <= number <= FUNCTION_KEYS:
        raise ValueError(f"F{number} is not a key")
    return F1 + number - 1


def _build_menu():
    """(label, keysyms) pairs, with None where a separator goes."""
    entries = [
        ("Ctrl+Alt+Del", CTRL_ALT_DEL),
        # Kills the X server on a Linux guest that has it enabled, which is
        # the way out of a wedged desktop.
        ("Ctrl+Alt+Backspace", (CONTROL_L, ALT_L, BACKSPACE)),
        None,
    ]
    entries += [
        (f"Ctrl+Alt+F{number}", (CONTROL_L, ALT_L, function_key(number)))
        for number in range(1, FUNCTION_KEYS + 1)
    ]
    entries += [
        None,
        ("Alt+Tab", (ALT_L, TAB)),
        ("Alt+Shift+Tab", (ALT_L, SHIFT_L, TAB)),
        ("Alt+F4", (ALT_L, function_key(4))),
        ("Ctrl+Esc", (CONTROL_L, ESCAPE)),
        ("PrintScreen", (PRINT,)),
    ]
    return tuple(entries)


SEND_KEYS = _build_menu()

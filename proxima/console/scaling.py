"""How large a guest pixel is drawn, shared by the two graphical consoles.

The setting is one number -- a percentage, 100 meaning "as before" -- but it
buys something different on each protocol, and the difference is worth
stating because it decides what the setting is *for*.

SPICE can change the guest's resolution, so the number is applied where it
does the most good: the guest is asked for fewer pixels and the smaller
picture is drawn larger. That is fewer pixels to render, encode, send and
decode, which is why it is the answer to a SPICE console that lags on a
high-resolution screen. spice-gtk asks the guest for
`window * scale_factor / zoom` pixels, and `scale_factor` is 2 on a HiDPI
display -- so a console that looks like a 1920x1080 window is really asking
the guest for 3840x2160, and 200% cancels that exactly.

VNC cannot. RFB's DesktopSize (-223) is a server-to-client pseudo-encoding:
the guest reports the resolution it chose, and there is no message in what
this client speaks for asking it to choose differently. So the number can
only magnify what arrives, which costs nothing and saves nothing -- it makes
a small guest readable on a large screen and that is all. 200% doubles every
pixel exactly and stays sharp; the steps between it and 100% interpolate.
"""

# The offered percentages. Anything between 100 and 200 works, but a menu
# wants a short list of round numbers rather than a spin button.
CONSOLE_SCALES = (100, 125, 150, 175, 200)

DEFAULT_CONSOLE_SCALE = 100


def clamp_console_scale(percent):
    """A usable percentage, whatever was stored or passed in.

    Settings files are edited by hand and older ones predate this, so the
    value is not assumed to be a number, let alone one of the offered ones.
    Anything unusable becomes 100, which is the behaviour every console had
    before the setting existed.
    """
    try:
        value = int(percent)
    except (TypeError, ValueError):
        return DEFAULT_CONSOLE_SCALE
    if value < min(CONSOLE_SCALES) or value > max(CONSOLE_SCALES):
        return DEFAULT_CONSOLE_SCALE
    return value


def console_scale_index(percent):
    """Where a percentage sits in CONSOLE_SCALES, for the radio menu.

    A value that is valid but not one of the offered ones -- 130, say, typed
    into the settings file -- picks the nearest offered one rather than
    leaving every radio item unset.
    """
    value = clamp_console_scale(percent)
    return min(
        range(len(CONSOLE_SCALES)),
        key=lambda index: abs(CONSOLE_SCALES[index] - value),
    )

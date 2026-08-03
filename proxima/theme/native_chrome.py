"""Match the native Windows titlebar to the application theme.

GTK draws client-side decorations only when it owns the frame; a normal
GtkWindow on Windows gets a real OS titlebar, which stays light while the
rest of the app goes dark. DWM exposes attributes for exactly this, but only
on Windows 10 1809 and later, and the "use dark mode" attribute number
changed between builds -- 19 in 1809, 20 from 2004 onward -- so both are
tried.

Everything here is a no-op off Windows.
"""

import ctypes
import os

DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
DWMWA_BORDER_COLOR = 34

IS_WINDOWS = os.name == "nt"


def _hwnd_for(window):
    """The Win32 HWND behind a GtkWindow, or None."""
    if not IS_WINDOWS:
        return None
    gdk_window = window.get_window()
    if gdk_window is None:
        return None

    # gdk_win32_window_get_handle() is not introspectable -- GdkWin32's
    # typelib exposes the Win32Window class but none of its accessors -- so
    # it has to be called through ctypes. The symbol has been stable for the
    # entire GTK3 series.
    raw = _gobject_pointer(gdk_window)
    if raw is None:
        return None

    for name in ("libgdk-3-0.dll", "libgdk-3.dll", "gdk-3.dll"):
        try:
            gdk = ctypes.CDLL(name)
            func = gdk.gdk_win32_window_get_handle
        except (OSError, AttributeError):
            continue
        func.restype = ctypes.c_void_p
        func.argtypes = [ctypes.c_void_p]
        handle = func(ctypes.c_void_p(raw))
        return int(handle) if handle else None
    return None


def _gobject_pointer(obj):
    """The raw C pointer behind a PyGObject wrapper, via its capsule."""
    try:
        capsule = obj.__gpointer__
        ctypes.pythonapi.PyCapsule_GetPointer.restype = ctypes.c_void_p
        ctypes.pythonapi.PyCapsule_GetPointer.argtypes = [
            ctypes.py_object,
            ctypes.c_char_p,
        ]
        return ctypes.pythonapi.PyCapsule_GetPointer(capsule, None)
    except Exception:
        return None


def _colorref(hex_colour):
    """#RRGGBB -> the 0x00BBGGRR integer DWM wants."""
    value = hex_colour.lstrip("#")
    red, green, blue = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    return (blue << 16) | (green << 8) | red


def _set_attribute(hwnd, attribute, value, size=4):
    try:
        dwm = ctypes.windll.dwmapi
    except (AttributeError, OSError):
        return False
    buffer = ctypes.c_int(value)
    result = dwm.DwmSetWindowAttribute(
        ctypes.c_void_p(hwnd),
        ctypes.c_uint(attribute),
        ctypes.byref(buffer),
        ctypes.c_uint(size),
    )
    return result == 0


def apply_dark_titlebar(window, dark=True, caption=None, text=None, border=None):
    """Set the titlebar appearance. Returns a dict describing what happened."""
    result = {"hwnd": None, "dark": False, "caption": False}
    if not IS_WINDOWS:
        return result

    hwnd = _hwnd_for(window)
    result["hwnd"] = hwnd
    if not hwnd:
        return result

    # Attribute 20 on current builds, 19 on 1809-1903. Setting the wrong one
    # simply fails, so try the modern number first.
    for attribute in (DWMWA_USE_IMMERSIVE_DARK_MODE, DWMWA_USE_IMMERSIVE_DARK_MODE_OLD):
        if _set_attribute(hwnd, attribute, 1 if dark else 0):
            result["dark"] = True
            break

    # Explicit caption colours need Windows 11; failure here is harmless.
    if caption:
        result["caption"] = _set_attribute(
            hwnd, DWMWA_CAPTION_COLOR, _colorref(caption)
        )
    if text:
        _set_attribute(hwnd, DWMWA_TEXT_COLOR, _colorref(text))
    if border:
        _set_attribute(hwnd, DWMWA_BORDER_COLOR, _colorref(border))

    # Nudge the frame so the change is repainted immediately rather than on
    # the next activation.
    try:
        user32 = ctypes.windll.user32
        SWP_FLAGS = (
            0x0002 | 0x0001 | 0x0004 | 0x0020
        )  # NOMOVE|NOSIZE|NOZORDER|FRAMECHANGED
        user32.SetWindowPos(ctypes.c_void_p(hwnd), None, 0, 0, 0, 0, SWP_FLAGS)
    except Exception:
        pass

    return result

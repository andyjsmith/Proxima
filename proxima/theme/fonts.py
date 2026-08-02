"""Font rendering controls.

On Windows, GTK3 ignores gtk-xft-rgba from settings.ini and forces subpixel
antialiasing on, which is what produces the red/green fringing -- especially
on dark backgrounds, where the lack of gamma correction makes it far more
visible. Setting cairo font options directly on the GdkScreen bypasses that
path entirely and actually works.

Hinting is a separate problem. Pango defaults to the win32 fontmap on
Windows, whose cairo scaled fonts are backed by GDI; GDI does its own hinting
and ignores cairo's hint_style completely. Only the FreeType backend honours
it, which is what the font_backend = "fontconfig" setting selects. It is read
by Pango exactly once, when the default fontmap is first built, so changing
it needs an application restart.
"""

import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gtk, PangoCairo  # noqa: E402

try:
    import cairo
except ImportError:                        # pragma: no cover
    cairo = None


# Keys are the values stored in settings.json; labels are what the settings
# dialog shows.
ANTIALIAS_CHOICES = [
    ("grayscale", "Grayscale (fixes colour fringing)"),
    ("subpixel", "Subpixel / ClearType"),
    ("none", "None (aliased)"),
    ("default", "System default"),
]

HINT_STYLE_CHOICES = [
    ("slight", "Slight (soft, shape-accurate)"),
    ("full", "Full (crisp, Windows-like)"),
    ("medium", "Medium"),
    ("none", "None"),
]

FONT_BACKEND_CHOICES = [
    ("fontconfig", "FreeType (hinting works)"),
    ("win32", "GDI (Windows, hinting ignored)"),
    ("default", "Platform default"),
]

# Fonts worth offering for a dense professional UI.
FONT_CHOICES = [
    "",                     # leave the theme's choice alone
    "Segoe UI 9",
    "Segoe UI 10",
    "Inter 10",
    "Noto Sans 10",
    "Cantarell 11",
    "DejaVu Sans 10",
    "Tahoma 9",
]


def _cairo_antialias(name):
    if cairo is None:
        return None
    return {
        "grayscale": cairo.ANTIALIAS_GRAY,
        "subpixel": cairo.ANTIALIAS_SUBPIXEL,
        "none": cairo.ANTIALIAS_NONE,
        "default": cairo.ANTIALIAS_DEFAULT,
    }.get(name, cairo.ANTIALIAS_GRAY)


def _cairo_hint_style(name):
    if cairo is None:
        return None
    return {
        "slight": cairo.HINT_STYLE_SLIGHT,
        "full": cairo.HINT_STYLE_FULL,
        "medium": cairo.HINT_STYLE_MEDIUM,
        "none": cairo.HINT_STYLE_NONE,
    }.get(name, cairo.HINT_STYLE_SLIGHT)


def font_backend():
    """Which cairo font backend Pango is using, and whether hinting works.

    Returns (name, hinting_supported).
    """
    try:
        fontmap = PangoCairo.FontMap.get_default()
        font_type = fontmap.get_font_type()
    except Exception:
        return ("unknown", True)

    try:
        value = int(font_type)
    except (TypeError, ValueError):
        return (str(font_type), True)

    names = {0: "toy", 1: "freetype", 2: "win32",
             3: "quartz", 4: "user", 5: "dwrite"}
    name = names.get(value, f"type-{value}")
    # Only the FreeType backend implements cairo hint styles. GDI, DirectWrite
    # and Quartz all do their own hinting and ignore the requested style.
    return (name, name == "freetype")


def apply_font_options(screen, config, root_widget=None):
    """Push the configured font rendering onto the screen and live widgets.

    Order matters. GTK3's settings code regenerates the screen font options
    from the gtk-xft-* properties whenever one of them changes, so those must
    be set FIRST -- otherwise they silently overwrite the cairo options we
    care about. Screen options go last and win.
    """
    if cairo is None:
        return

    settings = Gtk.Settings.get_default()
    antialias = config.get("antialias", "grayscale")
    hint_style = config.get("hint_style", "slight")

    # 1. GtkSettings first. Correct on Linux, ignored on Windows.
    settings.set_property("gtk-xft-antialias", 0 if antialias == "none" else 1)
    settings.set_property("gtk-xft-rgba",
                          "rgb" if antialias == "subpixel" else "none")
    settings.set_property("gtk-xft-hinting", 0 if hint_style == "none" else 1)
    settings.set_property("gtk-xft-hintstyle", f"hint{hint_style}")

    font = config.get("font_name") or ""
    if font:
        settings.set_property("gtk-font-name", font)

    # 2. Screen font options last, so nothing overwrites them.
    options = cairo.FontOptions()
    options.set_antialias(_cairo_antialias(antialias))
    options.set_hint_style(_cairo_hint_style(hint_style))
    options.set_hint_metrics(cairo.HINT_METRICS_ON
                             if config.get("hint_metrics")
                             else cairo.HINT_METRICS_OFF)
    options.set_subpixel_order(cairo.SUBPIXEL_ORDER_RGB)
    screen.set_font_options(options)

    # 3. Invalidate caches. Without this, existing widgets keep their old
    #    PangoContext and nothing visibly changes.
    try:
        settings.set_property("gtk-fontconfig-timestamp", int(time.time()))
    except (TypeError, ValueError):
        pass

    if root_widget is not None:
        refresh_pango(root_widget, options)


def refresh_pango(widget, options):
    """Push new font options into every widget's PangoContext, recursively."""
    try:
        context = widget.get_pango_context()
        if context is not None:
            PangoCairo.context_set_font_options(context, options)
            context.changed()
    except Exception:
        pass

    if isinstance(widget, Gtk.Label):
        # Labels cache a PangoLayout built from the old context.
        layout = widget.get_layout()
        if layout is not None:
            layout.context_changed()

    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            refresh_pango(child, options)

    widget.queue_resize()
    widget.queue_draw()

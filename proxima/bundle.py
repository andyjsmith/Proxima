"""The two things a packaged build still has to be told at run time.

A packaged build ships its own GTK: the pixbuf loaders, the GSettings
schemas, the icon themes, the GStreamer plugins. None of that is reached by
following imports -- GTK loads it at run time from paths that were compiled
into the libraries on the machine the build was made on, which on a user's
computer point at nothing.

Almost all of that is PyInstaller's job, and it does it: its runtime hooks
(pyi_rth_gi, _gdkpixbuf, _gio, _glib, _gstreamer, _gtk) set GI_TYPELIB_PATH,
GDK_PIXBUF_MODULE_FILE, GIO_MODULE_DIR, XDG_DATA_DIRS, GTK_DATA_PREFIX and
the GST_PLUGIN_* variables before any of our code runs. What is left is what
they do not cover and what they get wrong on a machine where the bundle is
not writable -- see apply().

Nothing in here runs from a source checkout: there the system's own GTK is
already right, and second-guessing it would only break a working desktop.

Deliberately dependency-free, and importable before gi, for the same reason
config.py is.
"""

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def is_bundled():
    """Whether this is a packaged build rather than a source checkout."""
    return getattr(sys, "frozen", False)


def bundle_root():
    """Where the bundle's data went, or None if this is not a bundle.

    PyInstaller's own directory, not the executable's: in a onedir build the
    data lives under _internal, and inside a .app it is Contents/Frameworks
    while the executable is in Contents/MacOS.
    """
    if not is_bundled():
        return None
    return Path(sys._MEIPASS) if hasattr(sys, "_MEIPASS") else None


def cache_dir():
    """Somewhere writable for the caches a bundle has to generate."""
    from .config import config_dir

    return config_dir() / "bundle"


def _fontconfig_env(root):
    """Where the bundle's fonts.conf is, if it carries one.

    The one thing no PyInstaller runtime hook does. Fontconfig on Windows
    looks for its configuration beside the DLL that loaded it, which in a
    bundle is nothing; without this it reports "Cannot load default config
    file", ends up with no font directories at all, and the FreeType backend
    theme/fonts.py deliberately asks for has nothing to draw with. Only
    Windows carries it -- macOS renders through CoreText and every Linux
    desktop has a fontconfig of its own.
    """
    fonts = root / "etc" / "fonts"
    if not (fonts / "fonts.conf").exists():
        return {}
    return {
        "FONTCONFIG_PATH": str(fonts),
        "FONTCONFIG_FILE": str(fonts / "fonts.conf"),
    }


def _gst_registry(root):
    """Where GStreamer may write its plugin index, or None to leave it alone.

    pyi_rth_gstreamer puts it inside the bundle, which is fine while the
    bundle is a directory in a downloads folder and wrong the moment it is
    installed: nothing writes to Program Files or /opt, so the registry is
    rebuilt by scanning every plugin on every single start. Moved to the
    config directory, which is writable by definition.

    Left alone if it already points somewhere outside the bundle: that is
    either a deliberate override or a platform whose hook did not set it.
    """
    current = os.environ.get("GST_REGISTRY")
    if current:
        try:
            inside = Path(current).resolve().is_relative_to(root.resolve())
        except (OSError, ValueError):  # pragma: no cover - unreadable path
            return None
        if not inside:
            return None
    return str(cache_dir() / "gst-registry.bin")


def apply(root=None):
    """Put the bundle's paths into the environment. Returns what it set.

    Values already in the environment win: someone debugging a build with
    FONTCONFIG_FILE pointing somewhere else means it. GST_REGISTRY is the
    exception, because PyInstaller has already set that one itself.
    """
    root = root or bundle_root()
    if root is None:
        return {}

    applied = {}
    for name, value in _fontconfig_env(root).items():
        if os.environ.get(name):
            continue
        os.environ[name] = value
        applied[name] = value

    registry = _gst_registry(root)
    if registry is not None:
        try:
            Path(registry).parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # a read-only home is not worth failing over
            log.warning("could not create %s: %s", Path(registry).parent, exc)
        else:
            os.environ["GST_REGISTRY"] = registry
            applied["GST_REGISTRY"] = registry
    return applied


def report(root=None):
    """What the bundle carries, for --diagnose."""
    root = root or bundle_root()
    lines = ["--- bundle ---"]
    if root is None:
        lines.append("running from a source checkout; using the system GTK")
        return lines
    lines.append(f"  root: {root}")
    # Required means the program is broken without it. The layout is
    # PyInstaller's, not a prefix-shaped one: typelibs, GStreamer plugins and
    # GIO modules each land in a directory of its own naming.
    contents = (
        ("typelibs", "gi_typelibs", True),
        ("pixbuf loaders", "lib/gdk-pixbuf", True),
        ("gstreamer plugins", "gst_plugins", True),
        ("gsettings schemas", "share/glib-2.0/schemas", True),
        ("icon themes", "share/icons", True),
        ("gio modules", "gio_modules", False),
        # Windows only, and required there: see _fontconfig_env.
        ("fontconfig", "etc/fonts", sys.platform == "win32"),
    )
    for name, path, required in contents:
        if (root / path).exists():
            state = "ok"
        else:
            state = "MISSING" if required else "not bundled"
        lines.append(f"  [{state}] {name}: {path}")
    for name in sorted(
        (
            "GDK_PIXBUF_MODULE_FILE",
            "GI_TYPELIB_PATH",
            "GST_PLUGIN_PATH",
            "GST_PLUGIN_SYSTEM_PATH",
            "GST_REGISTRY",
            "GSETTINGS_SCHEMA_DIR",
            "GIO_MODULE_DIR",
            "FONTCONFIG_PATH",
            "XDG_DATA_DIRS",
        )
    ):
        lines.append(f"  {name} = {os.environ.get(name, '(unset)')}")
    return lines

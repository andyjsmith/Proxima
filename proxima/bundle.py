"""Point GTK at the copy of itself that a standalone build carries.

A packaged build ships its own GTK: the pixbuf loaders, the GSettings
schemas, the icon themes, the GStreamer plugins. None of that is reached by
following imports -- GTK loads it at run time from paths that were compiled
into the libraries on the machine the build was made on, which on a user's
computer point at nothing. So the paths are rewritten here, to the bundle,
before anything imports gi.

Nothing in here runs from a source checkout: there the system's own GTK is
already right, and second-guessing it would only break a working desktop.

Deliberately dependency-free, and importable before gi, for the same reason
config.py is.
"""

import os
import sys
from pathlib import Path

# The pixbuf loader cache names every module it knows about. The paths in it
# are wherever the loaders were when the cache was generated -- absolute on
# Debian, relative to the prefix on MSYS2 -- so neither survives being moved
# into a bundle without rewriting.
LOADER_SUFFIXES = (".so", ".dll", ".dylib")


def is_bundled():
    """Whether this is a compiled build rather than a source checkout."""
    return "__compiled__" in globals() or getattr(sys, "frozen", False)


def bundle_root():
    """The directory the bundle was unpacked into, or None if not bundled."""
    if not is_bundled():
        return None
    return Path(sys.executable).resolve().parent


def cache_dir():
    """Somewhere writable to keep the caches that have to be rewritten."""
    from .config import config_dir

    return config_dir() / "bundle"


def rewrite_loader_cache(text, loaders_dir):
    """Re-point every module path in a pixbuf loaders.cache at the bundle.

    Only the quoted module paths are touched. Everything else -- the
    comments, and the mime types and extensions that follow each module --
    is left exactly as it was.
    """
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if (
            len(stripped) > 2
            and stripped.startswith('"')
            and stripped.endswith('"')
            and stripped[1:-1].lower().endswith(LOADER_SUFFIXES)
        ):
            name = stripped[1:-1].replace("\\\\", "/").replace("\\", "/")
            name = name.rsplit("/", 1)[-1]
            target = Path(loaders_dir) / name
            # A loader named in the cache but missing from the bundle is
            # left alone rather than pointed at a file that is not there.
            if target.exists():
                escaped = str(target).replace("\\", "\\\\")
                lines.append(f'"{escaped}"')
                continue
        lines.append(line)
    return "\n".join(lines) + "\n"


def _cached_copy(name, text):
    """Write text to the cache dir, and return the path.

    Written only when it has changed, so a read-only or slow home directory
    is touched once rather than on every start.
    """
    path = cache_dir() / name
    try:
        if path.exists() and path.read_text(encoding="utf-8") == text:
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        print(f"[bundle] could not write {path}: {exc}")
        return None
    return path


def _first_existing(*paths):
    for path in paths:
        if path is not None and path.exists():
            return path
    return None


def _pixbuf_env(root):
    loaders = _first_existing(*root.glob("lib/gdk-pixbuf-2.0/*/loaders"))
    if loaders is None:
        return {}
    env = {"GDK_PIXBUF_MODULEDIR": str(loaders)}
    cache = loaders.parent / "loaders.cache"
    if cache.exists():
        try:
            rewritten = rewrite_loader_cache(
                cache.read_text(encoding="utf-8", errors="replace"), loaders
            )
        except OSError:
            return env
        path = _cached_copy("loaders.cache", rewritten)
        if path is not None:
            env["GDK_PIXBUF_MODULE_FILE"] = str(path)
    return env


def _gstreamer_env(root):
    plugins = root / "lib" / "gstreamer-1.0"
    if not plugins.is_dir():
        return {}
    env = {
        "GST_PLUGIN_SYSTEM_PATH": str(plugins),
        "GST_PLUGIN_PATH": str(plugins),
        # The registry indexes the bundled plugins by path, so it cannot be
        # shared with a system GStreamer's.
        "GST_REGISTRY": str(cache_dir() / "gst-registry.bin"),
    }
    scanner = _first_existing(
        plugins / "gst-plugin-scanner.exe",
        plugins / "gst-plugin-scanner",
        root / "libexec" / "gstreamer-1.0" / "gst-plugin-scanner",
    )
    if scanner is not None:
        env["GST_PLUGIN_SCANNER"] = str(scanner)
    return env


def _glib_env(root):
    env = {}
    schemas = root / "share" / "glib-2.0" / "schemas"
    if (schemas / "gschemas.compiled").exists():
        env["GSETTINGS_SCHEMA_DIR"] = str(schemas)
    modules = root / "lib" / "gio" / "modules"
    if modules.is_dir():
        env["GIO_MODULE_DIR"] = str(modules)
    return env


def _gtk_env(root):
    env = {}
    if (root / "share" / "icons").is_dir():
        env["GTK_DATA_PREFIX"] = str(root)
        env["GTK_EXE_PREFIX"] = str(root)
    fonts = root / "etc" / "fonts"
    if (fonts / "fonts.conf").exists():
        env["FONTCONFIG_PATH"] = str(fonts)
        env["FONTCONFIG_FILE"] = str(fonts / "fonts.conf")
    return env


def _data_dirs(root):
    """The bundle's share/ goes in front of the system's, never instead.

    A desktop's own icon theme and mime database are still worth having;
    they are simply not allowed to be the only ones, or a machine without
    them shows a window full of missing icons.
    """
    share = root / "share"
    if not share.is_dir():
        return None
    existing = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    if str(share) in existing.split(os.pathsep):
        return None
    return str(share) + os.pathsep + existing


def apply(root=None):
    """Put the bundle's paths into the environment. Returns what it set.

    Values already in the environment win: someone debugging a build with
    GST_PLUGIN_PATH pointing somewhere else means it.
    """
    root = root or bundle_root()
    if root is None:
        return {}

    wanted = {}
    for part in (_pixbuf_env, _gstreamer_env, _glib_env, _gtk_env):
        wanted.update(part(root))

    applied = {}
    for name, value in wanted.items():
        if os.environ.get(name):
            continue
        os.environ[name] = value
        applied[name] = value

    data_dirs = _data_dirs(root)
    if data_dirs is not None:
        os.environ["XDG_DATA_DIRS"] = data_dirs
        applied["XDG_DATA_DIRS"] = data_dirs
    return applied


def report(root=None):
    """What the bundle carries, for --diagnose."""
    root = root or bundle_root()
    lines = ["--- bundle ---"]
    if root is None:
        lines.append("running from a source checkout; using the system GTK")
        return lines
    lines.append(f"  root: {root}")
    # Required means the program is broken without it. The optional two are
    # supplied by any Linux desktop, so they are only bundled on Windows.
    for name, path, required in (
        ("pixbuf loaders", "lib/gdk-pixbuf-2.0", True),
        ("gstreamer plugins", "lib/gstreamer-1.0", True),
        ("gsettings schemas", "share/glib-2.0/schemas", True),
        ("icon themes", "share/icons", True),
        ("gio modules", "lib/gio/modules", False),
        ("fontconfig", "etc/fonts", False),
    ):
        if (root / path).exists():
            state = "ok"
        else:
            state = "MISSING" if required else "not bundled"
        lines.append(f"  [{state}] {name}: {path}")
    for name in sorted(
        (
            "GDK_PIXBUF_MODULE_FILE",
            "GST_PLUGIN_SYSTEM_PATH",
            "GSETTINGS_SCHEMA_DIR",
            "GIO_MODULE_DIR",
            "FONTCONFIG_PATH",
            "XDG_DATA_DIRS",
        )
    ):
        lines.append(f"  {name} = {os.environ.get(name, '(unset)')}")
    return lines

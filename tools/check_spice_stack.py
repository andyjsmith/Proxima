#!/usr/bin/env python3
"""
Verify that the SPICE + GTK3 + PyGObject stack is usable.

Run with no arguments for a headless check:
    python3 check_spice_stack.py

Add --window to also open a real GtkNotebook containing an embedded
SpiceDisplay widget, which is the thing we actually care about:
    python3 check_spice_stack.py --window

On Windows this must run under the MSYS2 UCRT64 Python (/ucrt64/bin/python),
not a python.org install.
"""

import os
import sys
import glob
import platform

PASS = "  [ok]  "
FAIL = "  [FAIL]"
INFO = "         "

results = []


def report(ok, label, detail=""):
    results.append(ok)
    print(f"{PASS if ok else FAIL} {label}")
    if detail:
        for line in str(detail).splitlines():
            print(f"{INFO} {line}")


def find_typelib_dirs():
    """Locate girepository search paths across distros and MSYS2."""
    dirs = []
    for var in ("GI_TYPELIB_PATH", "GIRepository_TYPELIB_PATH"):
        if os.environ.get(var):
            dirs += os.environ[var].split(os.pathsep)
    for pat in (
        "/usr/lib/*/girepository-*",
        "/usr/lib/girepository-*",
        "/usr/local/lib/*/girepository-*",
        "/ucrt64/lib/girepository-*",
        "/mingw64/lib/girepository-*",
        os.path.join(sys.prefix, "lib", "girepository-*"),
    ):
        dirs += glob.glob(pat)
    return sorted({d for d in dirs if os.path.isdir(d)})


def discover_spice_namespaces(dirs):
    """Return [(namespace, version)] for every Spice*.typelib found."""
    found = {}
    for d in dirs:
        for path in glob.glob(os.path.join(d, "Spice*.typelib")):
            stem = os.path.basename(path)[: -len(".typelib")]
            if "-" in stem:
                ns, ver = stem.rsplit("-", 1)
                found[ns] = ver
    return sorted(found.items())


print(f"\nPython  {sys.version.split()[0]}  ({sys.executable})")
print(f"Platform {platform.system()} {platform.machine()}\n")

# 1. PyGObject itself
try:
    import gi

    report(True, "PyGObject importable", f"version {gi.__version__}")
except Exception as e:
    report(False, "PyGObject importable", e)
    print("\nInstall python3-gi (Debian) or "
          "mingw-w64-ucrt-x86_64-python-gobject (MSYS2).\n")
    sys.exit(1)

# 2. Where typelibs live, and which Spice ones exist
dirs = find_typelib_dirs()
report(bool(dirs), "girepository search path found", "\n".join(dirs) or "none")

spice_ns = discover_spice_namespaces(dirs)
report(
    bool(spice_ns),
    "Spice typelibs present on disk",
    "\n".join(f"{n}-{v}" for n, v in spice_ns) or
    "none -- install gir1.2-spiceclientgtk-3.0 / mingw-w64-*-spice-gtk",
)

# 3. GTK3
try:
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    report(True, "GTK 3 typelib loads",
           f"{Gtk.get_major_version()}.{Gtk.get_minor_version()}."
           f"{Gtk.get_micro_version()}")
except Exception as e:
    report(False, "GTK 3 typelib loads", e)

# 4. Import each discovered Spice namespace. Namespace spelling varies
#    between builds (SpiceClientGLib vs SpiceClientGlib), so probe rather
#    than hardcode.
mods = {}
for ns, ver in spice_ns:
    try:
        gi.require_version(ns, ver)
        mods[ns] = __import__("gi.repository", fromlist=[ns]).__dict__[ns]
        report(True, f"import {ns} {ver}")
    except Exception as e:
        report(False, f"import {ns} {ver}", e)

# 5. The two classes the whole project depends on
session_cls = None
display_cls = None
for ns, mod in mods.items():
    if session_cls is None and hasattr(mod, "Session"):
        session_cls = getattr(mod, "Session")
        report(True, f"{ns}.Session found")
    if display_cls is None and hasattr(mod, "Display"):
        display_cls = getattr(mod, "Display")
        report(True, f"{ns}.Display found (the embeddable widget)")

if session_cls is None:
    report(False, "SpiceSession class found",
           "no Spice namespace exposed a Session class")
if display_cls is None:
    report(False, "SpiceDisplay class found",
           "the GTK widget typelib is missing -- this is the blocker")

# 6. Actually instantiate a session and a display widget
if session_cls and display_cls:
    try:
        session = session_cls()
        report(True, "SpiceSession instantiates")
        try:
            display = display_cls.new(session, 0)
            is_widget = isinstance(display, Gtk.Widget)
            report(is_widget, "SpiceDisplay constructs as a GtkWidget",
                   f"type: {type(display).__name__}")
        except Exception as e:
            report(False, "SpiceDisplay constructs as a GtkWidget", e)
    except Exception as e:
        report(False, "SpiceSession instantiates", e)

# 7. Optional: prove it packs into a notebook and shows on screen
if "--window" in sys.argv and session_cls and display_cls:
    try:
        win = Gtk.Window(title="SPICE embedding smoke test")
        win.set_default_size(800, 500)
        win.connect("destroy", Gtk.main_quit)

        notebook = Gtk.Notebook()
        for i in range(2):
            sess = session_cls()
            disp = display_cls.new(sess, 0)
            frame = Gtk.Frame()
            frame.add(disp)
            notebook.append_page(frame, Gtk.Label(label=f"console {i + 1}"))

        win.add(notebook)
        win.show_all()
        report(True, "window opened -- close it to finish")
        Gtk.main()
    except Exception as e:
        report(False, "window smoke test", e)

ok = all(results)
print("\n" + ("ALL CHECKS PASSED - the stack is good to build on."
              if ok else
              "SOMETHING FAILED - see the [FAIL] lines above."))
sys.exit(0 if ok else 1)

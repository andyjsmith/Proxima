# PyInstaller spec for the macOS bundle.
#
# Why PyInstaller here and Nuitka on the other two platforms: PyInstaller
# ships runtime hooks for the whole GNOME stack (pyi_rth_gi, _gdkpixbuf,
# _gio, _glib, _gstreamer), so the paths proxima/bundle.py rewrites by hand
# for the Nuitka builds are already handled -- and, more to the point, it
# rewrites Mach-O load commands itself, which is the part that makes a
# Homebrew GTK non-relocatable (see tools/bundle_deps.py's docstring).
#
#     pyinstaller packaging/proxima.spec --noconfirm --distpath build/dist \
#         --workpath build/pyinstaller
#
# Run it with the same interpreter Homebrew built pygobject3 against; a
# separately installed PyGObject cannot find the typelibs, here as anywhere.

from pathlib import Path

SPEC_DIR = Path(SPECPATH)
ROOT = SPEC_DIR.parent


def gst_plugins():
    """The plugin list in packaging/gst-plugins.txt, as the hook wants it.

    One name per line, '#' comments; shared with the Nuitka builds so the
    three platforms carry the same set. Names missing on macOS cost nothing:
    include_plugins is matched against what is actually there.
    """
    names = []
    for line in (SPEC_DIR / "gst-plugins.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(line)
    return names


a = Analysis(
    [str(ROOT / "proxima.py")],
    pathex=[str(ROOT)],
    hookspath=[str(SPEC_DIR / "pyinstaller" / "hooks")],
    hooksconfig={
        "gi": {
            # Everything the toolbar and menus draw from. Without an icon
            # theme the buttons come up blank rather than failing loudly.
            "icons": ["Adwaita", "hicolor"],
            "themes": ["Adwaita"],
            "languages": ["en"],
        },
        "gstreamer": {"include_plugins": gst_plugins()},
    },
    datas=[
        # proxima/__init__.py reads the version straight out of it, so that
        # there is only ever one place the version is written.
        (str(ROOT / "pyproject.toml"), "."),
    ],
    hiddenimports=[
        # Imported through importlib by proxima/console/spicelib.py, so the
        # module graph never names them. Both spellings, for the same reason
        # spicelib.py probes for both.
        "gi.repository.SpiceClientGLib",
        "gi.repository.SpiceClientGtk",
    ],
    excludes=["tkinter", "test", "unittest", "pydoc_data"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="proxima",
    console=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="proxima",
)

app = BUNDLE(
    coll,
    name="Proxima.app",
    icon=str(SPEC_DIR / "proxima.icns"),
    bundle_identifier="org.ajsmith.proxima",
    info_plist={
        "CFBundleName": "Proxima",
        "CFBundleDisplayName": "Proxima",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        # A GTK window is not a document-less agent: without this the app
        # gets no menu bar and no Dock icon.
        "LSApplicationCategoryType": "public.app-category.utilities",
        "NSHighResolutionCapable": True,
    },
)

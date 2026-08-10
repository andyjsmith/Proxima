# PyInstaller spec for all three platforms.
#
# PyInstaller ships runtime hooks for the whole GNOME stack (pyi_rth_gi,
# _gdkpixbuf, _gio, _glib, _gstreamer, _gtk), so the paths a bundle has to be
# told about at run time are already handled, and its binary analysis follows
# the shared libraries the GStreamer plugins themselves pull in -- which is
# what a bundled codec needs to actually load. On macOS it also rewrites
# Mach-O load commands, which is the part that makes a Homebrew GTK
# relocatable at all.
#
#     pyinstaller packaging/proxima.spec --noconfirm \
#         --distpath build/dist --workpath build/pyinstaller
#
# That leaves build/dist/proxima/ on Linux and Windows, and
# build/dist/Proxima.app on macOS.
#
# Run it with the interpreter the system's PyGObject was built for -- the
# Homebrew python on macOS, python3 from apt on Linux, the MSYS2 UCRT64 python
# on Windows. A separately installed PyGObject cannot find the typelibs, here
# as anywhere.

import re
import sys
from pathlib import Path

SPEC_DIR = Path(SPECPATH)
ROOT = SPEC_DIR.parent

WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"

sys.path.insert(0, str(ROOT))


def version():
    """The version out of pyproject.toml, which is where it is written once.

    Read with the app's own parser, so that the number in the .app's plist
    and in the Windows resource cannot drift from the one the program
    reports about itself.
    """
    from proxima import _version_in

    return _version_in((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def gst_plugins():
    """The plugin list in packaging/gst-plugins.txt, as the hook wants it.

    One name per line, '#' comments. Names missing on a platform cost
    nothing: include_plugins is matched against what is actually there, which
    is how one list covers all three.
    """
    names = []
    for line in (SPEC_DIR / "gst-plugins.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(line)
    return names


def windows_version_info():
    """The VERSIONINFO resource Windows reads a program's identity out of.

    FileDescription is the one that shows: the taskbar's jump list, Task
    Manager's Name column, the UAC prompt. Without it all three fall back to
    "proxima.exe", and ProductName does not stand in for it.
    """
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    # A four-part number, with any pre-release suffix dropped: the resource
    # has nowhere to put "-rc1", while the strings below keep it in full.
    parts = [int(n) for n in re.findall(r"\d+", version().split("-")[0])][:4]
    numeric = tuple((parts + [0, 0, 0, 0])[:4])
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=numeric, prodvers=numeric),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "040904B0",  # US English, Unicode
                        [
                            StringStruct("CompanyName", "Proxima"),
                            StringStruct("FileDescription", "Proxima"),
                            StringStruct("FileVersion", version()),
                            StringStruct("InternalName", "proxima"),
                            StringStruct("OriginalFilename", "proxima.exe"),
                            StringStruct("ProductName", "Proxima"),
                            StringStruct("ProductVersion", version()),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )


datas = [
    # proxima/__init__.py reads the version straight out of it, so that there
    # is only ever one place the version is written.
    (str(ROOT / "pyproject.toml"), "."),
    # Where GTK's icon theme lookup can find the application icon; see
    # proxima/ui/appicon.py. macOS takes its icon from the .icns below.
    (str(SPEC_DIR / "proxima.png"), "share/icons/hicolor/256x256/apps"),
]

if WINDOWS:
    # PyInstaller looks for GTK's sysconfdir beside the GLib DLL -- bin/etc,
    # which is where gtkwin32.c says it is. MSYS2 keeps it one level up, in
    # ucrt64/etc, so PyInstaller's own etc collection quietly finds nothing
    # and fontconfig comes up with no configuration at all ("Fontconfig
    # error: Cannot load default config file"), which on Windows means no
    # font directories and no hinting -- and fontconfig is the backend
    # proxima/theme/fonts.py deliberately asks for. proxima/bundle.py points
    # fontconfig at what lands here; pyi_rth_gtk already points Pango at it.
    fonts = Path(sys.base_prefix) / "etc" / "fonts"
    if fonts.is_dir():
        datas.append((str(fonts), "etc/fonts"))

# The licences the bundle redistributes are not here: they belong beside the
# executable rather than under _internal with the rest of the data, and where
# they come from differs per platform -- MSYS2's share/licenses on Windows,
# dpkg's copyright files on Linux, NOTICE.md on macOS. The build workflow
# copies them in.

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
    datas=datas,
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
    icon=str(SPEC_DIR / "proxima.ico") if WINDOWS else None,
    version=windows_version_info() if WINDOWS else None,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    # Not stripped: the saving is real but strip has to be trusted with every
    # shared library in the bundle, and a wrongly stripped one fails at the
    # moment GTK dlopens it rather than here.
    strip=False,
    upx=False,
    name="proxima",
)

if MACOS:
    app = BUNDLE(
        coll,
        name="Proxima.app",
        icon=str(SPEC_DIR / "proxima.icns"),
        bundle_identifier="org.ajsmith.proxima",
        info_plist={
            "CFBundleName": "Proxima",
            "CFBundleDisplayName": "Proxima",
            "CFBundleShortVersionString": version(),
            "CFBundleVersion": version(),
            # A GTK window is not a document-less agent: without this the app
            # gets no menu bar and no Dock icon.
            "LSApplicationCategoryType": "public.app-category.utilities",
            "NSHighResolutionCapable": True,
        },
    )

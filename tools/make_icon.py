#!/usr/bin/env python3
"""Rebuild packaging/proxima.ico and packaging/proxima.icns from proxima.png.

Windows and macOS each want their icon carrying several sizes; the taskbar,
the title bar, Explorer and Finder/Dock all pick a different one. Run this
after changing the PNG.

    python3 tools/make_icon.py

The .icns half only runs on macOS -- it shells out to iconutil, which does
not exist anywhere else -- so run it there after changing the source image;
everyone else still gets a rebuilt .ico.
"""

import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "packaging" / "proxima.png"
ICO_TARGET = ROOT / "packaging" / "proxima.ico"
ICNS_TARGET = ROOT / "packaging" / "proxima.icns"

SIZES = (16, 24, 32, 48, 64, 128, 256)

# name in the .iconset -> pixel size. The source image is 256x256, so
# anything above that is a bilinear upscale rather than a real larger image
# -- soft, but iconutil wants the full set of slots and a slightly soft Dock
# icon beats one that is missing sizes entirely.
ICNS_SLOTS = {
    "icon_16x16.png": 16,
    "icon_16x16@2x.png": 32,
    "icon_32x32.png": 32,
    "icon_32x32@2x.png": 64,
    "icon_128x128.png": 128,
    "icon_128x128@2x.png": 256,
    "icon_256x256.png": 256,
    "icon_256x256@2x.png": 512,
    "icon_512x512.png": 512,
    "icon_512x512@2x.png": 1024,
}


def scaled_png(pixbuf, size):
    """One size, as PNG bytes. Vista and later read PNG inside an .ico."""
    scaled = pixbuf.scale_simple(size, size, GdkPixbuf.InterpType.BILINEAR)
    ok, data = scaled.save_to_bufferv("png", [], [])
    if not ok:
        raise RuntimeError(f"could not encode the {size}px image")
    return bytes(data)


def build_ico(images):
    """images: {size: png bytes} -> the bytes of an .ico file."""
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for size, png in sorted(images.items()):
        entries += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,  # 0 means 256
            size if size < 256 else 0,
            0,  # palette size: none, it is a PNG
            0,  # reserved
            1,  # colour planes
            32,  # bits per pixel
            len(png),
            offset + len(blobs),
        )
        blobs += png
    return header + entries + blobs


def build_icns(pixbuf):
    """The bytes of an .icns file, via a temporary .iconset and iconutil.

    There is no library here the way GdkPixbuf covers the .ico case: an
    .icns is iconutil's own binary format, and iconutil is the only thing
    that writes it. macOS ships it; nothing else does, so this is skipped
    everywhere else.
    """
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "proxima.iconset"
        iconset.mkdir()
        for name, size in ICNS_SLOTS.items():
            (iconset / name).write_bytes(scaled_png(pixbuf, size))
        out = Path(tmp) / "proxima.icns"
        subprocess.run(
            ["iconutil", "-c", "icns", "-o", str(out), str(iconset)],
            check=True,
        )
        return out.read_bytes()


def main():
    if not SOURCE.is_file():
        print(f"no {SOURCE}", file=sys.stderr)
        return 1
    pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(SOURCE))
    print(f"source: {SOURCE.name} {pixbuf.get_width()}x{pixbuf.get_height()}")

    ICO_TARGET.write_bytes(
        build_ico({size: scaled_png(pixbuf, size) for size in SIZES})
    )
    print(
        f"wrote {ICO_TARGET.name}: {', '.join(str(s) for s in SIZES)} "
        f"({ICO_TARGET.stat().st_size} bytes)"
    )

    if sys.platform == "darwin" and shutil.which("iconutil"):
        ICNS_TARGET.write_bytes(build_icns(pixbuf))
        print(f"wrote {ICNS_TARGET.name} ({ICNS_TARGET.stat().st_size} bytes)")
    else:
        print(f"skipped {ICNS_TARGET.name}: iconutil is macOS-only")

    return 0


if __name__ == "__main__":
    sys.exit(main())

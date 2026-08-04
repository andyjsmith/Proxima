#!/usr/bin/env python3
"""Rebuild packaging/proxima.ico from packaging/proxima.png.

Windows wants an .ico carrying several sizes; the taskbar, the title bar and
Explorer all pick a different one. Run this after changing the PNG.

    python3 tools/make_icon.py
"""

import struct
import sys
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "packaging" / "proxima.png"
TARGET = ROOT / "packaging" / "proxima.ico"

SIZES = (16, 24, 32, 48, 64, 128, 256)


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


def main():
    if not SOURCE.is_file():
        print(f"no {SOURCE}", file=sys.stderr)
        return 1
    pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(SOURCE))
    print(f"source: {SOURCE.name} {pixbuf.get_width()}x{pixbuf.get_height()}")
    TARGET.write_bytes(build_ico({size: scaled_png(pixbuf, size) for size in SIZES}))
    print(
        f"wrote {TARGET.name}: {', '.join(str(s) for s in SIZES)} ({TARGET.stat().st_size} bytes)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

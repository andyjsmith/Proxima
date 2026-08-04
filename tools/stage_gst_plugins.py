#!/usr/bin/env python3
"""Pick the GStreamer plugins named in packaging/gst-plugins.txt.

The build then includes the staged directory rather than the system's, which
is what keeps 200 MB of encoders out of a client that only decodes.

    python3 tools/stage_gst_plugins.py /usr/lib/x86_64-linux-gnu/gstreamer-1.0 \
        build/staging/gstreamer-1.0
"""

import argparse
import shutil
import sys
from pathlib import Path

LIST = Path(__file__).resolve().parent.parent / "packaging" / "gst-plugins.txt"


def wanted_plugins(path=LIST):
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(line)
    return names


def stage(source, destination, names):
    destination.mkdir(parents=True, exist_ok=True)
    found, missing = [], []
    for name in names:
        candidates = [
            source / f"libgst{name}.so",
            source / f"libgst{name}.dll",
            source / f"libgst{name}.dylib",
        ]
        for candidate in candidates:
            if candidate.exists():
                shutil.copy2(candidate, destination / candidate.name)
                found.append(name)
                break
        else:
            missing.append(name)

    # The plugin scanner is what GStreamer runs to index them, and it sits in
    # the plugin directory on MSYS2.
    for helper in ("gst-plugin-scanner", "gst-plugin-scanner.exe"):
        if (source / helper).exists():
            shutil.copy2(source / helper, destination / helper)
    return found, missing


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="the system's gstreamer-1.0 dir")
    parser.add_argument("destination", type=Path, help="where to stage them")
    args = parser.parse_args(argv)

    if not args.source.is_dir():
        parser.error(f"{args.source} is not a directory")

    names = wanted_plugins()
    found, missing = stage(args.source, args.destination, names)
    print(f"[gst] staged {len(found)} plugin(s): {' '.join(sorted(found))}")
    if missing:
        # Expected: the list covers both platforms at once.
        print(f"[gst] not on this platform: {' '.join(sorted(missing))}")
    if not found:
        print("[gst] nothing was staged", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

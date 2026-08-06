#!/usr/bin/env python3
"""Copy the libraries the bundled plugins need, which Nuitka never sees.

Nuitka follows what the program links against. It does not follow what GTK
loads later by hand: the pixbuf loaders, the GIO modules, and above all the
GStreamer plugins, which drag in whole codec stacks of their own. Those get
copied into the build as raw directories, dependencies and all missing, and
the result runs until the first SPICE video frame.

So after the build, every bundled plugin is asked what it needs, and anything
that resolves inside the toolchain prefix -- rather than to a system library
every machine already has -- is copied in beside the executable.

    python3 tools/bundle_deps.py build/proxima.dist --prefix /usr

Typelibs are the other half of the same problem, and a nastier one. A
typelib is data, not code: SpiceClientGLib-2.0.typelib merely *names*
libspice-client-glib-2.0-8.dll, and nothing in the program links against it,
so Nuitka has no reason to copy it. What makes this worth a comment is how
it fails. The import still works -- `from gi.repository import
SpiceClientGLib` succeeds against the typelib alone -- so every "is SPICE
available?" check says yes. Only when something asks for an actual GType
does g_typelib_symbol() come up empty, every type resolves to void, and
constructing one raises

    TypeError: could not get a reference to type class

from somewhere with no obvious connection to a missing file. So the
libraries the bundled typelibs name are treated as roots here too.

On Linux the plugins are also given an RPATH back to the top of the bundle,
because the loader does not look next to the executable for a library that a
plugin two directories down asked for.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Where plugins live inside the bundle, relative to its root.
PLUGIN_DIRS = (
    "lib/gstreamer-1.0",
    "lib/gio/modules",
    "lib/gdk-pixbuf-2.0",
    "libexec/gstreamer-1.0",
)

# Left to the host on purpose: the core C library and the graphics/X11 stack
# are not safely relocatable, and every machine that can run a GTK program
# already has them. Shipping our own glibc is how a bundle stops starting on
# a newer kernel than it was built on.
LINUX_SYSTEM_PREFIXES = (
    "libc.so",
    "libm.so",
    "libdl.so",
    "libpthread.so",
    "librt.so",
    "libresolv.so",
    "ld-linux",
    "libGL",
    "libEGL",
    "libGLX",
    "libGLdispatch",
    "libX11",
    "libxcb",
    "libXext",
    "libXrender",
    "libXi",
    "libXfixes",
    "libXdamage",
    "libXcomposite",
    "libXrandr",
    "libXcursor",
    "libXinerama",
    "libwayland",
    "libdrm",
    "libgbm",
)

WINDOWS_SYSTEM_PREFIXES = (
    "api-ms-win",
    "ext-ms-win",
    "kernel32",
    "user32",
    "gdi32",
    "advapi32",
    "shell32",
    "ole32",
    "oleaut32",
    "ws2_32",
    "msvcrt",
    "ucrtbase",
    "ntdll",
    "combase",
    "crypt32",
    "d3d",
    "dxgi",
    "opengl32",
    "winmm",
    "imm32",
    "setupapi",
    "secur32",
    "bcrypt",
    "iphlpapi",
    "dnsapi",
    "mswsock",
    "shlwapi",
    "comdlg32",
    "comctl32",
    "dwmapi",
    "usp10",
    "version",
    "winspool",
    "wtsapi32",
    "hid.dll",
    "cfgmgr32",
    "powrprof",
    "avrt",
    "mfplat",
    "dbghelp",
)


def is_system_library(name, windows):
    lowered = name.lower()
    prefixes = WINDOWS_SYSTEM_PREFIXES if windows else LINUX_SYSTEM_PREFIXES
    return any(lowered.startswith(prefix) for prefix in prefixes)


def plugin_files(root, windows):
    suffix = ".dll" if windows else ".so"
    for relative in PLUGIN_DIRS:
        directory = root / relative
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if path.is_file() and (
                path.suffix.lower() == suffix or suffix in path.name.lower()
            ):
                yield path


def typelib_library_names(path, windows):
    """The shared libraries a .typelib names, by reading its string pool.

    The field is properly reachable through GIRepository, but asking for it
    means requiring the namespace -- loading the very library that may not
    be there. The names sit in the typelib as plain nul-terminated strings,
    so they are read directly instead. A false positive costs nothing: only
    names that resolve to a real file in the prefix are ever copied.
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        print(f"  ! could not read {path.name}: {exc}")
        return []
    names = []
    for found in re.findall(rb"[ -~]{4,}", data):
        # A typelib may name several, comma separated.
        for candidate in found.decode("ascii", "replace").split(","):
            if looks_like_library(candidate.strip(), windows):
                names.append(candidate.strip())
    return names


def looks_like_library(name, windows):
    """Whether a string out of a typelib could be a library filename."""
    if not name or "/" in name or "\\" in name:
        return False
    if windows:
        return name.lower().endswith(".dll")
    return ".so" in name


def resolve_in_prefix(name, prefix, windows):
    """Where a library named by a typelib actually lives, or None."""
    if windows:
        candidate = prefix / "bin" / name
        return candidate if candidate.exists() else None
    for libdir in ("lib", "lib64", "lib/x86_64-linux-gnu", "usr/lib"):
        candidate = prefix / libdir / name
        if candidate.exists():
            return candidate
    # Multiarch layouts vary more than is worth enumerating.
    for candidate in prefix.glob(f"lib*/**/{name}"):
        return candidate
    return None


def typelib_libraries(root, prefix, windows):
    """Every library the bundle's typelibs name, resolved in the prefix."""
    found = {}
    for typelib in root.rglob("*.typelib"):
        for name in typelib_library_names(typelib, windows):
            if name in found or is_system_library(name, windows):
                continue
            resolved = resolve_in_prefix(name, prefix, windows)
            if resolved is not None:
                found[name] = resolved
    return found


def dependencies_windows(path, prefix):
    """Ask objdump what a DLL imports, and resolve it inside the prefix."""
    try:
        out = subprocess.run(
            ["objdump", "-p", str(path)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"  ! could not read {path.name}: {exc}")
        return []
    found = []
    for match in re.finditer(r"DLL Name:\s*(\S+)", out):
        name = match.group(1)
        if is_system_library(name, windows=True):
            continue
        candidate = prefix / "bin" / name
        if candidate.exists():
            found.append(candidate)
    return found


def dependencies_linux(path, prefix):
    """ldd resolves the whole chain; keep what lives under the prefix."""
    try:
        out = subprocess.run(
            ["ldd", str(path)], capture_output=True, text=True, check=False
        ).stdout
    except OSError as exc:
        print(f"  ! could not read {path.name}: {exc}")
        return []
    found = []
    for line in out.splitlines():
        match = re.search(r"=>\s*(/\S+)", line)
        if not match:
            continue
        resolved = Path(match.group(1))
        if is_system_library(resolved.name, windows=False):
            continue
        try:
            resolved.relative_to(prefix)
        except ValueError:
            continue
        found.append(resolved)
    return found


def set_rpath(path, root):
    """Point a plugin back at the top of the bundle.

    A plugin in lib/gstreamer-1.0 is loaded by an executable one or more
    directories up; without this the loader has no reason to look there for
    the codec libraries sitting next to it.
    """
    depth = len(path.parent.relative_to(root).parts)
    up = "/".join([".."] * depth) if depth else "."
    origin = f"$ORIGIN:$ORIGIN/{up}"
    try:
        subprocess.run(
            ["patchelf", "--set-rpath", origin, str(path)],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"  ! could not set rpath on {path.name}: {exc}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", type=Path, help="the .dist directory to fix up")
    parser.add_argument(
        "--prefix",
        type=Path,
        required=True,
        help="toolchain prefix the plugins came from (/usr, or the UCRT64 root)",
    )
    parser.add_argument(
        "--no-rpath",
        action="store_true",
        help="skip patchelf, for a bundle that will be run from one place",
    )
    args = parser.parse_args(argv)

    root = args.dist.resolve()
    if not root.is_dir():
        parser.error(f"{root} is not a directory")
    prefix = args.prefix.resolve()
    windows = os.name == "nt"

    present = {path.name.lower() for path in root.iterdir() if path.is_file()}
    plugins = sorted(plugin_files(root, windows))
    print(f"[deps] {len(plugins)} bundled plugin(s) under {root}")

    # Copying a library can introduce dependencies of its own, so the queue
    # is worked until nothing new turns up.
    queue = list(plugins)
    seen = set()
    copied = []

    # The libraries behind the typelibs go in first, as roots: nothing links
    # to them, so nothing else would ever bring them in. See the module
    # docstring for what their absence looks like.
    wanted = typelib_libraries(root, prefix, windows)
    print(f"[deps] {len(wanted)} librar(y|ies) named by bundled typelibs")
    for name, source in sorted(wanted.items()):
        destination = root / source.name
        if source.name.lower() not in present:
            shutil.copy2(source, destination)
            present.add(source.name.lower())
            copied.append(source.name)
            print(f"       + {name}")
        queue.append(destination)
    while queue:
        path = queue.pop()
        if path in seen:
            continue
        seen.add(path)
        finder = dependencies_windows if windows else dependencies_linux
        for dependency in finder(path, prefix):
            if dependency.name.lower() in present:
                continue
            destination = root / dependency.name
            shutil.copy2(dependency, destination)
            present.add(dependency.name.lower())
            copied.append(dependency.name)
            queue.append(destination)

    if not windows and not args.no_rpath:
        for path in plugins:
            set_rpath(path, root)

    print(f"[deps] copied {len(copied)} missing librar(y|ies)")
    for name in sorted(copied):
        print(f"       {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

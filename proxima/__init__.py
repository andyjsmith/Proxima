"""Proxima -- a VMware Workstation style client for Proxmox VE."""

import re
import sys
from pathlib import Path

APP_NAME = "Proxima"
APP_ID = "org.proxima.Client"

_VERSION_LINE = re.compile(r"""version\s*=\s*["']([^"']+)["']""")


def _version_in(text):
    """The `version` of the [project] table, without a TOML parser.

    tomllib is 3.11 and newer, and the Linux bundle is compiled on the oldest
    distribution it is meant to run on, whose python is older than that -- so
    the one value read here is found by hand rather than by importing a
    parser that is not always there. Only the [project] table is looked at,
    so a version under [tool.something] cannot be picked up by mistake.
    """
    section = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
        elif section == "project":
            found = _VERSION_LINE.match(line)
            if found:
                return found.group(1)
    return None


def _read_version():
    """The version, from wherever this copy of Proxima happens to be.

    pyproject.toml is the only place the version is written, so that
    `uv version` is all a release takes. Finding it again at run time costs
    one small file read and saves a second copy that can drift.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("proxima")  # pip- or uv-installed
        except PackageNotFoundError:
            pass
    except ImportError:  # pragma: no cover -- importlib.metadata is stdlib
        pass

    here = Path(__file__).resolve().parent
    for candidate in (
        here.parent / "pyproject.toml",  # a source checkout
        Path(sys.executable).resolve().parent / "pyproject.toml",  # a bundle
    ):
        try:
            found = _version_in(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if found:
            return found
    return "0.0.0+unknown"


__version__ = _read_version()

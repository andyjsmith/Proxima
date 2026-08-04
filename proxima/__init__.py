"""Proxima -- a VMware Workstation style client for Proxmox VE."""

import sys
import tomllib
from pathlib import Path

APP_NAME = "Proxima"
APP_ID = "org.proxima.Client"


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
            with candidate.open("rb") as handle:
                return tomllib.load(handle)["project"]["version"]
        except (OSError, KeyError, tomllib.TOMLDecodeError):
            continue
    return "0.0.0+unknown"


__version__ = _read_version()

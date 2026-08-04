#!/usr/bin/env python3
"""Run the UI test suite.

The checks this used to carry directly now live in tests/, driven by pytest:
the fakes and the main-loop pump are in tests/conftest.py. This stays as the
documented entry point, and to keep the suite runnable with nothing but the
MSYS2 UCRT64 Python.

    python3 tools/smoke_test.py            everything
    python3 tools/smoke_test.py -k console just the console tests
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    try:
        import pytest  # noqa: F401
    except ImportError:
        print(
            "pytest is not installed for this interpreter.\n"
            "  MSYS2:  pacman -S mingw-w64-ucrt-x86_64-python-pytest\n"
            "  Debian: apt install python3-pytest",
            file=sys.stderr,
        )
        return 2
    return subprocess.call(
        [sys.executable, "-m", "pytest", *sys.argv[1:]],
        cwd=ROOT,
    )


if __name__ == "__main__":
    sys.exit(main())

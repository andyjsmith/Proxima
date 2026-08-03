#!/usr/bin/env python3
"""Launcher for Proxima.

    python3 proxima.py                 normal start
    python3 proxima.py --diagnose      report the theme/SPICE stack
    python3 proxima.py --fontconfig    force the FreeType font backend

On Windows this must run under the MSYS2 UCRT64 Python
(C:/msys64/ucrt64/bin/python.exe), not a python.org install -- PyGObject and
spice-gtk come from pacman and will not load anywhere else.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from proxima.__main__ import main

if __name__ == "__main__":
    sys.exit(main() or 0)

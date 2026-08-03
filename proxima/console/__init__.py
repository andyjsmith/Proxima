from .spice import AVAILABLE as SPICE_AVAILABLE
from .spice import SpiceConsole
from .vnc import AVAILABLE as VNC_AVAILABLE
from .vnc import VncConsole

__all__ = ["SPICE_AVAILABLE", "VNC_AVAILABLE", "SpiceConsole", "VncConsole"]

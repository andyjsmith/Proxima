from .serial import AVAILABLE as SERIAL_AVAILABLE
from .serial import SerialConsole
from .spice import AVAILABLE as SPICE_AVAILABLE
from .spice import SpiceConsole
from .vnc import AVAILABLE as VNC_AVAILABLE
from .vnc import VncConsole

__all__ = [
    "SERIAL_AVAILABLE",
    "SPICE_AVAILABLE",
    "VNC_AVAILABLE",
    "SerialConsole",
    "SpiceConsole",
    "VncConsole",
]

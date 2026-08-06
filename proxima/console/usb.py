"""USB redirection over SPICE.

spice-gtk does the actual work -- it speaks usbredir and owns the host side
of the transfer -- so this is a thin, UI-shaped wrapper over
SpiceUsbDeviceManager. Four things about it are worth stating because none
of them are guessable from the API:

  * The guest needs a redirection port. Proxmox does not add one: the VM
    config has to carry a 'usbN: spice' line (Hardware -> Add -> USB Device
    -> Spice Port in the Proxmox UI). Without one the manager still lists
    every device on the host and still refuses to redirect any of them,
    which reads as a client bug unless you know to look at 'free-channels'.
  * On Windows, capturing a device needs the UsbDk driver. Enumeration
    works without it -- the list looks perfectly healthy -- and the failure
    only arrives when a device is actually claimed.
  * The device list is built from the manager's own 'device-added' and
    'device-removed' signals rather than from get_devices(). The devices
    that call hands out are not safely owned: releasing one drops the last
    reference to a device the manager is still holding, and the next
    enumeration walks into freed memory. Signal parameters are properly
    copied, so those are safe to keep and safe to let go. The one
    get_devices() call is the initial seed, and what it returns is kept
    alive for the life of the process rather than released. See _SEEDED.
  * spice_usb_device_get_description() is a printf() call with a fixed
    argument list. A format string that does not match it exactly is an
    access violation rather than an error, so none is ever passed.

auto-connect is deliberately left off. It is spice-gtk's own version of the
hotplug prompt -- grab anything that appears, silently -- and the point of
the prompt is that taking a keyboard away from the host without asking is
not a favour.
"""

import contextlib
import logging
import os

from gi.repository import GLib

from .spicelib import SpiceGLib, connect_signal

# The manager is only present when spice-gtk was built with usbredir.
AVAILABLE = SpiceGLib is not None and hasattr(SpiceGLib, "UsbDeviceManager")

# spice-gtk announces the devices that were already plugged in when the
# session was made, one 'device-added' at a time and not reliably before the
# manager is handed over. Those are not somebody reaching for a USB port, so
# the prompt stays quiet until the opening flurry is over.
SETTLE_MS = 2000

# Written by the UsbDk installer. Checked rather than waited for, because
# without it redirection fails at the moment of claiming the device, which
# is far too late to be a useful place to explain the requirement.
USBDK_DRIVER = "System32/drivers/UsbDk.sys"

# Where to get it. The installer offers to put it on for you, so this is the
# path for a zip install, or for somebody who said no the first time.
USBDK_RELEASES = "https://github.com/daynix/UsbDk/releases/latest"

USBDK_ADVICE = (
    "the UsbDk driver is not installed, so Windows will not hand a device "
    "over. Install UsbDk to redirect USB devices."
)

log = logging.getLogger(__name__)

# Devices from the seeding enumeration, deliberately never released: see the
# module docstring. A handful of small objects per console, once.
_SEEDED = []


def usbdk_installed():
    """Whether the Windows host driver is there. None off Windows."""
    if os.name != "nt":
        return None
    root = os.environ.get("SYSTEMROOT") or "C:/Windows"
    return os.path.exists(os.path.join(root, *USBDK_DRIVER.split("/")))


class UsbDevice:
    """One host device, as far as the UI is concerned.

    'key' is what to pass back to act on it; 'reason' explains a False
    'redirectable' in the words spice-gtk used.
    """

    __slots__ = ("key", "label", "connected", "redirectable", "reason")

    def __init__(self, key, label, connected, redirectable, reason=""):
        self.key = key
        self.label = label
        self.connected = connected
        self.redirectable = redirectable
        self.reason = reason

    def __repr__(self):
        state = "redirected" if self.connected else "on host"
        return f"<UsbDevice {self.label!r} {state}>"


class UsbRedirection:
    """The USB devices of one SPICE session, and what can be done with them.

    Every callback lands on the GTK main thread: spice-glib emits its
    signals there, and the async replies come back through the same context.
    """

    def __init__(self, session, on_changed=None, on_plugged=None, on_error=None):
        self.on_changed = on_changed or (lambda: None)
        # (key, label) for a device that has just appeared on the host.
        self.on_plugged = on_plugged or (lambda key, label: None)
        self.on_error = on_error or (lambda message: None)

        self.manager = None
        self.note = ""  # why there is no USB redirection here at all
        self.advice = ""  # why it is there but will not work
        self._handlers = []
        self._busy = set()
        self._known = {}  # key -> SpiceUsbDevice, maintained by the signals
        self._settled = False
        self._settle_source = None

        if not AVAILABLE:
            self.note = "this spice-gtk build has no USB redirection support"
            return

        try:
            self.manager = SpiceGLib.UsbDeviceManager.get(session)
        except Exception as exc:
            self.note = f"USB redirection unavailable: {exc}"
            log.info("no device manager: %s", exc)
            return

        try:
            # We ask before taking a device; see the module docstring.
            self.manager.set_property("auto-connect", False)
        except Exception as exc:
            log.warning("could not turn auto-connect off: %s", exc)

        for name, handler in (
            ("device-added", self._on_device_added),
            ("device-removed", self._on_device_removed),
            ("device-error", self._on_device_error),
        ):
            try:
                self._handlers.append(connect_signal(self.manager, name, handler))
            except Exception as exc:
                log.info("no '%s' signal: %s", name, exc)

        self._seed()
        self._arm_settle()

        if usbdk_installed() is False:
            self.advice = USBDK_ADVICE

    def _seed(self):
        """Take the devices already plugged in when the session was made.

        The manager also announces them, and normally does so before anyone
        can ask -- but "normally" is not something to hang an empty device
        list on. Whatever this call hands back is kept for good; see the
        module docstring for why it must not be released.
        """
        try:
            found = self.manager.get_devices() or []
        except Exception as exc:
            log.warning("could not seed the device list: %s", exc)
            return
        for device in found:
            _SEEDED.append(device)
            key = self._describe(device)
            if key is not None:
                self._known.setdefault(key, device)

    # -- state ---------------------------------------------------------

    @property
    def supported(self):
        return self.manager is not None

    def snapshot(self):
        """(devices, channels), from one reading of the registry.

        The status bar asks every second, and asking twice for two halves of
        the same answer can also disagree with itself in between.

        'channels' is how many redirection ports the guest offers in total:
        free-channels counts only the unused ones, so a guest with one port
        and something plugged into it reports zero -- which would otherwise
        read as "this guest cannot do USB at all" the moment it starts
        working.
        """
        devices = self.devices()
        if self.manager is None:
            return devices, 0
        try:
            free = int(self.manager.get_property("free-channels"))
        except Exception:
            return devices, 0
        return devices, free + sum(1 for device in devices if device.connected)

    @property
    def channels(self):
        return self.snapshot()[1]

    def devices(self):
        """Every USB device on the host, with its current state."""
        if self.manager is None:
            return []
        devices = []
        for key, device in sorted(self._known.items()):
            connected = self._is_connected(device)
            redirectable, reason = self._can_redirect(device)
            devices.append(
                UsbDevice(key, key, connected, connected or redirectable, reason)
            )
        return devices

    def redirected(self):
        return [device for device in self.devices() if device.connected]

    @staticmethod
    def _describe(device):
        """The device's own name, which doubles as its key.

        No format string is ever passed. spice_usb_device_get_description()
        hands whatever it is given straight to g_strdup_printf() with a
        fixed argument list, so a format that does not match exactly -- an
        int read as a char* -- is not an error but an access violation. Its
        own default carries the bus and address, which is what makes the
        result unique enough to key on.
        """
        try:
            text = device.get_description(None)
        except Exception as exc:
            log.warning("could not describe a device: %s", exc)
            return None
        return " ".join(text.split()) if text else None

    def _is_connected(self, device):
        try:
            return bool(self.manager.is_device_connected(device))
        except Exception:
            return False

    def _can_redirect(self, device):
        """(allowed, reason). spice-gtk reports the refusal as a GError."""
        try:
            return bool(self.manager.can_redirect_device(device)), ""
        except GLib.Error as exc:
            return False, exc.message
        except Exception as exc:
            return False, str(exc)

    def _lookup(self, key):
        """A live device object for a key, or None if it has been unplugged."""
        if self.manager is None:
            return None
        return self._known.get(key)

    # -- actions -------------------------------------------------------

    def connect_device(self, key, on_done=None):
        """Hand a host device to the guest. Reports through on_done(ok, msg)."""
        done = on_done or (lambda ok, message: None)
        device = self._lookup(key)
        if device is None:
            done(False, "the device is no longer plugged in")
            return

        allowed, reason = self._can_redirect(device)
        if not allowed:
            done(False, reason or "this device cannot be redirected")
            return

        def finished(manager, result, _data=None):
            self._busy.discard(key)
            try:
                manager.connect_device_finish(result)
            except GLib.Error as exc:
                done(False, exc.message)
            except Exception as exc:
                done(False, str(exc))
            else:
                done(True, "")
            self.on_changed()

        self._busy.add(key)
        self.on_changed()
        try:
            self.manager.connect_device_async(device, None, finished, None)
        except Exception as exc:
            self._busy.discard(key)
            done(False, str(exc))
            self.on_changed()

    def disconnect_device(self, key, on_done=None):
        """Give a device back to the host."""
        done = on_done or (lambda ok, message: None)
        device = self._lookup(key)
        if device is None:
            # Unplugged while redirected: spice-gtk has already let go.
            done(True, "")
            self.on_changed()
            return

        if hasattr(self.manager, "disconnect_device_finish"):

            def finished(manager, result, _data=None):
                self._busy.discard(key)
                try:
                    manager.disconnect_device_finish(result)
                except GLib.Error as exc:
                    done(False, exc.message)
                except Exception as exc:
                    done(False, str(exc))
                else:
                    done(True, "")
                self.on_changed()

            self._busy.add(key)
            self.on_changed()
            try:
                self.manager.disconnect_device_async(device, None, finished, None)
                return
            except Exception as exc:
                self._busy.discard(key)
                log.warning("async disconnect failed, falling back: %s", exc)

        try:
            self.manager.disconnect_device(device)
            done(True, "")
        except Exception as exc:
            done(False, str(exc))
        self.on_changed()

    def toggle(self, key, on_done=None):
        device = self._lookup(key)
        if device is not None and self._is_connected(device):
            self.disconnect_device(key, on_done)
        else:
            self.connect_device(key, on_done)

    def is_busy(self, key):
        """Whether a connect or disconnect for this device is still in flight."""
        return key in self._busy

    def disconnect_all(self):
        """Return every redirected device to the host."""
        for device in self.redirected():
            self.disconnect_device(device.key)

    # -- signals -------------------------------------------------------

    def _arm_settle(self):
        """(Re)start the quiet period before a plug is treated as a plug.

        Re-armed by each arrival while the opening burst is still coming in,
        so a slow enumeration does not turn into a queue of questions about
        devices that were already there.
        """
        if self._settle_source is not None:
            with contextlib.suppress(Exception):
                GLib.source_remove(self._settle_source)
        self._settle_source = GLib.timeout_add(SETTLE_MS, self._settle)

    def _settle(self):
        self._settle_source = None
        self._settled = True
        return False

    def _on_device_added(self, _manager, device):
        key = self._describe(device)
        if key is not None:
            self._known[key] = device
        self.on_changed()
        if not self._settled:
            self._arm_settle()
            return
        if key is None:
            return
        allowed, _reason = self._can_redirect(device)
        if allowed:
            self.on_plugged(key, key)

    def _on_device_removed(self, _manager, device):
        key = self._describe(device)
        if key is not None:
            self._known.pop(key, None)
        self.on_changed()

    def _on_device_error(self, _manager, device, error):
        key = self._describe(device) or "USB device"
        self._busy.discard(key)
        message = getattr(error, "message", None) or str(error)
        self.on_error(f"{key}: {message}")
        self.on_changed()

    # -- lifecycle -----------------------------------------------------

    def shutdown(self):
        """Drop the signals and give the guest's devices back to the host.

        spice-gtk would release them when the session dies anyway, but not
        promptly, and a keyboard that stays dead for a few seconds after a
        console is closed looks like the host has lost it for good.
        """
        if self._settle_source is not None:
            with contextlib.suppress(Exception):
                GLib.source_remove(self._settle_source)
            self._settle_source = None

        manager, self.manager = self.manager, None
        if manager is None:
            return
        for handler in self._handlers:
            with contextlib.suppress(Exception):
                manager.handler_disconnect(handler)
        self._handlers = []
        self.manager = manager
        try:
            self.disconnect_all()
        finally:
            self.manager = None

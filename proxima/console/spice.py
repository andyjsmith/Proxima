"""Embedded SPICE console driven straight from the Proxmox API.

The parameter dict comes from POST /nodes/<node>/qemu/<vmid>/spiceproxy,
which is the same payload Proxmox would otherwise hand you as a .vv file.
Its quirks are worth restating because none of them are guessable:

  * 'host' is NOT a hostname. Proxmox puts an opaque proxy ticket there
    (pvespiceproxy:<hash>:<vmid>:<node>:<port>::<token>) and the real network
    target is 'proxy'. spice-glib passes host through to the proxy verbatim.
  * 'host-subject' means certificate validation should check the subject, not
    the hostname -- the hostname is that ticket string and will not match.
  * The ticket in 'password' is short lived, so connect promptly.
"""

import contextlib
import os
import tempfile
import time

import gi

gi.require_version("Gtk", "3.0")

from gi.repository import Gdk, GLib, GObject, Gtk

from .decoders import gstreamer_report
from .status_panel import (
    CONNECTING_ICON,
    CONNECTING_TITLE,
    ConsoleStatusPanel,
    draw_offline_effect,
)

# --------------------------------------------------------------------------
# Namespace resolution
#
# The introspection namespace is spelled SpiceClientGLib in most builds but
# SpiceClientGlib in some, so probe rather than assume.
# --------------------------------------------------------------------------


def _import_namespace(candidates):
    for name, version in candidates:
        try:
            gi.require_version(name, version)
            module = __import__("gi.repository", fromlist=[name])
            return getattr(module, name), name
        except Exception:
            continue
    return None, None


SpiceGLib, SPICE_GLIB_NS = _import_namespace(
    [("SpiceClientGLib", "2.0"), ("SpiceClientGlib", "2.0")]
)
SpiceGtk, SPICE_GTK_NS = _import_namespace([("SpiceClientGtk", "3.0")])

AVAILABLE = SpiceGLib is not None and SpiceGtk is not None


# --------------------------------------------------------------------------
# Session method shadowing
#
# spice_session_connect/disconnect collide with GObject.Object's connect and
# disconnect. Which one wins depends on the PyGObject build, so call both
# explicitly and defensively.
# --------------------------------------------------------------------------


def connect_signal(obj, name, handler):
    """Always attach a GObject signal, never the shadowing Spice method."""
    return GObject.Object.connect(obj, name, handler)


def session_connect(session):
    try:
        result = SpiceGLib.Session.connect(session)
        if isinstance(result, bool):
            return result
        return True
    except TypeError:
        pass
    try:
        return bool(session.connect())
    except TypeError as exc:
        raise RuntimeError(f"could not call spice_session_connect: {exc}") from exc


def session_disconnect(session):
    for attempt in (
        lambda: SpiceGLib.Session.disconnect(session),
        lambda: session.disconnect(),
    ):
        try:
            attempt()
            return True
        except TypeError:
            continue
        except Exception:
            return False
    return False


def _enum(namespace_attr, member, fallback):
    """Look up a Spice enum member, falling back to its wire value."""
    try:
        return getattr(getattr(SpiceGLib, namespace_attr), member)
    except AttributeError:
        return fallback


# Wire values from spice-protocol, used if the enums are not introspectable.
VIDEO_CODECS = [
    ("server default", None),
    ("MJPEG", ("VideoCodecType", "MJPEG", 1)),
    ("VP8", ("VideoCodecType", "VP8", 2)),
    ("H.264", ("VideoCodecType", "H264", 3)),
    ("VP9", ("VideoCodecType", "VP9", 4)),
]

IMAGE_COMPRESSION = [
    ("server default", None),
    ("off (lossless)", ("ImageCompression", "OFF", 1)),
    ("auto GLZ", ("ImageCompression", "AUTO_GLZ", 2)),
    ("QUIC", ("ImageCompression", "QUIC", 4)),
    ("LZ4", ("ImageCompression", "LZ4", 7)),
]


def find_spice_function(base):
    """Locate a spice-gtk function under any of the names introspection uses.

    When the C function's first argument type does not match the class it is
    named after, introspection keeps the full name rather than shortening it.
    So spice_display_channel_change_preferred_video_codec_type() appears as
    'display_channel_change_preferred_video_codec_type', not as
    'change_preferred_video_codec_type'.
    """
    names = [
        base,
        f"display_channel_{base}",
        f"display_{base}",
        f"main_channel_{base}",
    ]

    holders = [
        ("DisplayChannel", getattr(SpiceGLib, "DisplayChannel", None)),
        ("MainChannel", getattr(SpiceGLib, "MainChannel", None)),
        ("Channel", getattr(SpiceGLib, "Channel", None)),
        (SPICE_GLIB_NS or "SpiceGLib", SpiceGLib),
    ]
    if SpiceGtk is not None:
        holders.append(("SpiceGtk.Display", getattr(SpiceGtk, "Display", None)))

    found = []
    seen = set()
    for holder_name, holder in holders:
        if holder is None:
            continue
        for name in names:
            func = getattr(holder, name, None)
            if callable(func) and (holder_name, name) not in seen:
                seen.add((holder_name, name))
                found.append((f"{holder_name}.{name}", func))
    return found


class DisplayHolder(Gtk.Bin):
    """Container that does not propagate its child's natural size.

    SpiceDisplay reports the guest resolution as its natural size. Packed
    directly into a box that creates a feedback loop: the guest resizes, the
    widget's natural size grows, the layout grows, the widget asks the guest
    to grow again, and so on until the guest runs out of video memory and the
    negotiation collapses back to a minimum. virt-viewer avoids this with its
    own container; this is the same trick.
    """

    __gtype_name__ = "SpiceDisplayHolder"

    MIN_WIDTH = 320
    MIN_HEIGHT = 240

    def do_get_preferred_width(self):
        return (self.MIN_WIDTH, self.MIN_WIDTH)

    def do_get_preferred_height(self):
        return (self.MIN_HEIGHT, self.MIN_HEIGHT)

    def do_get_preferred_width_for_height(self, _height):
        return self.do_get_preferred_width()

    def do_get_preferred_height_for_width(self, _width):
        return self.do_get_preferred_height()

    def do_size_allocate(self, allocation):
        self.set_allocation(allocation)
        child = self.get_child()
        if child is not None and child.get_visible():
            child.size_allocate(allocation)


# X11 keysyms. spice_display_send_keys() wants these as integers.
KEY_CONTROL_L = 0xFFE3
KEY_ALT_L = 0xFFE9
KEY_DELETE = 0xFFFF
KEY_CTRL_ALT_DEL = (KEY_CONTROL_L, KEY_ALT_L, KEY_DELETE)

CHANNEL_EVENTS = {
    0: "closed",
    1: "error: connect",
    2: "error: TLS",
    3: "error: link",
    4: "error: authentication",
    5: "error: I/O",
    6: "opened",
    7: "switching",
    8: "migration",
}

_REPORTED_GSTREAMER = False


class SpiceConsole(Gtk.Box):
    """A single SPICE connection with its display widget and controls."""

    protocol = "spice"
    pending = False

    # Which of the view-menu controls apply to this console type.
    supports = {
        "auto_resize": True,
        "scaling": True,
        "codec": True,
        "compression": True,
        "refresh": False,
        "ctrl_alt_del": True,
        "clipboard": True,
        "audio": True,
    }

    def __init__(
        self,
        params,
        title="console",
        on_status=None,
        enable_audio=True,
        auto_resize=True,
        scale_to_fit=False,
        on_agent=None,
        on_disconnect=None,
        on_reconnect=None,
        share_clipboard=True,
        play_audio=True,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        self.title = title
        self.on_status = on_status or (lambda text: None)
        self.on_agent = on_agent or (lambda connected: None)
        self.on_disconnect = on_disconnect or (lambda reason: None)
        self.on_reconnect = on_reconnect or (lambda: None)
        self.connected = False
        self.agent_connected = False
        self._channels = []
        self._last_bytes = None
        self._last_sample = None
        self.params = dict(params)
        # Per-guest switches from the status bar. Clipboard is a live
        # property of the GTK session. Audio is not: it is decided when the
        # session is built, because "enable-audio" governs whether the
        # playback channel is created at all. Muting the channel instead
        # only asks spice-gtk's audio backend nicely and does not reliably
        # silence it, so the honest implementation reconnects.
        self.share_clipboard = share_clipboard
        self.play_audio = play_audio
        self.enable_audio = bool(enable_audio) and bool(play_audio)
        self._gtk_session = None
        self.auto_resize = auto_resize
        self.scaling = scale_to_fit
        self.codec_index = 0
        self.compression_index = 0
        self._ca_file = None
        self._display = None
        self._holder = None
        self._display_channel = None
        self._main_channel = None
        self._closed = False
        self.last_status = ""

        if not AVAILABLE:
            self.session = None
            label = Gtk.Label()
            label.set_markup(
                "<b>spice-gtk is not installed.</b>\n\n"
                "MSYS2:  mingw-w64-ucrt-x86_64-spice-gtk\n"
                "Debian: gir1.2-spiceclientgtk-3.0"
            )
            self.pack_start(label, True, True, 0)
            return

        global _REPORTED_GSTREAMER
        if not _REPORTED_GSTREAMER:
            _REPORTED_GSTREAMER = True
            for line in gstreamer_report():
                print(f"[spice] {line}")

        self.stack_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # Blank, and only to hold the space until the display arrives. The
        # waiting is said by the status panel below, in the same words the
        # tab was already using -- a bare "connecting..." label appearing
        # where that panel had been read as the tab going backwards.
        self.placeholder = Gtk.Label(label="")
        self.stack_area.pack_start(self.placeholder, True, True, 0)

        self.overlay = Gtk.Overlay()
        self.overlay.add(self.stack_area)
        self.status_panel = ConsoleStatusPanel(on_reconnect=lambda: self.on_reconnect())
        self.overlay.add_overlay(self.status_panel)
        self.pack_start(self.overlay, True, True, 0)
        self.status_panel.show_message(
            CONNECTING_TITLE,
            "Opening the console session.",
            icon=CONNECTING_ICON,
            can_reconnect=False,
            busy=True,
        )

        self.session = self._build_session()
        self._gtk_session = self._get_gtk_session()
        self._apply_clipboard()
        connect_signal(self.session, "channel-new", self._on_channel_new)
        connect_signal(self.session, "disconnected", self._on_disconnected)

        self._status(f"connecting to {self.params.get('proxy') or 'host'}")
        if not session_connect(self.session):
            self._status("spice_session_connect returned false")

    # -- setup ---------------------------------------------------------

    @staticmethod
    def _resolve(spec):
        if spec is None:
            return None
        namespace_attr, member, fallback = spec
        return _enum(namespace_attr, member, fallback)

    def _apply_channel_setting(self, func_name, value, label):
        locations = find_spice_function(func_name)
        if not locations:
            self._status(f"{func_name} not exposed by this spice-gtk build")
            return

        # Some variants take the display channel, some take the widget, and
        # some are bound as methods rather than plain functions. Try each.
        targets = [t for t in (self._display_channel, self._display) if t is not None]
        errors = []
        for location, func in locations:
            for target in targets:
                for args in ((target, int(value)), (int(value),)):
                    try:
                        func(*args)
                        self._status(f"{label} via {location}")
                        print(f"[spice] applied {location} arity={len(args)}")
                        return
                    except Exception as exc:
                        errors.append(f"{location}: {exc}")
        self._status(f"{func_name} failed -- {errors[0] if errors else '?'}")
        for line in errors[:4]:
            print(f"[spice]   {line}")

    def set_codec_index(self, index):
        self.codec_index = index
        label, spec = VIDEO_CODECS[index]
        value = self._resolve(spec)
        if value is None:
            return
        self._apply_channel_setting(
            "change_preferred_video_codec_type", value, f"codec {label}"
        )

    def set_compression_index(self, index):
        self.compression_index = index
        label, spec = IMAGE_COMPRESSION[index]
        value = self._resolve(spec)
        if value is None:
            return
        self._apply_channel_setting(
            "change_preferred_compression", value, f"compression {label}"
        )

    def _build_session(self):
        session = SpiceGLib.Session()
        params = self.params

        if not self.enable_audio:
            # A broken audio pipeline (missing autoaudiosink) can make
            # spice-gtk retry against dead objects and stall the main loop.
            # Disabling audio isolates that from video latency.
            try:
                session.set_property("enable-audio", False)
                print("[spice] audio disabled")
            except Exception as exc:
                print(f"[spice] could not disable audio: {exc}")

        # host is the Proxmox proxy ticket, passed through verbatim.
        if params.get("host"):
            session.set_property("host", str(params["host"]))
        # port and tls-port are string properties in spice-glib, not ints.
        if params.get("port"):
            session.set_property("port", str(params["port"]))
        if params.get("tls-port"):
            session.set_property("tls-port", str(params["tls-port"]))
        if params.get("password"):
            session.set_property("password", str(params["password"]))
        if params.get("proxy"):
            session.set_property("proxy", str(params["proxy"]))

        # Writing the CA to a temp file and using ca-file avoids marshalling
        # a GByteArray property from Python, which is awkward.
        if params.get("ca"):
            handle, path = tempfile.mkstemp(suffix=".pem", prefix="spice-ca-")
            with os.fdopen(handle, "w") as pem:
                pem.write(params["ca"])
            self._ca_file = path
            session.set_property("ca-file", path)

        if params.get("host-subject"):
            session.set_property("cert-subject", params["host-subject"])
            # Verify the certificate subject, not the hostname -- the
            # hostname here is a ticket string that will never match.
            session.set_property("verify", self._verify_subject())

        return session

    @staticmethod
    def _verify_subject():
        try:
            return SpiceGLib.SessionVerify.SUBJECT
        except AttributeError:
            return 4  # SPICE_SESSION_VERIFY_SUBJECT

    # -- clipboard and audio -------------------------------------------

    def _get_gtk_session(self):
        """The SpiceGtkSession that owns clipboard sharing for our session.

        It is a per-session singleton owned by spice-gtk, fetched rather
        than constructed; the clipboard properties live on it, not on the
        SpiceSession the rest of this class configures.
        """
        try:
            return SpiceGtk.GtkSession.get(self.session)
        except Exception as exc:
            print(f"[spice] no GtkSession for clipboard control: {exc}")
            return None

    def _apply_clipboard(self):
        if self._gtk_session is None:
            return False
        try:
            self._gtk_session.set_property("auto-clipboard", bool(self.share_clipboard))
            return True
        except Exception as exc:
            print(f"[spice] could not set clipboard sharing: {exc}")
            return False

    def set_clipboard_enabled(self, enabled):
        """Turn clipboard sharing on or off. Takes effect immediately."""
        self.share_clipboard = bool(enabled)
        return self._apply_clipboard()

    def set_audio_enabled(self, enabled):
        """Record the audio choice. Returns False: it needs a reconnect.

        The caller is expected to rebuild the console. Reporting False
        rather than pretending is the point -- an audio switch that claims
        to have worked and then keeps playing is worse than one that says
        what it needs.
        """
        self.play_audio = bool(enabled)
        return False

    # -- signals -------------------------------------------------------

    def _on_channel_new(self, session, channel):
        connect_signal(channel, "channel-event", self._on_channel_event)
        # Every channel counts bytes, so keep them all for the throughput
        # figure, not just the two we otherwise care about.
        self._channels.append(channel)

        if isinstance(channel, SpiceGLib.DisplayChannel):
            channel_id = channel.get_property("channel-id")
            self._display_channel = channel
            self._status(f"display channel {channel_id} appeared")
            with contextlib.suppress(Exception):
                connect_signal(
                    channel, "display-primary-create", self._on_primary_create
                )
            GLib.idle_add(self._attach_display, channel_id)

        elif isinstance(channel, SpiceGLib.MainChannel):
            self._main_channel = channel
            self._status("main channel connected")
            # spice-vdagent presence, which is what clipboard sharing and
            # guest resize actually depend on.
            connect_signal(channel, "notify::agent-connected", self._on_agent_notify)
            self._on_agent_notify(channel, None)

    def _on_channel_event(self, channel, event):
        try:
            code = int(event)
        except (TypeError, ValueError):
            code = -1
        name = CHANNEL_EVENTS.get(code, f"event {code}")
        if code and code < 6:
            self._status(name)
            # 0 is a clean close, 1-5 are failures. Either way the channel
            # is gone; on the main channel that means the session is over.
            if isinstance(channel, SpiceGLib.MainChannel) or code != 0:
                self._disconnected(f"The connection {name}.")

    def _on_primary_create(self, _channel, _fmt, width, height, *_rest):
        """The guest's actual framebuffer size, after it honours a resize.

        If this stops tracking the window past a certain area, the guest is
        out of video memory -- raise it in Proxmox (Hardware -> Display,
        e.g. 'qxl,memory=64') rather than looking for a client-side fix.
        """
        megabytes = (width * height * 4) / (1024 * 1024)
        self._status(f"guest framebuffer {width}x{height} ({megabytes:.1f} MiB)")
        return False

    def _on_agent_notify(self, channel, _pspec):
        try:
            connected = bool(channel.get_property("agent-connected"))
        except Exception:
            return
        if connected == self.agent_connected:
            return
        self.agent_connected = connected
        self.on_agent(connected)
        self._status(
            "spice-vdagent connected" if connected else "spice-vdagent not running"
        )

    def _on_disconnected(self, _session):
        self.agent_connected = False
        self.on_agent(False)
        self._disconnected("The SPICE session ended.")

    # -- telemetry -----------------------------------------------------

    def telemetry(self):
        """Throughput and guest resolution, or None before it connects.

        Bytes come from each channel's own counter, so this is the real
        wire volume rather than an estimate.
        """
        if not self._channels:
            return None

        total = 0
        for channel in self._channels:
            try:
                total += int(channel.get_property("total-read-bytes"))
            except Exception:
                continue

        now = time.monotonic()
        rate = None
        if self._last_sample is not None and now > self._last_sample:
            rate = (total - self._last_bytes) / (now - self._last_sample)
        self._last_bytes = total
        self._last_sample = now

        size = ""
        if self._display_channel is not None:
            try:
                size = (
                    f"{self._display_channel.get_property('width')}x"
                    f"{self._display_channel.get_property('height')}"
                )
            except Exception:
                size = ""

        return {"rate": rate, "size": size, "fps": None}

    def _attach_display(self, channel_id):
        if self._display is not None or self._closed:
            return False

        display = SpiceGtk.Display.new(self.session, channel_id)
        display.set_property("resize-guest", self.auto_resize)
        display.set_property("scaling", self.scaling)

        # spice-gtk's own release sequence. It defaults to this, but state it
        # so it cannot drift away from what the window handler advertises.
        try:
            display.set_grab_keys(
                SpiceGtk.GrabSequence.new_from_string("Control_L+Alt_L")
            )
        except Exception as exc:
            print(f"[spice] could not set grab keys: {exc}")
        # Never let the widget's own size request drive layout; the holder
        # decides the size and the guest follows it.
        display.set_size_request(-1, -1)

        # connect_after so this paints over what SpiceDisplay just drew.
        display.connect_after("draw", self._on_display_drawn)

        # spice-gtk grabs the keyboard while the display has focus, and on
        # Windows that grab is a low-level hook that keeps taking keys even
        # after another window is focused. Tie the grab to the pointer being
        # over the console, which is what people actually expect.
        display.add_events(
            Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )
        display.connect("enter-notify-event", self._on_display_enter)
        display.connect("leave-notify-event", self._on_display_leave)

        holder = DisplayHolder()
        holder.add(display)
        holder.set_hexpand(True)
        holder.set_vexpand(True)

        self.stack_area.remove(self.placeholder)
        self.stack_area.pack_start(holder, True, True, 0)
        holder.show_all()
        display.grab_focus()

        self._display = display
        self._holder = holder
        self.connected = True
        self.status_panel.hide_message()
        self._status("connected")
        return False

    def _on_display_enter(self, widget, _event):
        """Pointer over the console: it may take the keyboard again."""
        if self.connected and not self._closed:
            widget.grab_focus()
        return False

    def _on_display_leave(self, _widget, event):
        """Pointer gone: hand the keyboard back to the rest of the desktop.

        Ignored while the guest holds the pointer -- in server-mouse mode
        spice-gtk warps the cursor, which generates crossing events that do
        not mean the user moved away.
        """
        if event.mode != Gdk.CrossingMode.NORMAL:
            return False
        if (
            self._display is not None
            and self._display.get_property("grab-mouse")
            and self._mouse_grabbed()
        ):
            return False
        self._ungrab_keyboard()
        return False

    def _mouse_grabbed(self):
        """Whether the guest currently owns the pointer."""
        if self._main_channel is None:
            return False
        try:
            # 1 is SPICE_MOUSE_MODE_SERVER: the guest draws its own cursor
            # and the client keeps the pointer captive.
            return int(self._main_channel.get_property("mouse-mode")) == 1
        except Exception:
            return False

    def _ungrab_keyboard(self):
        if self._display is None:
            return
        func = getattr(self._display, "keyboard_ungrab", None)
        if callable(func):
            try:
                func()
            except Exception as exc:
                print(f"[spice] keyboard_ungrab failed: {exc}")

    def _on_display_drawn(self, widget, context):
        if not self.connected or self.pending:
            draw_offline_effect(
                context, widget.get_allocated_width(), widget.get_allocated_height()
            )
        return False

    def set_auto_resize(self, enabled):
        """spice-gtk's own resize-guest, which is what virt-manager ships."""
        self.auto_resize = enabled
        if self._display is not None:
            self._display.set_property("resize-guest", enabled)

    def set_scaling(self, enabled):
        self.scaling = enabled
        if self._display is not None:
            self._display.set_property("scaling", enabled)

    # -- actions -------------------------------------------------------

    def send_keys(self, keyvals):
        """Press and release a key combination in the guest.

        spice_display_send_keys() takes X11 keyvals as integers, not key
        names -- handing it strings raises, which is silent unless you are
        watching the status line.
        """
        if self._display is None:
            self._status("no display yet")
            return False
        try:
            self._display.send_keys(list(keyvals), SpiceGtk.DisplayKeyEvent.CLICK)
            return True
        except Exception as exc:
            self._status(f"could not send keys: {exc}")
            return False

    def send_ctrl_alt_del(self):
        return self.send_keys(KEY_CTRL_ALT_DEL)

    def grab_focus_display(self):
        if self._display is not None:
            self._display.grab_focus()

    def release_input(self):
        """Hand the pointer and keyboard back to the desktop."""
        if self._display is None:
            return
        for name in ("mouse_ungrab", "keyboard_ungrab"):
            func = getattr(self._display, name, None)
            if callable(func):
                try:
                    func()
                except Exception as exc:
                    print(f"[spice] {name} failed: {exc}")

    # -- lifecycle -----------------------------------------------------

    def _status(self, text):
        self.last_status = text
        self.on_status(text)
        print(f"[spice] {self.title}: {text}")

    def screenshot(self, path):
        """Grab the display widget.

        spice-gtk keeps no framebuffer we can read, so this captures what is
        on screen -- which means the console has to be visible, and the
        result is at widget scale rather than guest resolution.
        """
        if self._display is None:
            return False
        window = self._display.get_window()
        if window is None:
            return False
        from gi.repository import Gdk

        width = self._display.get_allocated_width()
        height = self._display.get_allocated_height()
        pixbuf = Gdk.pixbuf_get_from_window(window, 0, 0, width, height)
        if pixbuf is None:
            return False
        pixbuf.savev(path, "png", [], [])
        return True

    def show_pending_state(self, title, detail=""):
        """An action has been asked for but the guest has not moved yet."""
        if self._closed or getattr(self, "status_panel", None) is None:
            return
        self.pending = True
        self.status_panel.show_message(title, detail, can_reconnect=False, busy=True)
        if self._display is not None:
            self._display.queue_draw()

    def clear_pending_state(self):
        if not self.pending:
            return
        self.pending = False
        if self.connected:
            self.status_panel.hide_message()
        if self._display is not None:
            self._display.queue_draw()

    def show_guest_state(self, status):
        """Called by the owner when the guest is no longer running.

        SPICE does not always tear the session down promptly when a guest
        stops -- the display just goes black with the pointer still grabbed --
        so the inventory poll drives this rather than waiting for the
        protocol to notice.
        """
        if self._closed or getattr(self, "status_panel", None) is None:
            return
        self.connected = False
        with contextlib.suppress(Exception):
            self.release_input()
        titles = {
            "stopped": "Guest is stopped",
            "io-error": "Guest stopped on an I/O error",
            "suspended": "Guest is suspended",
            "paused": "Guest is paused",
        }
        details = {
            "stopped": "Start the guest to reconnect.",
            "io-error": "Proxmox stopped it because its storage stopped answering. Fix the storage, then reset or stop the guest.",
            "suspended": "Resume the guest to reconnect.",
            "paused": "Resume the guest to reconnect.",
        }
        icons = {
            "io-error": "dialog-warning-symbolic",
            "paused": "media-playback-pause-symbolic",
            "suspended": "media-playback-pause-symbolic",
        }
        self.status_panel.show_message(
            titles.get(status, f"Guest is {status}"),
            details.get(status, ""),
            icon=icons.get(status, "media-playback-stop-symbolic"),
            can_reconnect=False,
        )
        if self._display is not None:
            self._display.queue_draw()

    def _disconnected(self, reason):
        """Show the panel instead of leaving a frozen frame on screen."""
        # No panel exists when spice-gtk is missing entirely, and none is
        # wanted once we are tearing the tab down on purpose.
        if self._closed or getattr(self, "status_panel", None) is None:
            return
        was_connected, self.connected = self.connected, False
        self.status_panel.show_message("Connection closed", reason)
        if self._display is not None:
            self._display.queue_draw()
        if was_connected:
            self.on_disconnect(reason)

    def shutdown(self):
        self._closed = True
        if getattr(self, "session", None) is not None:
            session_disconnect(self.session)
        if self._ca_file and os.path.exists(self._ca_file):
            with contextlib.suppress(OSError):
                os.unlink(self._ca_file)
            self._ca_file = None

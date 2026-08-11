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
import logging
import os
import tempfile
import time

import gi

gi.require_version("Gtk", "3.0")

from gi.repository import Gdk, GLib, GObject, Gtk

from . import guest_state
from .decoders import gstreamer_report
from .keys import CTRL_ALT_DEL
from .scaling import clamp_console_scale
from .spicelib import (
    AVAILABLE,
    MISSING_LIBRARY,
    SPICE_GLIB_NS,
    SpiceGLib,
    SpiceGtk,
    connect_signal,
)
from .status_panel import (
    CONNECTING_ICON,
    CONNECTING_TITLE,
    ConsoleStatusPanel,
    draw_offline_effect,
)
from .usb import UsbRedirection

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Session method shadowing
#
# spice_session_connect/disconnect collide with GObject.Object's connect and
# disconnect. Which one wins depends on the PyGObject build, so call both
# explicitly and defensively.
# --------------------------------------------------------------------------


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


# spice_channel_connect/disconnect collide the same way, and here the bound
# method that might win is worse than useless: GObject's disconnect takes a
# handler id, so handing it a SpiceChannelEvent would quietly detach some
# signal instead of closing the socket and report nothing. The unbound class
# method is therefore tried first and the bound one only as a fallback.


def channel_connect(channel):
    for attempt in (
        lambda: SpiceGLib.Channel.connect(channel),
        lambda: channel.connect(),
    ):
        try:
            return bool(attempt())
        except TypeError:
            continue
    raise RuntimeError("could not call spice_channel_connect")


def channel_disconnect(channel, reason):
    try:
        SpiceGLib.Channel.disconnect(channel, reason)
        return True
    except TypeError as exc:
        raise RuntimeError(f"could not call spice_channel_disconnect: {exc}") from exc


def disconnect_signal(obj, handler):
    """Drop a signal handler, without complaining if it is already gone.

    A handler dies with the object it is on, so disconnecting one on a
    destroyed window is both harmless and wrong: GLib prints "instance has
    no handler with id" and carries on. Asking first keeps that out of the
    log, where it reads like a real fault.
    """
    if obj is None or not handler:
        return
    with contextlib.suppress(Exception):
        if GObject.signal_handler_is_connected(obj, handler):
            obj.disconnect(handler)


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

# Guest desktop effects that can be asked away for the sake of the link, as
# (label, spice-gtk name). The guest agent carries these out, so a guest
# without spice-vdagent running simply keeps its wallpaper.
#
# "color-depth" is deliberately not offered beside them: spice-gtk deprecated
# it in 0.37 and now ignores it outright ("lack of support in drivers, only
# Windows 7 and older"), so a menu entry for it would do nothing at all.
DISABLE_EFFECTS = [
    ("Wallpaper", "wallpaper"),
    ("Font Smoothing", "font-smooth"),
    ("Animation", "animation"),
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


def _channel_opened():
    """SPICE_CHANNEL_OPENED, asked for rather than assumed.

    Deliberately not taken from CHANNEL_EVENTS above: that table's numbers
    do not match the SpiceChannelEvent enum in current spice-gtk, where
    OPENED is 10 and CLOSED is 12. Anything that has to *act* on an event
    rather than merely name it asks the library.
    """
    try:
        return int(SpiceGLib.ChannelEvent.OPENED)
    except Exception:
        return 10


_REPORTED_GSTREAMER = False


class SpiceConsole(Gtk.Box):
    """A single SPICE connection with its display widget and controls."""

    protocol = "spice"
    pending = False

    # Which of the view-menu controls apply to this console type.
    supports = {
        "auto_resize": True,
        "scaling": True,
        "console_scale": True,
        "codec": True,
        "compression": True,
        "refresh": False,
        "ctrl_alt_del": True,
        "clipboard": True,
        "audio": True,
        "microphone": True,
        "view_only": True,
        "effects": True,
        "usb": True,
        # Whether a guest head can be given a monitor of its own. Capable in
        # principle here; whether this guest actually has a second head is
        # monitor_count()'s question, not this one's.
        "multi_monitor": True,
    }

    # Switches that cannot take hold until the session is rebuilt, so a
    # caller that gets False back from the setter should reconnect rather
    # than report failure. Audio is one because "enable-audio" is read when
    # the session is created. The microphone deliberately is not: it is a
    # channel, so it moves live, and a False from it means there is no
    # record channel -- which reconnecting would not change.
    RECONNECT_SWITCHES = ("audio",)

    def __init__(
        self,
        params,
        title="console",
        on_status=None,
        enable_audio=True,
        auto_resize=True,
        scale_to_fit=False,
        console_scale=100,
        on_agent=None,
        on_disconnect=None,
        on_reconnect=None,
        share_clipboard=True,
        play_audio=True,
        capture_audio=False,
        view_only=False,
        disable_effects=(),
        on_usb=None,
        on_usb_plugged=None,
        on_monitors=None,
        head_limit=1,
        video_memory=16,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        self.title = title
        self.on_status = on_status or (lambda text: None)
        self.on_agent = on_agent or (lambda connected: None)
        self.on_disconnect = on_disconnect or (lambda reason: None)
        self.on_reconnect = on_reconnect or (lambda: None)
        self.on_usb = on_usb or (lambda: None)
        self.on_usb_plugged = on_usb_plugged or (lambda key, label: None)
        self.on_monitors = on_monitors or (lambda count: None)
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
        # The microphone is the other direction of the same backend, and it
        # is a channel rather than a property, so unlike audio it can be
        # switched live. It cannot be switched on without the backend
        # existing, though, which is what ties it to play_audio -- see
        # set_microphone_enabled.
        self.share_clipboard = share_clipboard
        self.play_audio = play_audio
        self.enable_audio = bool(enable_audio) and bool(play_audio)
        self.capture_audio = bool(capture_audio)
        self._record_channel = None
        self.view_only = bool(view_only)
        # Guest desktop effects to ask the agent to drop, for a link where
        # the wallpaper is not worth the bandwidth. See _build_session.
        self.disable_effects = tuple(disable_effects or ())
        self._gtk_session = None
        self.auto_resize = auto_resize
        self.scaling = scale_to_fit
        self.console_scale = clamp_console_scale(console_scale)
        self.codec_index = 0
        self.compression_index = 0
        self._ca_file = None
        self._display = None
        self._holder = None
        self._display_channel = None
        self._main_channel = None
        # Every display channel the guest offers, and the heads they add up
        # to. The primary head keeps _display above; the rest only get a
        # widget while something is showing them. See the monitors section.
        self._display_channels = {}
        self._heads = []
        self._head_displays = {}
        self._head_windows = {}
        self._head_panels = {}
        self._head_watchdogs = {}
        self._head_sizes = {}  # head -> (width, height) of its window
        self._reasserts = {}  # head -> how many times it has been asked for
        self._primary_waiters = []  # callbacks due when the guest resizes
        # How many heads this guest's display adapter can be asked for,
        # which is a property of the adapter rather than of the session --
        # see api.models.vga_head_limit. The memory is the other half of the
        # same question: QXL holds every head in one allocation, so it is
        # what decides whether a head that was asked for can exist.
        self.head_limit = max(1, int(head_limit or 1))
        self.video_memory = max(1, int(video_memory or 1))
        self._closed = False
        self.last_status = ""
        self.usb = None
        # The window this console is in, and whether the pointer is on it:
        # together they decide who is allowed the keyboard. See the grab
        # section further down.
        self._toplevel = None
        self._toplevel_handler = None
        self._pointer_inside = False
        self.connect("hierarchy-changed", self._on_hierarchy_changed)

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
                log.info("%s", line)

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
        self.usb = UsbRedirection(
            self.session,
            on_changed=self._on_usb_changed,
            on_plugged=self._on_usb_plugged,
            on_error=lambda message: self._status(f"USB: {message}"),
        )
        for line in (self.usb.note, self.usb.advice):
            if line:
                log.info("%s", line)
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
                        log.info("applied %s arity=%s", location, len(args))
                        return
                    except Exception as exc:
                        errors.append(f"{location}: {exc}")
        self._status(f"{func_name} failed -- {errors[0] if errors else '?'}")
        for line in errors[:4]:
            log.info("  %s", line)

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
        try:
            session = SpiceGLib.Session()
        except TypeError as exc:
            # "could not get a reference to type class": the typelib is
            # there but the library behind it is not, so every SPICE GType
            # resolves to void. Nothing about that error says "a file is
            # missing", so it is said here instead.
            log.error("%s (%s)", MISSING_LIBRARY, exc)
            raise RuntimeError(MISSING_LIBRARY) from exc
        params = self.params

        if not self.enable_audio:
            # A broken audio pipeline (missing autoaudiosink) can make
            # spice-gtk retry against dead objects and stall the main loop.
            # Disabling audio isolates that from video latency.
            try:
                session.set_property("enable-audio", False)
                log.info("audio disabled")
            except Exception as exc:
                log.warning("could not disable audio: %s", exc)

        if self.disable_effects:
            # Applied to display channels as they are created, which is why
            # this is a session property rather than something to switch on a
            # live console -- see set_disable_effects. The guest agent is what
            # carries these out, so a guest without spice-vdagent ignores them.
            try:
                session.set_property("disable-effects", list(self.disable_effects))
                log.info("effects disabled: %s", ", ".join(self.disable_effects))
            except Exception as exc:
                log.warning("could not disable effects: %s", exc)

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
            log.info("no GtkSession for clipboard control: %s", exc)
            return None

    def _apply_clipboard(self):
        if self._gtk_session is None:
            return False
        try:
            self._gtk_session.set_property("auto-clipboard", bool(self.share_clipboard))
            return True
        except Exception as exc:
            log.warning("could not set clipboard sharing: %s", exc)
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

    def set_disable_effects(self, effects):
        """Record which guest effects to drop. Returns False: needs a rebuild.

        spice-gtk applies disable-effects to display channels as they are
        created, so a live session keeps whatever it was built with. Reported
        rather than pretended, exactly as with audio: the caller rebuilds.
        """
        self.disable_effects = tuple(effects or ())
        return False

    # -- the microphone ------------------------------------------------
    #
    # SPICE carries the client's microphone into the guest on the record
    # channel, which is a real channel of its own: QEMU feeds it to the VM's
    # audio input device, so the guest hears this machine's microphone with
    # no USB redirection involved. There is no equivalent for a webcam --
    # SPICE has no camera channel at all, in any version, which is why
    # redirecting the USB device is the only way to give a guest a camera.
    #
    # spice-gtk offers no switch for the direction on its own: "enable-audio"
    # builds the whole audio backend, playback and record together, so it
    # cannot say "sound out, nothing in". The channel underneath it can, and
    # a channel that is not connected cannot carry a microphone anywhere.

    def microphone_available(self):
        """Whether there is a record channel to switch at all.

        False means the guest never offered one -- no audio device, or one
        with no input -- so there is nothing a switch could do.
        """
        return self._record_channel is not None

    def _apply_microphone(self):
        """Open or shut the record channel to match self.capture_audio.

        Disconnecting leaves the rest of the session untouched: the guest
        sees a client with no microphone, which is a state every guest
        already has to cope with.

        Mute is set as well, for a guest that reads the volume rather than
        noticing the channel. It is not what makes this work -- spice-gtk
        passes mute to whichever GStreamer source it managed to build, which
        need not honour it -- so the socket is what the switch really is.
        """
        channel = self._record_channel
        if channel is None or self._closed:
            return False
        wanted = bool(self.capture_audio)
        with contextlib.suppress(Exception):
            channel.set_property("mute", not wanted)
        try:
            if wanted:
                channel_connect(channel)
            else:
                # NONE, so no channel-event is emitted for a close nobody
                # needs to hear about: this is a switch, not a failure.
                channel_disconnect(channel, SpiceGLib.ChannelEvent.NONE)
        except Exception as exc:
            log.warning("could not turn the microphone %s: %s", wanted, exc)
            return False
        log.info("microphone %s", "on" if wanted else "off")
        self._status(f"microphone {'on' if wanted else 'off'}")
        return True

    def set_microphone_enabled(self, enabled):
        """Turn the guest's microphone on or off, immediately.

        Unlike playback this needs no reconnect: closing the channel stops
        the capture at its source rather than asking a pipeline to be quiet.

        Returns False when there is no record channel, which a reconnect
        would not conjure up either -- the caller is expected to say so
        rather than to rebuild the console. See RECONNECT_SWITCHES.
        """
        self.capture_audio = bool(enabled)
        if self._record_channel is None:
            return False
        return self._apply_microphone()

    # -- USB redirection -----------------------------------------------

    def usb_devices(self):
        return self.usb.devices() if self.usb is not None else []

    def usb_redirected(self):
        return self.usb.redirected() if self.usb is not None else []

    def usb_channels(self):
        """Redirection ports the guest offers, 0 when it has none."""
        return self.usb.channels if self.usb is not None else 0

    def usb_snapshot(self):
        """(devices, channels) together, for callers that want both."""
        return self.usb.snapshot() if self.usb is not None else ([], 0)

    def usb_note(self):
        """Why USB redirection cannot work here at all, or "" when it can."""
        return self.usb.note if self.usb is not None else ""

    def usb_advice(self):
        """What is missing on this machine, for a list that works anyway."""
        return self.usb.advice if self.usb is not None else ""

    def _on_usb_changed(self):
        if not self._closed:
            self.on_usb()

    def _on_usb_plugged(self, key, label):
        if not self._closed:
            self.on_usb_plugged(key, label)

    # -- signals -------------------------------------------------------

    def _on_channel_new(self, session, channel):
        connect_signal(channel, "channel-event", self._on_channel_event)
        # Every channel counts bytes, so keep them all for the throughput
        # figure, not just the two we otherwise care about.
        self._channels.append(channel)

        if isinstance(channel, SpiceGLib.DisplayChannel):
            channel_id = channel.get_property("channel-id")
            self._display_channels[channel_id] = channel
            # The first channel is the console's own display, and stays so:
            # a second head arriving must not move the codec and compression
            # controls off the picture the tab is showing.
            if self._display_channel is None:
                self._display_channel = channel
            self._status(f"display channel {channel_id} appeared")
            with contextlib.suppress(Exception):
                connect_signal(
                    channel, "display-primary-create", self._on_primary_create
                )
            with contextlib.suppress(Exception):
                connect_signal(channel, "notify::monitors", self._on_monitors_notify)
            self._refresh_heads()
            GLib.idle_add(self._attach_display, channel_id)

        elif isinstance(channel, SpiceGLib.RecordChannel):
            # The microphone. Not acted on from in here: spice-gtk's own
            # handler for this signal has not run yet, and it connects the
            # channel, which would undo a disconnect made now. The idle
            # callback lands after it.
            self._record_channel = channel
            self._status("microphone channel offered")
            GLib.idle_add(lambda: (self._apply_microphone(), False)[1])

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

        # The microphone coming back up on its own -- a migration, or
        # spice-gtk reconnecting the channel. Shut it again rather than
        # assuming it could only ever be opened once: a switch that says the
        # microphone is off has to keep being true.
        if (
            channel is self._record_channel
            and not self.capture_audio
            and code == _channel_opened()
        ):
            GLib.idle_add(lambda: (self._apply_microphone(), False)[1])
            return

        if code < 6 and isinstance(channel, SpiceGLib.DisplayChannel):
            # A head the guest has taken away is not one to offer a monitor to.
            with contextlib.suppress(Exception):
                self._display_channels.pop(channel.get_property("channel-id"), None)
            self._refresh_heads()
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
        # This is the resize that anyone waiting for the guest to settle was
        # waiting for -- and it is also the one that can lose a head, since
        # the config spice-gtk sends for it is built from the displays it
        # knows about, and a head asked for and not yet created is not one
        # of them. The retry that follows is why full screen no longer has
        # to be entered twice.
        for waiter in list(self._primary_waiters):
            GLib.idle_add(self._settle, waiter)
        return False

    # -- monitors -------------------------------------------------------
    #
    # A head is asked for, not waited for. This is the part that is easy to
    # get wrong, and getting it wrong reads as "this guest only has one
    # display": the guest does not offer a second head until a client says
    # it wants one, so counting what has turned up answers 1 for a guest
    # that would happily give you four.
    #
    # What a client does instead is what virt-viewer's Displays menu does:
    # tell the main channel that display N is enabled, send the monitor
    # config, and let the guest's driver create the head. QXL carries up to
    # four monitors, and it does not matter whether they arrive as four
    # monitors on one display channel (plain 'vga: qxl', one device, the
    # driver splitting it) or as one channel each ('vga: qxl2' and up, which
    # is two QXL devices). Heads are therefore numbered flat, 0-3, exactly
    # as spice_main_channel_update_display_enabled() numbers them, and the
    # (channel, monitor) pair behind a number is worked out from whatever
    # has actually connected.

    def _on_monitors_notify(self, channel, _pspec):
        self._log_monitors(channel)
        self._refresh_heads()

    @staticmethod
    def _log_monitors(channel):
        """Write down what the guest says its monitors are.

        The one piece of evidence that says whether a head that was asked
        for was created, ignored, or created at a size nobody can use.
        """
        try:
            channel_id = channel.get_property("channel-id")
        except Exception:
            channel_id = "?"
        try:
            monitors = channel.get_property("monitors")
        except Exception as exc:
            log.info("channel %s: no monitors property (%s)", channel_id, exc)
            return
        described = []
        for index, monitor in enumerate(monitors or []):
            described.append(
                {
                    "id": getattr(monitor, "id", index),
                    "x": getattr(monitor, "x", None),
                    "y": getattr(monitor, "y", None),
                    "w": getattr(monitor, "width", None),
                    "h": getattr(monitor, "height", None),
                }
            )
        log.info("channel %s monitors: %s", channel_id, described or "none")

    @staticmethod
    def _monitor_ids(channel):
        """The heads one display channel is carrying.

        The monitors-config only arrives once the guest sends one, and a
        head the guest has switched off is listed at zero size. Neither is a
        reason to believe the channel has no picture at all, so a channel
        with nothing usable to say still counts as the one head it has.
        """
        try:
            monitors = channel.get_property("monitors")
        except Exception:
            monitors = None

        ids = []
        for index, monitor in enumerate(monitors or []):
            try:
                if not (monitor.width and monitor.height):
                    continue
                ids.append(int(monitor.id))
            except Exception:
                ids.append(index)
        return ids or [0]

    def _refresh_heads(self):
        # Channel order, always, and never the order the tab happens to
        # like. A head's position in this list is its display id -- the
        # number spice-gtk and the guest's agent both use to name it -- so
        # the list has to be in the numbering they use, not ours.
        #
        # It is tempting to put the tab's own head first, and it is wrong:
        # a guest with two QXL devices announces its channels in whatever
        # order it likes, so the tab can perfectly well be showing channel
        # 1. Reordering the list around that renumbers every head, and then
        # switching off "the second head" on the way out switches off the
        # one the tab is showing. See primary_head_index for the other half.
        heads = []
        for channel_id in sorted(self._display_channels):
            for monitor_id in self._monitor_ids(self._display_channels[channel_id]):
                heads.append((channel_id, monitor_id))
        changed = heads != self._heads
        self._heads = heads
        if changed and len(heads) > 1:
            self._status(f"guest is showing {len(heads)} monitors")
        # Every time, not only when the list changes. A guest configured for
        # two heads has both display channels from the moment it connects,
        # so opening a window on the second one changes nothing here -- and
        # a head confirmed only on a change is a head reported missing while
        # it is on screen.
        self._confirm_heads()
        if changed:
            self._notify_monitors()

    def _notify_monitors(self):
        """Say that the answer to 'how many monitors?' may have changed.

        Connecting and disconnecting count as much as a head appearing: the
        controls that offer to use the extra monitors are only meaningful
        while there is a session to ask.
        """
        if not self._closed:
            self.on_monitors(self.available_heads())

    def monitor_count(self):
        """How many heads the guest is showing now, 0 before it connects."""
        return len(self._heads)

    def available_heads(self):
        """How many heads this console could show, 0 until it is connected.

        Not how many the guest is showing: a client asks for heads and the
        guest makes them, so this is the adapter's ceiling rather than a
        count of what is already there. A guest whose driver will not make
        the head leaves that window blank, which is what virt-viewer does
        too, and is why a head is only asked for when somebody chooses to
        use it.
        """
        if not self.connected or self._closed:
            return 0
        return max(self.head_limit, len(self._heads))

    def set_head_limit(self, limit):
        """Update the adapter's ceiling, e.g. after the config is re-read."""
        limit = max(1, int(limit or 1))
        if limit == self.head_limit:
            return
        self.head_limit = limit
        self._notify_monitors()

    def primary_head_index(self):
        """Which head the tab itself is showing.

        Not necessarily 0. The tab attaches to the first display channel to
        turn up, and on a guest with two QXL devices that can be channel 1
        -- in which case the tab's head is display 1 and the one going on
        the second monitor is display 0. Everything that adds or removes a
        head asks this first, because the one head that must never be
        switched off is the one already on screen.
        """
        if self._display is None:
            return 0
        try:
            address = (
                self._display.get_property("channel-id"),
                self._display_monitor_id(self._display),
            )
        except Exception:
            return 0
        for index, head in enumerate(self._heads):
            if head == address:
                return index
        return 0

    def head_address(self, index):
        """(channel, monitor) for head `index`.

        Straight out of the connected channels when they go that far. When
        they do not -- the usual case for a head nobody has asked for yet --
        the shape is guessed from what is connected: one display channel
        means one device splitting itself into monitors, so the head is
        another monitor on it; several means a device each, so the head is
        another channel.
        """
        if index < len(self._heads):
            return self._heads[index]
        if len(self._display_channels) > 1:
            return index, 0
        base = min(self._display_channels) if self._display_channels else 0
        return base, index

    def _main_channel_call(self, names, *args):
        """Call the first of `names` this spice-gtk build actually has.

        The main channel's functions have been renamed across releases --
        spice_main_set_display_enabled() became
        spice_main_channel_update_display_enabled() with an extra argument --
        and which spelling introspection exposes depends on the build.
        """
        if self._main_channel is None:
            return False
        for name in names:
            func = getattr(self._main_channel, name, None)
            if not callable(func):
                continue
            try:
                func(*args)
                return True
            except TypeError:
                continue  # a different arity of the same name
            except Exception as exc:
                log.info("%s failed: %s", name, exc)
                return False
        return False

    # -- the arrangement -------------------------------------------------
    #
    # The whole arrangement is stated at once, every time any of it moves.
    # Sending one head's worth of change on its own does not work, for two
    # reasons that both showed up as a second monitor that never arrived:
    #
    #   * the guest is told about heads as a set, and spice-gtk builds that
    #     set from the displays it currently knows. A config sent for the
    #     first head while the second is enabled-but-not-yet-created drops
    #     the second one -- which is what happens when going full screen
    #     resizes the first head at the moment the second is asked for. So
    #     the enable is re-stated whenever the first head resizes.
    #
    #   * where each head *goes* is the guest's business, not ours. It
    #     arranges the heads it has been given, and it is good at it: given
    #     two heads it puts the second one beside the first, at a negative
    #     origin if that is what its desktop wants. A client that sends
    #     positions as well is a second opinion arriving continuously --
    #     every position it sends provokes a resize, and every resize
    #     changes the numbers the next position is computed from. On a
    #     two-device guest it is worse than useless: the agent matches
    #     config entries to QXL devices in the guest's own enumeration
    #     order, which need not be the order the display channels are in,
    #     so a position meant for one monitor lands on the other and the
    #     two sizes swap back and forth for as long as anyone is watching.
    #
    # So: say which heads exist, let the widgets report their own sizes
    # through resize-guest, and leave the arrangement alone.

    def _enable_head(self, index, enabled, update=False):
        ok = self._main_channel_call(
            ("update_display_enabled", "set_display_enabled"),
            index,
            bool(enabled),
            update,
        )
        if not ok:
            ok = self._main_channel_call(("set_display_enabled",), index, bool(enabled))
        return ok

    def _send_monitor_config(self):
        """Nothing above reaches the guest until this goes out."""
        return self._main_channel_call(("send_monitor_config",))

    def set_head_enabled(self, index, enabled):
        """Add or remove one head. The guest decides where it goes.

        Never the tab's own head, whatever number that turns out to be:
        switching it off leaves the console showing a monitor the guest no
        longer has, which is a black rectangle that only comes back by
        going full screen again.
        """
        if self._main_channel is None or index < 0:
            return False
        if index == self.primary_head_index():
            log.info("refusing to switch off head %s: the tab is showing it", index)
            return False
        if not enabled:
            self._head_sizes.pop(index, None)
            self._cancel_head_watch(index)
            self._reasserts.pop(index, None)
        ok = self._enable_head(index, enabled)
        sent = self._send_monitor_config()
        self._status(
            f"display {index} {'enabled' if enabled else 'disabled'}"
            f"{'' if ok and sent else ' (spice-gtk refused)'}"
        )
        return ok and sent

    # How long to wait for the guest to answer a resize before going ahead
    # anyway. Long enough for a guest that is going to answer; short enough
    # that a guest which is not leaves no impression of a stuck window.
    PRIMARY_SETTLE_MS = 1500

    def after_primary_settles(self, callback):
        """Call back once the guest has answered the resize now in flight.

        For asking for a head at a moment when the answer will survive.
        spice-gtk describes the displays it knows about whenever the first
        head changes size, and a head that has been asked for but not yet
        created is not one it knows about -- so a head asked for while the
        first one is still on its way to full screen is dropped by the very
        next message, and only turns up if the whole thing is repeated. A
        head asked for after the resize has landed is not dropped, because
        by then it exists.

        Always calls back exactly once: on the guest's answer if there is
        one, and on a timer if there is not -- a guest with auto-resize off,
        or one that simply will not resize, must not leave the caller
        waiting for a resize that is never coming.
        """
        if self._closed:
            callback()
            return
        waiter = {"done": False, "callback": callback}
        self._primary_waiters.append(waiter)
        GLib.timeout_add(self.PRIMARY_SETTLE_MS, self._settle, waiter)

    def _settle(self, waiter):
        if waiter["done"]:
            return False
        waiter["done"] = True
        if waiter in self._primary_waiters:
            self._primary_waiters.remove(waiter)
        if not self._closed:
            waiter["callback"]()
        return False

    def set_head_size(self, index, width, height):
        """Note how big a head's window is. Recorded, not sent.

        The widget sends its own size, and does it better -- it is told
        first and knows the real allocation. This is only so that a head
        that never appears can be described accurately when saying so.
        """
        if width > 0 and height > 0:
            self._head_sizes[index] = (int(width), int(height))

    # How many times a head is asked for again before the answer is taken
    # to be no. Bounded because each retry is a real one -- see below --
    # and a client that never stops asking is worse than a blank window.
    MAX_RETRIES = 3

    def retry_missing_heads(self):
        """Ask again, properly, for any head that has not arrived.

        Off, on, and a new widget: the same thing as leaving full screen
        and going back into it, which is what people were doing by hand
        when the first ask did not take. A bare re-enable is not enough and
        it is worth saying why -- the size of a head is reported by its
        widget, and only when the widget is allocated. A head dropped from
        the guest's config and then merely re-enabled is a head with no
        size, which the guest is entitled to ignore. Building the widget
        again is what produces a fresh allocation, and with it a fresh
        size.

        A head that has arrived is left alone entirely.
        """
        if self._closed or self._main_channel is None:
            return False
        wanted = [
            index
            for index in sorted(self._head_displays)
            if index >= len(self._heads)
            and self._reasserts.get(index, 0) < self.MAX_RETRIES
        ]
        if not wanted:
            return False
        for index in wanted:
            self._reasserts[index] = self._reasserts.get(index, 0) + 1
            self._status(f"asking again for display {index}")
            # Off and on with the config sent between: the guest is told the
            # head has gone before being told it is back, which is what makes
            # this a new request rather than a repeat of one it has already
            # decided about.
            self._enable_head(index, False)
            self._send_monitor_config()
            self._enable_head(index, True)
            self._send_monitor_config()
            self._swap_head_display(index, self.head_address(index))
            self._watch_head(index)
        return True

    def _new_display(self, channel_id, monitor_id):
        """A SpiceDisplay for one monitor of one channel, however it is built.

        Which of these a build offers is not knowable in advance, and the
        difference matters: showing monitor 1 of a single QXL device is the
        plain 'vga: qxl' case, i.e. the usual one. Falling back to a
        widget for the whole channel would show monitor 0 again -- the same
        picture as the tab, on a second screen -- so every route to a real
        monitor-id is tried before giving up, and the failures are logged
        rather than swallowed.

        Property construction is the reliable one: 'channel-id' and
        'monitor-id' are construct properties of the widget, whatever the
        introspected constructors happen to be called in this build.
        """
        attempts = [
            (
                "Display.new_with_monitor",
                lambda: SpiceGtk.Display.new_with_monitor(
                    self.session, channel_id, monitor_id
                ),
            ),
            (
                "display_new_with_monitor",
                lambda: SpiceGtk.display_new_with_monitor(
                    self.session, channel_id, monitor_id
                ),
            ),
            (
                "Display(session=, channel-id=, monitor-id=)",
                lambda: SpiceGtk.Display(
                    session=self.session,
                    channel_id=channel_id,
                    monitor_id=monitor_id,
                ),
            ),
            (
                "Display.new + monitor-id",
                lambda: self._new_display_by_property(channel_id, monitor_id),
            ),
        ]
        if not monitor_id:
            # Only when monitor 0 is what was wanted anyway.
            attempts.append(
                ("Display.new", lambda: SpiceGtk.Display.new(self.session, channel_id))
            )

        for name, build in attempts:
            try:
                display = build()
            except Exception as exc:
                log.info("head %s.%s: %s failed: %s", channel_id, monitor_id, name, exc)
                continue
            if display is None:
                log.info("head %s.%s: %s gave nothing", channel_id, monitor_id, name)
                continue
            got = self._display_monitor_id(display)
            if got != monitor_id:
                # A widget on the wrong monitor is a duplicate of the tab,
                # not a second screen. Say so and keep looking.
                log.info(
                    "head %s.%s: %s built monitor %s instead",
                    channel_id,
                    monitor_id,
                    name,
                    got,
                )
                continue
            log.info("head %s.%s: built by %s", channel_id, monitor_id, name)
            return display
        return None

    def _new_display_by_property(self, channel_id, monitor_id):
        display = SpiceGtk.Display.new(self.session, channel_id)
        display.set_property("monitor-id", monitor_id)
        return display

    @staticmethod
    def _display_monitor_id(display):
        try:
            return int(display.get_property("monitor-id"))
        except Exception:
            return 0

    def create_head_display(self, index):
        """A display widget for head `index`, packed and ready to be shown.

        Nothing is reparented. The tab's own head keeps the widget it has
        had since it connected, and every other head gets a widget of its
        own that lives exactly as long as the window holding it -- moving a
        live SpiceDisplay between toplevels is the thing this avoids, for
        the same reason fullscreen.py does not reparent either.

        Returns a holder to add to a container, or None when there is no
        such head.
        """
        # The tab's own head already has a widget; asking for it again would
        # put the same picture in two places. Which head that is has to be
        # asked rather than assumed -- see primary_head_index.
        if not AVAILABLE or self._closed:
            return None
        if not 0 <= index < self.available_heads():
            return None
        if index == self.primary_head_index():
            return None
        existing = self._head_displays.get(index)
        if existing is not None:
            holder = existing.get_parent()
            if holder is not None:
                return holder
            # Its window went away without us being told. Start again.
            self.release_head_display(index)

        # Ask first. The widget below can be built for a head that does not
        # exist yet -- spice-gtk binds it when the channel turns up -- but
        # nothing will ever turn up unless the guest is told to make it.
        self.set_head_enabled(index, True)

        channel_id, monitor_id = self.head_address(index)
        display = self._new_display(channel_id, monitor_id)
        if display is None:
            self._status(
                f"display {index} (channel {channel_id}.{monitor_id}) could not be "
                "built -- see the log"
            )
            return None
        self._configure_head_display(display)

        holder = DisplayHolder()
        holder.add(display)
        holder.set_hexpand(True)
        holder.set_vexpand(True)

        # A head the guest never makes is a grey rectangle and no more, which
        # says nothing about why. The panel over it does, once it is clear
        # the guest is not going to answer.
        overlay = Gtk.Overlay()
        overlay.add(holder)
        panel = ConsoleStatusPanel()
        overlay.add_overlay(panel)
        panel.show_message(
            "Adding this display...",
            "Waiting for the guest to create it.",
            icon=CONNECTING_ICON,
            can_reconnect=False,
            busy=True,
        )

        self._head_displays[index] = display
        self._head_panels[index] = panel
        self._status(f"display {index} on channel {channel_id}.{monitor_id}")
        # Which starts the clock only if this head is not already there.
        self._confirm_heads()
        return overlay

    def _configure_head_display(self, display):
        """Everything an extra head's widget needs, wherever it came from.

        resize-guest is on here whatever the tab's own setting is, and that
        is deliberate. It is not only a convenience: it is the property that
        makes spice-gtk drive the head itself -- sizing it and keeping it
        enabled from the widget's allocation, on the same code path every
        other SPICE client uses. With it off, the only thing asking for the
        head is this class, and a head asked for once is easily lost. The
        window is exactly one monitor, so there is nothing for the guest to
        match but the monitor.
        """
        display.set_property("resize-guest", True)
        self._apply_scaling(display)
        with contextlib.suppress(Exception):
            display.set_grab_keys(
                SpiceGtk.GrabSequence.new_from_string("Control_L+Alt_L")
            )
        display.set_size_request(-1, -1)
        display.connect_after("draw", self._on_display_drawn)
        display.add_events(
            Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )
        display.connect("enter-notify-event", self._on_display_enter)
        display.connect("leave-notify-event", self._on_display_leave)
        display.connect("hierarchy-changed", self._on_head_hierarchy_changed)

    # -- did the guest actually make it? --------------------------------

    HEAD_WAIT_SECONDS = 5

    def _watch_head(self, index):
        """Give the guest a few seconds, then say what went wrong."""
        self._cancel_head_watch(index)
        self._head_watchdogs[index] = GLib.timeout_add_seconds(
            self.HEAD_WAIT_SECONDS, self._head_overdue, index
        )

    def _cancel_head_watch(self, index):
        source = self._head_watchdogs.pop(index, None)
        if source is not None:
            with contextlib.suppress(Exception):
                GLib.source_remove(source)

    def _head_overdue(self, index):
        self._head_watchdogs.pop(index, None)
        panel = self._head_panels.get(index)
        if panel is None or self._closed:
            return False
        title, detail = self._head_refusal()
        log.info("display %s never appeared: %s", index, detail)
        panel.show_message(
            title, detail, icon="dialog-warning-symbolic", can_reconnect=False
        )
        return False

    def _head_refusal(self):
        """Why a head that was asked for did not arrive.

        Only what can be shown to be true. Video memory is a real cause and
        the arithmetic is checkable -- QXL keeps every head in one
        allocation at four bytes a pixel -- so it is named only when the
        numbers actually say so, never as a guess. Same for the agent: it is
        what applies the monitor config inside the guest, and whether it is
        there is known rather than supposed.
        """
        needed = self._video_memory_needed()
        if needed and needed > self.video_memory:
            return (
                "Not enough video memory",
                f"These displays need about {needed} MiB and this guest has "
                f"{self.video_memory} MiB. In Proxmox, set Hardware -> Display "
                f"to qxl with memory {self._suggested_memory(needed)}, then stop "
                "and start the VM.",
            )
        if not self.agent_connected:
            return (
                "The guest agent is not running",
                "SPICE displays are added by the guest's own agent. Install or "
                "start spice-vdagent (Linux) or the SPICE guest tools (Windows).",
            )
        return (
            "The guest did not add this display",
            "It was asked for and did not appear. The guest's display driver "
            "decides this: check whether the guest itself can see a second "
            "monitor in its own display settings. The log has the monitor "
            "config that was sent.",
        )

    def _video_memory_needed(self):
        """MiB for every head at once, or 0 when the sizes are not known."""
        areas = []
        if self._display_channel is not None:
            with contextlib.suppress(Exception):
                areas.append(
                    int(self._display_channel.get_property("width"))
                    * int(self._display_channel.get_property("height"))
                )
        areas.extend(width * height for width, height in self._head_sizes.values())
        if not areas:
            return 0
        return round(sum(areas) * 4 / (1024 * 1024))

    @staticmethod
    def _suggested_memory(needed):
        """The next size up that Proxmox offers, given what is needed."""
        for size in (32, 64, 128, 256, 512):
            if size >= needed:
                return size
        return 512

    def _confirm_heads(self):
        """Match the open head windows against what the guest is showing.

        Both ways round: a head that is there stops being waited for, and a
        head that is not there starts being waited for. The second half is
        what starts the clock at all -- a window can be opened on a head
        that already exists, and then there is nothing to wait for.
        """
        for index in list(self._head_displays):
            if index >= len(self._heads):
                if index not in self._head_watchdogs:
                    self._watch_head(index)
                continue
            self._cancel_head_watch(index)
            self._rebind_head(index)
            panel = self._head_panels.get(index)
            if panel is not None:
                panel.hide_message()

    def _rebind_head(self, index):
        """Move a head's widget onto the address the guest actually used.

        Where a head will appear has to be guessed before it exists -- a
        second monitor on the one display channel, or a channel of its own --
        and the guess can be wrong. This is what makes a wrong guess cost a
        redraw rather than an evening: once the head is really there, its
        address is known, and the widget is rebuilt on it.
        """
        display = self._head_displays.get(index)
        if display is None:
            return
        wanted = self._heads[index]
        current = (
            display.get_property("channel-id"),
            self._display_monitor_id(display),
        )
        if current == wanted:
            return
        log.info("display %s: rebinding from %s to %s", index, current, wanted)
        self._swap_head_display(index, wanted)

    def _swap_head_display(self, index, address):
        """Put a new widget for `address` in the window head `index` is in.

        The window stays; only the picture inside it is rebuilt. That is
        what lets a head be asked for again without anything flashing on
        screen, and what lets a wrong guess about where a head would appear
        cost a redraw instead of the whole arrangement.
        """
        display = self._head_displays.get(index)
        if display is None:
            return False
        holder = display.get_parent()
        if holder is None:
            return False
        replacement = self._new_display(*address)
        if replacement is None:
            return False
        holder.remove(display)
        with contextlib.suppress(Exception):
            display.destroy()
        self._configure_head_display(replacement)
        holder.add(replacement)
        holder.show_all()
        self._head_displays[index] = replacement
        self._apply_keyboard_grab()
        return True

    def release_head_display(self, index):
        """Give the head back: the widget goes, and so does the head itself."""
        self._cancel_head_watch(index)
        self._head_panels.pop(index, None)
        self._head_sizes.pop(index, None)
        self._reasserts.pop(index, None)
        display = self._head_displays.pop(index, None)
        if display is None:
            return
        with contextlib.suppress(Exception):
            display.destroy()
        self._forget_head_windows()
        # Told, not merely dropped. A guest left with a head nobody is
        # watching keeps it in its desktop layout, and windows go and live
        # on it.
        if not self._closed:
            self.set_head_enabled(index, False)

    def release_head_displays(self):
        for index in list(self._head_displays):
            self.release_head_display(index)

    def give_back_heads(self):
        """Hand every extra head back to the guest, widgets and all.

        Every head this client ever asked for, not only the ones with a
        widget open: the two can differ if a window went away without the
        head being disabled, and a head nobody gives back is one the guest
        keeps.
        """
        self.release_head_displays()
        for index in sorted(self._head_sizes, reverse=True):
            self.set_head_enabled(index, False)

    def _extra_displays(self):
        return [d for d in self._head_displays.values() if d is not None]

    def _all_displays(self):
        displays = [self._display] if self._display is not None else []
        return displays + self._extra_displays()

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
        self._apply_scaling(display)

        # spice-gtk's own release sequence. It defaults to this, but state it
        # so it cannot drift away from what the window handler advertises.
        try:
            display.set_grab_keys(
                SpiceGtk.GrabSequence.new_from_string("Control_L+Alt_L")
            )
        except Exception as exc:
            log.warning("could not set grab keys: %s", exc)
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

        self._display = display
        self._holder = holder
        self.connected = True
        # Only after _display is set: this is what decides whether the guest
        # is allowed the keyboard at all, and it reads the display.
        self._apply_keyboard_grab()
        if self._window_active():
            display.grab_focus()
        self.status_panel.hide_message()
        self._status("connected")
        # Only now can a head be asked for, so only now is offering to use
        # the other monitors worth anything.
        self._notify_monitors()
        return False

    # -- keyboard grab --------------------------------------------------
    #
    # Two rules, and the second is the one that is easy to get wrong:
    #
    #   * the pointer has to be over the console, and
    #   * the window has to be the active one.
    #
    # Without the second, hovering the console of a window you are not using
    # takes the keyboard away from the window you *are* using. On Windows
    # that is not a figure of speech: spice-gtk's grab is a low-level
    # keyboard hook, so an unfocused Proxima will happily swallow what you
    # are typing into your browser.

    def _window_active(self):
        """Whether any window showing this console has the focus.

        Any, not just the tab's: in fullscreen across several monitors each
        extra head is a toplevel of its own, and clicking into one of those
        must not read as "the user has gone elsewhere" and take the guest's
        keyboard away.
        """
        return any(w.is_active() for w in self._console_windows())

    def _console_windows(self):
        windows = [self._toplevel] if self._toplevel is not None else []
        for window in self._head_windows:
            if window not in windows:
                windows.append(window)
        return windows

    def _on_head_hierarchy_changed(self, display, _old_toplevel):
        """Follow the window an extra head has been put into."""
        window = display.get_toplevel()
        if not isinstance(window, Gtk.Window) or window in self._head_windows:
            return
        self._head_windows[window] = window.connect(
            "notify::is-active", self._on_window_active_changed
        )
        self._apply_keyboard_grab()

    def _forget_head_windows(self):
        """Drop the windows no head is in any more."""
        live = {d.get_toplevel() for d in self._extra_displays() if d.get_toplevel()}
        for window in [w for w in self._head_windows if w not in live]:
            disconnect_signal(window, self._head_windows.pop(window))

    def _on_hierarchy_changed(self, _widget, _old_toplevel):
        """Follow the window this console is in, tab or pop-out."""
        window = self.get_toplevel()
        if not isinstance(window, Gtk.Window):
            window = None
        if window is self._toplevel:
            return
        disconnect_signal(self._toplevel, self._toplevel_handler)
        self._toplevel = window
        self._toplevel_handler = None
        if window is not None:
            self._toplevel_handler = window.connect(
                "notify::is-active", self._on_window_active_changed
            )
        self._apply_keyboard_grab()

    def _on_window_active_changed(self, *_args):
        self._apply_keyboard_grab()
        # Coming back to a window with the pointer already sitting on the
        # console: spice-gtk only reconsiders the grab on a crossing or
        # focus event, and neither is coming, so give it one.
        if self._window_active() and self._pointer_inside and not self._closed:
            with contextlib.suppress(Exception):
                self._display.grab_focus()

    def _apply_keyboard_grab(self):
        """Let spice-gtk hold the keyboard only for an active window.

        Belt and braces with the focus handling below: this switches off
        spice-gtk's own grab logic outright, so nothing it does on a
        crossing event can reach around us.
        """
        if self._display is None:
            return
        active = self._window_active()
        for display in self._all_displays():
            with contextlib.suppress(Exception):
                display.set_property("grab-keyboard", active)
        if not active:
            self._ungrab_keyboard()

    def _on_display_enter(self, widget, _event):
        """Pointer over the console: it may take the keyboard again."""
        self._pointer_inside = True
        if self.connected and not self._closed and self._window_active():
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
        self._pointer_inside = False
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
        for display in self._all_displays():
            func = getattr(display, "keyboard_ungrab", None)
            if callable(func):
                try:
                    func()
                except Exception as exc:
                    log.warning("keyboard_ungrab failed: %s", exc)

    def _on_display_drawn(self, widget, context):
        if not self.connected or self.pending:
            draw_offline_effect(
                context, widget.get_allocated_width(), widget.get_allocated_height()
            )
        return False

    def set_auto_resize(self, enabled):
        """spice-gtk's own resize-guest, which is what virt-manager ships.

        The tab's own head only. An extra head fills a whole monitor and
        keeps resize-guest on regardless -- see _configure_head_display.
        """
        self.auto_resize = enabled
        if self._display is not None:
            self._display.set_property("resize-guest", enabled)

    def set_scaling(self, enabled):
        self.scaling = enabled
        for display in self._all_displays():
            self._apply_scaling(display)

    def set_console_scale(self, percent):
        """Ask the guest for fewer pixels and draw them larger.

        spice-gtk's zoom-level, which is exactly this: with resize-guest on,
        the size it asks the guest for is `window * scale_factor / zoom`
        (spice-widget.c, recalc_geometry), so 200% asks for a quarter of the
        pixels and fills the same window with them. That is the whole point
        on a high-resolution screen -- a quarter as much for the guest to
        render, encode and send.

        Pointer input needs nothing from us: spice-gtk derives it from the
        same geometry it draws with, in transform_input(), so the guest
        pointer lands where the picture says it does at any zoom.
        """
        self.console_scale = clamp_console_scale(percent)
        for display in self._all_displays():
            self._apply_scaling(display)

    def set_view_only(self, enabled):
        """Stop sending input to the guest, or start again.

        spice-gtk's disable-inputs, which is per display widget and takes
        effect at once -- so this applies to every head, including any opened
        later: see _apply_scaling, which every display goes through.

        The pointer grab is dropped on the way in. Holding a grab for a
        console that ignores the pointer is the worst of both.
        """
        self.view_only = bool(enabled)
        for display in self._all_displays():
            self._apply_view_only(display)
        if self.view_only:
            with contextlib.suppress(Exception):
                self.release_input()
        return True

    def _apply_view_only(self, display):
        with contextlib.suppress(Exception):
            display.set_property("disable-inputs", self.view_only)

    def _apply_scaling(self, display):
        """Put `scaling` and `console_scale` onto one display together.

        They are one setting to spice-gtk even though they are two here:
        zoom-level is only read when scaling is allowed, so a zoomed console
        with scale-to-fit switched off would quietly stay at 100%. Zooming
        implies scaling and turns it on; the stored scale-to-fit preference
        is not touched, so switching back to 100% restores it.
        """
        # Every display arrives here, whether it is the tab's own head or one
        # opened onto a second monitor, so this is where view-only is made to
        # apply to all of them without a second list to keep.
        self._apply_view_only(display)
        zoomed = self.console_scale != 100
        with contextlib.suppress(Exception):
            display.set_property("scaling", self.scaling or zoomed)
            display.set_property("zoom-level", self.console_scale)

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
        return self.send_keys(CTRL_ALT_DEL)

    def grab_focus_display(self):
        if self._display is not None:
            self._display.grab_focus()

    def release_input(self):
        """Hand the pointer and keyboard back to the desktop."""
        for display in self._all_displays():
            for name in ("mouse_ungrab", "keyboard_ungrab"):
                func = getattr(display, name, None)
                if callable(func):
                    try:
                        func()
                    except Exception as exc:
                        log.warning("%s failed: %s", name, exc)

    # -- lifecycle -----------------------------------------------------

    def _status(self, text):
        self.last_status = text
        self.on_status(text)
        log.info("%s: %s", self.title, text)

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
        self._notify_monitors()
        with contextlib.suppress(Exception):
            self.release_input()
        state = guest_state.describe(status)
        self.status_panel.show_message(
            state.title,
            state.detail,
            icon=state.icon,
            # Not state.can_reconnect: this is only reached for a guest with
            # no console at all, and the poll rebuilds the session itself the
            # moment one comes back. A button here would only ever fail.
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
        self._notify_monitors()
        self.status_panel.show_message("Connection closed", reason)
        if self._display is not None:
            self._display.queue_draw()
        if was_connected:
            self.on_disconnect(reason)

    def shutdown(self):
        # Heads first, and before _closed, which is the whole point of the
        # order. A head exists because this client asked for it, and the
        # guest keeps it until told otherwise -- so a guest left holding one
        # has a monitor in its desktop that nothing is attached to, and a
        # desktop that remembers such a layout can come back to it on the
        # next boot with its session on a screen that is not there. Giving
        # them back needs a live channel, so it has to happen while there
        # still is one.
        self.give_back_heads()
        self._closed = True
        disconnect_signal(self._toplevel, self._toplevel_handler)
        self._toplevel_handler = None
        if getattr(self, "usb", None) is not None:
            # Before the session goes: a redirected device is only handed
            # back to the host while there is still a channel to say so on.
            with contextlib.suppress(Exception):
                self.usb.shutdown()
            self.usb = None
        if getattr(self, "session", None) is not None:
            session_disconnect(self.session)
        if self._ca_file and os.path.exists(self._ca_file):
            with contextlib.suppress(OSError):
                os.unlink(self._ca_file)
            self._ca_file = None

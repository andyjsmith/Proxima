"""GTK widget wrapping the RFB client.

Used for guests with no SPICE display -- Proxmox always offers VNC, so this
is the universal fallback. The tab shows a warning badge when it is in use so
the degraded path is never silently mistaken for the good one.

Rendering: the RFB client writes into a bytearray whose memory is wrapped by
a cairo ImageSurface, so a frame update is a memcpy into the surface's own
storage followed by mark_dirty_rectangle. No conversion, no reallocation per
frame.
"""

import contextlib
import logging
import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk

try:
    import cairo
except ImportError:  # pragma: no cover
    cairo = None

from .keys import CTRL_ALT_DEL
from .rfb import RfbClient
from .status_panel import (
    CONNECTING_ICON,
    CONNECTING_TITLE,
    ConsoleStatusPanel,
    draw_offline_effect,
)
from .wsclient import WebSocketStream

log = logging.getLogger(__name__)

AVAILABLE = cairo is not None

# GDK keyvals are X11 keysyms by definition, so key events need no mapping
# table at all. The ones we synthesise rather than receive come from
# console/keys.py, which both protocols share.


class VncConsole(Gtk.Box):
    """A VNC console tab: toolbar, framebuffer view, status."""

    protocol = "vnc"
    agent_connected = False
    pending = False

    # VNC has no guest-resize and no codec negotiation, so those view-menu
    # entries are disabled while a VNC tab is active.
    supports = {
        "auto_resize": False,
        "scaling": True,
        "codec": False,
        "compression": False,
        "refresh": True,
        "ctrl_alt_del": True,
        # RFB carries no audio at all, and Proxmox's VNC proxy has
        # no clipboard channel either. USB redirection is a SPICE
        # protocol extension, so there is nothing to carry it here.
        "clipboard": False,
        "audio": False,
        "usb": False,
    }

    def __init__(
        self,
        url,
        headers,
        password,
        title="console",
        on_status=None,
        verify_ssl=False,
        scale_to_fit=True,
        on_disconnect=None,
        on_reconnect=None,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        self.title = title
        self.on_status = on_status or (lambda text: None)
        self.last_status = ""
        self.scaling = scale_to_fit
        self.on_disconnect = on_disconnect or (lambda reason: None)
        self.on_reconnect = on_reconnect or (lambda: None)
        self.connected = False
        self.client = None
        self._surface = None
        self._buffer = None
        self._closed = False
        self._button_mask = 0
        self._last_pos = (0, 0)
        self._pending_damage = False
        self._last_sample = None
        self._last_bytes = 0
        self._last_frames = 0
        self._last_encodings = {}
        self._encoding = ""

        if not AVAILABLE:
            self.pack_start(
                Gtk.Label(
                    label="pycairo is required for the VNC console.\n"
                    "Install mingw-w64-ucrt-x86_64-python-cairo"
                ),
                True,
                True,
                0,
            )
            return

        self.area = Gtk.DrawingArea()
        self.area.set_can_focus(True)
        self.area.set_hexpand(True)
        self.area.set_vexpand(True)
        self.area.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.SCROLL_MASK
            | Gdk.EventMask.SMOOTH_SCROLL_MASK
            | Gdk.EventMask.KEY_PRESS_MASK
            | Gdk.EventMask.KEY_RELEASE_MASK
            | Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.FOCUS_CHANGE_MASK
        )

        self.area.connect("draw", self._on_draw)
        self.area.connect("button-press-event", self._on_button)
        self.area.connect("button-release-event", self._on_button)
        self.area.connect("motion-notify-event", self._on_motion)
        self.area.connect("scroll-event", self._on_scroll)
        self.area.connect("key-press-event", self._on_key)
        self.area.connect("key-release-event", self._on_key)
        self.area.connect("enter-notify-event", lambda w, e: (w.grab_focus(), False)[1])

        self.overlay = Gtk.Overlay()
        self.overlay.add(self.area)
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

        self._status("connecting...")
        self._connect(url, headers, password, verify_ssl)

    # -- setup ---------------------------------------------------------

    def _connect(self, url, headers, password, verify_ssl):
        def worker():
            try:
                stream = WebSocketStream(url, headers=headers, verify_ssl=verify_ssl)
            except Exception as exc:
                GLib.idle_add(self._disconnected, f"{exc}")
                return
            client = RfbClient(
                stream,
                password=password,
                on_resize=lambda w, h: GLib.idle_add(self._on_resize, w, h),
                on_damage=self._queue_damage,
                on_status=lambda text: GLib.idle_add(self._status, text),
                on_error=lambda text: GLib.idle_add(self._status, f"error: {text}"),
                on_bell=lambda: GLib.idle_add(self._on_bell),
                on_closed=lambda reason: GLib.idle_add(self._disconnected, reason),
                name=self.title,
            )
            self.client = client
            client.start()

        import threading

        threading.Thread(
            target=worker, daemon=True, name=f"vnc-connect-{self.title}"
        ).start()

    # -- rendering -----------------------------------------------------

    def _rebuild_surface(self):
        """Point the cairo surface at the client's current framebuffer.

        Must be called with fb_lock held. Returns whether there is a usable
        surface afterwards.
        """
        width, height = self.client.width, self.client.height
        if not width or not height:
            self._surface = None
            return False
        self._buffer = self.client.framebuffer
        stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_RGB24, width)
        if stride != width * 4:
            # A 32bpp surface is always 4-byte aligned in practice; if
            # cairo ever disagrees, fall back to a copy per frame rather
            # than hand it a mis-strided buffer.
            self._surface = None
            self._status(f"unexpected cairo stride {stride} for {width}px")
            return False
        self._surface = cairo.ImageSurface.create_for_data(
            memoryview(self._buffer), cairo.FORMAT_RGB24, width, height, stride
        )
        return True

    def _sync_surface(self):
        """Rebuild the surface if the guest has changed resolution.

        A guest can resize at any moment -- another client with guest resize
        enabled will do it the instant its window changes size -- and the
        RFB thread swaps the framebuffer for a new one of the new size
        immediately, while the resize callback only reaches the main loop on
        the next idle. Anything reading the geometry in between sees the old
        surface against the new framebuffer.

        That gap is what puts the pointer somewhere other than where the
        picture says it is: drawing and pointer mapping both derive from the
        surface, so they agree with each other but not with the guest. This
        closes it by making every reader check first, rather than trusting
        that the idle has already run.
        """
        if self._closed or self.client is None or cairo is None:
            return False
        with self.client.fb_lock:
            if (
                self._surface is not None
                and self._surface.get_width() == self.client.width
                and self._surface.get_height() == self.client.height
                and self._buffer is self.client.framebuffer
            ):
                return True
            return self._rebuild_surface()

    def _on_resize(self, width, height):
        if self._closed or self.client is None:
            return False
        with self.client.fb_lock:
            if not self._rebuild_surface():
                return False
        # A modest floor, not the guest's resolution. Asking for the full
        # framebuffer makes the drawing area's *minimum* size larger than the
        # tab it lives in, and a widget allocated more space than its parent
        # can show is drawn partly off the edge -- where it is invisible and
        # the pointer cannot reach it. Shrinking to fit is handled in
        # _scale_factors instead.
        self.area.set_size_request(min(width, 800), min(height, 600))
        self.connected = True
        self.status_panel.hide_message()
        self.area.queue_draw()
        return False

    def _queue_damage(self, x, y, width, height):
        """Called from the RFB thread; coalesce into one redraw per frame."""
        if self._pending_damage or self._closed:
            return
        self._pending_damage = True
        GLib.idle_add(self._flush_damage)

    def _flush_damage(self):
        self._pending_damage = False
        if self._closed or self._surface is None:
            return False
        self._surface.mark_dirty()
        self.area.queue_draw()
        return False

    def set_scaling(self, enabled):
        self.scaling = enabled
        self.area.queue_draw()

    def refresh_framebuffer(self):
        if self.client is not None:
            self.client.request_update(incremental=False)

    def telemetry(self):
        """Throughput, frame rate and guest resolution.

        No round-trip figure: RFB is request/response and the server holds
        an incremental update until something changes, so timing one would
        measure guest idleness rather than network latency.
        """
        if self.client is None or not self.client.width:
            return None

        now = time.monotonic()
        total, frames = self.client.bytes_in, self.client.frames
        counts = dict(self.client.encoding_counts)
        rate = fps = None
        if self._last_sample is not None and now > self._last_sample:
            elapsed = now - self._last_sample
            rate = (total - self._last_bytes) / elapsed
            fps = (frames - self._last_frames) / elapsed
        self._last_sample, self._last_bytes, self._last_frames = (now, total, frames)

        # Which encoding the server chose since the last sample, which is
        # not necessarily any of the ones we asked for.
        recent = {
            name: count - self._last_encodings.get(name, 0)
            for name, count in counts.items()
        }
        recent = {name: count for name, count in recent.items() if count > 0}
        self._last_encodings = counts
        encoding = max(recent, key=recent.get) if recent else self._encoding
        self._encoding = encoding

        return {
            "rate": rate,
            "fps": fps,
            "codec": encoding,
            "size": f"{self.client.width}x{self.client.height}",
        }

    def _guest_size(self):
        """The framebuffer's size, as the surface actually holds it.

        Taken from the surface rather than from client.width/height: a guest
        that changes resolution updates the client's idea of the size on the
        RFB thread, while the surface is rebuilt later on the main loop. In
        that gap the two disagree, and mapping a click through the wrong one
        puts the guest pointer somewhere else entirely -- which is exactly
        what a Windows guest does at boot, switching resolution when the
        display driver loads.
        """
        self._sync_surface()
        if self._surface is None:
            return 0, 0
        return self._surface.get_width(), self._surface.get_height()

    def _scale_factors(self):
        """(scale, offset_x, offset_y) mapping guest pixels to widget pixels.

        The one place the geometry is worked out; both drawing and pointer
        mapping go through it, so the picture and the pointer cannot drift
        apart.
        """
        guest_w, guest_h = self._guest_size()
        if not guest_w or not guest_h:
            return 1.0, 0.0, 0.0
        allocation = self.area.get_allocation()
        if self.scaling:
            scale = min(allocation.width / guest_w, allocation.height / guest_h)
        else:
            # Never enlarge, but do shrink to fit rather than letting the
            # guest screen run off the edge of the tab: the part that hangs
            # off is both invisible and unreachable with the pointer.
            scale = min(1.0, allocation.width / guest_w, allocation.height / guest_h)
        offset_x = (allocation.width - guest_w * scale) / 2
        offset_y = (allocation.height - guest_h * scale) / 2
        return scale, offset_x, offset_y

    def _on_draw(self, _widget, context):
        # Before the lock: a guest that resized since the last frame needs a
        # new surface, and drawing the old one would show the picture at a
        # size the pointer no longer agrees with.
        self._sync_surface()
        if self._surface is None:
            return False
        with self.client.fb_lock:
            scale, offset_x, offset_y = self._scale_factors()
            context.save()
            context.translate(offset_x, offset_y)
            context.scale(scale, scale)
            context.set_source_surface(self._surface, 0, 0)
            pattern = context.get_source()
            # GOOD is bilinear; at non-integer scales NEAREST turns small
            # text in the guest into noise.
            pattern.set_filter(
                cairo.FILTER_GOOD if scale != 1.0 else cairo.FILTER_NEAREST
            )
            context.paint()
            context.restore()

        if not self.connected or self.pending:
            # The frame is stale or about to be; grey it so it does not read
            # as live.
            allocation = self.area.get_allocation()
            draw_offline_effect(context, allocation.width, allocation.height)
        return False

    # -- input ---------------------------------------------------------

    def _widget_to_guest(self, x, y):
        scale, offset_x, offset_y = self._scale_factors()
        guest_w, guest_h = self._guest_size()
        if scale <= 0 or not guest_w:
            return self._last_pos
        # Clamped to the image rather than to the widget: with a letterboxed
        # console the margins are not part of the guest screen, and mapping
        # them through would push the pointer past the edge.
        guest_x = min(max(int((x - offset_x) / scale), 0), guest_w - 1)
        guest_y = min(max(int((y - offset_y) / scale), 0), guest_h - 1)
        self._last_pos = (guest_x, guest_y)
        return self._last_pos

    def _on_button(self, widget, event):
        if self.client is None:
            return False
        widget.grab_focus()
        if event.button > 7:
            return False
        bit = 1 << (event.button - 1)
        if event.type == Gdk.EventType.BUTTON_PRESS:
            self._button_mask |= bit
        elif event.type == Gdk.EventType.BUTTON_RELEASE:
            self._button_mask &= ~bit
        else:
            return False  # ignore the synthetic double/triple click events
        guest_x, guest_y = self._widget_to_guest(event.x, event.y)
        self.client.send_pointer(guest_x, guest_y, self._button_mask)
        return True

    def _on_motion(self, _widget, event):
        if self.client is None:
            return False
        guest_x, guest_y = self._widget_to_guest(event.x, event.y)
        self.client.send_pointer(guest_x, guest_y, self._button_mask)
        return True

    def _on_scroll(self, _widget, event):
        if self.client is None:
            return False
        # RFB models the wheel as buttons 4-7, pressed and released.
        direction = event.direction
        if direction == Gdk.ScrollDirection.SMOOTH:
            _, delta_x, delta_y = event.get_scroll_deltas()
            if delta_y < 0:
                button = 4
            elif delta_y > 0:
                button = 5
            elif delta_x < 0:
                button = 6
            elif delta_x > 0:
                button = 7
            else:
                return True
        else:
            button = {
                Gdk.ScrollDirection.UP: 4,
                Gdk.ScrollDirection.DOWN: 5,
                Gdk.ScrollDirection.LEFT: 6,
                Gdk.ScrollDirection.RIGHT: 7,
            }.get(direction)
            if button is None:
                return False

        guest_x, guest_y = self._widget_to_guest(event.x, event.y)
        bit = 1 << (button - 1)
        self.client.send_pointer(guest_x, guest_y, self._button_mask | bit)
        self.client.send_pointer(guest_x, guest_y, self._button_mask)
        return True

    def _on_key(self, _widget, event):
        if self.client is None:
            return False
        pressed = event.type == Gdk.EventType.KEY_PRESS
        self.client.send_key(event.keyval, pressed)
        # Swallow the event so GTK does not also drive focus navigation with
        # Tab or trigger mnemonics while the guest has the keyboard.
        return True

    def send_keys(self, keyvals):
        """Press and release a combination in the guest.

        RFB carries keysyms directly, so anything the Send Key menu offers
        works here as well as it does over SPICE -- which matters, because
        VNC is what a guest without a SPICE display gets.
        """
        if self.client is None:
            self._status("not connected")
            return False
        try:
            self.client.send_key_click(list(keyvals))
            return True
        except Exception as exc:
            self._status(f"could not send keys: {exc}")
            return False

    def send_ctrl_alt_del(self):
        return self.send_keys(CTRL_ALT_DEL)

    def grab_focus_display(self):
        if getattr(self, "area", None) is not None:
            self.area.grab_focus()

    def release_input(self):
        """Stop routing the keyboard to the guest.

        There is no pointer grab to drop -- this client tracks motion
        without confining the pointer -- so releasing input means dropping
        keyboard focus, which is what stops keystrokes reaching the guest.
        """
        if self.client is not None and self._button_mask:
            # Let go of any buttons the guest still thinks are held, at the
            # last known position so the guest pointer does not jump.
            self._button_mask = 0
            self.client.send_pointer(*self._last_pos, 0)
        toplevel = self.get_toplevel()
        if isinstance(toplevel, Gtk.Window):
            toplevel.set_focus(None)

    def _on_bell(self):
        window = self.get_toplevel()
        if isinstance(window, Gtk.Window):
            window.get_window().beep() if window.get_window() else None
        return False

    # -- lifecycle -----------------------------------------------------

    def _status(self, text):
        self.last_status = text
        self.on_status(text)
        log.info("%s: %s", self.title, text)
        return False

    def screenshot(self, path):
        """Write the current framebuffer to a PNG."""
        if self._surface is None or self.client is None:
            return False
        with self.client.fb_lock:
            self._surface.flush()
            self._surface.write_to_png(path)
        return True

    def show_pending_state(self, title, detail=""):
        """An action has been asked for but the guest has not moved yet."""
        if self._closed or getattr(self, "status_panel", None) is None:
            return
        self.pending = True
        self.status_panel.show_message(title, detail, can_reconnect=False, busy=True)
        if self.area is not None:
            self.area.queue_draw()

    def clear_pending_state(self):
        if not self.pending:
            return
        self.pending = False
        if self.connected:
            self.status_panel.hide_message()
        if self.area is not None:
            self.area.queue_draw()

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
        if self.area is not None:
            self.area.queue_draw()

    def _disconnected(self, reason):
        """Show the panel rather than leaving the last frame frozen."""
        if self._closed or getattr(self, "status_panel", None) is None:
            return False
        was_connected, self.connected = self.connected, False
        self.status_panel.show_message("Connection closed", reason)
        if self.area is not None:
            self.area.queue_draw()
        self._status(reason)
        if was_connected:
            self.on_disconnect(reason)
        return False

    def shutdown(self):
        self._closed = True
        self._surface = None
        if self.client is not None:
            self.client.stop()
            self.client = None

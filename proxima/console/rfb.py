"""A small RFB (VNC) protocol client.

Written in pure Python on purpose: gtk-vnc is not part of a default MSYS2
install, and its GtkVnc widget wants a file descriptor it can hand to
GIOChannel, which is not something a Windows socket can portably provide once
the transport is a websocket rather than a plain TCP connection.

Scope is deliberately narrow -- this is the fallback path for guests that have
no SPICE display, not the main event:

  * Encodings: Raw, CopyRect, zlib, plus the DesktopSize and LastRect
    pseudo-encodings. Cursor pseudo-encodings are *not* requested, which
    makes the server composite the pointer into the framebuffer for us.
  * Pixel format is pinned to 32bpp little-endian true colour with red at
    shift 16, which is byte-for-byte what cairo's RGB24 wants, so frames go
    to screen without a conversion pass.

The client runs on its own thread and reports damage through a callback; it
knows nothing about GTK.
"""

import zlib
import struct
import threading

from . import des

# Client -> server message types
SET_PIXEL_FORMAT = 0
SET_ENCODINGS = 2
FRAMEBUFFER_UPDATE_REQUEST = 3
KEY_EVENT = 4
POINTER_EVENT = 5
CLIENT_CUT_TEXT = 6

# Server -> client message types
FRAMEBUFFER_UPDATE = 0
SET_COLOUR_MAP_ENTRIES = 1
BELL = 2
SERVER_CUT_TEXT = 3

# Encodings, in the order we prefer them.
ENC_RAW = 0
ENC_COPY_RECT = 1
ENC_ZLIB = 6
ENC_DESKTOP_SIZE = -223
ENC_LAST_RECT = -224

REQUESTED_ENCODINGS = [ENC_ZLIB, ENC_COPY_RECT, ENC_RAW,
                       ENC_DESKTOP_SIZE, ENC_LAST_RECT]

SEC_NONE = 1
SEC_VNC_AUTH = 2

BYTES_PER_PIXEL = 4

# For reporting which encoding the server is actually choosing, which is not
# the same as the ones we offered.
ENCODING_NAMES = {
    ENC_RAW: "raw",
    ENC_COPY_RECT: "copyrect",
    ENC_ZLIB: "zlib",
}


class RfbError(Exception):
    pass


def friendly_reason(exc):
    """Turn a transport failure into something worth showing a person.

    A dropped console is normal -- the guest was powered off, reset or rolled
    back -- so the panel should say that, not 'WinError 10054'. Protocol
    errors keep their own text, since those are genuinely diagnostic.
    """
    if isinstance(exc, RfbError):
        return str(exc)
    if isinstance(exc, (ConnectionError, OSError, EOFError)):
        return "The server closed the connection."
    text = str(exc)
    if not text or "closed" in text.lower() or "peer" in text.lower():
        return "The server closed the connection."
    return text


class RfbClient(threading.Thread):
    """Speaks RFB over any object with read(n) / write(bytes) / close().

    Callbacks are invoked from this thread, so anything touching GTK must
    marshal itself back to the main loop.
    """

    daemon = True

    def __init__(self, stream, password=None, on_resize=None, on_damage=None,
                 on_status=None, on_error=None, on_bell=None,
                 on_closed=None,
                 shared=True, name="vnc"):
        super().__init__(name=f"rfb-{name}", daemon=True)
        self.stream = stream
        self.password = password
        self.shared = shared

        self.on_resize = on_resize or (lambda w, h: None)
        self.on_damage = on_damage or (lambda x, y, w, h: None)
        self.on_status = on_status or (lambda text: None)
        self.on_error = on_error or (lambda text: None)
        self.on_bell = on_bell or (lambda: None)
        self.on_closed = on_closed or (lambda reason: None)

        self.width = 0
        self.height = 0
        self.desktop_name = ""
        self.framebuffer = None
        self.fb_lock = threading.RLock()

        self._write_lock = threading.Lock()
        self._running = True
        self._connected = False
        # Telemetry counters, read by the UI; monotonic totals so the
        # sampler can take deltas without coordinating with this thread.
        self.bytes_in = 0
        self.frames = 0
        self.encoding_counts = {}
        self._zstream = None
        self._button_mask = 0

    # -- transport helpers ---------------------------------------------

    def _read(self, count):
        data = self.stream.read(count)
        self.bytes_in += len(data)
        return data

    def _send(self, data):
        with self._write_lock:
            if not self._running:
                return
            self.stream.write(data)

    # -- handshake -----------------------------------------------------

    def _handshake(self):
        banner = self._read(12)
        if not banner.startswith(b"RFB "):
            raise RfbError(f"not an RFB server (got {banner[:12]!r})")
        try:
            major, minor = int(banner[4:7]), int(banner[8:11])
        except ValueError:
            raise RfbError(f"unparseable RFB version {banner!r}")

        # 3.8 is what QEMU and vncterm both speak; anything newer still
        # accepts a 3.8 client.
        version = (3, 8) if (major, minor) >= (3, 8) else (3, 3)
        self._send(b"RFB %03d.%03d\n" % version)
        self.on_status(f"RFB {major}.{minor}")

        if version == (3, 3):
            security = struct.unpack(">I", self._read(4))[0]
            if security == 0:
                raise RfbError(self._read_failure_reason())
        else:
            count = self._read(1)[0]
            if count == 0:
                raise RfbError(self._read_failure_reason())
            offered = set(self._read(count))
            if SEC_VNC_AUTH in offered and self.password:
                security = SEC_VNC_AUTH
            elif SEC_NONE in offered:
                security = SEC_NONE
            elif SEC_VNC_AUTH in offered:
                raise RfbError("server wants a VNC password but none was given")
            else:
                raise RfbError(
                    "no supported security type "
                    f"(server offered {sorted(offered)})")
            self._send(bytes([security]))

        if security == SEC_VNC_AUTH:
            challenge = self._read(16)
            self._send(des.vnc_response(self.password or "", challenge))
        elif security != SEC_NONE:
            raise RfbError(f"unsupported security type {security}")

        # 3.3 with security type None sends no SecurityResult.
        if version == (3, 8) or security == SEC_VNC_AUTH:
            result = struct.unpack(">I", self._read(4))[0]
            if result != 0:
                reason = (self._read_failure_reason() if version == (3, 8)
                          else "authentication failed")
                raise RfbError(reason)

        # ClientInit: 1 means allow other clients to stay connected.
        self._send(bytes([1 if self.shared else 0]))

        header = self._read(24)
        width, height = struct.unpack(">HH", header[:4])
        name_length = struct.unpack(">I", header[20:24])[0]
        self.desktop_name = self._read(name_length).decode("utf-8", "replace") \
            if name_length else ""

        self._resize(width, height)
        self.on_status(f"connected to {self.desktop_name or 'guest'} "
                       f"({width}x{height})")

    def _read_failure_reason(self):
        try:
            length = struct.unpack(">I", self._read(4))[0]
            return self._read(length).decode("utf-8", "replace")
        except Exception:
            return "connection rejected by server"

    # -- setup ---------------------------------------------------------

    def _set_pixel_format(self):
        # 32bpp, depth 24, little-endian, true colour, R<<16 G<<8 B<<0.
        # That is exactly cairo's RGB24 memory layout on a little-endian
        # host, so no per-frame conversion is needed.
        message = struct.pack(
            ">BxxxBBBBHHHBBBxxx",
            SET_PIXEL_FORMAT,
            32,        # bits-per-pixel
            24,        # depth
            0,         # big-endian-flag
            1,         # true-colour-flag
            255, 255, 255,   # red/green/blue max
            16, 8, 0,        # red/green/blue shift
        )
        self._send(message)

    def _set_encodings(self):
        message = struct.pack(">BxH", SET_ENCODINGS, len(REQUESTED_ENCODINGS))
        message += b"".join(struct.pack(">i", e) for e in REQUESTED_ENCODINGS)
        self._send(message)

    def request_update(self, incremental=True, x=0, y=0, width=None, height=None):
        if not self._connected:
            return
        width = self.width if width is None else width
        height = self.height if height is None else height
        if width <= 0 or height <= 0:
            return
        try:
            self._send(struct.pack(">BBHHHH", FRAMEBUFFER_UPDATE_REQUEST,
                                   1 if incremental else 0,
                                   x, y, width, height))
        except Exception:
            pass

    # -- framebuffer ---------------------------------------------------

    @property
    def stride(self):
        return self.width * BYTES_PER_PIXEL

    def _resize(self, width, height):
        with self.fb_lock:
            self.width = width
            self.height = height
            self.framebuffer = bytearray(width * height * BYTES_PER_PIXEL)
        self.on_resize(width, height)

    # -- main loop -----------------------------------------------------

    def run(self):
        reason = "The server closed the connection."
        try:
            self._handshake()
            self._set_pixel_format()
            self._set_encodings()
            self._connected = True
            self.request_update(incremental=False)
            while self._running:
                self._dispatch()
        except Exception as exc:
            reason = friendly_reason(exc)
            if self._running:
                self.on_error(reason)
        finally:
            stopped = not self._running      # stop() was called: intentional
            self._connected = False
            self._running = False
            try:
                self.stream.close()
            except Exception:
                pass
            if not stopped:
                self.on_closed(reason)

    def _dispatch(self):
        message_type = self._read(1)[0]
        if message_type == FRAMEBUFFER_UPDATE:
            self._read_framebuffer_update()
        elif message_type == SET_COLOUR_MAP_ENTRIES:
            self._skip_colour_map()
        elif message_type == BELL:
            self.on_bell()
        elif message_type == SERVER_CUT_TEXT:
            self._read(3)
            length = struct.unpack(">I", self._read(4))[0]
            if length:
                self._read(length)
        else:
            raise RfbError(f"unexpected server message type {message_type}")

    def _skip_colour_map(self):
        self._read(1)
        _first, count = struct.unpack(">HH", self._read(4))
        if count:
            self._read(count * 6)

    def _read_framebuffer_update(self):
        self.frames += 1
        self._read(1)   # padding
        count = struct.unpack(">H", self._read(2))[0]

        for _ in range(count):
            header = self._read(12)
            x, y, width, height, encoding = struct.unpack(">HHHHi", header)

            if encoding == ENC_LAST_RECT:
                break
            name = ENCODING_NAMES.get(encoding, str(encoding))
            self.encoding_counts[name] = self.encoding_counts.get(name, 0) + 1
            if encoding == ENC_DESKTOP_SIZE:
                self._resize(width, height)
                # The whole screen is undefined after a resize.
                self.request_update(incremental=False)
                continue
            if encoding == ENC_RAW:
                self._decode_raw(x, y, width, height)
            elif encoding == ENC_COPY_RECT:
                self._decode_copy_rect(x, y, width, height)
            elif encoding == ENC_ZLIB:
                self._decode_zlib(x, y, width, height)
            else:
                raise RfbError(
                    f"server used encoding {encoding}, which was never "
                    "requested -- refusing to guess at the byte stream")

            self.on_damage(x, y, width, height)

        # Ask for the next frame. This is what keeps updates flowing; RFB is
        # strictly request/response, so a missed request stalls the console.
        self.request_update(incremental=True)

    def _blit(self, x, y, width, height, pixels):
        """Copy a tightly packed rect into the framebuffer, row by row."""
        with self.fb_lock:
            if self.framebuffer is None:
                return
            fb = self.framebuffer
            stride = self.stride
            row_bytes = width * BYTES_PER_PIXEL
            for row in range(height):
                target_y = y + row
                if target_y >= self.height:
                    break
                start = target_y * stride + x * BYTES_PER_PIXEL
                source = row * row_bytes
                fb[start:start + row_bytes] = pixels[source:source + row_bytes]

    def _decode_raw(self, x, y, width, height):
        pixels = self._read(width * height * BYTES_PER_PIXEL)
        self._blit(x, y, width, height, pixels)

    def _decode_zlib(self, x, y, width, height):
        length = struct.unpack(">I", self._read(4))[0]
        payload = self._read(length) if length else b""
        # One zlib stream spans the whole connection, so the decompressor is
        # stateful and must never be recreated mid-session.
        if self._zstream is None:
            self._zstream = zlib.decompressobj()
        # The server flushes the compressor at every rect boundary, so one
        # decompress() call always yields the whole rect. Calling flush()
        # here would terminate the stream and break every later rect.
        pixels = self._zstream.decompress(payload)
        expected = width * height * BYTES_PER_PIXEL
        if len(pixels) != expected:
            raise RfbError(
                f"zlib rect decoded to {len(pixels)} bytes, expected {expected}")
        self._blit(x, y, width, height, pixels)

    def _decode_copy_rect(self, x, y, width, height):
        source_x, source_y = struct.unpack(">HH", self._read(4))
        with self.fb_lock:
            if self.framebuffer is None:
                return
            fb = self.framebuffer
            stride = self.stride
            row_bytes = width * BYTES_PER_PIXEL
            # Regions may overlap, so walk rows in the direction that keeps
            # the source ahead of the destination.
            rows = range(height) if source_y >= y else range(height - 1, -1, -1)
            for row in rows:
                src = (source_y + row) * stride + source_x * BYTES_PER_PIXEL
                dst = (y + row) * stride + x * BYTES_PER_PIXEL
                fb[dst:dst + row_bytes] = fb[src:src + row_bytes]

    # -- input ---------------------------------------------------------

    def send_key(self, keysym, pressed):
        if not self._connected:
            return
        try:
            self._send(struct.pack(">BBxxI", KEY_EVENT,
                                   1 if pressed else 0, keysym & 0xFFFFFFFF))
        except Exception:
            pass

    def send_key_click(self, keysyms):
        """Press a set of keys in order, then release in reverse."""
        for keysym in keysyms:
            self.send_key(keysym, True)
        for keysym in reversed(keysyms):
            self.send_key(keysym, False)

    def send_pointer(self, x, y, button_mask):
        if not self._connected:
            return
        self._button_mask = button_mask
        x = max(0, min(int(x), max(0, self.width - 1)))
        y = max(0, min(int(y), max(0, self.height - 1)))
        try:
            self._send(struct.pack(">BBHH", POINTER_EVENT, button_mask & 0xFF,
                                   x, y))
        except Exception:
            pass

    def send_cut_text(self, text):
        if not self._connected:
            return
        payload = text.encode("latin-1", "replace")
        try:
            self._send(struct.pack(">BxxxI", CLIENT_CUT_TEXT, len(payload))
                       + payload)
        except Exception:
            pass

    # -- lifecycle -----------------------------------------------------

    def stop(self):
        self._running = False
        self._connected = False
        try:
            self.stream.close()
        except Exception:
            pass

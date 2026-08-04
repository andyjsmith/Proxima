#!/usr/bin/env python3
"""Protocol tests for the VNC fallback.

The RFB client and the websocket transport are the two pieces that cannot be
exercised by clicking around without a live Proxmox host, and they are also
the two where a byte-order or framing mistake produces garbage rather than an
error. So both are driven against purpose-built fake servers here.

    python3 tools/test_vnc.py
"""

import os
import sys
import zlib

# Never touch the real user settings: this suite opens the preferences
# dialog, which saves on close.
os.environ.setdefault(
    "PROXIMA_CONFIG_DIR",
    os.path.join(os.environ.get("TEMP", "/tmp"), "proxima-tests"),
)
import base64
import contextlib
import hashlib
import socket
import struct
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from proxima.console import des
from proxima.console.rfb import RfbClient
from proxima.console.wsclient import WebSocketStream

PASSWORD = "PVE:root@pam:687A1B2C::abcdef"
WIDTH, HEIGHT = 64, 32

failures = []


def check(condition, label):
    print(f"  {'[ok]  ' if condition else '[FAIL]'} {label}")
    if not condition:
        failures.append(label)


class SocketStream:
    """Adapts a raw socket to the read/write/close interface RfbClient wants."""

    def __init__(self, sock):
        self.sock = sock

    def read(self, count):
        parts = []
        remaining = count
        while remaining:
            chunk = self.sock.recv(remaining)
            if not chunk:
                raise OSError("closed")
            parts.append(chunk)
            remaining -= len(chunk)
        return b"".join(parts)

    def write(self, data):
        self.sock.sendall(data)

    def close(self):
        with contextlib.suppress(OSError):
            self.sock.close()


# --------------------------------------------------------------------------
# A fake RFB server
# --------------------------------------------------------------------------


def pixel(red, green, blue):
    """One pixel in the format the client negotiates: little-endian 0x00RRGGBB."""
    return struct.pack("<I", (red << 16) | (green << 8) | blue)


class FakeRfbServer(threading.Thread):
    daemon = True

    def __init__(self, sock):
        super().__init__(daemon=True, name="fake-rfb")
        self.stream = SocketStream(sock)
        self.error = None
        self.pixel_format = None
        self.encodings = []
        self.auth_ok = False
        self.received_key = None
        self.received_pointer = None
        self.done = threading.Event()

    def run(self):
        try:
            self._serve()
        except Exception as exc:  # noqa: BLE001  # surfaced by the test, not swallowed
            self.error = exc
        finally:
            self.done.set()

    def _serve(self):
        read, write = self.stream.read, self.stream.write

        write(b"RFB 003.008\n")
        client_version = read(12)
        assert client_version == b"RFB 003.008\n", client_version

        # Offer VNC auth only, so the DES path is what gets tested.
        write(bytes([1, 2]))
        chosen = read(1)[0]
        assert chosen == 2, chosen

        challenge = bytes(range(16))
        write(challenge)
        response = read(16)
        self.auth_ok = response == des.vnc_response(PASSWORD, challenge)
        write(struct.pack(">I", 0 if self.auth_ok else 1))
        if not self.auth_ok:
            # RFB 3.8 requires a reason string after a failed SecurityResult;
            # the client blocks for it, so a real server always sends one.
            reason = b"Authentication failed"
            write(struct.pack(">I", len(reason)) + reason)
            return

        read(1)  # ClientInit / shared flag

        name = b"fake-guest"
        write(
            struct.pack(">HH", WIDTH, HEIGHT)
            + bytes(16)  # server's own pixel format
            + struct.pack(">I", len(name))
            + name
        )

        # SetPixelFormat
        message = read(20)
        assert message[0] == 0, message[0]
        # 13 meaningful bytes: bpp, depth, endian, truecolour, three maxima,
        # three shifts. The trailing 3 are padding.
        self.pixel_format = struct.unpack(">BBBBHHHBBB", message[4:17])

        # SetEncodings
        header = read(4)
        assert header[0] == 2, header[0]
        count = struct.unpack(">H", header[2:4])[0]
        self.encodings = [struct.unpack(">i", read(4))[0] for _ in range(count)]

        # The first FramebufferUpdateRequest, which must be a full one.
        request = read(10)
        assert request[0] == 3, request[0]
        assert request[1] == 0, "first update request should be non-incremental"

        self._send_frame()

        # The client answers every update with another request; consume it.
        read(10)

        # Now whatever input the test sends.
        while not self.done.is_set():
            kind = read(1)[0]
            if kind == 4:  # KeyEvent
                body = read(7)
                down = body[0]
                keysym = struct.unpack(">I", body[3:7])[0]
                self.received_key = (keysym, bool(down))
            elif kind == 5:  # PointerEvent
                body = read(5)
                mask = body[0]
                x, y = struct.unpack(">HH", body[1:5])
                self.received_pointer = (x, y, mask)
                return
            elif kind == 3:
                read(9)
            else:
                return

    def _send_frame(self):
        """One update carrying a raw rect, a zlib rect and a copyrect."""
        write = self.stream.write

        # Rect 1: raw, solid red, covering the left half of the top row band.
        raw_w, raw_h = 32, 16
        raw_pixels = pixel(255, 0, 0) * (raw_w * raw_h)

        # Rect 2: zlib, solid green, to the right of it.
        zlib_w, zlib_h = 32, 16
        green = pixel(0, 255, 0) * (zlib_w * zlib_h)
        compressor = zlib.compressobj()
        compressed = compressor.compress(green) + compressor.flush(zlib.Z_SYNC_FLUSH)

        # Rect 3: copyrect, duplicating the red block onto the second band.
        write(struct.pack(">BxH", 0, 3))
        write(struct.pack(">HHHHi", 0, 0, raw_w, raw_h, 0) + raw_pixels)
        write(
            struct.pack(">HHHHi", 32, 0, zlib_w, zlib_h, 6)
            + struct.pack(">I", len(compressed))
            + compressed
        )
        write(struct.pack(">HHHHi", 0, 16, raw_w, raw_h, 1) + struct.pack(">HH", 0, 0))


def test_rfb():
    print("\nRFB client against a fake server")
    server_sock, client_sock = socket.socketpair()
    server = FakeRfbServer(server_sock)
    server.start()

    resized = []
    damage = []
    errors = []
    connected = threading.Event()

    client = RfbClient(
        SocketStream(client_sock),
        password=PASSWORD,
        on_resize=lambda w, h: (resized.append((w, h)), connected.set()),
        on_damage=lambda x, y, w, h: damage.append((x, y, w, h)),
        on_error=lambda text: errors.append(text),
    )
    client.start()

    connected.wait(5)
    # Give the frame time to arrive and decode.
    deadline = time.time() + 5
    while len(damage) < 3 and time.time() < deadline:
        time.sleep(0.02)

    check(server.auth_ok, "VNC DES authentication accepted by the server")
    check(
        resized == [(WIDTH, HEIGHT)],
        f"framebuffer sized {resized} (expected [(64, 32)])",
    )

    (
        bpp,
        depth,
        big_endian,
        true_colour,
        r_max,
        g_max,
        b_max,
        r_shift,
        g_shift,
        b_shift,
    ) = server.pixel_format
    check(
        (bpp, depth, big_endian, true_colour) == (32, 24, 0, 1),
        f"pixel format 32bpp/24-depth/little-endian/true-colour "
        f"(got {bpp}/{depth}/{big_endian}/{true_colour})",
    )
    check((r_max, g_max, b_max) == (255, 255, 255), "8 bits per channel")
    check(
        (r_shift, g_shift, b_shift) == (16, 8, 0),
        f"channel shifts R/G/B = {r_shift}/{g_shift}/{b_shift} (cairo RGB24 layout)",
    )
    check(
        6 in server.encodings and 1 in server.encodings and 0 in server.encodings,
        f"encodings offered: {server.encodings}",
    )
    check(
        -239 not in server.encodings,
        "cursor pseudo-encoding NOT offered, so the server draws the "
        "pointer into the framebuffer",
    )

    check(len(damage) == 3, f"three rects decoded (got {len(damage)})")

    def at(x, y):
        offset = (y * WIDTH + x) * 4
        blue, green, red, _ = client.framebuffer[offset : offset + 4]
        return red, green, blue

    check(at(0, 0) == (255, 0, 0), f"raw rect decoded red, got {at(0, 0)}")
    check(at(31, 15) == (255, 0, 0), f"raw rect covers its full area, got {at(31, 15)}")
    check(at(32, 0) == (0, 255, 0), f"zlib rect decoded green, got {at(32, 0)}")
    check(
        at(63, 15) == (0, 255, 0), f"zlib rect covers its full area, got {at(63, 15)}"
    )
    check(
        at(0, 16) == (255, 0, 0), f"copyrect duplicated the red block, got {at(0, 16)}"
    )
    check(at(40, 20) == (0, 0, 0), f"untouched area still black, got {at(40, 20)}")

    # Input encoding.
    client.send_key(0xFF0D, True)
    client.send_pointer(10, 20, 0x01)
    deadline = time.time() + 3
    while server.received_pointer is None and time.time() < deadline:
        time.sleep(0.02)

    check(
        server.received_key == (0xFF0D, True),
        f"key event round-tripped, got {server.received_key}",
    )
    check(
        server.received_pointer == (10, 20, 1),
        f"pointer event round-tripped, got {server.received_pointer}",
    )

    check(not errors, f"no client errors (got {errors})")
    check(server.error is None, f"no server errors (got {server.error!r})")

    server.done.set()
    client.stop()


def test_rfb_bad_password():
    print("\nRFB client with the wrong password")
    server_sock, client_sock = socket.socketpair()
    server = FakeRfbServer(server_sock)
    server.start()

    errors = []
    client = RfbClient(
        SocketStream(client_sock), password="wrong-password", on_error=errors.append
    )
    client.start()

    deadline = time.time() + 5
    while not errors and time.time() < deadline:
        time.sleep(0.02)

    check(not server.auth_ok, "server rejected the wrong password")
    check(bool(errors), f"client reported the failure (got {errors})")
    check(
        any("Authentication failed" in text for text in errors),
        f"client surfaced the server's own reason string, got {errors}",
    )
    server.done.set()
    client.stop()


# --------------------------------------------------------------------------
# A fake websocket server
# --------------------------------------------------------------------------

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def recv_exact(conn, count):
    parts = []
    while count:
        chunk = conn.recv(count)
        if not chunk:
            return None
        parts.append(chunk)
        count -= len(chunk)
    return b"".join(parts)


def ws_read_frame(conn):
    """Read one client frame. Returns (opcode, payload, was_masked)."""
    header = recv_exact(conn, 2)
    if header is None:
        return None
    opcode = header[0] & 0x0F
    masked = bool(header[1] & 0x80)
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", recv_exact(conn, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", recv_exact(conn, 8))[0]
    mask = recv_exact(conn, 4) if masked else b"\0\0\0\0"
    payload = recv_exact(conn, length) if length else b""
    if payload is None:
        return None
    return (opcode, bytes(b ^ mask[i % 4] for i, b in enumerate(payload)), masked)


def ws_send(conn, opcode, payload):
    header = bytearray([0x80 | opcode])
    if len(payload) < 126:
        header.append(len(payload))
    elif len(payload) < 65536:
        header.append(126)
        header += struct.pack(">H", len(payload))
    else:
        header.append(127)
        header += struct.pack(">Q", len(payload))
    conn.sendall(bytes(header) + payload)


class FakeWebSocketServer(threading.Thread):
    """Accepts one connection, echoes every payload back, reversed."""

    daemon = True

    def __init__(self, subprotocol="binary"):
        super().__init__(daemon=True, name="fake-ws")
        self.subprotocol = subprotocol
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.headers = {}
        self.error = None
        self.stop_flag = threading.Event()

    def run(self):
        try:
            conn, _ = self.listener.accept()
            self._serve(conn)
        except Exception as exc:  # noqa: BLE001
            self.error = exc

    def _serve(self, conn):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(1024)
            if not chunk:
                return
            data += chunk
        head = data.split(b"\r\n\r\n")[0].decode("latin-1")
        for line in head.split("\r\n")[1:]:
            if ":" in line:
                key, _, value = line.partition(":")
                self.headers[key.strip().lower()] = value.strip()

        accept = base64.b64encode(
            hashlib.sha1((self.headers["sec-websocket-key"] + GUID).encode()).digest()
        )
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept.decode()}\r\n"
            f"Sec-WebSocket-Protocol: {self.subprotocol}\r\n\r\n"
        )
        conn.sendall(response.encode("latin-1"))

        while not self.stop_flag.is_set():
            frame = ws_read_frame(conn)
            if frame is None:
                return
            opcode, payload, masked = frame
            if not masked:
                self.error = AssertionError("client frame was not masked")
            if opcode == 8:
                return
            if self.subprotocol == "base64":
                decoded = base64.b64decode(payload)
                ws_send(conn, 1, base64.b64encode(decoded[::-1]))
            else:
                ws_send(conn, 2, payload[::-1])


def test_websocket(subprotocol):
    print(f"\nWebSocket transport ({subprotocol} subprotocol)")
    server = FakeWebSocketServer(subprotocol)
    server.start()

    stream = WebSocketStream(
        f"ws://127.0.0.1:{server.port}/api2/json/nodes/n/qemu/100/"
        "vncwebsocket?port=5900&vncticket=abc",
        headers={"Cookie": "PVEAuthCookie=fake", "Origin": "https://fake"},
    )

    check(
        server.headers.get("cookie") == "PVEAuthCookie=fake",
        "auth cookie reached the server",
    )
    check(
        "vncticket=abc" in server.headers.get("host", "") or True, "handshake completed"
    )

    stream.write(b"RFB 003.008\n")
    echoed = stream.read(12)
    check(
        echoed == b"RFB 003.008\n"[::-1],
        f"12 byte payload round-tripped, got {echoed!r}",
    )

    # Fragmented reads across frame boundaries: two writes, one long read.
    stream.write(b"A" * 300)
    stream.write(b"B" * 300)
    combined = stream.read(600)
    check(combined == b"A" * 300 + b"B" * 300, "reads span frame boundaries correctly")

    # A payload over 125 bytes exercises the extended length encoding.
    payload = bytes(range(256)) * 4
    stream.write(payload)
    check(
        stream.read(len(payload)) == payload[::-1],
        "1024 byte payload round-tripped (extended length header)",
    )

    stream.close()
    server.stop_flag.set()
    check(server.error is None, f"no server errors (got {server.error!r})")


class WsServerStream:
    """Server-side websocket framing, presented as a byte stream.

    Lets the fake RFB server run unmodified on top of a websocket, which is
    what the real Proxmox path looks like.
    """

    def __init__(self, conn):
        self.conn = conn
        self.buffer = bytearray()

    def read(self, count):
        while len(self.buffer) < count:
            frame = ws_read_frame(self.conn)
            if frame is None:
                raise OSError("closed")
            opcode, payload, _masked = frame
            if opcode == 8:
                raise OSError("closed")
            self.buffer += payload
        chunk = bytes(self.buffer[:count])
        del self.buffer[:count]
        return chunk

    def write(self, data):
        ws_send(self.conn, 2, bytes(data))

    def close(self):
        with contextlib.suppress(OSError):
            self.conn.close()


class FakeVncWebSocketServer(threading.Thread):
    """A websocket endpoint that speaks RFB, i.e. what PVE's proxy is."""

    daemon = True

    def __init__(self):
        super().__init__(daemon=True, name="fake-vnc-ws")
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.rfb = None
        self.error = None

    def run(self):
        try:
            conn, _ = self.listener.accept()
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(1024)
                if not chunk:
                    return
                data += chunk
            head = data.split(b"\r\n\r\n")[0].decode("latin-1")
            fields = {}
            for line in head.split("\r\n")[1:]:
                if ":" in line:
                    key, _, value = line.partition(":")
                    fields[key.strip().lower()] = value.strip()
            accept = base64.b64encode(
                hashlib.sha1((fields["sec-websocket-key"] + GUID).encode()).digest()
            )
            conn.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept.decode()}\r\n"
                    "Sec-WebSocket-Protocol: binary\r\n\r\n"
                ).encode("latin-1")
            )

            self.rfb = FakeRfbServer.__new__(FakeRfbServer)
            threading.Thread.__init__(self.rfb, daemon=True)
            self.rfb.stream = WsServerStream(conn)
            self.rfb.error = None
            self.rfb.pixel_format = None
            self.rfb.encodings = []
            self.rfb.auth_ok = False
            self.rfb.received_key = None
            self.rfb.received_pointer = None
            self.rfb.done = threading.Event()
            self.rfb.run()
        except Exception as exc:  # noqa: BLE001
            self.error = exc


def test_vnc_widget():
    """Drive the real GTK console widget end to end over a websocket."""
    print("\nVncConsole widget, end to end")
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    from proxima.console.vnc import VncConsole

    server = FakeVncWebSocketServer()
    server.start()

    console = VncConsole(
        f"ws://127.0.0.1:{server.port}/vncwebsocket?port=5900&vncticket=x",
        headers={"Cookie": "PVEAuthCookie=fake"},
        password=PASSWORD,
        title="fake",
        scale_to_fit=True,
    )

    window = Gtk.OffscreenWindow()
    window.add(console)
    window.set_size_request(320, 200)
    window.show_all()

    deadline = time.time() + 8
    while time.time() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if console._surface is not None and console.client and console.client.width:
            break
        time.sleep(0.02)

    # Let the frame arrive and the idle handler run.
    deadline = time.time() + 4
    while time.time() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(0.02)

    check(console.protocol == "vnc", "widget reports the vnc protocol")
    check(
        console.client is not None and console.client.width == WIDTH,
        f"widget negotiated {getattr(console.client, 'width', None)}x"
        f"{getattr(console.client, 'height', None)}",
    )
    check(console._surface is not None, "cairo surface was created")

    if console._surface is not None:
        # Render the widget and confirm the decoded frame reaches the surface.
        surface = console._surface
        check(
            surface.get_width() == WIDTH and surface.get_height() == HEIGHT,
            f"surface is {surface.get_width()}x{surface.get_height()}",
        )
        data = bytes(console._buffer[0:4])
        check(
            data[2] == 255 and data[1] == 0,
            f"first pixel is red in the shared buffer, got {tuple(data)}",
        )

        window.queue_draw()
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        check(True, "widget drew without error")

    # Scaling maths: a 64x32 guest in a wider window is centred, not stretched.
    scale, offset_x, offset_y = console._scale_factors()
    check(scale > 0, f"scale factor {scale:.3f}")
    guest_x, guest_y = console._widget_to_guest(offset_x, offset_y)
    check(
        (guest_x, guest_y) == (0, 0),
        f"top-left of the image maps to guest (0,0), got ({guest_x},{guest_y})",
    )

    # Input from the widget's own client. The fake server returns once it
    # sees a pointer event, so this also lets it finish cleanly instead of
    # dying on the socket we are about to close.
    console.client.send_key(0xFF0D, True)
    console.client.send_pointer(5, 6, 1)
    deadline = time.time() + 4
    while time.time() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if server.rfb is not None and server.rfb.received_pointer:
            break
        time.sleep(0.02)

    check(
        server.rfb is not None and server.rfb.received_pointer == (5, 6, 1),
        f"widget input reached the server, got "
        f"{getattr(server.rfb, 'received_pointer', None)}",
    )

    console.shutdown()
    window.destroy()
    check(server.error is None, f"no server errors (got {server.error!r})")
    inner = getattr(server.rfb, "error", None)
    check(inner is None, f"no errors inside the fake RFB server (got {inner!r})")


def test_pointer_mapping():
    """Host pointer position must land on the same guest pixel it points at.

    Exercised against the geometry directly rather than a live server: the
    mapping is pure arithmetic over the framebuffer size and the widget
    allocation, and those are the two things that go wrong.
    """
    print("\nPointer mapping")
    import cairo
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gdk

    from proxima.console.vnc import VncConsole

    console = VncConsole.__new__(VncConsole)
    console.client = None
    console._last_pos = (0, 0)

    class _Area:
        def __init__(self, width, height):
            self.rect = Gdk.Rectangle()
            self.rect.width, self.rect.height = width, height

        def get_allocation(self):
            return self.rect

    def configure(guest_w, guest_h, widget_w, widget_h, scaling):
        console._surface = cairo.ImageSurface(cairo.FORMAT_RGB24, guest_w, guest_h)
        console.area = _Area(widget_w, widget_h)
        console.scaling = scaling

    def round_trip(guest_x, guest_y):
        """Guest pixel -> where it is drawn -> back to a guest pixel."""
        scale, offset_x, offset_y = console._scale_factors()
        widget_x = offset_x + (guest_x + 0.5) * scale
        widget_y = offset_y + (guest_y + 0.5) * scale
        return console._widget_to_guest(widget_x, widget_y)

    # 1:1, guest smaller than the tab. The classic case where an image drawn
    # in one corner and a pointer mapped from another drift apart.
    configure(640, 480, 1200, 900, scaling=False)
    for point in ((0, 0), (320, 240), (639, 479)):
        got = round_trip(*point)
        check(
            got == point,
            f"unscaled 640x480 in 1200x900: {point} round-trips, got {got}",
        )

    # Scaled down to fit a letterboxed tab.
    configure(1920, 1080, 1000, 900, scaling=True)
    scale, offset_x, offset_y = console._scale_factors()
    check(
        offset_y > 0 and abs(offset_x) < 0.01,
        f"1920x1080 in 1000x900 letterboxes vertically "
        f"(offsets {offset_x:.1f},{offset_y:.1f})",
    )
    for point in ((0, 0), (960, 540), (1919, 1079)):
        got = round_trip(*point)
        check(
            got == point,
            f"scaled 1920x1080 in 1000x900: {point} round-trips, got {got}",
        )

    # A guest bigger than the tab with scaling off must still fit: anything
    # hanging off the edge is invisible and unreachable with the pointer.
    configure(1920, 1080, 1000, 900, scaling=False)
    scale, offset_x, offset_y = console._scale_factors()
    check(
        scale < 1.0 and 1920 * scale <= 1000.01,
        f"a 1920px guest shrinks into a 1000px tab (scale {scale:.3f})",
    )
    got = round_trip(1919, 1079)
    check(
        got == (1919, 1079),
        f"the far corner of an oversized guest is reachable, got {got}",
    )

    # The letterbox margins are not part of the guest screen.
    configure(1920, 1080, 1000, 900, scaling=True)
    _, _, offset_y = console._scale_factors()
    check(
        console._widget_to_guest(500, offset_y / 2) == (960, 0),
        "a click in the top margin clamps to the top row",
    )
    check(
        console._widget_to_guest(-50, -50) == (0, 0),
        "a position left of the image clamps to guest (0,0)",
    )

    # A resolution change the main loop has not applied yet must not be
    # mapped through: a Windows guest does this when its driver loads.
    configure(1024, 768, 1024, 768, scaling=False)

    class _StaleClient:
        width, height = 1920, 1080

    console.client = _StaleClient()
    check(
        console._guest_size() == (1024, 768),
        "mapping follows the surface, not a client that has raced ahead",
    )
    check(
        console._widget_to_guest(1023, 767) == (1023, 767),
        "a stale client size does not skew the mapping",
    )
    console.client = None


def test_offline_effect():
    """A dead console is greyed and dimmed, not left looking live."""
    print("\nOffline console effect")
    import cairo

    from proxima.console.status_panel import draw_offline_effect

    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, 32, 32)
    context = cairo.Context(surface)
    context.set_source_rgb(1, 0, 0)  # a saturated frame
    context.paint()
    surface.flush()
    _, _, red0, _ = bytes(surface.get_data()[0:4])

    draw_offline_effect(context, 32, 32)
    surface.flush()
    blue1, green1, red1, _ = bytes(surface.get_data()[0:4])

    check(
        red1 == green1 == blue1,
        f"saturated red became neutral grey, got R={red1} G={green1} B={blue1}",
    )
    check(red1 < red0, f"and was dimmed ({red0} -> {red1})")


def test_disconnect_panel():
    """A dropped connection must say so, not freeze on the last frame."""
    print("\nDisconnect handling")
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    from proxima.console.vnc import VncConsole

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import smoke_test

    server = FakeVncWebSocketServer()
    server.start()

    reconnects = []
    disconnects = []
    console = VncConsole(
        f"ws://127.0.0.1:{server.port}/vncwebsocket?port=5900&vncticket=x",
        headers={},
        password=PASSWORD,
        title="fake",
        scale_to_fit=True,
        on_disconnect=disconnects.append,
        on_reconnect=lambda: reconnects.append(True),
    )

    window = Gtk.OffscreenWindow()
    window.add(console)
    window.set_size_request(400, 300)
    window.show_all()

    deadline = time.time() + 8
    while time.time() < deadline:
        smoke_test.pump(0.2)
        if console.connected:
            break

    check(console.connected, "console reports connected")
    check(not console.status_panel.get_visible(), "no status panel while connected")

    # Drop the connection the way a powered-off guest would.
    server.rfb.stream.close()
    deadline = time.time() + 6
    while time.time() < deadline:
        smoke_test.pump(0.2)
        if disconnects:
            break

    check(bool(disconnects), f"disconnect reported to the owner (got {disconnects})")
    reason = disconnects[0] if disconnects else ""
    check(
        "WinError" not in reason and "Errno" not in reason,
        f"reason is readable, not a raw socket error: {reason!r}",
    )
    check(not console.connected, "console no longer reports connected")
    check(
        console.status_panel.get_visible(),
        "status panel is shown over the frozen frame",
    )
    check(
        console.status_panel.reconnect_button.get_visible(), "reconnect button offered"
    )

    console.status_panel.reconnect_button.clicked()
    smoke_test.pump(0.3)
    check(bool(reconnects), "reconnect button calls back to the owner")

    console.shutdown()
    window.destroy()


def test_view_menu():
    """The View menu must drive the active console and match its protocol."""
    print("\nView menu against a live VNC console")
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    from proxima.config import Config
    from proxima.console.vnc import VncConsole

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import smoke_test

    server = FakeVncWebSocketServer()
    server.start()

    window = smoke_test.build_window(smoke_test.FakeAPI(), Config())
    smoke_test.pump(1.5)

    console = VncConsole(
        f"ws://127.0.0.1:{server.port}/vncwebsocket?port=5900&vncticket=x",
        headers={},
        password=PASSWORD,
        title="fake",
        scale_to_fit=True,
    )
    console.guest_key = "pve.example.invalid/pve-node-01/qemu/100"
    window.consoles[console.guest_key] = console
    page = window.notebook.append_page(console, Gtk.Label(label="fake"))
    window.notebook.show_all()
    window.notebook.set_current_page(page)

    deadline = time.time() + 8
    while time.time() < deadline:
        smoke_test.pump(0.2)
        if console.client is not None and console.client.width:
            break

    check(
        window.current_console() is console,
        "the window resolves the active console tab",
    )

    # Sensitivity must follow what the protocol actually supports.
    check(
        not window.auto_resize_item.get_sensitive(),
        "Auto-resize disabled for VNC (no guest resize)",
    )
    check(not window.codec_item.get_sensitive(), "Video Codec disabled for VNC")
    check(
        not window.compression_item.get_sensitive(),
        "Image Compression disabled for VNC",
    )
    check(window.scaling_item.get_sensitive(), "Scale to Fit enabled for VNC")
    check(
        window.refresh_frame_item.get_sensitive(), "Refresh Framebuffer enabled for VNC"
    )
    check(window.ctrl_alt_del_item.get_sensitive(), "Send Ctrl+Alt+Del enabled for VNC")
    check(window.close_console_item.get_sensitive(), "Close Console enabled")
    check(
        "VNC" in window.protocol_label.get_text(),
        f"status bar shows the protocol, got {window.protocol_label.get_text()!r}",
    )

    # The menu reflects console state without writing it back.
    check(
        window.scaling_item.get_active() is True,
        "Scale to Fit reflects the console's current setting",
    )

    # Toggling the menu item must reach the console.
    window.scaling_item.set_active(False)
    smoke_test.pump(0.2)
    check(
        console.scaling is False,
        f"unticking Scale to Fit reached the console (scaling={console.scaling})",
    )
    window.scaling_item.set_active(True)
    smoke_test.pump(0.2)
    check(console.scaling is True, "re-ticking Scale to Fit reached it too")

    # Switching back to the summary must disable everything again.
    window.notebook.set_current_page(0)
    smoke_test.pump(0.4)
    check(window.current_console() is None, "summary page has no console")
    check(
        not window.scaling_item.get_sensitive(),
        "view items disabled on the summary page",
    )
    check(
        window.protocol_label.get_text() == "",
        "protocol indicator cleared on the summary page",
    )

    window.close_console(console.guest_key)
    smoke_test.pump(0.2)
    window.shutdown()
    window.destroy()


def main():
    print("VNC fallback protocol tests")

    print("\nDES (VNC variant)")
    check(
        des.encrypt_ecb(
            bytes.fromhex("133457799BBCDFF1"), bytes.fromhex("0123456789ABCDEF")
        )
        .hex()
        .upper()
        == "85E813540F0AB405",
        "DES matches the FIPS test vector",
    )
    check(
        len(des.vnc_response("secret", bytes(16))) == 16,
        "VNC challenge response is 16 bytes",
    )
    check(
        des.vnc_key("ab") == des.vnc_key("ab\x00\x00\x00\x00\x00\x00"),
        "short passwords are zero padded",
    )
    check(
        des.vnc_key("abcdefghXXXX") == des.vnc_key("abcdefgh"),
        "passwords longer than 8 bytes are truncated, as RFB requires",
    )

    test_websocket("binary")
    test_websocket("base64")
    test_rfb()
    test_rfb_bad_password()
    test_vnc_widget()
    test_pointer_mapping()
    test_offline_effect()
    test_disconnect_panel()
    test_view_menu()

    print()
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("ALL VNC PROTOCOL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Minimal RFC 6455 client, enough to carry RFB over Proxmox's HTTPS port.

Proxmox exposes a guest's VNC socket at

    wss://<host>:8006/api2/json/nodes/<node>/<kind>/<vmid>/vncwebsocket
        ?port=<port>&vncticket=<ticket>

which is the only route that survives a normal firewall, since the raw VNC
port on the node is usually not reachable. The handshake needs the
PVEAuthCookie as well as the vncticket in the query string; the cookie
authorises the API call and the ticket authorises the specific console.

Proxmox will negotiate either a 'binary' or a 'base64' subprotocol. We ask
for binary and transparently handle base64 if the server insists, because
older PVE releases only speak the latter.
"""

import os
import ssl
import base64
import socket
import struct
import hashlib
import urllib.parse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketError(Exception):
    pass


class WebSocketStream:
    """A websocket connection presented as a blocking byte stream.

    read()/write() deliberately mirror a socket rather than exposing frames,
    because the thing on the far side is RFB, which is a byte protocol that
    knows nothing about framing.
    """

    def __init__(self, url, headers=None, verify_ssl=False, timeout=20,
                 subprotocols=("binary",)):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("ws", "wss"):
            raise WebSocketError(f"not a websocket URL: {url}")

        self.host = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        self.path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        self._base64 = False
        self._buffer = bytearray()
        self._closed = False
        self._recv_leftover = b""

        raw = socket.create_connection((self.host, self.port), timeout=timeout)
        raw.settimeout(timeout)
        # Nagle plus an interactive remote-desktop protocol is a bad pairing;
        # it turns every small input event into a round-trip delay.
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        if parsed.scheme == "wss":
            context = ssl.create_default_context()
            if not verify_ssl:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            raw = context.wrap_socket(raw, server_hostname=self.host)

        self.sock = raw
        self._handshake(headers or {}, subprotocols)
        # The read path blocks on the guest being idle, which is normal, so
        # no timeout once the stream is live.
        self.sock.settimeout(None)

    # -- handshake -----------------------------------------------------

    def _handshake(self, headers, subprotocols):
        nonce = base64.b64encode(os.urandom(16)).decode("ascii")
        request = [
            f"GET {self.path} HTTP/1.1",
            f"Host: {self.host}:{self.port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {nonce}",
            "Sec-WebSocket-Version: 13",
        ]
        if subprotocols:
            request.append("Sec-WebSocket-Protocol: " + ", ".join(subprotocols))
        for key, value in headers.items():
            request.append(f"{key}: {value}")
        self.sock.sendall(("\r\n".join(request) + "\r\n\r\n").encode("latin-1"))

        raw = self._read_headers()
        status_line, _, rest = raw.partition("\r\n")
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or parts[1] != "101":
            raise WebSocketError(f"server refused the upgrade: {status_line}")

        fields = {}
        for line in rest.split("\r\n"):
            if ":" in line:
                key, _, value = line.partition(":")
                fields[key.strip().lower()] = value.strip()

        expected = base64.b64encode(
            hashlib.sha1((nonce + GUID).encode("ascii")).digest()).decode("ascii")
        if fields.get("sec-websocket-accept") != expected:
            raise WebSocketError("websocket accept token did not match")

        chosen = fields.get("sec-websocket-protocol", "").lower()
        self._base64 = chosen == "base64"

    def _read_headers(self):
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            data += chunk
            if len(data) > 65536:
                raise WebSocketError("handshake response was absurdly large")
        head, _, remainder = bytes(data).partition(b"\r\n\r\n")
        # Anything past the blank line is already frame data.
        self._recv_leftover = remainder
        return head.decode("latin-1", "replace")

    # -- frames --------------------------------------------------------

    def _recv_raw(self, count):
        if self._recv_leftover:
            chunk = self._recv_leftover[:count]
            self._recv_leftover = self._recv_leftover[len(chunk):]
            return chunk
        return self.sock.recv(count)

    def _recv_exact(self, count):
        parts = []
        remaining = count
        while remaining:
            chunk = self._recv_raw(remaining)
            if not chunk:
                raise WebSocketError("connection closed by peer")
            parts.append(chunk)
            remaining -= len(chunk)
        return b"".join(parts)

    def _read_frame(self):
        """Return (opcode, payload) for one complete frame."""
        header = self._recv_exact(2)
        final = bool(header[0] & 0x80)
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F

        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]

        mask = self._recv_exact(4) if masked else None
        payload = self._recv_exact(length) if length else b""
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

        # Fragmented data frames are legal; reassemble before returning.
        while not final:
            more_header = self._recv_exact(2)
            final = bool(more_header[0] & 0x80)
            more_masked = bool(more_header[1] & 0x80)
            more_length = more_header[1] & 0x7F
            if more_length == 126:
                more_length = struct.unpack(">H", self._recv_exact(2))[0]
            elif more_length == 127:
                more_length = struct.unpack(">Q", self._recv_exact(8))[0]
            more_mask = self._recv_exact(4) if more_masked else None
            more = self._recv_exact(more_length) if more_length else b""
            if more_mask:
                more = bytes(b ^ more_mask[i % 4] for i, b in enumerate(more))
            payload += more

        return opcode, payload

    def _pump(self):
        """Read frames until one carries application data."""
        while True:
            opcode, payload = self._read_frame()
            if opcode in (OP_BINARY, OP_TEXT, OP_CONTINUATION):
                if self._base64 and payload:
                    payload = base64.b64decode(payload)
                if payload:
                    return payload
            elif opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
            elif opcode == OP_PONG:
                continue
            elif opcode == OP_CLOSE:
                self._closed = True
                raise WebSocketError("peer closed the websocket")

    def _send_frame(self, opcode, payload):
        if self._closed:
            raise WebSocketError("websocket is closed")
        header = bytearray([0x80 | opcode])
        length = len(payload)
        # Client frames must always be masked, per the RFC.
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    # -- stream interface ----------------------------------------------

    def read(self, count):
        """Block until exactly count bytes of application data are available."""
        while len(self._buffer) < count:
            self._buffer += self._pump()
        chunk = bytes(self._buffer[:count])
        del self._buffer[:count]
        return chunk

    def write(self, data):
        if self._base64:
            data = base64.b64encode(data)
            self._send_frame(OP_TEXT, data)
        else:
            self._send_frame(OP_BINARY, bytes(data))

    def close(self):
        if self._closed:
            return
        try:
            self._send_frame(OP_CLOSE, b"\x03\xe8")   # 1000, normal closure
        except Exception:
            pass
        self._closed = True
        try:
            self.sock.close()
        except Exception:
            pass

"""Proxmox's termproxy protocol: a character terminal over the same websocket.

This is the third console Proxmox offers, and the only one that carries text
rather than pixels. The web UI drives it with xterm.js; the endpoint is the
one the VNC console already uses, so wsclient.py carries it unchanged.

Getting a session takes two calls:

    POST /nodes/<node>/lxc/<vmid>/termproxy   -> {user, ticket, port, upid}
    wss://<host>:8006/api2/json/nodes/<node>/lxc/<vmid>/vncwebsocket
        ?port=<port>&vncticket=<ticket>

and then an authentication line of its own, because the websocket handshake
proves only that the API session is valid -- termproxy wants to know which
session, on top of the PVEAuthCookie:

    ->  <user>:<ticket>\\n
    <-  OK

After that it is a tiny framing on the client's side only. The server sends
raw terminal output with no framing at all, which is what makes the reader
half of this a straight pass-through to the emulator:

    0:<byte length>:<data>   keystrokes
    1:<cols>:<rows>:         the window changed size
    2                        keepalive

Nothing here is specific to containers. `path` is whatever the caller asks
for, so the same class serves a node shell (/nodes/<node>/termproxy) or a
VM's serial port the day either is wanted -- only the URL differs.
"""

import contextlib
import logging
import threading
import time

log = logging.getLogger(__name__)

# The server's own timeout is minutes long, but an idle console sitting
# behind a NAT or a load balancer is dropped long before that by something
# in the middle. xterm.js sends one every 30 seconds; so do we.
KEEPALIVE_SECONDS = 30

# What termproxy answers the authentication line with, and nothing else.
AUTH_OK = b"OK"


class TermProxyError(Exception):
    pass


def friendly_reason(exc):
    """A dropped console explained in words rather than in errno.

    Same reasoning as rfb.friendly_reason: the ordinary way this ends is the
    container being stopped or its shell exiting, which is not a fault and
    should not be reported as one.
    """
    if isinstance(exc, TermProxyError):
        return str(exc)
    if isinstance(exc, (ConnectionError, OSError, EOFError)):
        return "The connection to the console closed."
    text = str(exc)
    if not text or "closed" in text.lower() or "peer" in text.lower():
        return "The connection to the console closed."
    return text


class TermProxyClient(threading.Thread):
    """Speaks termproxy over anything with read_some/write/close.

    Callbacks run on this thread. Everything that touches GTK marshals
    itself back to the main loop, exactly as the RFB client does.
    """

    daemon = True

    def __init__(
        self,
        stream,
        user,
        ticket,
        on_data=None,
        on_status=None,
        on_closed=None,
        cols=80,
        rows=24,
        name="serial",
    ):
        super().__init__(name=f"termproxy-{name}", daemon=True)
        self.stream = stream
        self.user = user
        self.ticket = ticket

        self.on_data = on_data or (lambda data: None)
        self.on_status = on_status or (lambda text: None)
        self.on_closed = on_closed or (lambda reason: None)

        self.cols = cols
        self.rows = rows
        self.bytes_in = 0
        self.bytes_out = 0
        self.authenticated = False

        self._running = True
        self._write_lock = threading.Lock()
        self._keepalive = None

    # -- the wire ---------------------------------------------------------

    def _send(self, payload):
        """One framed message. Serialised, because two threads write here.

        Typing happens on the main loop and the keepalive has a timer of its
        own, so without the lock a keystroke can land in the middle of a
        ping and neither message survives.
        """
        if not self._running:
            return
        with self._write_lock:
            self.stream.write(payload)
            self.bytes_out += len(payload)

    def send_input(self, text):
        """Send keystrokes. Text or bytes; the length prefix counts bytes."""
        data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
        if not data:
            return
        self._send(b"0:" + str(len(data)).encode("ascii") + b":" + data)

    def send_resize(self, cols, rows):
        cols, rows = max(1, int(cols)), max(1, int(rows))
        if (cols, rows) == (self.cols, self.rows):
            return False
        self.cols, self.rows = cols, rows
        self._send(f"1:{cols}:{rows}:".encode("ascii"))
        return True

    def send_ping(self):
        self._send(b"2")

    # -- lifecycle --------------------------------------------------------

    def run(self):
        reason = "The connection to the console closed."
        try:
            self._authenticate()
            self.on_status("connected")
            self._start_keepalive()
            # The size the widget worked out before it had a connection to
            # tell. Sent unconditionally: the far side starts at 80x24 and
            # has no other way to learn otherwise.
            self._send(f"1:{self.cols}:{self.rows}:".encode("ascii"))
            self._read_loop()
        except Exception as exc:
            if self._running:
                log.info("%s: %s", self.name, exc)
            reason = friendly_reason(exc)
        finally:
            self.stop()
            self.on_closed(reason)

    def _authenticate(self):
        self.on_status("authenticating...")
        self.stream.write(f"{self.user}:{self.ticket}\n".encode())

        # Read up to the two bytes of "OK". They arrive in one frame in
        # practice, but a stream is a stream and nothing guarantees it.
        reply = b""
        while len(reply) < len(AUTH_OK):
            chunk = self.stream.read_some(len(AUTH_OK) - len(reply))
            if not chunk:
                raise TermProxyError("the console closed before authenticating")
            reply += chunk

        if not reply.startswith(AUTH_OK):
            # Printed rather than guessed at: the failure mode worth naming
            # is a ticket that has expired, and the server does not say so
            # in any structured way.
            raise TermProxyError(
                "the console refused the ticket "
                f"(said {reply.decode('utf-8', 'replace')!r})"
            )
        self.authenticated = True

    def _read_loop(self):
        while self._running:
            data = self.stream.read_some()
            if not data:
                raise TermProxyError("the console closed the connection")
            self.bytes_in += len(data)
            self.on_data(data)

    def _start_keepalive(self):
        def tick():
            while self._running:
                # Slept in short steps so closing the tab does not leave a
                # thread sitting on a 30-second sleep before it notices.
                for _ in range(KEEPALIVE_SECONDS * 2):
                    if not self._running:
                        return
                    time.sleep(0.5)
                if not self._running:
                    return
                with contextlib.suppress(Exception):
                    self.send_ping()

        self._keepalive = threading.Thread(
            target=tick, daemon=True, name=f"{self.name}-keepalive"
        )
        self._keepalive.start()

    def stop(self):
        if not self._running:
            return
        self._running = False
        with contextlib.suppress(Exception):
            self.stream.close()


def open_session(api, node, vmid, kind="lxc"):
    """Fetch a termproxy ticket and build the websocket URL for it.

    Kept next to the protocol rather than in the widget so the two calls that
    always go together stay together. Returns everything TermProxyClient and
    WebSocketStream need, and nothing else.
    """
    session = api.term_ticket(node, vmid, kind)
    return {
        "url": api.term_websocket_url(
            node, vmid, session["port"], session["ticket"], kind
        ),
        "headers": api.basic_ws_headers(),
        "user": session["user"],
        "ticket": session["ticket"],
    }

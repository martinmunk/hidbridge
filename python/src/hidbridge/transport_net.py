"""NETWORK HID backend transport for bt_hid -- an Ethernet-equipped ESP32 over TCP.

Same board, same NimBLE HID stack, same line protocol as `transport_esp32` -- but reached over the
LAN (TCP 3333) instead of a USB cable. So this is a THIN subclass of `Esp32Transport`: the wire
becomes a `socket_link.SocketLine` (which duck-types the write/readline surface the transport already
uses), and the only added behaviour is the TCP session handshake -- send `@<token>` right after
opening so the firmware's auth gate lets report frames through. Every report/slot/ping method is
inherited unchanged, so `reports.py` stays the single source of truth over the network too.

TARGET STRINGS (see conn.make_link_backend / cli --backend net):
    net:<host>            e.g. net:hidbridge-esp32.local  or  net:192.0.2.42
    net:<host>:<port>     non-default TCP port
The token is NOT in the target string (it is a secret). It is read from ~/.config/bt_hid/net.token
(0600, outside the repo) exactly like the passcode PIN, or passed explicitly / via BT_HID_NET_TOKEN.

SECURITY: the token authorises input injection into a bonded iPad, so it is treated like the PIN --
never logged, never placed in a repr, read from a private file. It is provisioned onto the board over
USB serial (`hidbridge net-provision`), never over this network path. The report bytes themselves
(including passcode frames) still cross the LAN in the clear -- this is a trusted-network interface;
see docs/bt-hid-network-backend.md.
"""

from __future__ import annotations

import os

from . import socket_link
from .transport_esp32 import DEFAULT_BAUD, Esp32Error, Esp32Transport

DEFAULT_PORT = socket_link.DEFAULT_PORT
TOKEN_PATH = os.path.expanduser("~/.config/bt_hid/net.token")


class NetError(Esp32Error):
    """A network-backend precondition failed (host unreachable, or auth rejected)."""


def load_token(explicit: str | None = None) -> str | None:
    """Resolve the network auth token: explicit arg > BT_HID_NET_TOKEN > ~/.config/bt_hid/net.token.

    Returns None if none is configured (the caller then connects unauthenticated -- only ping works,
    and any report frame gets 'ERR auth' from the board). Never logs the value.
    """
    if explicit:
        return explicit.strip() or None
    env = os.environ.get("BT_HID_NET_TOKEN")
    if env:
        return env.strip() or None
    try:
        with open(TOKEN_PATH) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _split_host_port(target: str | None) -> tuple[str | None, int]:
    """Parse '<host>' or '<host>:<port>' (IPv6 in [..] form) into (host, port)."""
    if not target:
        return None, DEFAULT_PORT
    s = target.strip()
    if s.startswith("["):                       # bracketed IPv6, optionally [::1]:3333
        host, sep, rest = s[1:].partition("]")
        if sep and rest.startswith(":") and rest[1:].isdigit():
            return host, int(rest[1:])
        return host, DEFAULT_PORT
    # host:port only when there is exactly one colon (a bare IPv6 has several -> treat as host).
    if s.count(":") == 1:
        host, _, p = s.partition(":")
        if p.isdigit():
            return host, int(p)
    return s, DEFAULT_PORT


class NetTransport(Esp32Transport):
    """Esp32Transport over a TCP line channel to an Ethernet-equipped board, with token auth."""

    BACKEND = "net"

    def __init__(self, host: str | None = None, port: int = DEFAULT_PORT, token: str | None = None,
                 timeout: float = 5.0, open_now: bool = True, **_ignored):
        self.host = host
        self.tcp_port = int(port)
        self._token = token
        self._authed = False
        # prefer_raw/baud are serial-only concepts; the base class never touches them once `_ser` is
        # a socket, but pass sane values so its attribute access is happy.
        super().__init__(port=None, baud=DEFAULT_BAUD, timeout=timeout, open_now=False,
                         prefer_raw=False)
        if open_now:
            self._open()

    def _open(self):
        if self._ser is not None:
            return
        if not self.host:
            raise NetError("no host for the network backend; pass net:<host> (e.g. "
                           "net:hidbridge-esp32.local) or --host")
        self._ser = socket_link.open_socket(self.host, self.tcp_port, self.timeout)
        self.port = self._ser.port                 # "host:port" label for status dicts
        self._authenticate()

    def _authenticate(self):
        """Send `@<token>` so the board's auth gate admits report frames. No token -> ping-only."""
        if not self._token:
            self._authed = False
            return
        self._ser.write(f"@{self._token}\n".encode("ascii"))
        resp = self._readline()
        if not resp.startswith("OK"):
            # Do NOT leak the token or the raw reply (it echoes our line on some stacks); the board's
            # own reply is a bare 'ERR auth', safe to surface.
            self.close()
            raise NetError("network auth rejected (bad or unprovisioned token); provision it over "
                           "USB with `hidbridge net-provision`")
        self._authed = True

    # A socket open never resets a board, so the base class's cold-reset detection is moot here.
    def _settle(self, deadline: float = 1.5):
        self._rebooted_on_open = False


def make_transport(**opts) -> NetTransport:
    """Factory for controller.make_transport('net'). Accepts host=, port=, token=, timeout=.

    The board is addressed as `net:<host>[:port]`. Depending on the caller that arrives either as
    host='<host>[:port]' (the link backend passes the target arg through) or as port='<host>[:port]'
    (the generic transport loader maps a target arg to `port`). Either way the "<host>:<port>" form is
    split here, so `net:board.local`, `net:board.local:4000`, and an explicit host=/port= all work.
    """
    host = opts.get("host")
    port = opts.get("port")
    tcp_port = DEFAULT_PORT
    if isinstance(host, str):                      # host, possibly carrying ":port"
        host, tcp_port = _split_host_port(host)
    elif host is None and isinstance(port, str):   # came in as the target arg "host[:port]"
        host, tcp_port = _split_host_port(port)
    elif isinstance(port, int):
        tcp_port = port
    return NetTransport(host=host, port=tcp_port, token=load_token(opts.get("token")),
                        timeout=opts.get("timeout", 5.0), open_now=opts.get("open_now", True))

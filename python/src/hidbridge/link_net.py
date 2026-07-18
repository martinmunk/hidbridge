"""NETWORK MCU link backend for HidSession (bt_hid.conn) -- connect-on-demand over TCP.

The network twin of `link_esp32`: an Ethernet-equipped ESP32 running the same NimBLE HID firmware,
reached over the LAN instead of USB. It subclasses Esp32LinkBackend and swaps only the transport
(NetTransport instead of the serial Esp32Transport) and the reset-free-serial concerns that do not
apply to a socket -- the connect / disconnect / readiness / host-slot lifecycle is identical, because
it is the same board speaking the same protocol.

    from hidbridge import HidBridge
    with HidBridge("hidbridge-esp32.local") as b, b.connect("My iPad") as hid:
        hid.type_text("hello"); hid.send_key("enter")

`conn.make_link_backend("net:...")` auto-loads this module. The auth token is NOT in the target
string; it comes from ~/.config/bt_hid/net.token / BT_HID_NET_TOKEN (see transport_net.load_token).
"""

from __future__ import annotations

import time

from .conn import BtHidError
from .link_esp32 import Esp32LinkBackend
from .transport_net import make_transport


class NetLinkBackend(Esp32LinkBackend):
    name = "net"
    capabilities = {
        **Esp32LinkBackend.capabilities,
        "medium": "tcp",               # was "serial"; everything else about the board is the same
    }

    def __init__(self, host: str | None = None, **opts):
        super().__init__(port=None, **opts)
        self._host = host

    def prepare(self):
        opts = dict(self._opts)
        if self._host:
            opts["host"] = self._host
        try:
            self._tp = make_transport(**opts)      # opens the TCP channel + authenticates
        except Exception as e:  # noqa: BLE001
            raise BtHidError(f"net backend not ready: {e}") from e

    def disconnect(self):
        # Same as the serial backend minus the leave-tty-at-B0 dance (there is no tty): drop the BLE
        # central (firmware 'X', bond kept), then close the socket.
        if self._tp is None:
            return
        try:
            self._tp.disconnect()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
        try:
            self._tp.close()
        finally:
            self._tp = None

    def describe(self) -> str:
        return f"net (nimble-hog over tcp, {self._host or 'auto'})"


def make_link_backend(arg: str | None = None, **opts) -> NetLinkBackend:
    """Factory for conn.make_link_backend, target 'net:<host>[:port]' (arg = host[:port]).

    The transport parses host:port; we hand `arg` through as the host so `net:board.local:4000`
    works. The bond identity + token are supplied separately (HidSession ios_device / net.token).
    """
    return NetLinkBackend(host=arg, **opts)

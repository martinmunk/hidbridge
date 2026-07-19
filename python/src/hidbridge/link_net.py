"""NETWORK MCU link backend for HidSession (bt_hid.conn) -- LEGACY, capability shim only.

The network twin of `link_esp32`: an Ethernet-equipped ESP32 running the same NimBLE HID firmware,
reached over the LAN instead of USB. SUPERSEDED like its serial sibling -- the drive lifecycle now
lives in `board.McuBoard` over JSON-RPC. This module is kept ONLY so
`conn.make_link_backend("net:...")` auto-loads it and `targets.py` reads its static `capabilities`
dict. It subclasses Esp32LinkBackend and swaps only the transport (NetTransport instead of the serial
Esp32Transport); the removed single-char drive methods are inherited as superseded stubs.

`conn.make_link_backend("net:...")` auto-loads this module. The auth token is NOT in the target
string; it comes from ~/.config/bt_hid/net.token / BT_HID_NET_TOKEN (see transport_net.load_token).
"""

from __future__ import annotations

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

    def describe(self) -> str:
        return f"net (nimble-hog over tcp, {self._host or 'auto'})"


def make_link_backend(arg: str | None = None, **opts) -> NetLinkBackend:
    """Factory for conn.make_link_backend, target 'net:<host>[:port]' (arg = host[:port]).

    The transport parses host:port; we hand `arg` through as the host so `net:board.local:4000`
    works. The bond identity + token are supplied separately (HidSession ios_device / net.token).
    """
    return NetLinkBackend(host=arg, **opts)

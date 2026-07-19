"""Pico MCU link backend for HidSession (bt_hid.conn) -- LEGACY, capability shim only.

SUPERSEDED: the connect-on-demand drive lifecycle for the MCU boards now lives in `board.McuBoard`
(driven over JSON-RPC via `rpc.py`), which HidBridge uses. This module is kept ONLY because
`conn.make_link_backend("pico:...")` still auto-loads it and `targets.py` reads its static
`capabilities` dict (no I/O). The old single-char device drive methods (connect/disconnect/
select_slot/target_slot/slots_status/park/ping/readiness) have been REMOVED: the firmware no longer
speaks the single-char protocol, so those senders were dead. Drive a Pico via
`HidBridge` / `board.McuBoard` instead.

CAPABILITY -- per_device_identity=True: like the ESP32, the Pico owns its BLE stack (BTstack), so
the firmware CAN advertise a distinct address per bonded iOS device -- the clean route to a distinct
identity per iPad (not achievable on a single BlueZ adapter). See docs/bt-hid-mcu-approach.md.
"""

from __future__ import annotations

from .conn import BtHidError, LinkBackend
from .controller import make_transport

_SUPERSEDED = ("the MCU link backend is superseded by board.McuBoard; drive via HidBridge")


def _pong_connected(reply: str) -> bool:
    """Parse a Pico 'PONG name=HIDBRIDGE-pico conn=<0|1>' reply for the link state."""
    return "conn=1" in (reply or "")


class PicoLinkBackend(LinkBackend):
    name = "pico"
    capabilities = {
        "per_device_identity": True,   # BTstack firmware CAN pick the advertising address per bond
        # The arduino-pico PicoBluetoothBLEHID wrapper tracks ONE con handle (single-connection), so
        # the Pico presents distinct per-slot identities but SEQUENTIALLY (deterministic time-sliced
        # slot selection), not simultaneously. Simultaneous hold needs a wrapper bypass -- follow-up.
        "simultaneous_identities": False,
        "role": "peripheral",
        "medium": "serial",
        "stack": "btstack-hog",
        "connect_on_demand": True,
    }
    # Settle fallback: the current Pico firmware does not yet emit the `subscribed=` readiness ack
    # (BTstack HOG subscribe hook = tracked follow-up), so HidSession falls back to this ~1-3s
    # activation-window settle.
    READY_SETTLE = 3.3

    def __init__(self, port: str | None = None, **opts):
        self._port = port
        self._opts = opts
        self._tp = None
        self._ready_supported = None

    def prepare(self):
        opts = dict(self._opts)
        if self._port:
            opts["port"] = self._port
        try:
            self._tp = make_transport("pico", **opts)
        except Exception as e:  # noqa: BLE001
            raise BtHidError(f"pico backend not ready: {e}") from e

    # ---- drive lifecycle: REMOVED (single-char senders were dead; use board.McuBoard) ----
    def connect(self, ios_device, timeout=20.0, exclusive=True) -> bool:
        raise NotImplementedError(_SUPERSEDED)

    def disconnect(self):
        raise NotImplementedError(_SUPERSEDED)

    def is_connected(self) -> bool:
        raise NotImplementedError(_SUPERSEDED)

    def transport(self):
        return self._tp

    def describe(self) -> str:
        return f"pico (btstack-hog, {self._port or 'auto'})"


def make_link_backend(arg: str | None = None, **opts) -> PicoLinkBackend:
    """Factory used by conn.make_link_backend for target 'pico:<port>' (arg = the serial port)."""
    return PicoLinkBackend(port=arg, **opts)

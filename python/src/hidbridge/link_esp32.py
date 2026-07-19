"""ESP32 MCU link backend for HidSession (bt_hid.conn) -- LEGACY, capability shim only.

SUPERSEDED: the connect-on-demand drive lifecycle for the MCU boards now lives in `board.McuBoard`
(driven over JSON-RPC via `rpc.py`), which HidBridge uses. This module is kept ONLY because
`conn.make_link_backend("esp32:...")` still auto-loads it and `targets.py` reads its static
`capabilities` dict (no I/O). The old single-char device drive methods (advertise/connect/disconnect/
select_slot/target_slot/slots_status/park/ping/readiness) have been REMOVED: the firmware no longer
speaks the single-char protocol, so those senders were dead. Drive an ESP32 via
`HidBridge` / `board.McuBoard` instead.

CAPABILITY -- per_device_identity=True: unlike a BlueZ adapter (one identity per adapter, verdict in
docs/bt-hid-multi-device.md), an MCU owns its BLE stack, so the FIRMWARE can advertise a distinct
BLE address per bonded iOS device -- the clean route to "a different identity per iPad". See
docs/bt-hid-mcu-approach.md.
"""

from __future__ import annotations

from .conn import BtHidError, LinkBackend
from .controller import make_transport

_SUPERSEDED = ("the MCU link backend is superseded by board.McuBoard; drive via HidBridge")


class Esp32LinkBackend(LinkBackend):
    name = "esp32"
    capabilities = {
        "per_device_identity": True,   # MCU firmware CAN pick the advertising address per bond
        # NimBLE holds MULTIPLE centrals AT ONCE (CONFIG_BT_NIMBLE_MAX_CONNECTIONS>1) and notifies a
        # SPECIFIC connection, so the ESP32 can present distinct per-slot identities SIMULTANEOUSLY
        # (one board holds mini5 AND mini6, each on its own address). See the firmware JSON-RPC.
        "simultaneous_identities": True,
        "role": "peripheral",
        "medium": "serial",
        "stack": "nimble-hog",
        "connect_on_demand": True,      # board.McuBoard disconnects on session exit (bond kept)
    }
    # Fallback settle (s) if the firmware is too old to report the HID-report subscription. The
    # NimBLE firmware reports it, so this is rarely used; the value matches the observed ~1-3s iOS
    # activation window (with margin) that once forced a manual sleep.
    READY_SETTLE = 3.3

    def __init__(self, port: str | None = None, **opts):
        self._port = port
        self._opts = opts
        self._tp = None
        self._ready_supported = None    # cached firmware-capability probe

    def prepare(self):
        opts = dict(self._opts)
        if self._port:
            opts["port"] = self._port
        try:
            self._tp = make_transport("esp32", **opts)   # RawSerialLink no-reset open
        except Exception as e:  # noqa: BLE001
            raise BtHidError(f"esp32 backend not ready: {e}") from e

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
        return f"esp32 (nimble-hog, {self._port or 'auto'})"


def make_link_backend(arg: str | None = None, **opts) -> Esp32LinkBackend:
    """Factory used by conn.make_link_backend for target 'esp32:<port>' (arg = the serial port)."""
    return Esp32LinkBackend(port=arg, **opts)

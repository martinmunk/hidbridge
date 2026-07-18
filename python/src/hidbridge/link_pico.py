"""Pico MCU link backend for HidSession (bt_hid.conn) -- connect-on-demand over USB serial.

Plugs the Raspberry Pi Pico 2 W (BTstack HOG) in BEHIND the shared `HidSession(target, ios_device)`
context manager, interchangeable with the dongle + ESP32:

    from hidbridge import HidBridge
    with HidBridge("/dev/ttyACM0") as b, b.connect("My iPad") as hid:
        hid.type_text("hello")

NO-RESET NOTE: the Pico's USB-CDC is native to the RP2350 (USB vendor 2e8a); opening the port does
NOT reboot the running firmware the way an ESP32's DTR/RTS-wired bridge does -- the ONLY reset
hazard is a 1200-baud open, which the RP2 ROM treats as "reboot to BOOTSEL". We only ever open at
115200, so a Pico session is inherently reset-free (no B0 trick needed). Connect-on-demand still
applies: we wait for the bonded iOS device to (re)connect before sending HID.

CAPABILITY -- per_device_identity=True: like the ESP32, the Pico owns its BLE stack (BTstack), so
the firmware CAN advertise a distinct address per bonded iOS device -- the clean route to a distinct
identity per iPad (not achievable on a single BlueZ adapter). Host API + capability are here; the
firmware address-per-bond feature + multi-iPad validation are tracked in docs/bt-hid-mcu-approach.md.
"""

from __future__ import annotations

import time

from .conn import BtHidError, LinkBackend
from .controller import make_transport


def _pong_connected(reply: str) -> bool:
    """Parse a Pico 'PONG name=HIDBRIDGE-pico conn=<0|1>' reply for the link state."""
    return "conn=1" in (reply or "")


def _pong_subscribed(reply: str):
    """Parse the optional 'subscribed=<0|1>' readiness field from a PONG reply.

    Returns True/False when the firmware reports it, or None when absent (older firmware) -- in which
    case readiness is unobservable and HidSession uses the settle fallback. Kept forward-compatible
    so a future Pico firmware that emits `subscribed=` lights up the real readiness path with no host
    change (the BTstack HOG subscribe hook is a tracked follow-up -- see docs/bt-hid-mcu-approach.md)."""
    toks = dict(t.split("=", 1) for t in (reply or "").split() if "=" in t)
    sub = toks.get("subscribed")
    return None if sub is None else (sub == "1")


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
    # activation-window settle. A firmware that adds the field flips readiness_supported() -> True.
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

    def _ping_connected(self) -> bool:
        try:
            return _pong_connected(self._tp.ping().get("reply", ""))
        except Exception:  # noqa: BLE001
            return False

    def connect(self, ios_device, timeout=20.0, exclusive=True) -> bool:
        # The Pico firmware advertises automatically; wait for the bonded iOS device to (re)connect.
        end = time.time() + timeout
        while time.time() < end:
            if self._ping_connected():
                return True
            time.sleep(1.0)
        return self._ping_connected()

    def disconnect(self):
        # The current Pico firmware has no explicit disconnect command; close the port (the BLE link
        # is dropped by the firmware on idle / next connect). A firmware 'X' (as on the ESP32) is a
        # tracked follow-up. Closing here keeps the API symmetric.
        if self._tp is None:
            return
        try:
            self._tp.close()
        finally:
            self._tp = None

    def is_connected(self) -> bool:
        return bool(self._tp) and self._ping_connected()

    # ---- readiness: iOS subscribed to the HID reports (firmware `subscribed=`; None if absent) ----
    def _ping_reply(self) -> str:
        try:
            return self._tp.ping().get("reply", "")
        except Exception:  # noqa: BLE001
            return ""

    def readiness_supported(self) -> bool:
        if self._ready_supported is None:
            self._ready_supported = _pong_subscribed(self._ping_reply()) is not None
        return bool(self._ready_supported)

    def is_ready(self) -> bool:
        return _pong_subscribed(self._ping_reply()) is True

    # ---- per-bond identity (host-slot) control ----
    def select_identity(self, slot: int, name: str | None = None) -> dict:
        """Select/create host slot `slot` as the advertising identity + (re)advertise (`I<n>`).

        Optional `name` sets that slot's target-scoped advertised BLE name (e.g. `HIDBRIDGE-pico-mini5`)."""
        return self._tp.select_slot(slot, name)

    def target_identity(self, slot: int) -> dict:
        """Set the send-target slot (`T<n>`). On the single-connection Pico wrapper this only
        matters once a genuine multi-connection firmware lands; accepted for API symmetry."""
        return self._tp.target_slot(slot)

    def slots_status(self) -> dict:
        return self._tp.slots_status()

    def park(self) -> dict:
        """Stop advertising, keep connection + bonds (`O`) -- the cross-board handoff park."""
        return self._tp.park()

    def transport(self):
        return self._tp

    def describe(self) -> str:
        return f"pico (btstack-hog, {self._port or 'auto'})"


def make_link_backend(arg: str | None = None, **opts) -> PicoLinkBackend:
    """Factory used by conn.make_link_backend for target 'pico:<port>' (arg = the serial port)."""
    return PicoLinkBackend(port=arg, **opts)

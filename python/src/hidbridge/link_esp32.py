"""ESP32 MCU link backend for HidSession (bt_hid.conn) -- connect-on-demand over USB serial.

Plugs the ESP32 NimBLE HID board in BEHIND the shared, backend-agnostic `HidSession(target,
ios_device)` context manager, so it is INTERCHANGEABLE with the BlueZ dongle:

    from hidbridge.conn import HidSession
    with HidSession("esp32:/dev/ttyUSB1", "My iPad") as hid:
        hid.type_text("hello"); hid.send_key("enter")

`conn.make_link_backend("esp32:...")` auto-loads this module and calls `make_link_backend(arg)`.

Lifecycle mapping (LinkBackend contract):
  prepare()     open the serial port RESET-FREE (serial_link.RawSerialLink; see transport_esp32) so
                talking to the board does NOT reboot it / drop its BLE link. Held open for the
                whole session.
  connect()     (re)start advertising + WAIT for the bonded iOS device to (re)connect (conn=1).
  disconnect()  firmware 'X' -> drop the central's link, keep the bond (so another device can take
                the single HID link next).
  is_connected  firmware 'P' ping -> conn state.
  transport()   the Esp32Transport (its .send relays report frames; reports.py stays the source).

CAPABILITY -- per_device_identity=True: unlike a BlueZ adapter (one identity per adapter, verdict in
docs/bt-hid-multi-device.md), an MCU owns its BLE stack, so the FIRMWARE can advertise a distinct
BLE address per bonded iOS device -- the clean route to "a different identity per iPad". The host
API + capability are in place here; the firmware address-per-bond feature + its multi-iPad
validation are tracked in docs/bt-hid-mcu-approach.md (gated on an iPad re-pair session).
"""

from __future__ import annotations

import time

from .conn import BtHidError, LinkBackend
from .controller import make_transport


class Esp32LinkBackend(LinkBackend):
    name = "esp32"
    capabilities = {
        "per_device_identity": True,   # MCU firmware CAN pick the advertising address per bond
        # NimBLE holds MULTIPLE centrals AT ONCE (CONFIG_BT_NIMBLE_MAX_CONNECTIONS>1) and notifies a
        # SPECIFIC connection, so the ESP32 can present distinct per-slot identities SIMULTANEOUSLY
        # (one board holds mini5 AND mini6, each on its own address). See the firmware `I`/`T`/`S`.
        "simultaneous_identities": True,
        "role": "peripheral",
        "medium": "serial",
        "stack": "nimble-hog",
        "connect_on_demand": True,      # firmware 'X' disconnects on session exit (bond kept)
    }
    # Fallback settle (s) if the firmware is too old to report the HID-report subscription. The
    # NimBLE firmware reports it (readiness_supported -> True), so this is rarely used; the value
    # matches the observed ~1-3s iOS activation window (with margin) that once forced a manual sleep.
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

    def connect(self, ios_device, timeout=20.0, exclusive=True) -> bool:
        # exclusive is inherent: the firmware serves ONE central at a time. Kick advertising so a
        # bonded device reconnects, then wait for the link. (Choosing WHICH bonded device reconnects
        # is the directed-advertising / per-device-identity firmware feature -- see the doc.)
        try:
            self._tp.advertise()
        except Exception:  # noqa: BLE001
            pass
        return self._tp.wait_connected(timeout=timeout)

    def disconnect(self):
        if self._tp is None:
            return
        try:
            self._tp.disconnect()      # firmware 'X': drop the central, keep the bond
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
        try:
            self._tp.close()           # leaves the tty at B0 -> next open is reset-free
        finally:
            self._tp = None

    def is_connected(self) -> bool:
        return bool(self._tp) and self._tp.is_connected()

    # ---- readiness: iOS has subscribed to the HID reports (firmware `subscribed=` ack) ----
    def readiness_supported(self) -> bool:
        if self._ready_supported is None:
            try:
                self._ready_supported = self._tp.ping().get("subscribed") is not None
            except Exception:  # noqa: BLE001
                self._ready_supported = False
        return bool(self._ready_supported)

    def is_ready(self) -> bool:
        try:
            return bool(self._tp.is_ready())
        except Exception:  # noqa: BLE001
            return False

    def wait_ready(self, timeout: float = 8.0, poll: float = 0.5) -> bool:
        if not self.readiness_supported():
            return False
        return bool(self._tp.wait_ready(timeout=timeout, poll=poll))

    # ---- per-bond identity (host-slot) control ----
    def select_identity(self, slot: int, name: str | None = None) -> dict:
        """Select/create host slot `slot` as the advertising identity + (re)advertise (`I<n>`).

        Optional `name` sets that slot's target-scoped advertised BLE name (e.g. `wsb-esp32-mini5`)."""
        return self._tp.select_slot(slot, name)

    def target_identity(self, slot: int) -> dict:
        """Route subsequent HID reports to slot `slot`'s connection (`T<n>`)."""
        return self._tp.target_slot(slot)

    def slots_status(self) -> dict:
        """Query the board's slot table + per-slot connect/subscribe state (`S`)."""
        return self._tp.slots_status()

    def park(self) -> dict:
        """Stop advertising, keep connections + bonds (`O`) -- the cross-board handoff park."""
        return self._tp.park()

    def transport(self):
        return self._tp

    def describe(self) -> str:
        return f"esp32 (nimble-hog, {self._port or 'auto'})"


def make_link_backend(arg: str | None = None, **opts) -> Esp32LinkBackend:
    """Factory used by conn.make_link_backend for target 'esp32:<port>' (arg = the serial port)."""
    return Esp32LinkBackend(port=arg, **opts)

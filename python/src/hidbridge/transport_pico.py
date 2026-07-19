"""bt_hid Pico backend -- HID reports over a USB-serial link to a Raspberry Pi Pico 2 W.

WHY: the BlueZ/dongle path (`ble_hid.py`) is stuck on iOS not subscribing to our HID input report
characteristics (a CCCD / Report-Reference GATT quirk on the BlueZ side). A Pi Pico 2 W (RP2350 +
CYW43439, ~USD 6) running BTstack's HID-over-GATT (HOG) profile implements HOG the iOS-compatible
way out of the box, so it enables the input reports where BlueZ fights us. This makes the Pico a
first-class, low-cost, interchangeable HID BACKEND: a contributor who has a Pico instead of a USB
Bluetooth dongle can still wake / unlock / type / click an iPad-iPhone-Mac.

The Pico runs the firmware in `firmware/pico/` (BTstack HOG, advertising as `HIDBRIDGE-pico` -- a
distinct name so it does NOT collide with the dongle's `HIDBRIDGE-dongle`). The production drive path
is JSON-RPC (`board.McuBoard` + `rpc.py`): the BOARD builds every report AND owns advertising /
device management. This module is now JUST a pure host-side report FRAMER
(`encode_frame`/`decode_frame`/`send`) kept for the offline byte-parity tests + the central
self-test's encodability check. NOTE: the firmware NO LONGER accepts K/M frame lines on the wire (the
host-builds relay was removed) and NO LONGER speaks the single-char device commands (P/I/T/S/X/O --
all folded into JSON-RPC); `send()` is a host encoder, not a live MCU drive path.

Line protocol (ASCII, `\n`-terminated), host -> Pico: a line is routed by its FIRST char -- '{' =
JSON-RPC (the board builds the report + manages the radio). That is the only protocol the firmware
speaks now.

`pyserial` is imported LAZILY (only when actually opening a port), so the pure framing helpers +
their offline tests run with no serial dependency and no hardware.
"""

from __future__ import annotations

import glob
import os

# Kind -> line prefix + expected report-body length (matches reports.py layouts). The kinds are the
# HidReport.kind values produced by reports.py: keyboard(ID 1), mouse(ID 2, relative). Body length is
# the bytes AFTER the report ID (the firmware prepends the ID); it MUST equal reports.py's *_BODY_LEN.
_KIND_PREFIX = {"keyboard": "K", "mouse": "M"}
_KIND_BODYLEN = {"keyboard": 8, "mouse": 4}
_PREFIX_KIND = {v: k for k, v in _KIND_PREFIX.items()}

PICO_ADVERT_NAME = "HIDBRIDGE-pico"
DEFAULT_BAUD = 115200


# --------------------------------------------------------------------------- #
# Pure framing helpers (no serial dependency -- fully unit-tested offline)
# --------------------------------------------------------------------------- #
def encode_frame(kind: str, body: bytes) -> bytes:
    """Encode one (kind, body) report frame into a Pico serial command line.

    Raises ValueError on an unknown kind or a wrong body length (so a malformed report never
    silently reaches the device).
    """
    prefix = _KIND_PREFIX.get(kind)
    if prefix is None:
        raise ValueError(
            f"unknown report kind {kind!r} (want one of {sorted(_KIND_PREFIX)})")
    want = _KIND_BODYLEN[kind]
    if len(body) != want:
        raise ValueError(f"{kind} report body must be {want} bytes, got {len(body)}")
    return f"{prefix} {body.hex()}\n".encode("ascii")


def decode_frame(line: bytes) -> tuple[str, bytes]:
    """Inverse of encode_frame (mirrors the firmware parser; used by tests to prove agreement)."""
    text = line.decode("ascii").strip()
    parts = text.split()
    if len(parts) != 2 or parts[0] not in _PREFIX_KIND:
        raise ValueError(f"bad frame line {line!r}")
    kind = _PREFIX_KIND[parts[0]]
    body = bytes.fromhex(parts[1])
    if len(body) != _KIND_BODYLEN[kind]:
        raise ValueError(f"bad {kind} body length in {line!r}")
    return kind, body


def ack_is_ok(line: bytes) -> bool:
    """True iff a Pico reply line indicates success (starts with 'OK')."""
    return line.decode("ascii", "replace").strip().upper().startswith("OK")


# --------------------------------------------------------------------------- #
# Port autodetection
# --------------------------------------------------------------------------- #
# Raspberry Pi USB vendor id (RP2040/RP2350 CDC). NEVER select a non-Pico serial device: this host
# also has an unrelated CH340 (1a86, the KVM control adapter) and a CP2102 on /dev/ttyUSB* -- writing
# HID lines to those would be harmful, so autodetect is Pico-vendor-gated, ttyACM-only.
_PICO_USB_VENDOR = "2e8a"
_PICO_BYID_TAGS = ("pico", "rp2", "raspberry", "board_cdc")


def _tty_usb_vendor(dev: str) -> str:
    """Return the USB idVendor (hex string) of a tty device, or '' if not resolvable."""
    name = os.path.basename(os.path.realpath(dev))
    cur = os.path.realpath(os.path.join("/sys/class/tty", name, "device"))
    while cur and cur != "/":
        vid_path = os.path.join(cur, "idVendor")
        if os.path.exists(vid_path):
            try:
                with open(vid_path) as f:
                    return f.read().strip().lower()
            except OSError:
                return ""
        cur = os.path.dirname(cur)
    return ""


def autodetect_port() -> str | None:
    """Locate the running Pico's USB-CDC serial device -- Pico ONLY, never another serial adapter.

    1. a /dev/serial/by-id path whose name looks like a Pi Pico / RP2350;
    2. else a /dev/ttyACM* whose USB vendor is Raspberry Pi (2e8a).
    Returns None if no genuine Pico is present (e.g. it is still in BOOTSEL mass-storage mode, or
    not plugged in). Deliberately does NOT fall back to an arbitrary serial device.
    """
    for path in sorted(glob.glob("/dev/serial/by-id/*")):
        low = os.path.basename(path).lower()
        if any(tag in low for tag in _PICO_BYID_TAGS):
            return path
    for dev in sorted(glob.glob("/dev/ttyACM*")):
        if _tty_usb_vendor(dev) == _PICO_USB_VENDOR:
            return dev
    return None


class PicoNotFoundError(RuntimeError):
    """No Pico serial port could be found/opened."""


# --------------------------------------------------------------------------- #
# The transport (implements the HidController transport interface: .send(frames))
# --------------------------------------------------------------------------- #
class PicoSerialTransport:
    """Send HID report frames to a Pico 2 W over USB serial.

    Implements the same duck-typed interface HidController expects: `send(frames) -> dict`, where
    `frames` is a list of `(kind, body_bytes)` tuples produced from the pure `reports` layer.

    `ack=True` (default) reads back the Pico's per-line `OK`/`ERR` reply and fails loudly on ERR;
    set `ack=False` for a fire-and-forget stream. The serial port is opened lazily on first send and
    kept open (`persist=True`, the default) so a scripted timeline does not re-open per report.
    """

    def __init__(self, port: str | None = None, baud: int = DEFAULT_BAUD, timeout: float = 2.0,
                 ack: bool = True, persist: bool = True, open_factory=None):
        self.port = port or os.environ.get("BT_HID_PICO_PORT") or autodetect_port()
        self.baud = baud
        self.timeout = timeout
        self.ack = ack
        self.persist = persist
        # open_factory(port, baud, timeout) -> serial-like object; injectable for tests (mock serial)
        self._open_factory = open_factory or self._pyserial_open
        self._ser = None

    # -- serial lifecycle -------------------------------------------------- #
    def _pyserial_open(self, port, baud, timeout):
        try:
            import serial  # lazy: only needed when we actually talk to hardware
        except ImportError as e:  # pragma: no cover - env-dependent
            raise PicoNotFoundError(
                "pyserial is required for the Pico backend: install python3-serial "
                "(apt) or `uv pip install pyserial`") from e
        if not port:
            raise PicoNotFoundError(
                "no Pico serial port found; flash firmware/pico and plug it in, or set "
                "BT_HID_PICO_PORT=/dev/ttyACMx")
        return serial.Serial(port, baud, timeout=timeout)

    def _get(self):
        if self._ser is None:
            self._ser = self._open_factory(self.port, self.baud, self.timeout)
        return self._ser

    def close(self):
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    # -- transport interface ----------------------------------------------- #
    def _write_line(self, ser, line: bytes) -> None:
        ser.write(line)
        if hasattr(ser, "flush"):
            ser.flush()

    def send(self, frames: list[tuple[str, bytes]]) -> dict:
        ser = self._get()
        sent = 0
        try:
            for kind, body in frames:
                self._write_line(ser, encode_frame(kind, body))
                if self.ack:
                    reply = ser.readline()
                    if not ack_is_ok(reply):
                        return {"ok": False, "sent": sent,
                                "error": f"pico rejected frame {sent}: "
                                         f"{reply.decode('ascii', 'replace').strip()!r}"}
                sent += 1
        finally:
            if not self.persist:
                self.close()
        return {"ok": True, "sent": sent}


def make_transport(**opts) -> PicoSerialTransport:
    """Factory used by the generic backend loader (controller.make_transport('pico')).

    Mirrors transport_esp32.make_transport so `bt_hid --backend pico` routes through the same
    backend-agnostic path. `port` may be omitted -> the transport falls back to BT_HID_PICO_PORT +
    Pico-vendor-gated autodetect. The port opens lazily on first send, so this is hardware-free.
    """
    return PicoSerialTransport(port=opts.get("port"), baud=opts.get("baud", DEFAULT_BAUD),
                               timeout=opts.get("timeout", 2.0),
                               ack=opts.get("ack", True), persist=opts.get("persist", True))

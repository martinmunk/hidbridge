"""bt_hid Pico backend -- HID reports over a USB-serial link to a Raspberry Pi Pico 2 W.

WHY: the BlueZ/dongle path (`ble_hid.py`) is stuck on iOS not subscribing to our HID input report
characteristics (a CCCD / Report-Reference GATT quirk on the BlueZ side). A Pi Pico 2 W (RP2350 +
CYW43439, ~USD 6) running BTstack's HID-over-GATT (HOG) profile implements HOG the iOS-compatible
way out of the box, so it enables the input reports where BlueZ fights us. This makes the Pico a
first-class, low-cost, interchangeable HID BACKEND: a contributor who has a Pico instead of a USB
Bluetooth dongle can still wake / unlock / type / click an iPad-iPhone-Mac.

The Pico runs the firmware in `firmware/pico/` (BTstack HOG, advertising as `HIDBRIDGE-pico` -- a
distinct name so it does NOT collide with the dongle's `HIDBRIDGE-dongle`). This host ships each HID
report as ONE ASCII line over the Pico's USB-CDC serial; the Pico adds only the reportID and emits
it as a BLE HID notification. `reports.py` stays the SINGLE SOURCE OF TRUTH for the report BYTES --
we send the exact 8-byte keyboard / 4-byte mouse body it builds; the Pico never re-derives them.

Line protocol (ASCII, one report per line, `\n`-terminated), host -> Pico:
    K <16 hex>   keyboard report    (8-byte boot body: mod,resv,k1..k6)          -> report ID 1
    M <8 hex>    relative mouse      (4-byte body: buttons,dx,dy,wheel)           -> report ID 2
    A <10 hex>   absolute mouse      (5-byte body: buttons,Xlo,Xhi,Ylo,Yhi)       -> report ID 3
    D <12 hex>   digitizer/touch     (6-byte body: flags,contactID,Xlo,Xhi,Ylo,Yhi) -> report ID 4
    P            ping           -> Pico replies e.g. `PONG name=HIDBRIDGE-pico conn=1`
Pico -> host reply, one line per command: `OK` on success, `ERR <reason>` on failure.

`pyserial` is imported LAZILY (only when actually opening a port), so the pure framing helpers +
their offline tests run with no serial dependency and no hardware.
"""

from __future__ import annotations

import glob
import os

# Kind -> line prefix + expected report-body length (matches reports.py layouts). The kinds are the
# HidReport.kind values produced by reports.py: keyboard(ID 1), mouse(ID 2, relative), abs_mouse
# (ID 3, absolute pointer), digitizer(ID 4, touchscreen). Body length is the bytes AFTER the report
# ID (the firmware prepends the ID); it MUST equal reports.py's *_BODY_LEN for that kind.
_KIND_PREFIX = {"keyboard": "K", "mouse": "M", "abs_mouse": "A", "digitizer": "D"}
_KIND_BODYLEN = {"keyboard": 8, "mouse": 4, "abs_mouse": 5, "digitizer": 6}
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


def encode_ping() -> bytes:
    return b"P\n"


# --------------------------------------------------------------------------- #
# Per-bond IDENTITY (host-slot) commands -- pure framing + reply parsers.
#
# Same wire protocol as the ESP32 (transport_esp32) so the host side is symmetric:
#   I<n>  select slot n as the advertising identity + (re)advertise -> "OK slot=<n> addr=<hex> conn="
#   T<n>  send-target slot -> "OK tgt=<n>"
#   S     slots status -> "OK slots=<N> adv=<n> tgt=<n> multi=<0|1> c0=.. s0=.."
# On the Pico (BTstack) slot 0 is the native CYW43439 public address (existing bond preserved) and
# slots >=1 are static-random addresses set via gap_random_address_set(). NOTE the arduino-pico
# PicoBluetoothBLEHID wrapper tracks a SINGLE con handle, so the Pico reports multi=0 (deterministic
# time-sliced slot selection, not simultaneous hold -- see docs/bt-hid-mcu-approach.md).
# --------------------------------------------------------------------------- #
def encode_slot_select(slot: int, name: str | None = None) -> bytes:
    """`I<n>` / `I<n>:<name>` -- select slot n + optionally set its target-scoped advertised name."""
    if not isinstance(slot, int) or slot < 0:
        raise ValueError(f"slot must be a non-negative int, got {slot!r}")
    if name:
        if ":" in name or "\n" in name:
            raise ValueError(f"slot name must not contain ':' or newline, got {name!r}")
        return f"I{slot}:{name}\n".encode("ascii")
    return f"I{slot}\n".encode("ascii")


def encode_slot_target(slot: int) -> bytes:
    if not isinstance(slot, int) or slot < 0:
        raise ValueError(f"slot must be a non-negative int, got {slot!r}")
    return f"T{slot}\n".encode("ascii")


def encode_slots_query() -> bytes:
    return b"S\n"


def _kv_tokens(resp: str) -> dict:
    return dict(t.split("=", 1) for t in (resp or "").split() if "=" in t)


def parse_slot_reply(resp: str) -> dict:
    toks = _kv_tokens(resp)
    slot = toks.get("slot")
    return {
        "ok": (resp or "").upper().startswith("OK"),
        "slot": int(slot) if slot is not None and slot.isdigit() else None,
        "addr": toks.get("addr"),
        "name": toks.get("name"),
        "connected": toks.get("conn") == "1",
        "resp": resp,
    }


def parse_slots_status(resp: str) -> dict:
    toks = _kv_tokens(resp)

    def _int(key):
        v = toks.get(key)
        return int(v) if v is not None and v.lstrip("-").isdigit() else None

    conn, sub = {}, {}
    for k, v in toks.items():
        if len(k) >= 2 and k[0] == "c" and k[1:].isdigit():
            conn[int(k[1:])] = v == "1"
        elif len(k) >= 2 and k[0] == "s" and k[1:].isdigit():
            sub[int(k[1:])] = v == "1"
    return {
        "ok": (resp or "").upper().startswith("OK"),
        "slots": _int("slots"), "adv": _int("adv"), "tgt": _int("tgt"),
        "multi": toks.get("multi") == "1", "conn": conn, "sub": sub, "resp": resp,
    }


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

    def ping(self) -> dict:
        """Probe the Pico: returns its PONG status line (or an error)."""
        ser = self._get()
        try:
            self._write_line(ser, encode_ping())
            reply = ser.readline().decode("ascii", "replace").strip()
        finally:
            if not self.persist:
                self.close()
        return {"ok": bool(reply), "reply": reply}

    # -- per-bond identity (host-slot) control (see the framing helpers above) --
    def _cmd(self, line: bytes) -> str:
        ser = self._get()
        try:
            self._write_line(ser, line)
            return ser.readline().decode("ascii", "replace").strip()
        finally:
            if not self.persist:
                self.close()

    def select_slot(self, slot: int, name: str | None = None) -> dict:
        """Select host slot `slot` as the advertising identity + (re)advertise (`I<n>`).

        Optional `name` sets that slot's target-scoped advertised BLE name (e.g. `HIDBRIDGE-pico-mini5`)."""
        return parse_slot_reply(self._cmd(encode_slot_select(slot, name)))

    def target_slot(self, slot: int) -> dict:
        """Set the send-target slot (`T<n>`)."""
        resp = self._cmd(encode_slot_target(slot))
        toks = _kv_tokens(resp)
        tgt = toks.get("tgt")
        return {"ok": resp.upper().startswith("OK"),
                "tgt": int(tgt) if tgt is not None and tgt.isdigit() else None, "resp": resp}

    def slots_status(self) -> dict:
        """Query the slot table + per-slot connect/subscribe state (`S`)."""
        return parse_slots_status(self._cmd(encode_slots_query()))

    def disconnect(self) -> dict:
        """Disconnect the current central (keep bond) + park (`X`). Frees the iPad for another board."""
        resp = self._cmd(b"X\n")
        return {"ok": resp.upper().startswith("OK"), "resp": resp}

    def park(self) -> dict:
        """Stop advertising, keep any connection + bonds (`O`)."""
        resp = self._cmd(b"O\n")
        return {"ok": resp.upper().startswith("OK"), "resp": resp}


def make_transport(**opts) -> PicoSerialTransport:
    """Factory used by the generic backend loader (controller.make_transport('pico')).

    Mirrors transport_esp32.make_transport so `bt_hid --backend pico` routes through the same
    backend-agnostic path. `port` may be omitted -> the transport falls back to BT_HID_PICO_PORT +
    Pico-vendor-gated autodetect. The port opens lazily on first send, so this is hardware-free.
    """
    return PicoSerialTransport(port=opts.get("port"), baud=opts.get("baud", DEFAULT_BAUD),
                               timeout=opts.get("timeout", 2.0),
                               ack=opts.get("ack", True), persist=opts.get("persist", True))

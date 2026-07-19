"""ESP32 HID backend transport for bt_hid.

An ESP32 (~$5) is an alternative HID BACKEND: instead of a USB Bluetooth dongle driven by BlueZ, an
ESP32 running the `HIDBRIDGE-esp32` firmware (hidbridge/firmware/esp32/) is the BLE HID keyboard+
mouse peripheral, and this host talks to it over USB serial. The production drive path is JSON-RPC
(`board.McuBoard` + `rpc.py`): the BOARD builds every report AND owns advertising / device management.
This module is now JUST a pure host-side report FRAMER (`encode_frame`/`send`) plus the serial
plumbing (open/close, port autodetect): the framer is kept for the offline byte-parity tests + the
central self-test's encodability check. NOTE: the firmware NO LONGER accepts K/M frame lines on the
wire (the host-builds relay was removed) and NO LONGER speaks the single-char device commands
(P/A/I/T/S/N/V/W/X/O -- all folded into JSON-RPC); `send()` is a host encoder, not a live MCU drive
path.

Why an ESP32 backend at all: the BlueZ/dongle path is stuck on iOS not subscribing to our HID report
characteristics (a CCCD / Report-Reference GATT quirk). The ESP32's NimBLE HID-over-GATT stack serves
that correctly + iOS-compatibly out of the box, so this backend works where BlueZ fights us. It also
lowers the contributor hardware bar: an ESP32 is a common, cheap board.

Serial wire (host -> ESP32, newline-terminated ASCII lines): a line is routed by its FIRST char --
'{' = JSON-RPC (the board builds the report + manages the radio). That is the only protocol the
firmware speaks now. The board answers each request with a single JSON line, and emits asynchronous
link state as "EVT connected"/"EVT disconnected" / "EVT ready"/"EVT unsubscribed" lines, which
`_readline` skips when reading a reply.

This module is import-safe WITHOUT pyserial: pyserial is imported lazily only when opening a real
device, so the pure framing (`encode_frame`) + a duck-typed port are fully unit-testable offline.
"""

from __future__ import annotations

import glob
import os

# report kind -> serial command prefix + expected body length (matches reports.py bodies).
_KIND_PREFIX = {
    "keyboard": (b"K", 8),
    "mouse": (b"M", 4),
}

DEFAULT_BAUD = 115200
# PREFERRED ESP32 USB-serial bridges, by USB idVendor:
#   10c4 = Silicon Labs CP2102 (the ESP32 DevKit dev-board bridge on wsb, /dev/ttyUSB1).
#   0403 = FTDI FT2232H (the ESP32-Ethernet-Kit's dual bridge, /dev/ttyUSB2 + ttyUSB3). The
#          FT2232H exposes TWO tty channels: interface 00 = channel A (JTAG on the Ethernet-Kit),
#          interface 01 = channel B (the ESP32 UART). We prefer the HIGHER interface number so
#          autodetect lands on the UART channel, not JTAG (override with BT_HID_ESP32_PORT).
_ESP32_PREFERRED_VENDORS = ("10c4", "0403")
# HARD BAN (CLAUDE.md, guardrail): USB vendor 1a86 is the CH340 KVM CONTROL adapter on this host
# (/dev/ttyUSB0). Writing HID frames there would drive the KVM, not an ESP32. NEVER auto-select it.
# (A CH340 ESP32 elsewhere would need an explicit BT_HID_ESP32_PORT; on wsb 1a86 is always the KVM.)
_BANNED_VENDORS = ("1a86",)
_FTDI_VENDOR = "0403"


class Esp32Error(RuntimeError):
    pass


def encode_frame(kind: str, body: bytes) -> bytes:
    """Encode one (kind, body) HID report frame into a firmware serial command line.

    Pure + offline-testable. Raises Esp32Error on an unknown kind or a wrong body length.
    """
    spec = _KIND_PREFIX.get(kind)
    if spec is None:
        raise Esp32Error(f"unknown report kind {kind!r}")
    prefix, expect = spec
    if len(body) != expect:
        raise Esp32Error(f"{kind} body must be {expect} bytes, got {len(body)}")
    return prefix + body.hex().encode("ascii") + b"\n"


def _tty_usb_vendor(dev: str) -> str:
    """Return the USB idVendor (lowercase hex string) of a tty device, or '' if not resolvable.

    Walks up from /sys/class/tty/<name>/device to the enclosing USB device (mirrors the Pico
    backend's vendor gate), so autodetect can vendor-gate rather than guess from name strings.
    """
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


def _tty_usb_iface(dev: str) -> str:
    """Return the USB bInterfaceNumber (e.g. '00'/'01') of a tty device, or '' if not resolvable.

    Used to pick the ESP32 UART channel on a dual-channel FT2232H (channel A = JTAG, channel B =
    UART on the ESP32-Ethernet-Kit): the interface node lives one level up from the tty in sysfs.
    """
    name = os.path.basename(os.path.realpath(dev))
    cur = os.path.realpath(os.path.join("/sys/class/tty", name, "device"))
    for path in (cur, os.path.dirname(cur)):
        p = os.path.join(path, "bInterfaceNumber")
        if os.path.exists(p):
            try:
                with open(p) as f:
                    return f.read().strip()
            except OSError:
                return ""
    return ""


def detect_port(candidates: list[str] | None = None, vendor_of=None, iface_of=None) -> str | None:
    """Best-effort auto-detect of the ESP32 serial port -- VENDOR-GATED, never the KVM.

    Honors BT_HID_ESP32_PORT. Otherwise scans /dev/ttyUSB* + /dev/ttyACM*, HARD-EXCLUDES the KVM
    CH340 (vendor 1a86 -- see _BANNED_VENDORS; writing HID there would drive the KVM), and prefers a
    known ESP32 bridge (CP2102 10c4, then FTDI 0403) over any other serial node. On a dual-channel
    FT2232H (vendor 0403) it prefers the HIGHER interface number (channel B = the ESP32 UART) over
    interface 00 (channel A = JTAG on the ESP32-Ethernet-Kit). Returns None if no permissible device
    is found (the caller then asks the user to pass --port).

    `candidates` + `vendor_of` + `iface_of` are injectable for unit tests (no /sys, no hardware).
    """
    env = os.environ.get("BT_HID_ESP32_PORT")
    if env:
        return env
    if candidates is None:
        candidates = sorted(glob.glob("/dev/ttyUSB*")) + sorted(glob.glob("/dev/ttyACM*"))
    vendor_of = vendor_of or _tty_usb_vendor
    iface_of = iface_of or _tty_usb_iface
    # Vendor priority: CP2102 (the flashed DevKit) first, then FTDI (Ethernet-Kit), then any node.
    vprio = {"10c4": 0, "0403": 1}
    ranked: list[tuple[int, int, str]] = []
    for dev in candidates:
        vid = (vendor_of(dev) or "").lower()
        if vid in _BANNED_VENDORS:
            continue   # NEVER the KVM CH340
        score = vprio.get(vid, 2)
        # FT2232H channel A (interface 00) is JTAG on the Ethernet-Kit; prefer channel B (the UART).
        iface_pen = 0
        if vid == _FTDI_VENDOR:
            try:
                iface_pen = -int(iface_of(dev) or "0")   # higher interface -> more preferred
            except ValueError:
                iface_pen = 0
        ranked.append((score, iface_pen, dev))
    ranked.sort()
    return ranked[0][2] if ranked else None


class Esp32Transport:
    """HidController transport that streams report frames to the ESP32 over USB serial.

    `port` may be a device path (str, opened lazily via pyserial) or any object exposing
    `write(bytes)` + `readline() -> bytes` (a fake serial for tests / an already-open handle) --
    which is also how `transport_net.NetTransport` reuses this whole framing layer over a TCP
    channel: the wire changes, the encoded frames do not.
    """

    BACKEND = "esp32"          # label in the status dicts; subclasses (net) override it

    def __init__(self, port=None, baud: int = DEFAULT_BAUD, timeout: float = 2.0,
                 open_now: bool = True, prefer_raw: bool = True):
        self.baud = baud
        self.timeout = timeout
        self.prefer_raw = prefer_raw
        self._ser = None
        self._rebooted_on_open = False   # True if the last open triggered a (cold) board reset
        if port is None or isinstance(port, str):
            self.port = port
            if open_now:
                self._open()
        else:
            # duck-typed serial-like object (tests / caller-managed handle)
            self.port = getattr(port, "port", "<obj>")
            self._ser = port

    # -- lifecycle --
    def _open(self):
        if self._ser is not None:
            return
        dev = self.port or detect_port()
        if not dev:
            raise Esp32Error("no ESP32 serial port found; pass --port /dev/ttyUSBx or set "
                             "BT_HID_ESP32_PORT (flash the firmware first)")
        from . import serial_link
        try:
            # NO-RESET open: on Linux this uses RawSerialLink (leaves the tty at B0 on close so the
            # next open does not assert DTR/RTS -> no EN pulse -> the board does NOT reboot and keeps
            # its BLE link). See serial_link.py. Falls back to pyserial off Linux (which resets, but
            # the persistent session + reconnect still self-heal).
            self._ser = serial_link.open_serial(dev, self.baud, self.timeout,
                                                prefer_raw=self.prefer_raw)
        except ImportError as e:
            raise Esp32Error("pyserial not installed: uv run --with pyserial ... (or pip install "
                             "pyserial)") from e
        self.port = dev
        self._settle()

    def _settle(self, deadline: float = 1.5):
        """Detect whether this open cold-reset the board; drain any boot banner so it does not eat
        the first command. With the no-reset path there is no banner (fast path)."""
        import time
        self._rebooted_on_open = False
        end = time.time() + deadline
        while time.time() < end:
            raw = self._ser.readline()
            if not raw:
                return   # quiet line == no reset; ready immediately (no-reset fast path)
            if b"HIDBRIDGE-esp32 ready" in raw or b"POWERON_RESET" in raw or b"rst:0x" in raw:
                self._rebooted_on_open = True   # a cold reset happened; keep draining briefly
        return

    def open_channel(self):
        """Open the serial/socket channel if needed and return the duck-typed line object
        (write(bytes)/readline()->bytes). This is the JSON-RPC channel `board.McuBoard` drives over --
        one uniform hook across the esp32 (reset-free serial), net (TLS socket) and pico transports."""
        if self._ser is None:
            self._open()
        return self._ser

    def close(self):
        if self._ser is not None and hasattr(self._ser, "close"):
            try:
                self._ser.close()
            finally:
                self._ser = None

    # -- context manager: open-once / use / close (leaves the tty B0 for a reset-free next open) --
    def __enter__(self):
        if self._ser is None:
            self._open()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- transport interface --
    def send(self, frames: list[tuple[str, bytes]]) -> dict:
        """Send a list of (kind, body) report frames; return {"ok": bool, ...}."""
        if self._ser is None:
            self._open()
        sent = 0
        for kind, body in frames:
            line = encode_frame(kind, body)
            self._ser.write(line)
            flush = getattr(self._ser, "flush", None)
            if flush:
                flush()
            resp = self._readline()
            if not resp.startswith("OK"):
                return {"ok": False, "error": f"esp32 rejected {kind} frame: {resp!r}",
                        "sent": sent}
            sent += 1
        # `resp` is the last frame's ack ("OK" = notified the target slot's central, "OK noconn" =
        # the board had no central on the target slot to notify).
        return {"ok": True, "sent": sent, "resp": resp, "backend": self.BACKEND, "port": self.port}

    def _readline(self) -> str:
        # skip async EVT lines; return the first non-EVT reply (bounded)
        for _ in range(8):
            raw = self._ser.readline()
            if not raw:
                return ""
            text = raw.decode("ascii", "replace").strip()
            if text.startswith("EVT"):
                continue
            return text
        return ""


def make_transport(**opts) -> Esp32Transport:
    """Factory used by the generic backend loader (controller.make_transport('esp32'))."""
    return Esp32Transport(port=opts.get("port"), baud=opts.get("baud", DEFAULT_BAUD),
                          timeout=opts.get("timeout", 2.0),
                          open_now=opts.get("open_now", True),
                          prefer_raw=opts.get("prefer_raw", True))

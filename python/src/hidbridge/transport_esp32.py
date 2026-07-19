"""ESP32 HID backend transport for bt_hid.

An ESP32 (~$5) is an alternative HID BACKEND: instead of a USB Bluetooth dongle driven by BlueZ, an
ESP32 running the `HIDBRIDGE-esp32` firmware (hidbridge/firmware/esp32/) is the BLE HID keyboard+
mouse peripheral, and this host talks to it over USB serial. The production drive path is JSON-RPC
(`board.McuBoard` + `rpc.py`): the BOARD builds every report. This module owns the radio/device-mgmt
side of the same serial channel (advertise/ping/status/slots), plus a pure host-side report FRAMER
(`encode_frame`/`send`) kept for the offline byte-parity tests + the central self-test's encodability
check. NOTE: the firmware NO LONGER accepts K/M frame lines on the wire (the host-builds relay was
removed); `send()` is a host encoder, not a live MCU drive path.

Why an ESP32 backend at all: the BlueZ/dongle path is stuck on iOS not subscribing to our HID report
characteristics (a CCCD / Report-Reference GATT quirk). The ESP32's NimBLE HID-over-GATT stack serves
that correctly + iOS-compatibly out of the box, so this backend works where BlueZ fights us. It also
lowers the contributor hardware bar: an ESP32 is a common, cheap board.

Serial wire protocol (must match the firmware; newline-terminated ASCII lines, host -> ESP32). A line
is routed by its FIRST char: '{' = JSON-RPC (the board builds the report), else a single-char command:
    P        ping   -> "OK HIDBRIDGE-esp32 subscribed=<0|1> conn=<0|1>"
    A        (re)start advertising -> "OK adv"  (bare 'A' with NO hex body; disambiguated by length)
The ESP32 answers each line with a single "OK..."/"ERR..." line. Asynchronous link state is emitted
as "EVT connected"/"EVT disconnected", and HID-report subscription as "EVT ready"/"EVT unsubscribed"
(iOS wrote/cleared the report CCCDs). The ping's subscribed= field is the readiness signal HidSession
awaits; it is placed BEFORE conn= so a legacy endswith("conn=1") parser still works. An older
firmware without the field replies "OK HIDBRIDGE-esp32 conn=<0|1>" (subscribed parses as unknown).

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


# --------------------------------------------------------------------------- #
# Per-bond IDENTITY (host-slot) commands -- pure framing + reply parsers.
#
# A board presents a DISTINCT BLE identity/address per bonded iPad ("host slot"), like a multi-host
# keyboard. On the ESP32 (NimBLE) slot 0 is the board's NATIVE public address (so an existing bond
# survives a reflash) and slots >=1 are derived static-random addresses; the firmware can hold
# MULTIPLE centrals connected AT ONCE (CONFIG_BT_NIMBLE_MAX_CONNECTIONS>1) and notify a SPECIFIC one.
#   I<n>  select slot n as the advertising identity + (re)start advertising it
#         -> "OK slot=<n> addr=<12hex> conn=<0|1>"
#   T<n>  set the SEND-target slot (subsequent K/M/A/D notify ONLY that slot's connection)
#         -> "OK tgt=<n>"
#   S     slots status -> "OK slots=<N> adv=<n> tgt=<n> multi=<0|1> c0=<0|1> c1=.. s0=<0|1> s1=.."
#         (multi=1: the board can hold distinct identities SIMULTANEOUSLY; c<i>=connected, s<i>=iOS
#          subscribed on slot i)
# --------------------------------------------------------------------------- #
def encode_slot_select(slot: int, name: str | None = None) -> bytes:
    """`I<n>` / `I<n>:<name>` -- select/create host slot n as the advertising identity + advertise.

    An optional `name` sets that slot's advertised BLE LocalName so the identity names its TARGET
    iPad (e.g. `HIDBRIDGE-esp32-mini5`); see naming.slot_identity_name. The firmware disambiguates the
    two forms by the ':' separator.
    """
    if not isinstance(slot, int) or slot < 0:
        raise Esp32Error(f"slot must be a non-negative int, got {slot!r}")
    if name:
        if ":" in name or "\n" in name:
            raise Esp32Error(f"slot name must not contain ':' or newline, got {name!r}")
        return f"I{slot}:{name}\n".encode("ascii")
    return f"I{slot}\n".encode("ascii")


def encode_slot_target(slot: int) -> bytes:
    """`T<n>` -- route subsequent HID reports to slot n's connection (the send target)."""
    if not isinstance(slot, int) or slot < 0:
        raise Esp32Error(f"slot must be a non-negative int, got {slot!r}")
    return f"T{slot}\n".encode("ascii")


def encode_slots_query() -> bytes:
    """`S` -- query the slot table / per-slot connection + subscription state."""
    return b"S\n"


def _kv_tokens(resp: str) -> dict:
    """Parse space-separated key=value tokens (order-independent), like the ping parser."""
    return dict(t.split("=", 1) for t in (resp or "").split() if "=" in t)


# --------------------------------------------------------------------------- #
# NETWORK commands (an Ethernet-equipped board; firmware built with -DHIDBRIDGE_NET).
#
# The board serves this SAME line protocol on TCP 3333 (see transport_net.py). Two commands manage
# that path, and both are meaningful over SERIAL:
#   N        network status -> "OK net eth=<0|1> ip=<a.b.c.d> port=<n> mdns=<host> token=<0|1>
#                              clients=<n>"   (a serial-only firmware answers eth=0)
#   W<token> provision the network auth token. SERIAL ONLY -- the firmware refuses it over TCP, so
#            the secret can only be set across the physical cable. The board never echoes it back.
# --------------------------------------------------------------------------- #
MIN_TOKEN_LEN = 8      # must match HB_MIN_TOKEN_LEN in firmware/esp32/src/main.cpp


def encode_set_token(token: str) -> bytes:
    """`W<token>` -- set the board's network auth token (send over SERIAL only).

    SECURITY: the caller must never log the returned line -- it CONTAINS the secret.
    """
    if not isinstance(token, str) or len(token) < MIN_TOKEN_LEN:
        raise Esp32Error(f"token must be a string of >= {MIN_TOKEN_LEN} chars")
    if "\n" in token or "\r" in token:
        raise Esp32Error("token must not contain a newline")
    return f"W{token}\n".encode("ascii")


def parse_net_status(resp: str) -> dict:
    """Parse an `N` reply into {ok, eth, ip, port, mdns, token, clients, resp}.

    `token` is whether a secret has been PROVISIONED (never the secret itself); `eth` is whether the
    board has a live Ethernet link with an IP. A firmware without the network build answers eth=0.
    """
    toks = _kv_tokens(resp)
    port = toks.get("port")
    clients = toks.get("clients")
    return {
        "ok": (resp or "").startswith("OK"),
        "eth": toks.get("eth") == "1",
        "ip": toks.get("ip"),
        "port": int(port) if port is not None and port.isdigit() else None,
        "mdns": toks.get("mdns") or None,
        "token": toks.get("token") == "1",
        "clients": int(clients) if clients is not None and clients.isdigit() else None,
        "resp": resp,
    }


def parse_slot_reply(resp: str) -> dict:
    """Parse an `I<n>` reply 'OK slot=<n> addr=<hex> conn=<0|1>' into a dict."""
    toks = _kv_tokens(resp)
    slot = toks.get("slot")
    return {
        "ok": (resp or "").startswith("OK"),
        "slot": int(slot) if slot is not None and slot.isdigit() else None,
        "addr": toks.get("addr"),
        "name": toks.get("name"),
        "connected": toks.get("conn") == "1",
        "resp": resp,
    }


def parse_slots_status(resp: str) -> dict:
    """Parse an `S` reply into {ok, slots, adv, tgt, multi, conn:{i:bool}, sub:{i:bool}}."""
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
        "ok": (resp or "").startswith("OK"),
        "slots": _int("slots"),
        "adv": _int("adv"),
        "tgt": _int("tgt"),
        "multi": toks.get("multi") == "1",
        "conn": conn,
        "sub": sub,
        "resp": resp,
    }


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
    which is also how `transport_net.NetTransport` reuses this whole command layer over a TCP
    channel: the wire changes, the protocol does not.
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
            # the persistent session + wait_connected still self-heal).
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

    def wait_connected(self, timeout: float = 45.0, poll: float = 1.0) -> bool:
        """Poll the board until the bonded iOS device (re)connects (conn=1) or timeout.

        This is the reconnect half of the robust lifecycle: after a (cold) reset the iPad/iPhone
        auto-reconnects to the bonded peripheral within a few seconds -- we wait for that BEFORE
        sending HID so reports are not dropped on a down link. Returns True once connected.
        """
        import time
        end = time.time() + timeout
        while time.time() < end:
            try:
                st = self.ping()
            except Exception:  # noqa: BLE001 -- transient serial hiccup; retry within the window
                st = {"connected": False}
            if st.get("connected"):
                return True
            time.sleep(poll)
        return False

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

    def ping(self) -> dict:
        if self._ser is None:
            self._open()
        self._ser.write(b"P\n")
        resp = self._readline()
        ok = resp.startswith("OK")
        # Parse space-separated key=value tokens, order-independent. The firmware reply is
        # "OK HIDBRIDGE-esp32 [subscribed=<0|1> ]conn=<0|1>"; an OLD firmware omits subscribed=.
        toks = dict(t.split("=", 1) for t in resp.split() if "=" in t)
        sub = toks.get("subscribed")
        return {"ok": ok, "resp": resp, "backend": self.BACKEND, "port": self.port,
                "connected": toks.get("conn") == "1",
                "subscribed": None if sub is None else (sub == "1")}

    def is_ready(self) -> bool:
        """True iff iOS has SUBSCRIBED to (enabled notifications on) the HID report chars.

        This is the real "the central will act on our reports" signal -- stronger than is_connected
        (a link can be up for ~1-3s before iOS writes the report CCCDs). Returns False if the
        firmware does not report the subscription field (caller then uses a settle fallback)."""
        try:
            return self.ping().get("subscribed") is True
        except Exception:  # noqa: BLE001
            return False

    def wait_ready(self, timeout: float = 8.0, poll: float = 0.5) -> bool:
        """Poll until the bonded iOS device subscribes to the HID reports (is_ready) or timeout."""
        import time
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self.ping().get("subscribed") is True:
                    return True
            except Exception:  # noqa: BLE001  -- transient serial hiccup; retry within the window
                pass
            if poll > 0:
                time.sleep(poll)
        return self.is_ready()

    def advertise(self) -> dict:
        if self._ser is None:
            self._open()
        self._ser.write(b"A\n")
        resp = self._readline()
        return {"ok": resp.startswith("OK"), "resp": resp}

    def is_connected(self) -> bool:
        """True iff a central (the bonded iOS device) is currently connected to the board."""
        try:
            return bool(self.ping().get("connected"))
        except Exception:  # noqa: BLE001
            return False

    def disconnect(self) -> dict:
        """Drop the current central's BLE link but KEEP the bond (connect-on-demand teardown).

        Sends the firmware 'X' command. On firmware too old to support it the board replies
        'ERR unknown' (harmless); the link then stays up until the port is idle.
        """
        if self._ser is None:
            self._open()
        self._ser.write(b"X\n")
        resp = self._readline()
        return {"ok": resp.startswith("OK"), "resp": resp}

    def park(self) -> dict:
        """Stop advertising but KEEP existing connections + bonds (`O`).

        Lets the OTHER board serve an iPad without this board grabbing it -- the deterministic
        cross-board handoff. Resume advertising a slot with select_slot().
        """
        if self._ser is None:
            self._open()
        self._ser.write(b"O\n")
        resp = self._readline()
        return {"ok": resp.startswith("OK"), "resp": resp}

    # -- per-bond identity (host-slot) control --
    def select_slot(self, slot: int, name: str | None = None) -> dict:
        """Select host slot `slot` as the advertising identity + (re)advertise it (`I<n>`).

        Slot 0 = the board's native identity (existing bond); slots >=1 = distinct static-random
        identities. An optional `name` sets that slot's advertised BLE name (target-scoped, e.g.
        `HIDBRIDGE-esp32-mini5`). Use this to bring a specific bonded iPad's identity up so IT reconnects,
        and to add a SECOND identity while the first is still connected (simultaneous hold).
        """
        if self._ser is None:
            self._open()
        self._ser.write(encode_slot_select(slot, name))
        return parse_slot_reply(self._readline())

    def target_slot(self, slot: int) -> dict:
        """Route subsequent HID reports to slot `slot`'s connection (`T<n>`).

        With two iPads held at once, pick which one the next K/M/A/D reports drive.
        """
        if self._ser is None:
            self._open()
        self._ser.write(encode_slot_target(slot))
        resp = self._readline()
        toks = _kv_tokens(resp)
        tgt = toks.get("tgt")
        return {"ok": resp.startswith("OK"),
                "tgt": int(tgt) if tgt is not None and tgt.isdigit() else None, "resp": resp}

    def slots_status(self) -> dict:
        """Query the slot table + per-slot connect/subscribe state (`S`)."""
        if self._ser is None:
            self._open()
        self._ser.write(encode_slots_query())
        return parse_slots_status(self._readline())

    # -- network path (an Ethernet-equipped board; see transport_net.py) --
    def net_status(self) -> dict:
        """Query the board's Ethernet/TCP state (`N`): link, IP, port, mDNS name, token, clients."""
        if self._ser is None:
            self._open()
        self._ser.write(b"N\n")
        return parse_net_status(self._readline())

    def set_net_token(self, token: str) -> dict:
        """Provision the board's network auth token (`W<token>`) -- over SERIAL only.

        SECURITY: `token` is the shared secret. It crosses the (physical) serial cable and lands in
        the board's NVS; the firmware never echoes it and neither does this method -- the returned
        dict carries only the board's "OK token set" acknowledgement, never the token.
        """
        if self._ser is None:
            self._open()
        self._ser.write(encode_set_token(token))
        resp = self._readline()
        return {"ok": resp.startswith("OK"), "resp": resp}

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

"""Real board drivers behind `HidBridge` -- resolve a LOCATION to a board object.

`bridge._resolve_board(location, ...)` calls `resolve_board()` here; this module holds the concrete
board objects that conform to the protocol `HidBridge` drives (prepare/close/board_info/capabilities/
list_devices/pair/unpair/rename/connect/disconnect/transport/wait_ready). The board SELF-IDENTIFIES:
the caller never passes a board TYPE -- the location's SHAPE picks a family, and a probe fills in
`board` (esp32/pico) + `fw`.

  a device path ("/dev/ttyUSB3", or a bare path) ....... a serial MCU (esp32/pico)
  "hciN" or a MAC ...................................... the BlueZ dongle
  a host / IP / "*.local" ............................. a network MCU over TLS (needs `token`)
  None ................................................ autodetect a plugged-in serial board

The MCU family reuses the existing, load-bearing layers rather than reimplementing BLE: the JSON-RPC
client (`rpc.RpcClient`) drives BOTH the name-aware device management (identities/pair/unpair/connect/
nickname/layout) AND the high-level HID verbs (type/key/move/click/scroll) over the SAME open channel.
The BOARD builds the report bytes on the firmware -- so the target slot's per-slot keyboard LAYOUT
applies board-side (the point of "board in charge"). The raw frame transport (`transport_esp32`/
`transport_net`, incl. the RESET-FREE serial open) is kept only for the board-level radio ops
(advertise / net-status / ping), not for driving.

A BlueZ dongle has no board to delegate to, so DongleBoard builds the bytes HOST-side via reports.py /
HidController and hands them to the daemon -- the SAME high-level verbs, host-built under the hood.

This module lazy-imports serial/dbus INSIDE the resolver, so `import hidbridge` stays dep-free.
"""

from __future__ import annotations

import os
import re

from .bridge import (  # typed errors reused
    HidbridgeError,
    IncompatibleFirmware,
    NotBonded,
    ProbeError,
    Unsupported,
)


# --------------------------------------------------------------------------- #
# Device record (what list_devices returns)
# --------------------------------------------------------------------------- #
class Device:
    """One entry in `bridge.list_devices()`. `name` is the connect() key (a client label / BLE name)."""

    def __init__(self, name, *, bonded=True, connected=False, slot=None, layout=None):
        self.name = name
        self.bonded = bonded
        self.connected = connected
        self.slot = slot
        self.layout = layout

    def __repr__(self):
        return (f"Device(name={self.name!r}, bonded={self.bonded}, connected={self.connected}"
                f"{f', slot={self.slot}' if self.slot is not None else ''})")


class AmbiguousLocation(HidbridgeError):
    """Autodetect found several candidate boards; name one explicitly (see discover())."""


class NoBoardFound(HidbridgeError):
    """Autodetect found no plugged-in board."""


def _board_from_device_name(device: str) -> str:
    """'HIDBRIDGE-esp32' -> 'esp32', 'HIDBRIDGE-pico' -> 'pico'; fall back to esp32."""
    d = (device or "").lower()
    if "pico" in d:
        return "pico"
    if d.startswith("hidbridge-"):
        tail = d.split("-", 1)[1]
        # the base device name is HIDBRIDGE-<board>; a nickname replaces it, so only trust known tails
        if tail in ("esp32", "pico"):
            return tail
    return "esp32"


# --------------------------------------------------------------------------- #
# MCU board (esp32 / pico serial, and net over TCP)
# --------------------------------------------------------------------------- #
class McuBoard:
    """A serial/network MCU running the hidbridge firmware. Both device management AND the drive verbs
    are JSON-RPC (rpc.py) over the transport's OWN open channel -- the BOARD builds every report (the
    slot's layout applies board-side). The raw frame transport is kept only for the board-level radio
    ops (advertise/net-status/ping)."""

    def __init__(self, *, transport_factory=None, transport=None, rpc_client=None):
        # transport_factory() -> an open frame transport (owns the reset-free channel). Injectables
        # `transport`/`rpc_client` skip the factory + probe for offline tests.
        self._factory = transport_factory
        self._tp = transport
        self._rpc = rpc_client
        self._info: dict = {}
        self.capabilities: dict = {}
        self._by_name: dict = {}     # name -> slot, refreshed on list_devices/connect

    # ---- lifecycle / probe ----
    def prepare(self):
        if self._tp is None:
            self._tp = self._factory()
        if self._rpc is None:
            from .rpc import RpcClient
            ch = _transport_channel(self._tp)
            if ch is None:
                raise HidbridgeError("MCU transport exposes no open JSON-RPC channel")
            self._rpc = RpcClient(ch)
        self._probe()

    def _probe(self):
        from .rpc import RpcProtocolError, RpcTimeout
        port = getattr(self._tp, "port", None) or "the board"
        try:
            h = self._rpc.hello()
        except RpcProtocolError as e:
            # The board ANSWERED but not with JSON-RPC -- legacy / pre-JSON-RPC firmware. Refuse with a
            # clear, typed error (upgrade the firmware) rather than a degraded, capability-less board
            # that would only fail cryptically on the first drive verb.
            raise IncompatibleFirmware(
                f"{port} runs firmware that does not speak hidbridge JSON-RPC (legacy / pre-RPC "
                f"build); upgrade the firmware (or pin an older wheel). [{e}]") from e
        except RpcTimeout as e:
            # Silence: nothing that speaks hidbridge is here (unreachable / asleep / wrong device / a
            # Pico opened without DTR). RAISE -- never hand back a driveless board.
            raise ProbeError(
                f"no hidbridge board answered at {port} (asleep, unplugged, wrong device, or "
                f"incompatible firmware). [{e}]") from e
        board = _board_from_device_name(h.get("device", ""))
        self._info = {"board": board, "fw": h.get("fw", ""), "device": h.get("device", ""),
                      "nick": h.get("nick", ""), "proto": h.get("proto"),
                      "layouts": list(h.get("layouts") or [])}
        caps = frozenset(h.get("caps") or [])
        # ESP32/NimBLE holds several centrals at once (multi identities); the Pico wrapper is single.
        max_sessions = 2 if board == "esp32" else 1
        self.capabilities = {
            "max_sessions": max_sessions,
            "per_device_identity": "connect" in caps,
            "layouts": "layout" in caps,
            "logs": "logs" in caps,
            "rpc_frames": "frames" in caps,
            "medium": getattr(self._tp, "BACKEND", board),
            "caps": sorted(caps),
        }

    def close(self):
        if self._tp is not None:
            try:
                self._tp.close()
            finally:
                self._tp = None
                self._rpc = None

    def board_info(self) -> dict:
        return dict(self._info)

    # ---- device management (JSON-RPC, name-aware) ----
    def _identities(self) -> list:
        slots = self._rpc.identities().get("slots", [])
        self._by_name = {}
        devs = []
        for s in slots:
            client = s.get("client") or ""
            bonded = bool(s.get("bonded"))
            if not client and not bonded:
                continue                          # an empty, unbonded slot is not a device
            name = client or f"slot{s.get('slot')}"
            self._by_name[name] = s.get("slot")
            devs.append(Device(name, bonded=bonded or bool(client),
                               connected=bool(s.get("conn")), slot=s.get("slot"),
                               layout=s.get("layout")))
        return devs

    def list_devices(self) -> list:
        return self._identities()

    def _slot_of(self, name):
        if name in self._by_name:
            return self._by_name[name]
        self._identities()                        # refresh the name->slot map
        return self._by_name.get(name)

    def pair(self, name: str):
        return self._rpc.pair(name)

    def unpair(self, name: str):
        slot = self._slot_of(name)
        params = {"slot": slot} if slot is not None and name.startswith("slot") else {"client": name}
        self._rpc.require_cap("connect")
        return self._rpc.call("unpair", params)

    def rename(self, nick: str):
        return self._rpc.nickname(nick)

    # ---- connect on demand ----
    def _connect_params(self, name):
        slot = self._slot_of(name)
        if slot is None:
            raise NotBonded(f"{name!r} is not a known device on this board")
        return ({"slot": slot} if name.startswith("slot") else {"client": name}), slot

    def connect(self, name: str, timeout: float) -> bool:
        import time
        params, slot = self._connect_params(name)
        self._rpc.require_cap("connect")
        self._rpc.call("connect", params)          # bring the identity up + claim it for this session
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self._slot_connected(slot):
                return True
            time.sleep(0.5)
        return self._slot_connected(slot)

    def disconnect(self, name: str):
        slot = self._slot_of(name)
        params = {"slot": slot} if slot is not None and name.startswith("slot") else {"client": name}
        self._rpc.require_cap("connect")
        return self._rpc.call("disconnect", params)

    def _slot_state(self, slot):
        for s in self._rpc.identities().get("slots", []):
            if s.get("slot") == slot:
                return s
        return {}

    def _slot_connected(self, slot) -> bool:
        return bool(self._slot_state(slot).get("conn"))

    def wait_ready(self, name: str, timeout: float) -> bool:
        """Block until the device has SUBSCRIBED to the HID reports (iOS activation window)."""
        import time
        slot = self._slot_of(name)
        if slot is None:
            return False
        end = time.monotonic() + timeout
        while True:
            if bool(self._slot_state(slot).get("sub")):
                return True
            if time.monotonic() >= end:
                return bool(self._slot_state(slot).get("sub"))
            time.sleep(0.5)

    # ---- drive verbs: tell the firmware WHAT; it builds the reports (slot layout applies board-side) ----
    def type(self, text: str):
        return self._rpc.type_text(text)

    def key(self, name: str, mod: int = 0):
        return self._rpc.key(name, mod)

    def move(self, dx: int, dy: int):
        return self._rpc.move(dx, dy)

    def click(self, button: str = "left"):
        return self._rpc.click(button)

    def scroll(self, clicks: int):
        return self._rpc.scroll(clicks)

    # ---- board-level radio / management ops (JSON-RPC now, not single-char). The CLI's ping /
    #      pair-mode advertise / net-status route through these; each is a thin pass to rpc.py, which
    #      capability-gates so an older/minimal board fails clearly instead of sending a dead command. ----
    def ping(self) -> dict:
        return self._rpc.ping()

    def advertise(self) -> dict:
        return self._rpc.advertise()

    def park(self) -> dict:
        return self._rpc.park()

    def net_status(self) -> dict:
        return self._rpc.net_status()

    def provision(self, token: str) -> dict:
        """Set the network auth token (SERIAL ONLY -- the firmware refuses `provision` over TCP)."""
        return self._rpc.provision(token)

    def set_log_level(self, level: str) -> dict:
        """Set the board's runtime log filter ('error'|'warn'|'info'|'debug'); board `log`
        notifications route to the `hidbridge.mcu` logger (see rpc._route_log)."""
        return self._rpc.set_log_level(level)

    # ---- raw frame transport: NOT the drive path; kept for the offline byte-parity tests + the
    #      central self-test's encodability check. NOT a live MCU drive path (the board builds reports). ----
    def transport(self, name=None):
        return self._tp


# --------------------------------------------------------------------------- #
# BlueZ dongle board
# --------------------------------------------------------------------------- #
class DongleBoard:
    """The host's own BlueZ adapter + the per-adapter ble_hid daemon. Single BLE identity: it holds one
    bonded host at a time and pairs via `pair-mode`, so pair/unpair/rename here are Unsupported."""

    def __init__(self, dongle_id=None, *, allow_builtin=False, link_manager=None,
                 transport=None):
        from . import conn as _conn
        self._backend = _conn.DongleLinkBackend(dongle_id, allow_builtin=allow_builtin,
                                                link_manager=link_manager, transport=transport)
        self._ctl = None
        self.capabilities = {"max_sessions": 1, "per_device_identity": False, "layouts": True,
                             "medium": "bluez"}

    def prepare(self):
        self._backend.prepare()

    def close(self):
        try:
            self._backend.disconnect()
        except Exception:  # noqa: BLE001
            pass

    def board_info(self) -> dict:
        info = self._backend.info or {}
        return {"board": "dongle", "fw": info.get("hci") or self._backend.slug or "", "hci": info.get("hci")}

    def list_devices(self) -> list:
        link = self._backend.link
        out = []
        for _path, props in link.all_devices(self._backend.adapter_path):
            name = str(props.get("Alias", props.get("Name", ""))) or str(props.get("Address", ""))
            out.append(Device(name, bonded=bool(props.get("Paired")),
                              connected=bool(props.get("Connected"))))
        return out

    def pair(self, name):
        raise Unsupported("the dongle pairs via `hidbridge pair-mode` (BlueZ), not bridge.pair()")

    def unpair(self, name):
        raise Unsupported("remove a dongle bond with bluetoothctl / BlueZ, not bridge.unpair()")

    def rename(self, nick):
        raise Unsupported("the dongle name is per-adapter (naming.py); rename via pair-mode --name")

    def connect(self, name, timeout) -> bool:
        return self._backend.connect(name, timeout, exclusive=True)

    def disconnect(self, name):
        self._backend.disconnect()

    def wait_ready(self, name, timeout) -> bool:
        return self._backend.wait_ready(timeout)

    # ---- drive verbs: no board to delegate to, so build the reports HOST-side (reports.py) and hand
    #      them to the daemon over the control socket. Same surface as the MCU, host-built. ----
    def _controller(self):
        if self._ctl is None:
            from .controller import HidController
            self._ctl = HidController(self._backend.transport())
        return self._ctl

    def type(self, text: str):
        return self._controller().type_text(text)

    def key(self, name: str, mod: int = 0):
        return self._controller().send_key(name, mod)

    def move(self, dx: int, dy: int):
        return self._controller().move(dx, dy)

    def click(self, button: str = "left"):
        return self._controller().click(button)

    def scroll(self, clicks: int):
        return self._controller().scroll(clicks)

    def transport(self, name=None):
        return self._backend.transport()


# --------------------------------------------------------------------------- #
# location classification + resolver + discovery
# --------------------------------------------------------------------------- #
_HCI_RE = re.compile(r"^hci\d+$", re.IGNORECASE)


def _is_mac(s: str) -> bool:
    parts = s.split(":")
    return len(parts) == 6 and all(len(p) == 2 and all(c in "0123456789abcdefABCDEF" for c in p)
                                   for p in parts)


def _classify(location):
    """Map a location to a family: 'auto' | 'serial' | 'dongle' | 'net' (+ the normalised arg)."""
    if location is None:
        return "auto", None
    s = str(location).strip()
    if s.startswith("/dev/") or s.startswith("./") or s.startswith("/") or os.path.exists(s):
        return "serial", s
    if _HCI_RE.match(s) or _is_mac(s):
        return "dongle", s
    return "net", s                               # host / IP / *.local


def _transport_channel(tp):
    """Return the transport's open JSON-RPC channel (write(bytes)/readline()->bytes), opening it if
    needed. Prefers the uniform `open_channel()` hook (esp32/net/pico all expose it); falls back to an
    already-open `_ser` or an `_open()` nudge for a duck-typed fake that has neither."""
    getter = getattr(tp, "open_channel", None)
    if getter is not None:
        return getter()
    ch = getattr(tp, "_ser", None)
    if ch is None:
        opener = getattr(tp, "_open", None)
        if opener is not None:
            opener()
        ch = getattr(tp, "_ser", None)
    return ch


def _is_pico_port(port) -> bool:
    """True for a Pico native USB-CDC device: a /dev/ttyACM* whose USB idVendor is Raspberry Pi
    (2e8a). Such a port needs a DTR-ASSERTED open (the Pico's CDC only transmits with DTR up); an
    ESP32 bridge (/dev/ttyUSB*, CP210x/FT2232) instead needs the reset-free B0 open (DTR de-asserted),
    so the transport family is keyed off this."""
    from . import transport_pico as TP
    # Resolve /dev/serial/by-id/... symlinks (the stable path we hand out) to the real /dev/ttyACMx
    # BEFORE the basename check -- a by-id path's basename is "usb-...-if00", not "ttyACM*", so without
    # this a by-id Pico location falls back to the ESP32 transport and the Pico's CDC never transmits.
    real = os.path.realpath(str(port or ""))
    base = os.path.basename(real)
    if not base.startswith("ttyACM"):
        return False
    try:
        return TP._tty_usb_vendor(real) == TP._PICO_USB_VENDOR
    except OSError:
        return False


def _serial_transport_factory(port, *, board_hint=None, **opts):
    def make():
        # The Pico's native USB-CDC needs a plain DTR-asserted open (transport_pico); the ESP32's
        # bridge needs the reset-free B0 open (transport_esp32). Honour an explicit board_hint, else
        # pick by the device (ttyACM + Raspberry Pi VID = a Pico); default to the ESP32 transport.
        use_pico = board_hint == "pico" or (board_hint is None and _is_pico_port(port))
        if use_pico:
            from .transport_pico import make_transport as mk
        else:
            from .transport_esp32 import make_transport as mk
        return mk(port=port, **opts)
    return make


def _net_transport_factory(host, *, token=None, **opts):
    def make():
        from .transport_net import make_transport as mk
        return mk(host=host, token=token, **opts)
    return make


def resolve_board(location, *, token=None, allow_builtin=False, **opts):
    """Return a real board object for `location` (see module docstring). Lazy-imports the drivers."""
    family, arg = _classify(location)
    if family == "auto":
        arg = _autodetect_port()
        family = "serial"
    if family == "serial":
        return McuBoard(transport_factory=_serial_transport_factory(arg, **opts))
    if family == "net":
        return McuBoard(transport_factory=_net_transport_factory(arg, token=token, **opts))
    if family == "dongle":
        dongle_id = None if _HCI_RE.match(arg or "") is None and not _is_mac(arg or "") else arg
        # a bare "hciN"/MAC is the adapter id; anything else (shouldn't reach here) -> autodetect
        return DongleBoard(arg, allow_builtin=allow_builtin)
    raise HidbridgeError(f"cannot resolve a board from location {location!r}")


# --------------------------------------------------------------------------- #
# serial autodetect / discovery
# --------------------------------------------------------------------------- #
def _candidate_ports(vendor_of=None):
    """Vendor-gated list of plausible MCU serial ports (never the KVM CH340). Injectable for tests."""
    import glob

    from . import transport_esp32 as TE
    vendor_of = vendor_of or TE._tty_usb_vendor
    ports = sorted(glob.glob("/dev/ttyUSB*")) + sorted(glob.glob("/dev/ttyACM*"))
    out = []
    for dev in ports:
        vid = (vendor_of(dev) or "").lower()
        if vid in TE._BANNED_VENDORS:
            continue
        out.append(dev)
    return out


def _probe_port(port):
    """Open `port`, ask the board `hello`, and return {port, board, fw, device} or None.

    Opens with the RIGHT style per device: a Pico native USB-CDC (ttyACM + VID 2e8a) needs a
    DTR-asserted open (transport_pico) or it never transmits its reply; an ESP32 bridge (ttyUSB) needs
    the reset-free B0 open (transport_esp32) so probing does not reboot it and drop its BLE link. Both
    are covered by _serial_transport_factory's per-device selection. A non-board tty (or legacy/asleep
    board) just fails to answer `hello` -> None (dropped from discovery)."""
    from .rpc import RpcClient
    tp = None
    try:
        tp = _serial_transport_factory(port)()
        ch = _transport_channel(tp)
        if ch is None:
            return None
        h = RpcClient(ch).hello()
        dev = h.get("device", "")
        if not str(dev).upper().startswith("HIDBRIDGE-"):
            return None
        return {"port": port, "board": _board_from_device_name(dev), "fw": h.get("fw", ""),
                "device": dev}
    except Exception:  # noqa: BLE001 -- a non-board tty / legacy / asleep board just fails to answer
        return None
    finally:
        if tp is not None:
            try:
                tp.close()
            except Exception:  # noqa: BLE001
                pass


def discover(candidates=None, prober=None):
    """List plugged-in hidbridge boards: [{port, board, fw, device}]. Vendor-gated + handshake-probed."""
    ports = candidates if candidates is not None else _candidate_ports()
    prober = prober or _probe_port
    found = []
    for p in ports:
        info = prober(p)
        if info:
            found.append(info)
    return found


def _autodetect_port():
    # An explicit BT_HID_ESP32_PORT short-circuits the scan (no handshake needed).
    env = os.environ.get("BT_HID_ESP32_PORT")
    if env:
        return env
    found = discover()
    if not found:
        raise NoBoardFound(
            "no plugged-in hidbridge board found; pass a location (/dev/ttyUSBx, a host, or hciN) "
            "or set BT_HID_ESP32_PORT")
    if len(found) > 1:
        listing = ", ".join(f"{f['port']} ({f['device']})" for f in found)
        raise AmbiguousLocation(
            f"several boards found ({listing}); name one explicitly as the location")
    return found[0]["port"]

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

The MCU family reuses the existing, load-bearing layers rather than reimplementing BLE: the frame
transports (`transport_esp32`/`transport_net`, incl. the RESET-FREE serial open) relay the raw report
bytes, and the JSON-RPC client (`rpc.RpcClient`) does the name-aware device management (identities/
pair/unpair/connect/nickname/layout) over the SAME open channel. `mode` selects only the LAST hop:
"raw" relays host-built frames as K/M lines; "rpc" relays them via the JSON-RPC `frames` method (the
board notifies the same bytes -- byte-parity either way; the passcode is always pre-built host-side,
never sent as text).

This module lazy-imports serial/dbus INSIDE the resolver, so `import hidbridge` stays dep-free.
"""

from __future__ import annotations

import os
import re

from .bridge import HidbridgeError, NotBonded, Unsupported  # typed errors reused


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


# --------------------------------------------------------------------------- #
# raw <-> rpc frame relay (the "rpc" drive path's transport)
# --------------------------------------------------------------------------- #
class _RpcFramesTransport:
    """A HidController transport whose `.send(frames)` relays PRE-BUILT report bytes via the board's
    JSON-RPC `frames` method (the board just notifies them). Host still builds the bytes (reports.py),
    so byte-parity with the raw path holds; the wire is JSON-RPC instead of K/M lines."""

    _RID = {"keyboard": 1, "mouse": 2}

    def __init__(self, rpc_client):
        self._rpc = rpc_client

    def send(self, frames):
        hexframes = ["%02x%s" % (self._RID[k], b.hex()) for (k, b) in frames]
        res = self._rpc.frames(hexframes)
        # normalise to the {"ok": ...} shape the controller callers expect
        return {"ok": True, **(res if isinstance(res, dict) else {})}


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
    """A serial/network MCU running the hidbridge firmware. Device management is JSON-RPC (rpc.py) over
    the transport's OWN open channel; report relay is the raw transport (raw mode) or the RPC `frames`
    method (rpc mode)."""

    def __init__(self, *, transport_factory=None, mode="raw", transport=None, rpc_client=None):
        # transport_factory() -> an open frame transport (owns the reset-free channel). Injectables
        # `transport`/`rpc_client` skip the factory + probe for offline tests.
        self._factory = transport_factory
        self.mode = mode
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
            ch = getattr(self._tp, "_ser", None)
            if ch is None:                       # nudge the transport to open its channel
                opener = getattr(self._tp, "_open", None)
                if opener is not None:
                    opener()
                ch = getattr(self._tp, "_ser", None)
            if ch is None:
                raise HidbridgeError("MCU transport exposes no open JSON-RPC channel (_ser)")
            self._rpc = RpcClient(ch)
        self._probe()

    def _probe(self):
        h = self._rpc.hello()
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

    # ---- report transport (per the drive mode) ----
    def transport(self, name=None):
        if self.mode == "rpc":
            return _RpcFramesTransport(self._rpc)
        return self._tp


# --------------------------------------------------------------------------- #
# BlueZ dongle board
# --------------------------------------------------------------------------- #
class DongleBoard:
    """The host's own BlueZ adapter + the per-adapter ble_hid daemon. Single BLE identity: it holds one
    bonded host at a time and pairs via `pair-mode`, so pair/unpair/rename here are Unsupported."""

    def __init__(self, dongle_id=None, *, mode="raw", allow_builtin=False, link_manager=None,
                 transport=None):
        if mode == "rpc":
            raise Unsupported("the dongle backend has no rpc drive path; use mode='raw'")
        from . import conn as _conn
        self._backend = _conn.DongleLinkBackend(dongle_id, allow_builtin=allow_builtin,
                                                link_manager=link_manager, transport=transport)
        self.mode = mode
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


def _serial_transport_factory(port, *, board_hint=None, **opts):
    def make():
        # pico's USB-CDC is native (no reset trick); esp32 needs the reset-free open. Pick by hint or
        # default to esp32's transport (which handles both via serial_link).
        if board_hint == "pico":
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


def resolve_board(location, *, token=None, mode="raw", allow_builtin=False, **opts):
    """Return a real board object for `location` (see module docstring). Lazy-imports the drivers."""
    family, arg = _classify(location)
    if family == "auto":
        arg = _autodetect_port()
        family = "serial"
    if family == "serial":
        return McuBoard(transport_factory=_serial_transport_factory(arg, **opts), mode=mode)
    if family == "net":
        return McuBoard(transport_factory=_net_transport_factory(arg, token=token, **opts), mode=mode)
    if family == "dongle":
        dongle_id = None if _HCI_RE.match(arg or "") is None and not _is_mac(arg or "") else arg
        # a bare "hciN"/MAC is the adapter id; anything else (shouldn't reach here) -> autodetect
        return DongleBoard(arg, mode=mode, allow_builtin=allow_builtin)
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
    """Open `port`, ask the board `hello`, and return {port, board, fw, device} or None. Reset-free."""
    from .rpc import RpcError
    from .transport_esp32 import make_transport as mk
    tp = None
    try:
        tp = mk(port=port)
        from .rpc import RpcClient
        ch = getattr(tp, "_ser", None)
        if ch is None:
            return None
        h = RpcClient(ch).hello()
        dev = h.get("device", "")
        if not str(dev).upper().startswith("HIDBRIDGE-"):
            return None
        return {"port": port, "board": _board_from_device_name(dev), "fw": h.get("fw", ""),
                "device": dev}
    except (RpcError, Exception):  # noqa: BLE001 -- a non-board tty just fails to answer
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

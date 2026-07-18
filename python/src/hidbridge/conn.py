"""bt_hid dongle LINK layer -- BlueZ connect/disconnect + the LinkBackend interface (BACKEND-AGNOSTIC).

The connect-on-demand CONTEXT MANAGER lives in `hidbridge.bridge` (HidBridge -> connect() -> session);
this module holds the pieces the DONGLE board driver reuses: `BluezLinkManager` (the only thing that
touches BlueZ D-Bus, scoped per-adapter to `/org/bluez/hciN`) and the `LinkBackend` family with
`DongleLinkBackend`. `board.DongleBoard` adapts DongleLinkBackend into the board protocol HidBridge
drives; the MCU boards (`board.McuBoard`) drive their own firmware over serial/TCP instead.

`make_link_backend(target)` still dispatches a target string to a backend, mirroring the report-layer
`make_transport("dongle"/"pico"/"esp32")` pattern (kept for the targets registry + link_* factories):
  * "hci1" / a MAC / a USB-path / None ......... the BlueZ dongle backend (auto-picks if None)
  * "dongle:hci1" ............................. explicit dongle backend
  * "pico:/dev/ttyACM0" / "esp32:/dev/ttyUSB1" . an MCU backend (its own BLE stack, over serial)
  * "<name>:<arg>" ............................. any bt_hid.link_<name> backend

ARCHITECTURE (see docs/bt-hid-multi-device.md): the DONGLE backend uses the HOST bluetoothd via D-Bus,
scoping EVERY operation to `/org/bluez/hciN`. The per-adapter `ble_hid` daemon serves the GATT HID
profile + advertises; the backend drives the LE LINK (connect target, disconnect others) and relays
report frames to that daemon's per-adapter control socket.

DISTINCT BLE IDENTITY PER iPad is a per-backend CAPABILITY (`capabilities`): the dongle reports
`per_device_identity=False` (BlueZ gives one adapter one identity), an MCU firmware that picks the
advertising address per bond reports `True`.

Testability: all BlueZ access goes through an injectable `link_manager` (default `BluezLinkManager`,
lazy dbus import). Backends + transports are injectable, so the connect/act/disconnect lifecycle is
exercised offline with a fake link manager + a RecordingTransport, no hardware.
"""

from __future__ import annotations

import importlib
import os
import time

from . import adapter as _adapter
from . import naming as _naming
from .controller import SocketTransport, make_transport

DEVICE_IFACE = "org.bluez.Device1"


class BtHidError(RuntimeError):
    """A HidSession precondition failed (daemon down, device not bonded, connect timed out)."""


# --------------------------------------------------------------------------- #
# BlueZ device operations -- the ONLY thing that touches BlueZ; injectable for offline tests
# --------------------------------------------------------------------------- #
class BluezLinkManager:
    """Connect/disconnect bonded LE devices on ONE adapter via the host bluetoothd (D-Bus).

    Every method is scoped to `adapter_path` (`/org/bluez/hciN`) so this never touches another
    adapter's devices. dbus is imported lazily so `conn.py` imports under uv's CPython (no dbus)
    for the offline tests that inject a fake instead.
    """

    def __init__(self):
        self._bus = None

    def _get_bus(self):
        if self._bus is None:
            import dbus
            self._bus = dbus.SystemBus()
        return self._bus

    def _managed(self):
        import dbus
        om = dbus.Interface(self._get_bus().get_object("org.bluez", "/"),
                            "org.freedesktop.DBus.ObjectManager")
        return om.GetManagedObjects()

    def find_device(self, adapter_path: str, ios_device: str):
        """Resolve a bonded device on `adapter_path` by MAC / name substring / object-path suffix.

        Returns (object_path, props) or (None, None).
        """
        t = ios_device.lower()
        for path, ifaces in self._managed().items():
            if DEVICE_IFACE not in ifaces or not path.startswith(adapter_path + "/"):
                continue
            props = ifaces[DEVICE_IFACE]
            addr = str(props.get("Address", "")).lower()
            name = str(props.get("Alias", props.get("Name", ""))).lower()
            if t == addr or t in name or path.lower().endswith(t.replace(":", "_")):
                return path, props
        return None, None

    def all_devices(self, adapter_path: str):
        """[(path, props)] for every known (bonded/paired or seen) device on this adapter."""
        return [(path, ifaces[DEVICE_IFACE])
                for path, ifaces in self._managed().items()
                if DEVICE_IFACE in ifaces and path.startswith(adapter_path + "/")]

    def connected_devices(self, adapter_path: str):
        """[(path, props)] for currently-connected devices on this adapter."""
        out = []
        for path, ifaces in self._managed().items():
            if DEVICE_IFACE in ifaces and path.startswith(adapter_path + "/") \
                    and ifaces[DEVICE_IFACE].get("Connected"):
                out.append((path, ifaces[DEVICE_IFACE]))
        return out

    def is_connected(self, device_path: str) -> bool:
        import dbus
        props = dbus.Interface(self._get_bus().get_object("org.bluez", device_path),
                               "org.freedesktop.DBus.Properties")
        try:
            return bool(props.Get(DEVICE_IFACE, "Connected"))
        except dbus.DBusException:
            return False

    def connect(self, device_path: str, timeout: float = 20.0) -> bool:
        """Nudge Device1.Connect() then WAIT (poll) until the central (re)connects, up to timeout.

        As a peripheral we cannot force it; Connect() is best-effort and the wait covers the iPad
        reconnecting to our advertisement.
        """
        import dbus
        dev = dbus.Interface(self._get_bus().get_object("org.bluez", device_path), DEVICE_IFACE)
        try:
            dev.Connect()
        except dbus.DBusException:
            pass  # normal for a BLE peripheral: the central reconnects to the advertisement
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_connected(device_path):
                return True
            time.sleep(0.25)
        return self.is_connected(device_path)

    def disconnect(self, device_path: str) -> bool:
        import dbus
        dev = dbus.Interface(self._get_bus().get_object("org.bluez", device_path), DEVICE_IFACE)
        try:
            dev.Disconnect()
            return True
        except dbus.DBusException:
            return False


# --------------------------------------------------------------------------- #
# LinkBackend interface + implementations
# --------------------------------------------------------------------------- #
class LinkBackend:
    """A connect-on-demand HID link. Implementations: dongle (BlueZ) + MCU (serial firmware).

    Contract used by HidSession:
      prepare()                     resolve/open the backend; raise BtHidError if not ready.
      connect(ios_device, timeout, exclusive) -> bool   (re)connect that iOS device.
      disconnect()                  drop the active link; KEEP the bond.
      is_connected() -> bool
      transport()                   an object with .send(frames) (relayed reports).
      capabilities -> dict          backend traits, incl. "per_device_identity": bool.
      describe() -> str             human summary (for logs/status).
    """

    name = "base"
    capabilities: dict = {"per_device_identity": False}

    # ---- readiness (see HidSession) --------------------------------------- #
    # "Connected" is NOT "ready to use": a freshly (re)connected iOS central spends ~1-3s reading the
    # HID report map + writing the report CCCDs (StartNotify) before it ACTS on input notifications;
    # anything sent in that window is dropped. Backends that can OBSERVE the real subscribe signal
    # override readiness_supported()/is_ready(); the rest fall back to READY_SETTLE seconds.
    READY_SETTLE = 0.0   # fallback settle (s) when no real readiness signal is observable

    def prepare(self):  # pragma: no cover - overridden
        raise NotImplementedError

    def connect(self, ios_device, timeout=20.0, exclusive=True) -> bool:  # pragma: no cover
        raise NotImplementedError

    def disconnect(self):  # pragma: no cover
        raise NotImplementedError

    def is_connected(self) -> bool:  # pragma: no cover
        raise NotImplementedError

    def transport(self):  # pragma: no cover
        raise NotImplementedError

    def readiness_supported(self) -> bool:
        """True if this backend can OBSERVE iOS enabling (subscribing to) the HID reports."""
        return False

    def is_ready(self) -> bool:
        """One-shot: has iOS subscribed to the HID input reports? (meaningful when supported)."""
        return self.is_connected()

    def wait_ready(self, timeout: float = 8.0, poll: float = 0.2) -> bool:
        """Block until is_ready() (up to timeout). Returns False at once if readiness is unobservable."""
        if not self.readiness_supported():
            return False
        end = time.monotonic() + timeout
        while True:
            if self.is_ready():
                return True
            if time.monotonic() >= end:
                return self.is_ready()
            time.sleep(poll)

    def describe(self) -> str:  # pragma: no cover
        return self.name


class DongleLinkBackend(LinkBackend):
    """BlueZ USB-dongle backend: per-adapter ble_hid daemon (GATT + advertise) + Device1 link ops."""

    name = "dongle"
    # BlueZ gives one adapter ONE identity (single public/static address + one local IRK); distinct
    # per-iPad identity is NOT achievable via the stock D-Bus API (see the feasibility verdict).
    capabilities = {"per_device_identity": False, "role": "peripheral", "medium": "bluez"}

    def __init__(self, dongle_id: str | None = None, *, allow_builtin: bool = False,
                 link_manager=None, transport=None):
        self.dongle_id = dongle_id
        self.allow_builtin = allow_builtin
        self.link = link_manager if link_manager is not None else BluezLinkManager()
        self._transport = transport
        self.info = None
        self.slug = None
        self.adapter_path = None
        self.sock = None
        self.device_path = None
        # An injected transport (tests / a caller-managed link) owns its own readiness, so we never
        # impose the ~3.3s hardware settle on it; a REAL daemon socket needs the fallback settle when
        # the subscribe query is unavailable.
        self.READY_SETTLE = 0.0 if transport is not None else 3.3

    def prepare(self):
        self.info = _adapter.resolve_dongle(self.dongle_id, allow_builtin=self.allow_builtin)
        self.slug = _adapter.adapter_slug(self.info)
        self.adapter_path = f"/org/bluez/{self.info['hci']}"
        self.sock = _naming.control_sock(self.slug, env_override=os.environ.get("BT_HID_SOCK"))
        # The daemon (GATT server + advertiser) must be up for this adapter, else there is nothing
        # for the iPad to connect to and nowhere to send frames.
        if self._transport is None and not os.path.exists(self.sock):
            raise BtHidError(
                f"no bt_hid daemon for {self.slug} (missing control socket {self.sock}); start it: "
                f"sudo hidbridge/service.sh start <name> on adapter {self.info['hci']}")

    def connect(self, ios_device, timeout=20.0, exclusive=True) -> bool:
        path, _props = self.link.find_device(self.adapter_path, ios_device)
        if path is None:
            raise BtHidError(
                f"{ios_device!r} is not bonded on {self.info['hci']}; pair it first "
                f"(bt_hid --adapter {self.info['hci']} pair-mode, then tap it on the iPad)")
        self.device_path = path
        if exclusive:
            for other_path, _ in self.link.connected_devices(self.adapter_path):
                if other_path != self.device_path:
                    self.link.disconnect(other_path)
        return self.link.connect(self.device_path, timeout)

    def disconnect(self):
        # Drop the link but KEEP the bond (never RemoveDevice) so next reconnect is instant.
        if self.device_path is not None:
            self.link.disconnect(self.device_path)

    def is_connected(self) -> bool:
        return self.device_path is not None and self.link.is_connected(self.device_path)

    def transport(self):
        return self._transport if self._transport is not None else SocketTransport(self.sock)

    # ---- readiness: the ble_hid daemon reports when iOS issued StartNotify on the report chars ----
    def _status(self):
        """Query the daemon's `status` control command, or None if unavailable."""
        fn = getattr(self.transport(), "status", None)
        if not callable(fn):
            return None
        try:
            st = fn()
        except Exception:  # noqa: BLE001  (daemon down / socket hiccup -> treat as unobservable)
            return None
        return st if isinstance(st, dict) else None

    def readiness_supported(self) -> bool:
        # Only when the transport can answer the subscription query (the running daemon over its
        # socket, or an injected status-capable transport). A plain RecordingTransport cannot.
        return self._status() is not None

    def is_ready(self) -> bool:
        st = self._status()
        if st is None:
            return self.is_connected()
        return bool(st.get("subscribed"))

    def describe(self) -> str:
        return f"dongle {self.info['hci'] if self.info else self.dongle_id} (sock {self.sock})"


class SerialMcuLinkBackend(LinkBackend):
    """Generic fallback wrapping an existing transport_<name> (pico/esp32) as a link backend.

    The MCU firmware serves HID + owns bonding on its own BLE stack, so "connect a specific iOS
    device" and per-iPad identity are FIRMWARE features. This generic wrapper just relays reports
    and treats the link as always-available (the firmware advertises + reconnects on its own); a
    dedicated `bt_hid.link_<name>` module (bt-hid-mcu-robust) supersedes it with real per-device
    connect/disconnect + an accurate `per_device_identity` capability.
    """

    def __init__(self, backend_name: str, arg: str | None = None, **opts):
        self.name = backend_name
        self.capabilities = {"per_device_identity": None, "role": "peripheral", "medium": "serial"}
        if arg:
            opts.setdefault("port", arg)
        self._opts = opts
        self._transport = None

    def prepare(self):
        self._transport = make_transport(self.name, **self._opts)

    def connect(self, ios_device, timeout=20.0, exclusive=True) -> bool:
        # Best-effort: kick advertising if the transport supports it; firmware handles the rest.
        adv = getattr(self._transport, "advertise", None)
        if callable(adv):
            try:
                adv()
            except Exception:  # noqa: BLE001
                pass
        return True

    def disconnect(self):
        disc = getattr(self._transport, "disconnect", None)
        if callable(disc):
            try:
                disc()
            except Exception:  # noqa: BLE001
                pass

    def is_connected(self) -> bool:
        chk = getattr(self._transport, "is_connected", None)
        return bool(chk()) if callable(chk) else True

    def transport(self):
        return self._transport

    def describe(self) -> str:
        return f"mcu:{self.name} ({self._opts.get('port', 'auto')})"


# --------------------------------------------------------------------------- #
# Backend dispatch (mirror controller.make_transport)
# --------------------------------------------------------------------------- #
_DONGLE_ALIASES = {"dongle", "ble", "bluez", "daemon", "socket"}


def _parse_target(target: str | None):
    """Split a target into (backend, arg). 'pico:/dev/ttyACM0' -> ('pico', '/dev/ttyACM0');
    'hci1' / a MAC / None -> ('dongle', 'hci1'/mac/None)."""
    if not target:
        return "dongle", None
    if ":" in target and not _looks_like_mac(target):
        backend, _, arg = target.partition(":")
        return backend.lower(), (arg or None)
    return "dongle", target


def _looks_like_mac(s: str) -> bool:
    parts = s.split(":")
    return len(parts) == 6 and all(len(p) == 2 for p in parts)


def make_link_backend(target: str | None = None, **opts) -> LinkBackend:
    """Return a LinkBackend for `target` (see module docstring for the target grammar).

    Injectables for the dongle backend: allow_builtin=, link_manager=, transport=.
    """
    backend, arg = _parse_target(target)
    if backend in _DONGLE_ALIASES:
        dongle_kw = {k: opts[k] for k in ("allow_builtin", "link_manager", "transport") if k in opts}
        return DongleLinkBackend(arg, **dongle_kw)
    # Prefer a dedicated link backend module (bt_hid.link_<name>); else the generic serial wrapper.
    try:
        mod = importlib.import_module(f"{__package__}.link_{backend}")
    except ImportError:
        return SerialMcuLinkBackend(backend, arg, **opts)
    return mod.make_link_backend(arg, **opts)

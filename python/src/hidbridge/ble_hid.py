"""BLE HID-over-GATT (HOG) peripheral daemon -- emulate a keyboard+mouse to an iPad/iPhone.

Route choice (BLE HOG, not classic BT-HID): the classic HID-device role needs an SDP record + the
L2CAP HID PSMs 0x11/0x13, which collide with BlueZ's `input` plugin (the HID *host* role) -- and we
are forbidden from disabling that plugin globally (it serves wsb's own BT input). BLE HID-over-GATT
sidesteps that entirely: it is a GATT service (0x1812), no SDP, no HID PSMs, and every BlueZ object
is PER-ADAPTER (`/org/bluez/hciN/...`) so we scope everything to the DONGLE. iPadOS/iOS fully
support BLE HID keyboards + mice (they enter the lock-screen passcode too).

This daemon MUST run under the SYSTEM python (`/usr/bin/python3`): it needs `dbus` + `gi` (GLib
mainloop), which uv's managed CPython lacks. The pure report layer (`reports.py`) is shared and
imports fine under either interpreter.

It registers the GATT app + an LE advertisement on the dongle adapter, then listens on a Unix
control socket for report frames from the CLI (`bt_hid`). It never logs report bytes (they can carry
a passcode).

Scope / safety:
  * Only touches the dongle adapter object (refuses the built-in via bt_hid.adapter).
  * Powers the adapter on + sets Discoverable/Pairable ONLY while running; restores on exit.
  * Registers/unregisters its GATT app + advertisement cleanly on shutdown.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import threading

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

from hidbridge import adapter as _adapter  # noqa: E402
from hidbridge import naming as _naming  # noqa: E402
from hidbridge import reports as R  # noqa: E402

BLUEZ = "org.bluez"
DBUS_OM = "org.freedesktop.DBus.ObjectManager"
DBUS_PROP = "org.freedesktop.DBus.Properties"
GATT_MANAGER = "org.bluez.GattManager1"
LE_ADV_MANAGER = "org.bluez.LEAdvertisingManager1"
GATT_SERVICE = "org.bluez.GattService1"
GATT_CHRC = "org.bluez.GattCharacteristic1"
GATT_DESC = "org.bluez.GattDescriptor1"
LE_ADV = "org.bluez.LEAdvertisement1"

# Legacy DEFAULT D-Bus application path, kept only as the single-daemon fallback. Multi-daemon runs
# derive a PER-ADAPTER path via bt_hid.naming.app_path(slug) so two daemons never collide on the
# system bus. The daemon computes its own paths in BleHidDaemon.__init__.
APP_PATH = _naming.DBUS_ROOT

# 16-bit GATT UUID helpers
HID_SVC = "00001812-0000-1000-8000-00805f9b34fb"
BATTERY_SVC = "0000180f-0000-1000-8000-00805f9b34fb"
DEVINFO_SVC = "0000180a-0000-1000-8000-00805f9b34fb"

REPORT_MAP_UUID = "00002a4b-0000-1000-8000-00805f9b34fb"
HID_INFO_UUID = "00002a4a-0000-1000-8000-00805f9b34fb"
HID_CTRL_PT_UUID = "00002a4c-0000-1000-8000-00805f9b34fb"
PROTOCOL_MODE_UUID = "00002a4e-0000-1000-8000-00805f9b34fb"
REPORT_UUID = "00002a4d-0000-1000-8000-00805f9b34fb"
REPORT_REF_UUID = "00002908-0000-1000-8000-00805f9b34fb"
BATTERY_LVL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
PNP_ID_UUID = "00002a50-0000-1000-8000-00805f9b34fb"

APPEARANCE_KEYBOARD = 0x03C1   # GAP appearance: HID keyboard

# The control socket is PER-ADAPTER now (see bt_hid.naming): the daemon computes self.control_sock
# from its adapter slug in BleHidDaemon.__init__, honouring a BT_HID_SOCK pin. There is no single
# module-global socket any more -- that was the two-daemons-collide bug.


def _b(byte_list) -> "dbus.Array":
    return dbus.Array([dbus.Byte(x) for x in byte_list], signature="y")


# --------------------------------------------------------------------------- #
# GATT object base classes (standard BlueZ D-Bus GATT server pattern)
# --------------------------------------------------------------------------- #
class Application(dbus.service.Object):
    def __init__(self, bus, path=APP_PATH):
        self.path = path
        self.services = []
        super().__init__(bus, self.path)

    def add_service(self, service):
        self.services.append(service)

    @dbus.service.method(DBUS_OM, out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        out = {}
        for s in self.services:
            out[s.get_path()] = s.get_properties()
            for c in s.chrcs:
                out[c.get_path()] = c.get_properties()
                for d in c.descs:
                    out[d.get_path()] = d.get_properties()
        return out


class Service(dbus.service.Object):
    def __init__(self, bus, app_path, index, uuid, primary=True):
        self.path = f"{app_path}/service{index}"
        self.uuid = uuid
        self.primary = primary
        self.chrcs = []
        super().__init__(bus, self.path)

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_chrc(self, chrc):
        self.chrcs.append(chrc)

    def get_properties(self):
        return {GATT_SERVICE: {
            "UUID": self.uuid,
            "Primary": self.primary,
            "Characteristics": dbus.Array([c.get_path() for c in self.chrcs], signature="o"),
        }}


class Characteristic(dbus.service.Object):
    def __init__(self, bus, svc, index, uuid, flags, value=None):
        self.path = f"{svc.path}/char{index}"
        self.uuid = uuid
        self.flags = flags
        self.svc = svc
        self.descs = []
        self.value = list(value or [])
        self.notifying = False
        super().__init__(bus, self.path)

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_desc(self, desc):
        self.descs.append(desc)

    def get_properties(self):
        return {GATT_CHRC: {
            "Service": self.svc.get_path(),
            "UUID": self.uuid,
            "Flags": dbus.Array(self.flags, signature="s"),
            "Descriptors": dbus.Array([d.get_path() for d in self.descs], signature="o"),
        }}

    @dbus.service.method(DBUS_PROP, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface != GATT_CHRC:
            raise dbus.exceptions.DBusException("org.bluez.Error.InvalidArguments")
        props = self.get_properties()[GATT_CHRC]
        props["Value"] = _b(self.value)
        return props

    @dbus.service.method(GATT_CHRC, in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, options):
        return _b(self.value)

    @dbus.service.method(GATT_CHRC, in_signature="aya{sv}")
    def WriteValue(self, value, options):
        self.value = list(value)

    @dbus.service.method(GATT_CHRC)
    def StartNotify(self):
        self.notifying = True
        _log(f"StartNotify {self.uuid[4:8]} @ {self.path.split('/')[-1]} -> notifying")

    @dbus.service.method(GATT_CHRC)
    def StopNotify(self):
        self.notifying = False
        _log(f"StopNotify {self.uuid[4:8]} @ {self.path.split('/')[-1]}")

    @dbus.service.signal(DBUS_PROP, signature="sa{sv}as")
    def PropertiesChanged(self, interface, changed, invalidated):
        pass

    def send_report(self, body_bytes):
        """Update the characteristic value + notify the central (the actual HID input event)."""
        self.value = list(body_bytes)
        if self.notifying:
            self.PropertiesChanged(GATT_CHRC, {"Value": _b(self.value)}, [])
            _log(f"notify {self.uuid[4:8]} len={len(body_bytes)}")
        else:
            _log(f"send_report DROPPED (not subscribed) {self.uuid[4:8]}")


class Descriptor(dbus.service.Object):
    def __init__(self, bus, chrc, index, uuid, flags, value):
        self.path = f"{chrc.path}/desc{index}"
        self.uuid = uuid
        self.flags = flags
        self.chrc = chrc
        self.value = list(value)
        super().__init__(bus, self.path)

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def get_properties(self):
        return {GATT_DESC: {
            "Characteristic": self.chrc.get_path(),
            "UUID": self.uuid,
            "Flags": dbus.Array(self.flags, signature="s"),
        }}

    @dbus.service.method(DBUS_PROP, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface != GATT_DESC:
            raise dbus.exceptions.DBusException("org.bluez.Error.InvalidArguments")
        props = self.get_properties()[GATT_DESC]
        props["Value"] = _b(self.value)
        return props

    # BUG FIX: this MUST be on org.bluez.GattDescriptor1, not GattCharacteristic1. When it was on
    # the wrong interface, BlueZ could not serve the descriptor's value, so iOS could not READ the
    # Report Reference (report ID/type) of each HID report char -> it never enabled the report and
    # never subscribed (no CCCD write on 2a4d). That was the "reads the HID service but does not
    # subscribe the report chars" symptom.
    @dbus.service.method(GATT_DESC, in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, options):
        return _b(self.value)


# Legacy default agent path (single-daemon fallback). Multi-daemon runs pass a per-adapter path
# via bt_hid.naming.agent_path(slug) so two daemons register distinct agents.
AGENT_PATH = f"{_naming.DBUS_ROOT}/agent"
AGENT_MANAGER = "org.bluez.AgentManager1"
AGENT_IFACE = "org.bluez.Agent1"


class PairingAgent(dbus.service.Object):
    """Just-Works pairing agent (NoInputNoOutput) -- auto-accepts pairing FOR THE DONGLE ONLY.

    A BLE HID central (the iPad) must BOND before it will use the HID service. BlueZ needs a
    registered agent to authorize the incoming pairing; without one the bond never completes
    (device stays paired=False). This agent auto-accepts, but ONLY for devices under the dongle
    adapter path -- any pairing on the built-in adapter is REJECTED so we never silently authorize
    something on the user's built-in BT.
    """

    def __init__(self, bus, adapter_path, agent_path=AGENT_PATH):
        self.adapter_path = adapter_path
        super().__init__(bus, agent_path)

    def _guard(self, device):
        # INSECURE test mode (on-host loopback only): accept ALL adapters so a local BlueZ<->BlueZ
        # Just-Works bond completes (the single default agent must authorize both roles). NEVER set
        # this for real iPad use.
        if os.environ.get("BT_HID_INSECURE"):
            return
        if not str(device).startswith(self.adapter_path + "/"):
            raise dbus.exceptions.DBusException(
                "org.bluez.Error.Rejected: not the dongle adapter")

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Release(self):
        pass

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        self._guard(device)

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        self._guard(device)

    @dbus.service.method(AGENT_IFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        self._guard(device)  # Just Works numeric-compare -> accept

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        self._guard(device)
        return "0000"

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        self._guard(device)
        return dbus.UInt32(0)

    @dbus.service.method(AGENT_IFACE, in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        pass

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        pass

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Cancel(self):
        pass


class Advertisement(dbus.service.Object):
    def __init__(self, bus, app_path, index, local_name):
        self.path = f"{app_path}/adv{index}"
        self.local_name = local_name
        super().__init__(bus, self.path)

    def get_path(self):
        return dbus.ObjectPath(self.path)

    @dbus.service.method(DBUS_PROP, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface != LE_ADV:
            raise dbus.exceptions.DBusException("org.bluez.Error.InvalidArguments")
        return {
            "Type": "peripheral",
            "ServiceUUIDs": dbus.Array([HID_SVC], signature="s"),
            "LocalName": dbus.String(self.local_name),
            "Appearance": dbus.UInt16(APPEARANCE_KEYBOARD),
            "Discoverable": dbus.Boolean(True),
            "IncludeTxPower": dbus.Boolean(True),
        }

    @dbus.service.method(LE_ADV)
    def Release(self):
        pass


# --------------------------------------------------------------------------- #
# HID application assembly
# --------------------------------------------------------------------------- #
def build_application(bus, app_path=APP_PATH):
    app = Application(bus, app_path)

    # HID char flags: PLAIN read/notify. BlueZ auto-elevates the HID service to require an
    # authenticated encrypted link (that is why the DisplayYesNo authenticated bond is mandatory --
    # see the agent), so the reads are already protected by the link. We deliberately do NOT add
    # explicit "encrypt-*" flags: with them, iOS subscribes to plain-notify chars (battery) but NOT
    # to the encrypt-notify HID report chars -- observed live on mini6. Plain flags match the working
    # battery char and let iOS enable the keyboard/mouse over the authenticated link.
    rd = ["read"]
    rd_notify = ["read", "notify"]

    # --- HID service (0x1812) ---
    hid = Service(bus, app_path, 0, HID_SVC)
    app.add_service(hid)

    # Report Map (the USB HID report descriptor).
    rmap = Characteristic(bus, hid, 0, REPORT_MAP_UUID, rd, list(R.REPORT_DESCRIPTOR))
    hid.add_chrc(rmap)

    # HID Information: bcdHID=0x0111, country=0, flags=0x03 (remote-wake + normally-connectable)
    hinfo = Characteristic(bus, hid, 1, HID_INFO_UUID, ["read"], [0x11, 0x01, 0x00, 0x03])
    hid.add_chrc(hinfo)

    # HID Control Point (write-without-response)
    hctrl = Characteristic(bus, hid, 2, HID_CTRL_PT_UUID, ["write-without-response"], [0x00])
    hid.add_chrc(hctrl)

    # Protocol Mode (read + write-without-response), 0x01 = Report protocol
    pmode = Characteristic(bus, hid, 3, PROTOCOL_MODE_UUID,
                           ["read", "write-without-response"], [0x01])
    hid.add_chrc(pmode)

    # Keyboard input report (reportID 1)
    kbd = Characteristic(bus, hid, 4, REPORT_UUID, rd_notify, [0] * R.KEYBOARD_BODY_LEN)
    kbd.add_desc(Descriptor(bus, kbd, 0, REPORT_REF_UUID, rd,
                            [R.REPORT_ID_KEYBOARD, 0x01]))  # reportID 1, Input
    hid.add_chrc(kbd)

    # Mouse input report (reportID 2)
    mouse = Characteristic(bus, hid, 5, REPORT_UUID, rd_notify, [0] * R.MOUSE_BODY_LEN)
    mouse.add_desc(Descriptor(bus, mouse, 0, REPORT_REF_UUID, rd,
                              [R.REPORT_ID_MOUSE, 0x01]))  # reportID 2, Input
    hid.add_chrc(mouse)

    # --- Battery service (0x180F) ---
    batt = Service(bus, app_path, 1, BATTERY_SVC)
    app.add_service(batt)
    batt.add_chrc(Characteristic(bus, batt, 0, BATTERY_LVL_UUID, ["read", "notify"], [100]))

    # --- Device Information (0x180A): PnP ID (vendor source USB, VID/PID/version) ---
    devinfo = Service(bus, app_path, 2, DEVINFO_SVC)
    app.add_service(devinfo)
    # source=0x02 (USB), VID=0x1D6B (Linux Foundation), PID=0x0104, version=0x0100
    devinfo.add_chrc(Characteristic(bus, devinfo, 0, PNP_ID_UUID, ["read"],
                                    [0x02, 0x6B, 0x1D, 0x04, 0x01, 0x00, 0x01]))

    return app, kbd, mouse


# --------------------------------------------------------------------------- #
# Daemon
# --------------------------------------------------------------------------- #
class BleHidDaemon:
    def __init__(self, hci, name, allow_builtin=False):
        # resolve/verify the adapter is the dongle (accepts hciN/MAC/USB-path/product, or auto).
        self.info = _adapter.resolve_dongle(hci, allow_builtin=allow_builtin)
        self.hci = self.info["hci"]
        self.adapter_path = f"/org/bluez/{self.hci}"

        # Per-adapter isolation: a distinct D-Bus app path, agent path, control socket and (unless
        # pinned) advertised name, all derived from the adapter slug -- so N daemons on N dongles
        # never collide on the system bus or the socket. See bt_hid.naming.
        self.slug = _adapter.adapter_slug(self.info)
        self.app_path = _naming.app_path(self.slug)
        self.agent_path = _naming.agent_path(self.slug)
        self.control_sock = _naming.control_sock(
            self.slug, env_override=os.environ.get("BT_HID_SOCK"))
        self.name = name or _naming.default_name(self.slug)

        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SystemBus()
        self.loop = GLib.MainLoop()

        self.adapter_props = dbus.Interface(
            self.bus.get_object(BLUEZ, self.adapter_path), DBUS_PROP)
        self._saved = {}

    def _set_adapter(self, key, value):
        try:
            self._saved.setdefault(key, self.adapter_props.Get("org.bluez.Adapter1", key))
        except dbus.DBusException:
            pass
        self.adapter_props.Set("org.bluez.Adapter1", key, value)

    def start(self):
        # Configure adapter (scoped to the dongle): power on, name, pairable. We deliberately do
        # NOT set BR/EDR Discoverable -- LE discoverability comes from our LE advertisement, so the
        # iPad sees exactly ONE entry (the BLE HID), not a duplicate classic BR/EDR entry.
        self._set_adapter("Powered", dbus.Boolean(True))
        self._set_adapter("Alias", dbus.String(self.name))
        self._set_adapter("Discoverable", dbus.Boolean(False))
        self._set_adapter("Pairable", dbus.Boolean(True))
        self._set_adapter("PairableTimeout", dbus.UInt32(0))  # stay pairable

        # Register the pairing agent (dongle-scoped) so incoming bonds complete. IO capability
        # "DisplayYesNo" -> LE Secure Connections NUMERIC COMPARISON = an AUTHENTICATED bond, which
        # is REQUIRED: BlueZ elevates the HID characteristics to need authentication (a plain
        # Just-Works/NoInputNoOutput bond gets "Insufficient Authentication" on the HID report reads,
        # so iOS refuses to subscribe and the keyboard never activates). Our agent auto-confirms the
        # numeric code (RequestConfirmation), so the only human step is the user tapping "Pair" once.
        default_io = "NoInputNoOutput" if os.environ.get("BT_HID_INSECURE") else "DisplayYesNo"
        self.io_capability = os.environ.get("BT_HID_IO", default_io)
        self.agent = PairingAgent(self.bus, self.adapter_path, self.agent_path)
        agent_mgr = dbus.Interface(self.bus.get_object(BLUEZ, "/org/bluez"), AGENT_MANAGER)
        try:
            agent_mgr.RegisterAgent(self.agent_path, self.io_capability)
            # RequestDefaultAgent grabs the ONE default-agent slot for auto-confirming incoming
            # bonds. With multiple daemons only the LAST requester holds it (a BlueZ limitation),
            # and each daemon's agent REJECTS pairings on other adapters -- so PAIR one dongle at a
            # time (whichever daemon last called this owns pairing). Concurrent CONNECT of already-
            # bonded devices needs no agent, so steady-state multi-dongle use is unaffected. Best-
            # effort: if another daemon holds the slot we log + continue (see docs/bt-hid-multi-device).
            try:
                agent_mgr.RequestDefaultAgent(self.agent_path)
                _log(f"pairing agent registered + DEFAULT ({self.io_capability}, dongle-scoped)")
            except dbus.DBusException as e:
                _log(f"pairing agent registered (NOT default -- another daemon holds it: {e}); "
                     f"pair this dongle's device while this daemon owns the default agent")
        except dbus.DBusException as e:
            _log(f"WARN agent register: {e}")
        self._agent_mgr = agent_mgr

        self.app, self.kbd, self.mouse = build_application(self.bus, self.app_path)
        # route a frame's `kind` to the characteristic whose Report Reference matches its reportID
        self._report_char = {
            "keyboard": self.kbd,
            "mouse": self.mouse,
        }
        self.adv = Advertisement(self.bus, self.app_path, 0, self.name)

        gatt_mgr = dbus.Interface(
            self.bus.get_object(BLUEZ, self.adapter_path), GATT_MANAGER)
        adv_mgr = dbus.Interface(
            self.bus.get_object(BLUEZ, self.adapter_path), LE_ADV_MANAGER)

        gatt_mgr.RegisterApplication(self.app.path, {},
                                     reply_handler=lambda: _log("GATT app registered"),
                                     error_handler=lambda e: _fail("RegisterApplication", e))
        adv_mgr.RegisterAdvertisement(self.adv.get_path(), {},
                                      reply_handler=lambda: _log("advertisement registered"),
                                      error_handler=lambda e: _fail("RegisterAdvertisement", e))

        # Auto-trust watcher: the instant a device on the DONGLE pairs/connects, mark it Trusted so
        # its post-bond ENCRYPTED reconnect succeeds (else iOS bonds then drops to "Other Devices").
        # Scoped to hci1: we only ever touch devices under the dongle adapter path.
        self.bus.add_signal_receiver(
            self._on_ifaces_added, dbus_interface=DBUS_OM, signal_name="InterfacesAdded")
        self.bus.add_signal_receiver(
            self._on_dev_props_changed, dbus_interface=DBUS_PROP,
            signal_name="PropertiesChanged", arg0="org.bluez.Device1", path_keyword="path")
        self._trust_existing()

        self._start_control_socket()
        # Clean shutdown on SIGTERM too (systemctl stop, timeout, pkill) -- not only SIGINT/Ctrl-C.
        # Without this the control socket + agent registration leaked on every non-Ctrl-C stop,
        # leaving a stale socket that looks like a live daemon.
        def _quit(*_):
            self.loop.quit()
            return False  # G_SOURCE_REMOVE
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, _quit)
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, _quit)
        _log(f"bt_hid BLE HID up on {self.hci} ({self.info.get('product_name') or self.info.get('address')}) "
             f"as {self.name!r}; discoverable + advertising. Ctrl-C to stop.")
        try:
            self.loop.run()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self):
        try:
            os.unlink(self.control_sock)
        except OSError:
            pass
        try:
            if getattr(self, "_agent_mgr", None):
                self._agent_mgr.UnregisterAgent(self.agent_path)
        except dbus.DBusException:
            pass
        for key, val in self._saved.items():
            try:
                self.adapter_props.Set("org.bluez.Adapter1", key, val)
            except dbus.DBusException:
                pass
        _log("bt_hid stopped; adapter settings restored.")

    # ---- auto-trust / bond persistence (dongle-scoped) ----
    def _trust_device(self, path):
        if not str(path).startswith(self.adapter_path + "/"):
            return  # NEVER touch devices on the built-in adapter
        try:
            props = dbus.Interface(self.bus.get_object(BLUEZ, path), DBUS_PROP)
            if not bool(props.Get("org.bluez.Device1", "Trusted")):
                props.Set("org.bluez.Device1", "Trusted", dbus.Boolean(True))
                _log(f"trusted {path.split('/')[-1]} (bond persistence)")
        except dbus.DBusException:
            pass

    def _trust_existing(self):
        try:
            objs = dbus.Interface(self.bus.get_object(BLUEZ, "/"), DBUS_OM).GetManagedObjects()
        except dbus.DBusException:
            return
        for path, ifaces in objs.items():
            if "org.bluez.Device1" in ifaces and path.startswith(self.adapter_path + "/"):
                self._trust_device(path)

    def _on_ifaces_added(self, path, ifaces):
        if "org.bluez.Device1" in ifaces:
            self._trust_device(path)

    def _on_dev_props_changed(self, iface, changed, invalidated, path=None):
        # trust as soon as it pairs OR connects (whichever fires first) -- beats the ~1s drop.
        if path and (changed.get("Paired") or changed.get("Connected")):
            self._trust_device(path)

    # ---- report emission ----
    def _emit(self, frames):
        """frames: iterable of (kind, body_bytes). Emitted on the GLib thread."""
        def do():
            for kind, body in frames:
                # default to the relative mouse for any unknown kind (back-compat)
                self._report_char.get(kind, self.mouse).send_report(body)
            return False
        GLib.idle_add(do)

    def handle_command(self, cmd: dict) -> dict:
        c = cmd.get("cmd")
        try:
            if c == "ping":
                return {"ok": True, "hci": self.hci, "name": self.name,
                        "slug": self.slug, "sock": self.control_sock,
                        "address": self.info.get("address")}
            if c == "status":
                # Readiness for HidSession: which report chars has iOS enabled (StartNotify/CCCD)?
                # The keyboard char subscribing is the real "iOS will act on our reports" signal --
                # without it, send_report() DROPS (see Characteristic.send_report). subscribed keys
                # on the keyboard char specifically (the lost-keystroke symptom this gate fixes).
                chars = {kind: bool(ch.notifying) for kind, ch in self._report_char.items()}
                return {"ok": True, "hci": self.hci, "name": self.name,
                        "subscribed": bool(chars.get("keyboard")),
                        "any_subscribed": any(chars.values()), "chars": chars}
            if c == "type":
                self._emit(self._kbd_frames(R.type_text(cmd["text"])))
                return {"ok": True}
            if c == "key":
                self._emit(self._kbd_frames(
                    R.key_reports(cmd["name"], int(cmd.get("modifiers", 0)))))
                return {"ok": True}
            if c == "reports":
                # raw pre-expanded frames (used for passcode so the secret text never crosses here)
                frames = [(f["kind"], bytes.fromhex(f["hex"])) for f in cmd["frames"]]
                self._emit(frames)
                return {"ok": True}
            if c == "move":
                self._emit(self._mouse_frames(
                    R.mouse_move_reports(int(cmd["dx"]), int(cmd["dy"]))))
                return {"ok": True}
            if c == "click":
                self._emit(self._mouse_frames(R.mouse_click_reports(cmd.get("button", "left"))))
                return {"ok": True}
            return {"ok": False, "error": f"unknown cmd {c!r}"}
        except Exception as e:  # noqa: BLE001  (report the error back to the CLI, keep serving)
            return {"ok": False, "error": str(e)}

    @staticmethod
    def _kbd_frames(reps):
        return [("keyboard", r.data) for r in reps]

    @staticmethod
    def _mouse_frames(reps):
        return [("mouse", r.data) for r in reps]

    def _start_control_socket(self):
        os.makedirs(os.path.dirname(self.control_sock), exist_ok=True)
        try:
            os.unlink(self.control_sock)
        except OSError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.control_sock)
        os.chmod(self.control_sock, 0o600)
        srv.listen(4)

        def serve():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    break
                with conn:
                    data = conn.recv(65536)
                    if not data:
                        continue
                    try:
                        cmd = json.loads(data.decode())
                    except ValueError:
                        conn.sendall(b'{"ok": false, "error": "bad json"}')
                        continue
                    resp = self.handle_command(cmd)
                    conn.sendall(json.dumps(resp).encode())

        threading.Thread(target=serve, daemon=True).start()


def _log(msg):
    print(f"[bt_hid] {msg}", flush=True)


def _fail(where, err):
    _log(f"ERROR {where}: {err}")


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="BLE HID keyboard+mouse peripheral (dongle only)")
    p.add_argument("--adapter", default=os.environ.get("BT_HID_ADAPTER"),
                   help="pin the dongle by hciN / MAC / USB-path / product (default: auto-detect)")
    p.add_argument("--name", default=os.environ.get("BT_HID_NAME"),
                   help="advertised name the iPad shows (default: HIDBRIDGE-dongle-<hciN>, per-adapter)")
    p.add_argument("--force-builtin", action="store_true",
                   help="DANGER: allow the built-in adapter (default refuses it)")
    args = p.parse_args(argv)
    daemon = BleHidDaemon(args.adapter, args.name, allow_builtin=args.force_builtin)
    daemon.start()


if __name__ == "__main__":
    main()

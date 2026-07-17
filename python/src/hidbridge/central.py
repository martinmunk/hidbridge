"""BLE-central HID self-test: drive an MCU HID peripheral from the wsb BUILT-IN adapter (hci0).

This is the RUN/VERIFY/CLEANUP live driver behind tests/bt_hid/test_bt_hid_central_selftest.py. It
validates an MCU BT-HID peripheral END TO END with NO iPad: the built-in adapter acts as a transient
BLE CENTRAL (host), connects to the MCU, reads + byte-verifies the HID Report Map + Report Reference
descriptors against reports.py (the single source of truth), then -- for each report kind it can
drive -- injects a KNOWN frame over the MCU's USB serial and asserts the exact bytes arrive back as a
GATT input-report NOTIFICATION on hci0. This proves the whole serial -> MCU -> BLE HID chain.

GUARDRAILS (the maintainer): hci0 is used as a CENTRAL ONLY here -- never as a HID peripheral. Its prior
Powered state is recorded + restored, the transient bond is removed on cleanup, and its identity is
never touched. A BLE peripheral serves ONE central at a time, so if the MCU is already connected to
another central (e.g. an iPad) we SKIP rather than disturb it.

The inject step runs in either DRIVE MODE (--mode, docs/bt-hid-modes.md): "frames" (Mode 2 -- the
host builds the report bytes and the board relays them, `transport.send`) or "rpc" (Mode 1 -- a
JSON-RPC `frames` call over the transport's OWN open channel; the board relays the same pre-built
bytes). Both modes must land the IDENTICAL GATT notification, so this proves BOTH firmware paths carry
HID over the air. mode=rpc needs JSON-RPC firmware, so it applies to esp32/pico/net only (the dongle
daemon self-skips).

Runs under the SYSTEM python (needs dbus + gi +, for the serial backends, pyserial). Emits a single
JSON verdict line with --json. Exit codes: 0 = PASS, 2 = SKIP (with reason), 1 = FAIL.

Backends (--backend): pico | esp32 | dongle. See central_core.BACKEND_PROFILES.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

from hidbridge import adapter as A  # noqa: E402
from hidbridge import central_core as CC  # noqa: E402
from hidbridge import reports as R  # noqa: E402

BLUEZ = "org.bluez"
OM = "org.freedesktop.DBus.ObjectManager"
PROP = "org.freedesktop.DBus.Properties"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
CHRC = "org.bluez.GattCharacteristic1"
DESC = "org.bluez.GattDescriptor1"

SCAN_TIMEOUT_S = 20
CONNECT_TIMEOUT_S = 45
AGENT_PATH = "/wsb/bt_hid/central_agent"
AGENT_MANAGER = "org.bluez.AgentManager1"
AGENT_IFACE = "org.bluez.Agent1"


class AutoAcceptAgent(dbus.service.Object):
    """A NoInputNoOutput BlueZ pairing agent that auto-accepts Just Works bonding.

    The MCU HID peripherals require an encrypted (bonded) link to expose their HID input reports, so
    hci0 must PAIR to subscribe. The MCUs bond Just Works (no MITM), so a headless auto-accept agent
    completes pairing with no user prompt. It grants nothing beyond what pairing already implies.
    """

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Release(self):
        pass

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        return

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        return "0000"

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        return dbus.UInt32(0)

    @dbus.service.method(AGENT_IFACE, in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        pass

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        pass

    @dbus.service.method(AGENT_IFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        return   # auto-accept the numeric comparison / Just Works

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        return

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Cancel(self):
        pass


# --------------------------------------------------------------------------- #
# Serial / daemon drive helpers -- turn a report kind into an MCU trigger
# --------------------------------------------------------------------------- #
# Each entry: report id -> (kind, body-building callable) using reports.py (single source of truth).
KNOWN_DRIVES = {
    R.REPORT_ID_KEYBOARD: ("keyboard", lambda: R.keyboard_report(0, [0x04]).data),  # 'a' down
    R.REPORT_ID_MOUSE:    ("mouse",    lambda: R.mouse_report(dx=3).data),          # move dx=3
}


def make_transport(profile):
    """Build the MCU driver for a backend, or return (None, skip_reason)."""
    if profile.driver == "serial_pico":
        from hidbridge.transport_pico import PicoSerialTransport, autodetect_port
        if not (os.environ.get("BT_HID_PICO_PORT") or autodetect_port()):
            return None, "no Pico serial device present (2e8a ttyACM)"
        return PicoSerialTransport(), None
    if profile.driver == "serial_esp32":
        from hidbridge.transport_esp32 import Esp32Transport, detect_port
        if not detect_port():
            return None, "no ESP32 serial device present (10c4/CP2102)"
        return Esp32Transport(open_now=False), None
    if profile.driver == "net":
        # Drive the ESP32 over TCP (the network backend). Host from BT_HID_NET_HOST (mDNS name or
        # IP); default the mDNS name. Skip (not fail) if the board is unreachable or unprovisioned,
        # so the suite stays green on a host without the Ethernet board.
        from hidbridge.transport_net import NetError, load_token, make_transport
        host = os.environ.get("BT_HID_NET_HOST") or "hidbridge-esp32.local"
        if not load_token():
            return None, "no network token (run `hidbridge net-provision`; set BT_HID_NET_TOKEN)"
        try:
            return make_transport(host=host), None
        except NetError as e:
            return None, f"net backend unreachable at {host}: {e}"
    if profile.driver == "daemon":
        from hidbridge.controller import CONTROL_SOCK, SocketTransport
        if not os.path.exists(CONTROL_SOCK):
            return None, f"ble_hid daemon socket {CONTROL_SOCK} absent"
        return SocketTransport(), None
    return None, f"unknown driver {profile.driver!r}"


def _board_ble_address(transport) -> str | None:
    """The BLE address the target board advertises, as 'AA:BB:CC:DD:EE:FF', or None.

    CRUCIAL when more than one board runs the same firmware: they all advertise the SAME LocalName
    (`HIDBRIDGE-esp32`), so matching the scan by NAME can connect the central to a DIFFERENT board than
    the one the transport drives -- the central then subscribes to board A while the injected frame
    goes to board B, so board B has no central to notify (`OK noconn`) and the test spuriously fails.
    Pinning the central to the DRIVEN board's address removes that ambiguity. Uses the MCU's slot-0
    query (also (re)asserts slot 0 = the public identity as the advertised one). Best-effort: returns
    None for a transport that cannot report it (e.g. the dongle daemon), and the caller falls back to
    name matching."""
    fn = getattr(transport, "select_slot", None)
    if fn is None:
        return None
    try:
        a = (fn(0).get("addr") or "").strip()
    except Exception:  # noqa: BLE001
        return None
    if len(a) == 12 and all(c in "0123456789abcdefABCDEF" for c in a):
        return ":".join(a[i:i + 2] for i in range(0, 12, 2)).upper()
    return None


def _free_board(transport) -> None:
    """Between retries: drop any orphaned central our failed attempt left on the board, and
    re-advertise so it is connectable again. Best-effort + backend-agnostic (guarded by hasattr):
    the MCU transports expose disconnect()/select_slot()/advertise(); the daemon backend exposes
    none, so this is a no-op there. A short settle lets advertising resume before the next attempt.
    """
    disc = getattr(transport, "disconnect", None)
    if disc:
        try:
            disc()                       # firmware 'X': drop the target slot's central (keep bond)
        except Exception:  # noqa: BLE001
            pass
    for meth, a in (("select_slot", (0,)), ("advertise", ())):
        fn = getattr(transport, meth, None)
        if fn:
            try:
                fn(*a)                   # re-advertise (un-park) so the central can reconnect
                break
            except Exception:  # noqa: BLE001
                pass
    time.sleep(2)


def transport_connected(transport, profile) -> bool | None:
    """Best-effort: is the MCU already connected to a central? True/False, or None if unknown."""
    ping = getattr(transport, "ping", None)
    if ping is None:
        return None
    try:
        res = ping()
    except Exception:  # noqa: BLE001
        return None
    if profile.driver == "serial_pico":
        # PONG name=HIDBRIDGE-pico conn=<0|1>
        return res.get("reply", "").rstrip().endswith("conn=1")
    if profile.driver in ("serial_esp32", "net"):
        return bool(res.get("connected"))
    return None


# --------------------------------------------------------------------------- #
# The central self-test flow
# --------------------------------------------------------------------------- #
class CentralSelfTest:
    def __init__(self, backend: str, central_hci: str, transport, connect_method: str = "scan",
                 mode: str = "frames"):
        self.profile = CC.BACKEND_PROFILES[backend]
        self.backend = backend
        self.central_path = f"/org/bluez/{central_hci}"
        self.central_hci = central_hci
        self.transport = transport
        # "scan" (direct Connect during active scan) or "connectdevice" (Adapter1.ConnectDevice
        # direct create-connection -- the hci0 auto-connect-stall escape). See central_core.
        self.connect_method = connect_method
        # Drive mode for the INJECT step (docs/bt-hid-modes.md): "frames" = Mode 2 frame relay
        # (transport.send, host builds the report bytes); "rpc" = Mode 1 JSON-RPC `frames` call over
        # the transport's OWN open channel (the board relays the same pre-built bytes). Both must land
        # the IDENTICAL notification; the verify path is unchanged either way.
        self.mode = mode
        self.dev_address = None       # captured from the advertisement (for ConnectDevice)
        self.dev_addr_type = None
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SystemBus()
        self.loop = GLib.MainLoop()
        self.dev_path = None
        # status starts "init"; a handler moves it to pass/skip/fail. run() enters the mainloop only
        # while it is still "init" (so an immediate skip/fail in _prepare short-circuits the loop).
        self.result = {"status": "init", "backend": backend, "central": central_hci,
                       "mode": mode, "steps": {}}
        self.report_chars = {}       # report_id -> {char_path, report_type, ref_path}
        self.received = {}           # report_id -> bytes
        self.notify_started = []     # char paths we StartNotify'd (to StopNotify on cleanup)
        self._prior_powered = None
        self._added_device = False
        self._drive_targets = []     # [(report_id, kind, body)] we will inject + verify
        self._agent = None
        self._agent_registered = False
        self._bonding = False        # guard: a Pair() is in flight (bond-before-discovery)
        self.target_address = None   # the DRIVEN board's BLE address (disambiguates same-name boards)

    # ---- dbus plumbing ----
    def _obj(self, path):
        return self.bus.get_object(BLUEZ, path)

    def _iface(self, path, name):
        return dbus.Interface(self._obj(path), name)

    def _prop(self, path):
        return dbus.Interface(self._obj(path), PROP)

    def _get(self, path, iface, key, default=None):
        try:
            return self._prop(path).Get(iface, key)
        except dbus.DBusException:
            return default

    def _managed(self):
        return self._iface("/", OM).GetManagedObjects()

    def _fail(self, msg):
        self.result["status"] = "fail"
        self.result["error"] = msg
        self.loop.quit()
        return False

    def _skip(self, msg):
        self.result["status"] = "skip"
        self.result["reason"] = msg
        self.loop.quit()
        return False

    # ---- flow ----
    def run(self):
        GLib.timeout_add_seconds(CONNECT_TIMEOUT_S + SCAN_TIMEOUT_S + 15, self._overall_timeout)
        try:
            self._prepare()
            if self.result["status"] == "init":
                self.loop.run()
        except dbus.DBusException as e:
            self.result["status"] = "fail"
            self.result["error"] = f"dbus: {e}"
        self._cleanup()
        if self.result["status"] == "init":   # loop exited without a verdict
            self.result["status"] = "fail"
            self.result.setdefault("error", "mainloop exited without a verdict")
        return self.result

    def _overall_timeout(self):
        if self.result["status"] not in ("pass", "skip"):
            self._fail("overall timeout")
        return False

    def _register_agent(self):
        """Register + default a headless Just Works pairing agent.

        BOTH steps matter: the pairing our peripheral needs is Secure-Connections Just Works, whose
        User Confirmation must be auto-accepted -- and when the link is brought up via the kernel
        auto-connect path (ConnectDevice adds the device with Action=Auto-connect), bluetoothd routes
        that confirmation to the DEFAULT agent, not the caller's. So if RequestDefaultAgent does not
        take, pairing dies with 'No agent available for request type 2' and the board loops
        connect/pair-fail/disconnect. We record the outcome so a failure here is visible in the
        verdict instead of silently sinking the whole test.
        """
        try:
            self._agent = AutoAcceptAgent(self.bus, AGENT_PATH)
            mgr = dbus.Interface(self._obj("/org/bluez"), AGENT_MANAGER)
            mgr.RegisterAgent(AGENT_PATH, "NoInputNoOutput")
            self._agent_registered = True
            try:
                mgr.RequestDefaultAgent(AGENT_PATH)
                self.result["steps"]["agent"] = "default"
            except dbus.DBusException as e:
                self.result["steps"]["agent"] = f"registered-not-default: {e}"
        except dbus.DBusException as e:
            self.result["steps"]["agent"] = f"register-failed: {e}"

    def _prepare(self):
        ad_props = self._prop(self.central_path)
        self._prior_powered = bool(self._get(self.central_path, ADAPTER, "Powered", False))
        ad_props.Set(ADAPTER, "Powered", dbus.Boolean(True))
        self._register_agent()
        # Pin the central to the SPECIFIC board this transport drives (by BLE address), so a second
        # board running the same firmware / advertising the same name can't be connected instead.
        self.target_address = _board_ble_address(self.transport)
        if self.target_address:
            self.result["steps"]["target_addr"] = self.target_address
        try:
            self._iface(self.central_path, ADAPTER).SetDiscoveryFilter(
                {"Transport": dbus.String("le")})
        except dbus.DBusException:
            pass
        # If the MCU is already bonded/known from a prior run, start from connect.
        for path, ifaces in self._managed().items():
            if DEVICE not in ifaces or not path.startswith(self.central_path + "/dev_"):
                continue
            props = ifaces[DEVICE]
            if self._matches(props):
                self.dev_path = path
                self._capture_address(props)
                self.result["steps"]["discover"] = "already-known"
                return self._connect()
        # else scan
        self.result["steps"]["discover"] = "scanning"
        self.bus.add_signal_receiver(self._on_iface_added, dbus_interface=OM,
                                     signal_name="InterfacesAdded")
        self._iface(self.central_path, ADAPTER).StartDiscovery()
        self._scan_deadline = SCAN_TIMEOUT_S
        GLib.timeout_add_seconds(1, self._scan_tick)

    def _scan_tick(self):
        if self.dev_path is not None:
            return False
        # poll managed objects too (Name can arrive without a fresh InterfacesAdded)
        self._look_for_device(self._managed())
        if self.dev_path is not None:
            return False
        self._scan_deadline -= 1
        if self._scan_deadline <= 0:
            self._skip(f"{self.profile.advert_name} not found advertising within "
                       f"{SCAN_TIMEOUT_S}s (MCU absent, connected elsewhere, or not advertising)")
            return False
        return True

    def _on_iface_added(self, path, ifaces):
        if DEVICE in ifaces:
            self._look_for_device({path: ifaces})

    def _matches(self, props) -> bool:
        """Is this device the board we drive? Prefer the DRIVEN board's exact BLE address (set when
        the transport can report it -- see _board_ble_address); otherwise fall back to the advertised
        LocalName by PREFIX (the board appends -<nick>[-<client>] to the profile name, so an exact
        compare stopped matching once nicknames landed). Address wins because multiple boards share
        the name prefix."""
        if self.target_address:
            return str(props.get("Address", "")).upper() == self.target_address
        name = str(props.get("Name", props.get("Alias", "")))
        return CC.advert_name_matches(name, self.profile.advert_name)

    def _look_for_device(self, objs):
        for path, ifaces in objs.items():
            if DEVICE not in ifaces or not path.startswith(self.central_path + "/dev_"):
                continue
            props = ifaces[DEVICE]
            if self._matches(props):
                self.dev_path = path
                self._added_device = True
                self._capture_address(props)
                self.result["steps"]["discover"] = "found"
                # Deliberately DO NOT StopDiscovery here: on BlueZ 5.82 + this Intel hci0, stopping
                # discovery makes Connect() fall back to the accept-list background-scan method, which
                # stalls (~44s -> le-connection-abort-by-local; proven via btmon). Connecting WHILE
                # the active scan runs makes BlueZ issue a direct connection; it stops discovery
                # itself when it initiates the connection.
                GLib.timeout_add(200, lambda: (self._connect(), False)[1])
                return

    def _capture_address(self, props):
        """Record the peripheral BD address + type from an advertisement (for ConnectDevice)."""
        if props.get("Address"):
            self.dev_address = str(props["Address"])
        if props.get("AddressType"):
            self.dev_addr_type = str(props["AddressType"])

    def _connect(self):
        self.result["steps"]["connect"] = "connecting"
        # connectdevice path: a DIRECT Adapter1.ConnectDevice() create-connection, bypassing the
        # accept-list auto-connect that stalls on the Intel built-in hci0.
        if self.connect_method == "connectdevice":
            return self._connect_via_connectdevice()
        self.bus.add_signal_receiver(
            self._on_dev_props, dbus_interface=PROP, signal_name="PropertiesChanged",
            arg0=DEVICE, path=self.dev_path)
        # NOTE: do NOT set Trusted before connecting. A Trusted device makes BlueZ use the background
        # auto-connect (HCI_AUTO_CONN_ALWAYS) path -- accept-list + passive scan, no LE Create
        # Connection -> the stall we see. Leaving it untrusted makes Connect() a DIRECT connection.
        # Use a DIRECT connection (Device.Connect), NOT Pair(): on BlueZ 5.82 + this kernel, Pair()
        # (and the auto-connect path) uses the accept-list method, which stalls here -- it adds the
        # MCU to the accept list + background-scans but never issues an LE Create Connection, so
        # pairing times out (verified via btmon: Add Device + Pair Device, then no Connection
        # Complete, then Cancel Pair at the timeout). Connect() issues a direct LE Create Connection
        # to the advertised address. Bonding (the MCU requires an encrypted link for its HID service)
        # then happens ON DEMAND when we StartNotify an encrypted characteristic -- the auto-accept
        # agent completes the Just Works pairing. Async so the mainloop keeps servicing signals.
        self.result["steps"]["pair"] = "on-demand-at-notify"
        self._async_connect()

    def _async_connect(self):
        if bool(self._get(self.dev_path, DEVICE, "ServicesResolved", False)):
            return self._on_resolved()
        self._iface(self.dev_path, DEVICE).Connect(
            reply_handler=self._on_connected, error_handler=self._on_connect_err,
            timeout=CONNECT_TIMEOUT_S)

    def _connect_via_connectdevice(self):
        """Direct-connect using Adapter1.ConnectDevice (bluetoothd Experimental).

        ConnectDevice issues an LE Create Connection to the given address itself (no accept-list), so
        it sidesteps the hci0 auto-connect stall. It refuses if a device object for that address
        already exists, so we drop the scanned object first, then create+connect in one call. The
        reply is the new device object path.
        """
        if not self.dev_address:
            return self._fail("connectdevice: no peripheral address captured from the advertisement")
        # stop the discovery scan; ConnectDevice drives its own connection
        try:
            self._iface(self.central_path, ADAPTER).StopDiscovery()
        except dbus.DBusException:
            pass
        # remove the scanned device object so ConnectDevice does not hit AlreadyExists
        try:
            self._iface(self.central_path, ADAPTER).RemoveDevice(self.dev_path)
        except dbus.DBusException:
            pass
        props = CC.connectdevice_properties(self.dev_address, self.dev_addr_type)
        dbus_props = {k: dbus.String(v) for k, v in props.items()}
        self.result["steps"]["connect"] = "connectdevice"
        try:
            self._iface(self.central_path, ADAPTER).ConnectDevice(
                dbus_props, reply_handler=self._on_connectdevice_ok,
                error_handler=self._on_connect_err, timeout=CONNECT_TIMEOUT_S)
        except dbus.DBusException as e:
            return self._fail(f"ConnectDevice call failed (Experimental off?): {e}")

    def _on_connectdevice_ok(self, dev_path):
        self.dev_path = str(dev_path)
        self.bus.add_signal_receiver(
            self._on_dev_props, dbus_interface=PROP, signal_name="PropertiesChanged",
            arg0=DEVICE, path=self.dev_path)
        self.result["steps"]["connect"] = "connected"
        if bool(self._get(self.dev_path, DEVICE, "ServicesResolved", False)):
            self._on_resolved()

    def _on_connected(self):
        self.result["steps"]["connect"] = "connected"
        if bool(self._get(self.dev_path, DEVICE, "ServicesResolved", False)):
            self._on_resolved()

    def _on_connect_err(self, e):
        name = getattr(e, "get_dbus_name", lambda: "")()
        if "AlreadyConnected" in str(name):
            self.result["steps"]["connect"] = "already-connected"
            if bool(self._get(self.dev_path, DEVICE, "ServicesResolved", False)):
                self._on_resolved()
            return
        # ConnectDevice (and Connect) can report a "Connect failed" / timeout even though the LE link
        # actually came up: BlueZ's connect wraps service resolution, and our peripheral only bonds
        # ON DEMAND (at StartNotify of an encrypted char), so the higher-level connect can report
        # failure while the device is genuinely Connected with its GATT tree browsable. Before failing,
        # adopt an established link if one exists and proceed to wait for ServicesResolved.
        if self._adopt_established_link():
            return
        if self.backend == "dongle":
            return self._skip(
                "BlueZ same-host GATT limitation: hci0 (central) cannot connect to the local hci1 "
                f"HID peripheral on the same bluetoothd ({e})")
        self._fail(f"connect failed: {e}")

    def _adopt_established_link(self) -> bool:
        """If a device object for the peripheral is actually Connected, adopt it and continue.

        Returns True if it recovered the link (and kicked off resolution), False otherwise. Locates
        the device by the advertised address under the central adapter, since a ConnectDevice error
        may fire before we captured the new object path from its reply.
        """
        dev_path = self.dev_path
        if not dev_path and self.dev_address:
            suffix = "dev_" + self.dev_address.upper().replace(":", "_")
            dev_path = next(
                (p for p in self._managed()
                 if p.startswith(self.central_path + "/") and p.endswith(suffix)), None)
        if not dev_path:
            return False
        if not bool(self._get(dev_path, DEVICE, "Connected", False)):
            return False
        self.dev_path = dev_path
        self.result["steps"]["connect"] = "adopted-established"
        try:
            self.bus.add_signal_receiver(
                self._on_dev_props, dbus_interface=PROP, signal_name="PropertiesChanged",
                arg0=DEVICE, path=self.dev_path)
        except dbus.DBusException:
            pass
        # Nudge resolution: it may already be resolved, or arrive via _on_dev_props shortly.
        self._on_resolved()
        return True

    def _on_dev_props(self, iface, changed, invalidated):
        if changed.get("ServicesResolved"):
            self._on_resolved()

    def _ensure_bonded(self) -> bool:
        """Bond over the ALREADY-ESTABLISHED link before discovery, if not bonded yet.

        The MCU requires an encrypted link to expose/read its HID service (0x1812), so on a stack
        where the on-demand-at-StartNotify bonding is too late (the HID service is hidden or its reads
        time out pre-bond), we must Pair() first. Crucially the device is already CONNECTED here (via
        Connect/ConnectDevice), so Pair() runs SMP over the existing link -- it does NOT trigger a
        fresh LE Create Connection, so it avoids the accept-list auto-connect stall that made pairing
        FROM DISCONNECTED unusable on this host. Returns True if the caller must WAIT (a Pair() is in
        flight or was just kicked off), False if already bonded (proceed straight to discovery).
        """
        if bool(self._get(self.dev_path, DEVICE, "Paired", False)):
            return False                 # already bonded -> discovery may proceed
        if self._bonding:
            return True                  # a Pair() is ALREADY in flight -> keep waiting (do NOT
            #                              fall through to discovery: a racing ServicesResolved event
            #                              must not run discovery on the still-unencrypted link)
        self._bonding = True
        self.result["steps"]["bond"] = "pairing"
        self._iface(self.dev_path, DEVICE).Pair(
            reply_handler=self._on_paired, error_handler=self._on_pair_err,
            timeout=CONNECT_TIMEOUT_S)
        return True

    def _on_paired(self):
        self._bonding = False
        self.result["steps"]["bond"] = "paired"
        self._on_resolved()

    def _on_pair_err(self, e):
        # An already-in-progress or already-paired race is benign: fall through to discovery, which
        # reads the encrypted chars and will surface a real failure if the link is genuinely unusable.
        self._bonding = False
        self.result["steps"]["bond"] = f"pair-err: {getattr(e, 'get_dbus_name', lambda: e)()}"
        self._on_resolved(_force=True)

    def _on_resolved(self, _attempt=0, _force=False):
        if self.result["steps"].get("resolved"):
            return
        # Establish encryption before we try to read the HID service (see _ensure_bonded).
        if not _force and self._ensure_bonded():
            return
        objs = self._managed()
        # wait until the HID service + its report map char are populated (can lag ServicesResolved)
        have_map = any(
            CHRC in i and p.startswith(self.dev_path + "/")
            and str(i[CHRC].get("UUID", "")).lower() == CC.REPORT_MAP_UUID
            for p, i in objs.items())
        if not have_map and _attempt < 10:
            GLib.timeout_add(400, lambda: (self._on_resolved(_attempt + 1), False)[1])
            return
        self.result["steps"]["resolved"] = True
        self.result["steps"]["connect"] = "connected"
        try:
            self._discover(objs)
        except dbus.DBusException as e:
            return self._fail(f"discovery: {e}")

    # ---- discovery + verification ----
    def _read_char(self, path) -> bytes:
        val = self._iface(path, CHRC).ReadValue({})
        return bytes(bytearray(val))

    def _read_desc(self, path) -> bytes:
        val = self._iface(path, DESC).ReadValue({})
        return bytes(bytearray(val))

    def _discover(self, objs):
        # 1. HID service present?
        if not any(str(i.get("org.bluez.GattService1", {}).get("UUID", "")).lower()
                   == CC.HID_SERVICE_UUID for i in objs.values()):
            return self._fail("HID service (0x1812) not found on peripheral")
        # 2. Report Map (0x2A4B)
        map_path = next(
            (p for p, i in objs.items()
             if CHRC in i and p.startswith(self.dev_path + "/")
             and str(i[CHRC].get("UUID", "")).lower() == CC.REPORT_MAP_UUID), None)
        if map_path is None:
            return self._fail("Report Map char (0x2A4B) not found")
        report_map = self._read_char(map_path)
        self.result["report_map_hex"] = report_map.hex()
        diff = CC.compare_report_maps(
            R.REPORT_DESCRIPTOR, report_map,
            report_id_remap=self.profile.report_id_remap,
            subset_fields=self.profile.map_subset)
        self.result["report_map"] = {
            "byte_exact": diff.byte_exact, "load_bearing_ok": diff.load_bearing_ok,
            "ok": diff.ok, "matched_reports": diff.matched_reports,
            "actual_reports": diff.actual_reports, "mismatches": diff.mismatches}
        if self.profile.byte_exact_map and not diff.byte_exact:
            return self._fail(f"Report Map not byte-exact vs reports.py: {diff.mismatches}")
        if not diff.ok:
            return self._fail(f"Report Map load-bearing mismatch: {diff.mismatches}")
        # 3. Report chars (0x2A4D) + Report Reference (0x2908)
        self.report_chars = CC.find_report_characteristics(objs, self.dev_path, self._read_desc)
        self.result["report_refs"] = {
            str(rid): [rid, v["report_type"]] for rid, v in self.report_chars.items()}
        # required minimum: keyboard [1,1] + mouse [mouse_id,1]
        required = {R.REPORT_ID_KEYBOARD: "keyboard", self.profile.mouse_report_id: "mouse"}
        for rid, label in required.items():
            v = self.report_chars.get(rid)
            if v is None:
                return self._fail(f"missing {label} Report Reference [{rid},1]")
            if v["report_type"] != CC.REPORT_TYPE_INPUT:
                return self._fail(f"{label} report {rid} is not an INPUT report "
                                  f"(type={v['report_type']})")
        # optional abs (3) / digitizer (4) refs are recorded if present (harness may assert them)
        # 4. build the drive/verify targets: every present report we can drive over the transport
        self._plan_drives()
        if not self._drive_targets:
            return self._fail("no driveable report kinds resolved on this peripheral")
        self._start_notify_and_inject()

    def _plan_drives(self):
        # firmware report id -> reports.py canonical id (invert the remap)
        inverse = {v: k for k, v in self.profile.report_id_remap.items()}
        for fw_id in self.report_chars:
            canon = inverse.get(fw_id, fw_id)
            drive = KNOWN_DRIVES.get(canon)
            if drive is None:
                continue   # abs/digitizer: verified at map+ref level; transport can't drive it yet
            kind, build = drive
            try:
                body = build()
                # confirm the transport can encode this kind (fail fast, don't send on the wire yet)
                self._encode_check(kind, body)
            except Exception as e:  # noqa: BLE001
                self.result["steps"].setdefault("undriveable", {})[str(fw_id)] = str(e)
                continue
            self._drive_targets.append((fw_id, kind, body))

    def _encode_check(self, kind, body):
        # serial transports expose a pure encode_frame; the daemon transport encodes at send time.
        mod = None
        if self.profile.driver == "serial_pico":
            from hidbridge import transport_pico as mod
        elif self.profile.driver in ("serial_esp32", "net"):
            # net drives the same ESP32 firmware; the frame encoding is identical.
            from hidbridge import transport_esp32 as mod
        if mod is not None:
            mod.encode_frame(kind, body)

    def _start_notify_and_inject(self):
        for fw_id, _kind, _body in self._drive_targets:
            cpath = self.report_chars[fw_id]["char_path"]
            self.bus.add_signal_receiver(
                self._make_notify_cb(fw_id), dbus_interface=PROP,
                signal_name="PropertiesChanged", arg0=CHRC, path=cpath)
            try:
                self._iface(cpath, CHRC).StartNotify()
                self.notify_started.append(cpath)
            except dbus.DBusException as e:
                return self._fail(f"StartNotify report {fw_id}: {e}")
        self.result["steps"]["notify"] = "armed"
        GLib.timeout_add(900, lambda: (self._inject(), False)[1])

    def _make_notify_cb(self, fw_id):
        def cb(iface, changed, invalidated):
            if "Value" in changed:
                self.received[fw_id] = bytes(bytearray(changed["Value"]))
                self._check_done()
        return cb

    def _inject(self):
        self.result["steps"]["inject"] = f"sending ({self.mode})"
        # Inject the SAME KNOWN report bodies either as Mode 2 frames (transport.send) or, for
        # mode=rpc, as a Mode 1 JSON-RPC `frames` call over the transport's OWN already-open channel.
        # One report per target so the per-report ack ("notified" vs "noconn") stays visible.
        try:
            client = self._rpc_client() if self.mode == "rpc" else None
            for fw_id, kind, body in self._drive_targets:
                if client is not None:
                    # Mode 1: relay the pre-built report bytes; a real send returns {sent, conn}
                    # (conn:false = accepted but no central on the target slot -- the Mode 2 "noconn").
                    res = client.frames([CC.frames_method_hex(fw_id, body)])
                    self.result["steps"].setdefault("inject_resp", {})[str(fw_id)] = res
                    if res.get("sent", 1) == 0:
                        return self._fail(f"mode=rpc frames send reported nothing sent: {res}")
                else:
                    res = self.transport.send([(kind, body)])
                    # record the board's ack so a "notified" vs "noconn" (board saw no central on the
                    # target slot) is visible in the verdict when a notification fails to arrive.
                    self.result["steps"].setdefault("inject_resp", {})[str(fw_id)] = res.get("resp")
                    if not res.get("ok", True):
                        return self._fail(f"transport send failed: {res}")
        except Exception as e:  # noqa: BLE001
            return self._fail(f"transport send raised: {e}")
        # give notifications a moment; verdict is evaluated on arrival or at timeout
        GLib.timeout_add_seconds(4, self._settle)

    def _rpc_client(self):
        """A Mode1Client bound to the transport's OWN open channel (transport._ser).

        Reusing the ALREADY-open serial/TCP channel is deliberate: opening a second connection to the
        same board would fight the first (a serial port is single-open; the board resets on a fresh
        USB open). By inject time the frame transports have opened `_ser` (ping/select_slot ran on the
        connect path); nudge it open defensively otherwise. The daemon (dongle) backend has no such
        channel -- it never reaches here (main() skips mode=rpc for it)."""
        ser = getattr(self.transport, "_ser", None)
        if ser is None:
            for opener in ("_open", "_get"):   # esp32/net expose _open; pico exposes _get
                fn = getattr(self.transport, opener, None)
                if fn is not None:
                    fn()
                    ser = getattr(self.transport, "_ser", None)
                    break
        if ser is None:
            raise RuntimeError("mode=rpc: transport exposes no open JSON-RPC channel (_ser)")
        from hidbridge.mode1 import Mode1Client
        return Mode1Client(ser)

    def _settle(self):
        self._verdict()
        self.loop.quit()
        return False

    def _check_done(self):
        if all(fw_id in self.received for fw_id, _k, _b in self._drive_targets):
            self._verdict()
            self.loop.quit()

    def _verdict(self):
        expected = {}
        received = {}
        pad = {}
        for fw_id, _kind, body in self._drive_targets:
            expected[fw_id] = body
            received[fw_id] = self.received.get(fw_id, b"")
            pad[fw_id] = self.profile.mouse_pad_zero and fw_id == self.profile.mouse_report_id
        v = CC.notification_verdict(received, expected, allow_trailing_zero=pad)
        self.result["notifications"] = {
            "expected": {str(k): b.hex() for k, b in expected.items()},
            "received": {str(k): received[k].hex() for k in received},
            "verdict": v}
        map_ok = self.result.get("report_map", {}).get("ok", False)
        self.result["status"] = "pass" if (v["ok"] and map_ok) else "fail"

    # ---- cleanup: restore hci0 as found ----
    def _cleanup(self):
        for cpath in self.notify_started:
            try:
                self._iface(cpath, CHRC).StopNotify()
            except dbus.DBusException:
                pass
        try:
            if self.transport is not None and hasattr(self.transport, "close"):
                self.transport.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._iface(self.central_path, ADAPTER).StopDiscovery()
        except dbus.DBusException:
            pass
        if self.dev_path is not None:
            try:
                self._iface(self.dev_path, DEVICE).Disconnect()
            except dbus.DBusException:
                pass
            # drop the transient bond so hci0 is left as found
            try:
                self._iface(self.central_path, ADAPTER).RemoveDevice(self.dev_path)
            except dbus.DBusException:
                pass
        # unregister the pairing agent
        if self._agent_registered:
            try:
                dbus.Interface(self._obj("/org/bluez"), AGENT_MANAGER).UnregisterAgent(AGENT_PATH)
            except dbus.DBusException:
                pass
        if self._agent is not None:
            try:
                self._agent.remove_from_connection()
            except Exception:  # noqa: BLE001
                pass
        # restore prior Powered state (never touch identity/discoverable)
        if self._prior_powered is False:
            try:
                self._prop(self.central_path).Set(ADAPTER, "Powered", dbus.Boolean(False))
            except dbus.DBusException:
                pass


def resolve_central(prefer: str | None) -> str | None:
    """Pick the adapter to act as the transient central.

    Default: the built-in adapter (hci0). Pass --central hciN/MAC to pin a specific adapter -- e.g.
    a USB DONGLE as the central instead of the Intel built-in, the stall-workaround (b): a dongle may
    not exhibit the built-in's LE auto-connect stall. (The hci0-only guardrail is about not SERVING
    HID on hci0; using ANY adapter purely as a transient CENTRAL is fine.)
    """
    adapters = A.list_adapters()
    if prefer:
        match = next((a for a in adapters if a["hci"] == prefer
                      or a.get("address", "").lower() == prefer.lower()), None)
        return match["hci"] if match else None
    builtins = [a for a in adapters if a["kind"] == "builtin"]
    return builtins[0]["hci"] if builtins else None


def experimental_available(bus, central_hci: str) -> bool:
    """True iff this bluetoothd exposes Adapter1.ConnectDevice (i.e. Experimental is on).

    Detected by introspecting the adapter interface, so it needs no config-file access and reflects
    the LIVE daemon state.
    """
    try:
        obj = bus.get_object(BLUEZ, f"/org/bluez/{central_hci}")
        xml = dbus.Interface(obj, "org.freedesktop.DBus.Introspectable").Introspect()
        return "ConnectDevice" in str(xml)
    except dbus.DBusException:
        return False


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--backend", required=True, choices=sorted(CC.BACKEND_PROFILES))
    p.add_argument("--central", default=None,
                   help="central hci/MAC (default: the built-in adapter; pass a dongle hci to work "
                        "around the built-in LE auto-connect stall)")
    p.add_argument("--connect-method",
                   default=os.environ.get("BT_HID_CONNECT_METHOD", "auto"),
                   choices=CC.CONNECT_METHODS,
                   help="LE connect method: scan (direct Connect during scan), connectdevice "
                        "(Adapter1.ConnectDevice, needs bluetoothd Experimental -- the hci0 "
                        "auto-connect-stall escape), or auto (connectdevice if available, else scan)")
    p.add_argument("--mode", default=os.environ.get("BT_HID_MODE", "frames"),
                   choices=("frames", "rpc"),
                   help="inject mode: frames (Mode 2 frame relay -- the host builds the report bytes) "
                        "or rpc (Mode 1 JSON-RPC `frames` over the transport's OWN channel -- the "
                        "board relays the same bytes). Both must deliver identical GATT notifications")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    profile = CC.BACKEND_PROFILES[args.backend]
    central = resolve_central(args.central)
    if central is None:
        out = {"status": "skip", "backend": args.backend, "mode": args.mode,
               "reason": "no built-in adapter present to use as the transient central"}
        print(json.dumps(out) if args.json else f"SKIP: {out['reason']}")
        return 2

    # mode=rpc drives the board via Mode 1 JSON-RPC, which only the MCU firmwares (esp32/pico/net)
    # speak. The dongle backend is a local BlueZ daemon with no JSON-RPC channel -- skip clearly
    # rather than fail (mode=frames still covers it).
    if args.mode == "rpc" and profile.driver == "daemon":
        reason = ("mode=rpc needs JSON-RPC firmware; the dongle backend is a BlueZ daemon (no "
                  "JSON-RPC) -- only mode=frames applies")
        out = {"status": "skip", "backend": args.backend, "mode": args.mode, "central": central,
               "reason": reason}
        print(json.dumps(out) if args.json else f"SKIP: {reason}")
        return 2

    # Resolve the effective LE-connect method against the live daemon's Experimental capability.
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    probe_bus = dbus.SystemBus()
    experimental = experimental_available(probe_bus, central)
    try:
        connect_method = CC.resolve_connect_method(args.connect_method, experimental)
    except CC.ConnectMethodError as e:
        out = {"status": "skip", "backend": args.backend, "mode": args.mode, "central": central,
               "reason": str(e)}
        print(json.dumps(out) if args.json else f"SKIP: {e}")
        return 2

    transport, skip = make_transport(profile)
    if skip:
        out = {"status": "skip", "backend": args.backend, "mode": args.mode, "central": central,
               "reason": skip}
        print(json.dumps(out) if args.json else f"SKIP: {skip}")
        return 2

    # If the board is ALREADY connected to a central at the very start, that is (probably) a real
    # iPad -- skip rather than disturb it. (An orphaned link from one of OUR own retries below is
    # freed between attempts, not here.)
    conn = transport_connected(transport, profile)
    if conn is True:
        reason = (f"{profile.advert_name} is already connected to another central (an iPad?); a BLE "
                  "peripheral serves one central -- skipping to avoid disturbing it")
        try:
            transport.close()
        except Exception:  # noqa: BLE001
            pass
        out = {"status": "skip", "backend": args.backend, "mode": args.mode, "central": central,
               "reason": reason}
        print(json.dumps(out) if args.json else f"SKIP: {reason}")
        return 2

    test = CentralSelfTest(args.backend, central, transport, connect_method=connect_method,
                           mode=args.mode)
    result = test.run()
    result["connect_method"] = connect_method
    result["retryable"] = CC.is_retryable_failure(result)

    if args.json:
        print(json.dumps(result))
    else:
        status = result["status"].upper()
        print(f"central self-test {status}: backend={args.backend} central={central} "
              f"mode={args.mode} connect_method={connect_method}")
        if result.get("report_map"):
            print(f"  report map: {result['report_map']}")
        if result.get("report_refs"):
            print(f"  report refs: {result['report_refs']}")
        if result.get("notifications"):
            print(f"  notifications: {result['notifications']}")
        for k in ("reason", "error"):
            if result.get(k):
                print(f"  {k}: {result[k]}")
        print(f"  steps: {result['steps']}")
    return {"pass": 0, "fail": 1, "skip": 2}.get(result["status"], 1)


if __name__ == "__main__":
    sys.exit(main())

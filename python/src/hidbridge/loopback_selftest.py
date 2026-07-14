"""On-host loopback self-test: the BUILT-IN adapter (central) pairs with + drives our emulated
keyboard/mouse (peripheral on the DONGLE), and we assert the exact HID report bytes arrive.

This closes the whole chain WITHOUT an iPad: hci0 (built-in, acting as the "iPad" central) scans,
connects, and subscribes to our BLE HID report characteristics on hci1 (dongle); we then inject a
known keyboard report + a known mouse report through the normal `HidController` -> daemon path and
confirm the central receives the exact bytes over GATT notifications.

Using the built-in adapter as the CENTRAL here is deliberate + the maintainer-sanctioned for the self-test;
it is non-destructive: we RemoveDevice (drop the bond) from the built-in on cleanup, restoring it.

Runs under the SYSTEM python (needs dbus + gi) and needs root (adapter control) + the daemon up on
the dongle. Exit 0 = PASS, non-zero = FAIL. `--json` prints a machine-readable result line.
"""

from __future__ import annotations

import argparse
import json
import sys

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

from hidbridge import adapter as A  # noqa: E402
from hidbridge import reports as R  # noqa: E402
from hidbridge.controller import HidController, SocketTransport  # noqa: E402

BLUEZ = "org.bluez"
OM = "org.freedesktop.DBus.ObjectManager"
PROP = "org.freedesktop.DBus.Properties"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
CHRC = "org.bluez.GattCharacteristic1"

REPORT_UUID = "00002a4d-0000-1000-8000-00805f9b34fb"
REPORT_REF_UUID = "00002908-0000-1000-8000-00805f9b34fb"

# Known test reports (must match reports.py encoders).
KBD_EXPECT = bytes([0x00, 0x00, 0x04, 0, 0, 0, 0, 0])   # 'a' key down
MOUSE_EXPECT = bytes([0x00, 3, 0, 0])                   # move dx=3


class Loopback:
    def __init__(self, central_hci, dongle_addr):
        self.central_path = f"/org/bluez/{central_hci}"
        self.dongle_addr = dongle_addr.upper()
        self.dev_path = f"{self.central_path}/dev_" + dongle_addr.upper().replace(":", "_")
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SystemBus()
        self.loop = GLib.MainLoop()
        self.received = {}          # reportID -> bytes
        self.result = {"ok": False, "steps": {}}
        self._notify_chars = {}     # reportID -> char path

    # ---- helpers ----
    def _obj(self, path):
        return self.bus.get_object(BLUEZ, path)

    def _prop(self, path):
        return dbus.Interface(self._obj(path), PROP)

    def _managed(self):
        return dbus.Interface(self._obj("/"), OM).GetManagedObjects()

    def _fail(self, msg):
        self.result["error"] = msg
        self.loop.quit()

    # ---- flow ----
    def run(self):
        GLib.timeout_add_seconds(45, self._timeout)
        try:
            self._start()
        except dbus.DBusException as e:
            return self._done(False, f"dbus: {e}")
        self.loop.run()
        self._cleanup()
        return self.result

    def _timeout(self):
        self._fail("timeout (45s) before completing loopback")
        return False

    def _start(self):
        ad = dbus.Interface(self._obj(self.central_path), ADAPTER)
        self._prop(self.central_path).Set(ADAPTER, "Powered", dbus.Boolean(True))
        try:
            ad.SetDiscoveryFilter({"Transport": dbus.String("le")})
        except dbus.DBusException:
            pass
        # if we already know the device (stale), start from connect
        if DEVICE in self._managed().get(self.dev_path, {}):
            self.result["steps"]["discover"] = "already-known"
            return self._connect()
        self.result["steps"]["discover"] = "scanning"
        self.bus.add_signal_receiver(self._on_iface_added,
                                     dbus_interface=OM, signal_name="InterfacesAdded")
        ad.StartDiscovery()

    def _on_iface_added(self, path, ifaces):
        if path == self.dev_path and DEVICE in ifaces:
            self.result["steps"]["discover"] = "found"
            try:
                dbus.Interface(self._obj(self.central_path), ADAPTER).StopDiscovery()
            except dbus.DBusException:
                pass
            GLib.timeout_add(500, lambda: (self._connect(), False)[1])

    def _connect(self):
        self.result["steps"]["connect"] = "connecting"
        dev = dbus.Interface(self._obj(self.dev_path), DEVICE)
        # subscribe to ServicesResolved
        self.bus.add_signal_receiver(
            self._on_dev_props, dbus_interface=PROP, signal_name="PropertiesChanged",
            arg0=DEVICE, path=self.dev_path)
        try:
            dev.Connect()
        except dbus.DBusException as e:
            # some stacks need Pair() first for a HID peripheral
            try:
                dev.Pair()
                dev.Connect()
            except dbus.DBusException:
                return self._fail(f"connect failed: {e}")
        # If services already resolved synchronously, proceed.
        if bool(self._safe_get(self.dev_path, DEVICE, "ServicesResolved")):
            self._on_resolved()

    def _safe_get(self, path, iface, key, default=False):
        try:
            return self._prop(path).Get(iface, key)
        except dbus.DBusException:
            return default

    def _on_dev_props(self, iface, changed, invalidated):
        if changed.get("ServicesResolved"):
            self._on_resolved()

    def _on_resolved(self, _attempt=0):
        if self.result["steps"].get("resolved"):
            return
        # chars can populate a beat after ServicesResolved; retry enumeration a few times.
        objs = self._managed()
        have = [p for p, i in objs.items()
                if CHRC in i and p.startswith(self.dev_path + "/")
                and str(i[CHRC].get("UUID", "")).lower() == REPORT_UUID]
        if not have and _attempt < 8:
            GLib.timeout_add(400, lambda: (self._on_resolved(_attempt + 1), False)[1])
            return
        self.result["steps"]["resolved"] = True
        self.result["steps"]["connect"] = "connected"
        # find the two report characteristics under the device, keyed by reportID (from 0x2908)
        for path, ifaces in objs.items():
            if CHRC not in ifaces or not path.startswith(self.dev_path + "/"):
                continue
            if str(ifaces[CHRC].get("UUID", "")).lower() != REPORT_UUID:
                continue
            report_id = self._report_id_of(objs, path)
            if report_id in (R.REPORT_ID_KEYBOARD, R.REPORT_ID_MOUSE):
                self._notify_chars[report_id] = path
        if R.REPORT_ID_KEYBOARD not in self._notify_chars or \
                R.REPORT_ID_MOUSE not in self._notify_chars:
            return self._fail(f"HID report chars not found: {list(self._notify_chars)}")
        self.result["steps"]["chars"] = {str(k): v for k, v in self._notify_chars.items()}
        for rid, cpath in self._notify_chars.items():
            self.bus.add_signal_receiver(
                self._make_notify_cb(rid), dbus_interface=PROP,
                signal_name="PropertiesChanged", arg0=CHRC, path=cpath)
            try:
                dbus.Interface(self._obj(cpath), CHRC).StartNotify()
            except dbus.DBusException as e:
                return self._fail(f"StartNotify {rid}: {e}")
        # give notifications a moment to arm, then inject
        GLib.timeout_add(800, lambda: (self._inject(), False)[1])

    def _report_id_of(self, objs, char_path):
        for path, ifaces in objs.items():
            if path.startswith(char_path + "/") and "org.bluez.GattDescriptor1" in ifaces:
                desc = ifaces["org.bluez.GattDescriptor1"]
                if str(desc.get("UUID", "")).lower() == REPORT_REF_UUID:
                    try:
                        val = dbus.Interface(self._obj(path), "org.bluez.GattDescriptor1")\
                            .ReadValue({})
                        return int(val[0])
                    except dbus.DBusException:
                        return None
        return None

    def _make_notify_cb(self, report_id):
        def cb(iface, changed, invalidated):
            if "Value" in changed:
                self.received[report_id] = bytes(changed["Value"])
                self._check_done()
        return cb

    def _inject(self):
        self.result["steps"]["inject"] = "sending"
        ctl = HidController(SocketTransport())
        ctl.type_text("a")          # keyboard 'a' down + release
        ctl.move(3, 0)              # mouse dx=3
        self._check_done()

    def _check_done(self):
        if (self.received.get(R.REPORT_ID_KEYBOARD) is not None and
                self.received.get(R.REPORT_ID_MOUSE) is not None):
            self.loop.quit()

    def _cleanup(self):
        # drop the bond from the built-in adapter (leave it clean) + disconnect
        try:
            dbus.Interface(self._obj(self.dev_path), DEVICE).Disconnect()
        except dbus.DBusException:
            pass
        try:
            dbus.Interface(self._obj(self.central_path), ADAPTER).RemoveDevice(self.dev_path)
        except dbus.DBusException:
            pass

    def _done(self, ok, err=None):
        self.result["ok"] = ok
        if err:
            self.result["error"] = err
        return self.result

    def verdict(self):
        kbd = self.received.get(R.REPORT_ID_KEYBOARD)
        mouse = self.received.get(R.REPORT_ID_MOUSE)
        self.result["received"] = {
            "keyboard": kbd.hex() if kbd else None,
            "mouse": mouse.hex() if mouse else None,
        }
        ok = (kbd == KBD_EXPECT and mouse == MOUSE_EXPECT)
        self.result["expected"] = {"keyboard": KBD_EXPECT.hex(), "mouse": MOUSE_EXPECT.hex()}
        self.result["ok"] = ok
        return ok


def main(argv=None):
    p = argparse.ArgumentParser(description="on-host BLE HID loopback self-test (built-in <- dongle)")
    p.add_argument("--central", default=None, help="central hci (default: the built-in adapter)")
    p.add_argument("--dongle", default=None, help="dongle hci/MAC (default: auto-detect)")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    adapters = A.list_adapters()
    dongle = A.select_adapter(adapters, prefer=args.dongle)
    if args.central:
        central = next(a for a in adapters if a["hci"] == args.central)["hci"]
    else:
        builtins = [a for a in adapters if a["kind"] == "builtin"]
        if not builtins:
            print("no built-in adapter to use as central", file=sys.stderr)
            return 2
        central = builtins[0]["hci"]

    lb = Loopback(central, dongle["address"])
    lb.run()
    ok = lb.verdict()
    if args.json:
        print(json.dumps(lb.result))
    else:
        print(f"loopback {'PASS' if ok else 'FAIL'}: central={central} dongle={dongle['hci']} "
              f"({dongle['address']})")
        print(f"  keyboard: got {lb.result['received']['keyboard']} "
              f"want {lb.result['expected']['keyboard']}")
        print(f"  mouse:    got {lb.result['received']['mouse']} "
              f"want {lb.result['expected']['mouse']}")
        if not ok and lb.result.get("error"):
            print(f"  error: {lb.result['error']}")
        print(f"  steps: {lb.result['steps']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

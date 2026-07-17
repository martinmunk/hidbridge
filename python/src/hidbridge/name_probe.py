"""BLE name probe -- scan on an adapter and report a peripheral's ADVERTISED Complete Local Name.

This is the missing HW-in-loop check for the naming surface: earlier tests verified the report-map
bytes + notification bytes over BLE, but never that the board ADVERTISES the name it should, and never
the GAP Device Name characteristic (0x2A00) read after connecting -- exactly the surface that once
showed two different names (advertised "HIDBRIDGE-<nick>-<client>" while scanning, then a stale fixed
"HIDBRIDGE-esp32" once connected). test_bt_hid_naming_live.py drives the board through states with the
Mode1 client and calls this to assert the over-the-air name at each state.

SCAN vs CONNECT on this host: scanning works even where the built-in adapter's LE auto-connect stalls
(docs/bt-hid-central-selftest-status.md), so the advertised-name half is runnable; the optional
--read-gap connects to read 0x2A00 and is best-effort (it may stall/skip, reported as such).

Runnable (needs the SYSTEM python -- dbus + gi -- an adapter, and usually root):
    python -m hidbridge.name_probe --central hci4 --address AA:BB:CC:DD:EE:03 --json
    python -m hidbridge.name_probe --central hci4 --address AA:BB:CC:DD:EE:03 --read-gap --json
Prints one JSON line: {address, central, adv_name, status: ok|timeout|error, [gap: {...}]}.
"""

from __future__ import annotations

import argparse
import json
import time

BLUEZ = "org.bluez"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
GATT_CHRC = "org.bluez.GattCharacteristic1"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
GAP_DEVICE_NAME_UUID = "00002a00-0000-1000-8000-00805f9b34fb"   # GAP service 0x1800 / char 0x2A00


def _bus():
    import dbus
    return dbus.SystemBus()


def _managed(bus) -> dict:
    import dbus
    om = dbus.Interface(bus.get_object(BLUEZ, "/"), OM_IFACE)
    return om.GetManagedObjects()


def _find_device_path(bus, target: str):
    """Return (path, advertised_name) for the Device1 whose Address matches `target`, or (None, None)."""
    for path, ifaces in _managed(bus).items():
        d = ifaces.get(DEVICE)
        if not d:
            continue
        if str(d.get("Address", "")).upper() == target:
            nm = d.get("Name", d.get("Alias"))
            return path, (str(nm) if nm is not None else None)
    return None, None


def _read_gap_name(bus, dev_path: str, seconds: float = 8.0) -> dict:
    """Connect + read the GAP Device Name characteristic (0x2A00). Best-effort: connect may stall."""
    import dbus
    dev = dbus.Interface(bus.get_object(BLUEZ, dev_path), DEVICE)
    props = dbus.Interface(bus.get_object(BLUEZ, dev_path), PROPS_IFACE)
    try:
        dev.Connect()
    except Exception as e:  # noqa: BLE001 -- connect stall is an expected outcome, reported not raised
        return {"status": "connect-failed", "error": str(e)[:140]}
    try:
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                if bool(props.Get(DEVICE, "ServicesResolved")):
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.3)
        for path, ifaces in _managed(bus).items():
            c = ifaces.get(GATT_CHRC)
            if not c or not path.startswith(dev_path + "/"):
                continue
            if str(c.get("UUID", "")).lower() == GAP_DEVICE_NAME_UUID:
                chrc = dbus.Interface(bus.get_object(BLUEZ, path), GATT_CHRC)
                try:
                    val = chrc.ReadValue({})
                    return {"status": "ok", "gap_name": bytes(bytearray(val)).decode("utf-8", "replace")}
                except Exception as e:  # noqa: BLE001
                    return {"status": "read-failed", "error": str(e)[:140]}
        return {"status": "no-2a00"}
    finally:
        try:
            dev.Disconnect()
        except Exception:  # noqa: BLE001
            pass


def probe(central_hci: str, address: str, seconds: float = 8.0, read_gap: bool = False) -> dict:
    """Scan `central_hci` for `address`; return its advertised name (+ optional GAP name)."""
    import dbus
    bus = _bus()
    target = address.upper()
    result = {"address": target, "central": central_hci, "adv_name": None, "status": "timeout"}
    try:
        adapter = dbus.Interface(bus.get_object(BLUEZ, f"/org/bluez/{central_hci}"), ADAPTER)
    except Exception as e:  # noqa: BLE001
        return {**result, "status": "error", "error": f"no adapter {central_hci}: {str(e)[:120]}"}
    try:
        try:
            adapter.SetDiscoveryFilter(dbus.Dictionary({"Transport": "le"}, signature="sv"))
        except Exception:  # noqa: BLE001 -- filter is an optimisation; scanning works without it
            pass
        adapter.StartDiscovery()
    except Exception as e:  # noqa: BLE001
        return {**result, "status": "error", "error": f"StartDiscovery: {str(e)[:120]}"}
    dev_path = None
    try:
        # CRITICAL: BlueZ caches a sticky Name per address, so a device seen earlier keeps its OLD
        # advertised name even after the board changes it (proven: a freshly-labelled slot still read
        # the base name). Remove the cached entry so the poll below reads the CURRENT advertisement.
        cached, _ = _find_device_path(bus, target)
        if cached:
            try:
                adapter.RemoveDevice(cached)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.4)
        deadline = time.time() + seconds
        while time.time() < deadline:
            dev_path, name = _find_device_path(bus, target)
            if name is not None:
                result["adv_name"] = name
                result["status"] = "ok"
                break
            time.sleep(0.4)
        if read_gap and dev_path is not None:
            result["gap"] = _read_gap_name(bus, dev_path)
    finally:
        try:
            adapter.StopDiscovery()
        except Exception:  # noqa: BLE001
            pass
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Scan for a BLE peripheral's advertised name.")
    ap.add_argument("--central", required=True, help="scanning adapter, e.g. hci4")
    ap.add_argument("--address", required=True, help="target BD address AA:BB:CC:DD:EE:FF")
    ap.add_argument("--seconds", type=float, default=8.0, help="scan timeout (default 8)")
    ap.add_argument("--read-gap", action="store_true", help="also connect + read GAP name (0x2A00)")
    ap.add_argument("--json", action="store_true", help="emit the verdict as JSON (always on)")
    a = ap.parse_args(argv)
    r = probe(a.central, a.address, a.seconds, a.read_gap)
    print(json.dumps(r))
    return 0 if r.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

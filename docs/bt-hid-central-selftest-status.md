# bt_hid BLE-central self-test -- status + live blocker (2026-07-10)

Branch: `bt-hid-selftest-central`. Goal: validate the MCU BT-HID peripherals (Pico `wsb-hid-pico`,
ESP32 `wsb-hid-esp32`) END TO END with NO iPad, using the wsb BUILT-IN Bluetooth adapter (hci0) as a
transient BLE CENTRAL -- connect, byte-verify the HID Report Map (0x2A4B) + Report References
(0x2908) against `python/src/hidbridge/reports.py` (the single source of truth), and for each driveable
report kind inject a known frame over the MCU serial and assert the exact bytes arrive back as a GATT
notification on hci0.

## What is DONE + banked (green, pushed)

- **Pure core** `python/src/hidbridge/central_core.py`: USB-HID report-descriptor parser + byte-exact /
  load-bearing / subset comparator vs `reports.py`; GATT-tree helper (dbus reads injected);
  notification byte-verdict (incl. the Pico trailing-zero pan pad); per-backend profiles.
- **Live driver** `python/src/hidbridge/central.py` (system python / dbus+gi): scan-by-advert-name ->
  connect -> read+verify Report Map + Report Refs -> StartNotify on input Report chars (0x2A4D) ->
  inject over the MCU serial -> assert notification bytes. hci0 is CENTRAL-only; Powered + transient
  bond restored on teardown; identity never touched. Includes a NoInputNoOutput auto-accept pairing
  agent (the MCU HID service needs a bonded link) and fully async connect.
- **Device-gated pytest** `python/tests/test_bt_hid_central_selftest.py` (`BT_HID_CENTRAL=1`),
  parametrized pico/esp32/dongle, PREPARE/RUN/VERIFY/CLEANUP.
- **Offline unit tests** `python/tests/test_bt_hid_central_core.py` (15) + the CH340 safety
  regression `python/tests/test_bt_hid_esp32_portsafety.py` (5). Full offline suite green; ruff clean.

## SAFETY FIX (guardrail-critical) -- must propagate on reconciliation

`python/src/hidbridge/transport_esp32.py` `detect_port()` used to rank a **CH340 (USB vendor 1a86)** as a
valid ESP32 bridge (its hint list contained `"ch34"`), so on wsb it returned `/dev/ttyUSB0` = **the
KVM control adapter**. Streaming HID frames there would drive the KVM. CLAUDE.md is explicit: NEVER
write to `/dev/ttyUSB0` (KVM CH340, vendor 1a86); vendor-gate the ESP32 to the CP2102 (10c4).
Fixed on this branch: `detect_port` now reads each candidate's USB idVendor, HARD-EXCLUDES `1a86`,
and prefers `10c4` (the real ESP32 = `/dev/ttyUSB1`). 5 regression tests.

**PROPAGATION REQUIRED:** the ORIGINAL `transport_esp32.py` on BOTH `origin/bt-hid-esp32` and
`origin/bt-hid-abs-esp32` still has the hazard (`_ESP32_USB_HINTS` with `"ch34"`, no `1a86`
exclusion). The `1a86`-exclusion fix MUST be carried into those during the eventual reconciliation
merge -- it is a safety issue, not just this branch's concern.

## LIVE BLOCKER (why the on-glass verify is a MORNING task, not this code)

### 1. hci0 kernel LE-auto-connect stall
On this host (BlueZ 5.82, kernel 6.12.90, Intel built-in `hci0`), `Device.Connect()` to an LE
peripheral is translated by BlueZ into a kernel mgmt **Add Device (auto-connect)** and the kernel
then NEVER issues an `LE Create Connection` -- despite the peripheral advertising. btmon (`--index 0`)
ground truth:

- hci0 scans fine (1000+ advertising reports, other devices discovered normally).
- The ESP32 IS advertising during the attempt (29 `AA:BB:CC:DD:EE:04` adverts seen, its serial
  reports `conn=0`).
- BlueZ: `Add Device (0x0033) Action: Auto-connect`, `LE Add Device To Accept List`, passive scan
  with "ignore not in accept list" -- then NO `Create Connection` for ~42s -> `Connect Failed` ->
  `le-connection-abort-by-local`.
- Reproduces across every variant: Pair-first, Connect-first, Trusted-before-connect vs not,
  discovery-on vs discovery-off, and an hci0 power-cycle. It is the kernel background auto-connect
  path, independent of the harness code.
- `Adapter1.ConnectDevice` (which would do a DIRECT connect, bypassing the accept-list path) is NOT
  available: bluetoothd Experimental is OFF.

**Fixes -- now WIRED INTO central.py (2026-07-10, bt-hid-tests-docs), pending a live run.** All three
are disruptive to run live (each risks the hci1/mini6 dongle daemon + other in-flight work), so the
code is in place + offline-tested but the on-glass verify is still a coordinated MORNING task:
- (a) `--connect-method connectdevice` (or `BT_HID_CONNECT_METHOD=connectdevice`) uses
  `Adapter1.ConnectDevice({Address, AddressType})` for a DIRECT LE Create Connection that bypasses the
  stalling accept-list path. It needs bluetoothd `Experimental = true`; central.py DETECTS Experimental
  by introspecting the adapter (`ConnectDevice` in the interface) and the default `--connect-method
  auto` picks it automatically when available, else falls back to `scan`. Requesting `connectdevice`
  explicitly with Experimental OFF fails loudly (skip with a clear reason) rather than silently
  stalling. Enabling Experimental needs a bluetoothd restart (`ExecStart=/usr/libexec/bluetooth/
  bluetoothd -E`, then `systemctl restart bluetooth`).
- (b) `--central hciN` (or `BT_HID_CENTRAL_ADAPTER=hciN`) pins a USB DONGLE (e.g. the RTL8761 `hci1`)
  as the CENTRAL instead of the Intel built-in -- the RTL may not exhibit the auto-connect stall. (The
  "hci0-only-as-central" guardrail is about not SERVING HID on hci0, so a dongle-as-central is fine.)
- (c) A clean host reboot to clear kernel BT state (then re-run under the resume service).

Pure policy (`central_core.resolve_connect_method` + `connectdevice_properties`) is unit-tested
offline (`python/tests/test_bt_hid_central_core.py`); the live verdict now records which
`connect_method` carried the link.

### 2. ESP32 not cleanly free
The ESP32 prints `EVT connected` from a central within ~5s of every boot, but NO ESP32 device object
exists on hci0 OR hci1 -- i.e. a THIRD central is grabbing it. Diagnosis: **mini5's STALE ESP32 bond
auto-reconnecting** (the abs-esp32 subagent is idle/done; no other active central). A BLE peripheral
serves one central, so mini5 holding the ESP32 fights the hci0 connect regardless of blocker #1.
Freeing it needs **mini5 to Forget the ESP32** (a the maintainer tap on the iPad). Also note: opening the
ESP32's USB serial RESETS the chip (expected for this backend; the transport's `_wait_ready` handles
it), so its BLE link state is fragile during testing.

## How to run the live verify once unblocked

```
# after mini5 forgets the ESP32, and with a working central (dongle, or Experimental+ConnectDevice):
BT_HID_CENTRAL=1 sudo -E /usr/bin/python3 -m pytest \
    python/tests/test_bt_hid_central_selftest.py -k esp32 -rs
# steer the connect method / central adapter via env (both optional; auto is the default):
BT_HID_CENTRAL=1 BT_HID_CONNECT_METHOD=connectdevice sudo -E /usr/bin/python3 -m pytest ...   # (a)
BT_HID_CENTRAL=1 BT_HID_CENTRAL_ADAPTER=hci1        sudo -E /usr/bin/python3 -m pytest ...   # (b)

# or directly, JSON verdict:
sudo /usr/bin/python3 python/src/hidbridge/central.py --backend esp32 --json
sudo /usr/bin/python3 python/src/hidbridge/central.py --backend esp32 --connect-method connectdevice --json
sudo /usr/bin/python3 python/src/hidbridge/central.py --backend esp32 --central hci1 --json   # dongle-as-central
```

Verified BEFORE the connect wall: hci0 discovers `wsb-hid-esp32` by name; the ESP32 abs firmware
boots ("OK wsb-hid-esp32 ready"). The report-map / report-ref / notification VERIFY steps could not
run because the connection never completes.

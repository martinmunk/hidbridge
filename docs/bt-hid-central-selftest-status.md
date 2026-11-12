# bt_hid BLE-central self-test -- status + live blocker (2026-07-10)

Branch: `bt-hid-selftest-central`. Goal: validate the MCU BT-HID peripherals (Pico `HIDBRIDGE-pico`,
ESP32 `HIDBRIDGE-esp32`) END TO END with NO iPad, using the wsb BUILT-IN Bluetooth adapter (hci0) as a
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
Freeing it needs **mini5 to Forget the ESP32** (a user tap on the iPad). Also note: opening the
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

Verified BEFORE the connect wall: hci0 discovers `HIDBRIDGE-esp32` by name; the ESP32 abs firmware
boots ("OK HIDBRIDGE-esp32 ready"). The report-map / report-ref / notification VERIFY steps could not
run because the connection never completes.

## Update 2026-07-14 -- `net` backend added + a full PASS captured (UB500 central)

The self-test now parametrizes over a `net` backend too: same host-radio-as-central harness, but the
injected frame is driven to the board **over TCP** (the network backend) instead of USB serial -- the
end-to-end proof of the network HID path with **no iPad**.

**A full green verdict was captured** on this host using the TP-Link **UB500 (hci1)** as the central
(`--central hci1 --connect-method connectdevice`, bluetoothd Experimental on): connect -> bond ->
byte-exact report map (all 4 reports) -> report refs -> notify armed -> inject over TCP -> the
injected keyboard `0000040000000000` ('a') and mouse `00030000` (dx=3) arrived **byte-exact as GATT
notifications**. So `hidbridge --backend net` -> board -> BLE HID delivery is verified end to end.

**Still flaky on THIS host, both adapters.** The built-in hci0 stall (above) is not adapter-specific:
the UB500 shows the same `org.bluez.Error.InProgress` on the scan/Connect path, and even via
ConnectDevice the ACL frequently **drops right after StartNotify** -- the board then reports `OK
noconn` at inject time (no central to notify). Observed per-attempt success is low here, so the pass
above took several tries. This is a host BlueZ/kernel defect, not the network code (the board reaches
`c0=1`; the notify path is the same firmware the serial esp32 test uses).

Harness robustness added for this (helps everywhere, masks nothing):
- **bond BEFORE discovery** (`_ensure_bonded`): the MCU needs an encrypted link to expose its HID
  service, and on this stack the on-demand-at-StartNotify bond was too late; Pair() runs SMP over the
  already-established link (no fresh Create Connection, so no accept-list stall).
- **adopt an established link** if Connect/ConnectDevice reports failure while the device is actually
  Connected.
- **transient-only retry** (`is_retryable_failure` + `BT_HID_CENTRAL_ATTEMPTS`, default 8): retries a
  flaky connect / a link that died before delivery, but NEVER a real report-map or notification-byte
  mismatch (those are returned immediately as a genuine failure).

Net result: reliable on a healthy BLE-central host; on this dev box it needs the ConnectDevice escape
plus retries, and BlueZ may still drop the link before delivery on any given attempt.

## Update 2026-07-14 (2) -- HCI-level root cause: it was NOT a "post-StartNotify link drop"

Dug into the apparent "link drops right after StartNotify" with btmon + a pairing-agent trace. The
earlier characterisation was largely WRONG; the real causes:

1. **TWO boards advertise the same name (dominant cause).** Both ESP32s on the bench advertise
   `HIDBRIDGE-esp32` -- the CP2102 board (`AA:BB:CC:DD:EE:04`) and the Ethernet board
   (`AA:BB:CC:DD:EE:03`). The self-test matched the scan by NAME, so on ~half the attempts the host
   central connected + subscribed to the WRONG board, while the `net` inject always went over TCP to
   the Ethernet board -> that board had no central -> `OK noconn`, the verdict failed, and the test's
   own TEARDOWN disconnected the (wrong) board. That teardown is the "disconnect right after
   StartNotify" I first saw -- not a spontaneous BlueZ drop. **Fix:** pin the central to the DRIVEN
   board's BLE address, not the shared name (`central._board_ble_address` + `_matches`; the transport
   reports its slot-0 address). This also matters for the plain `esp32` backend whenever two boards
   run the same firmware.

2. **Flaky LE Secure-Connections Just-Works PAIRING (secondary).** When the central does reach the
   right board, pairing sometimes stalls: btmon shows SMP reaching the `User Confirmation Request`
   (confirm hint 0x01 = auto-accept), then bluetoothd issues `Disconnect` with reason **0x16
   (Connection Terminated By Local Host)** ~0.85 s later, and the kernel auto-connect retries in a
   ~3 s loop. A pairing-agent trace showed bluetoothd calls the agent's `RequestAuthorization` (HID
   service auth -- we accept it) but NEVER `RequestConfirmation`, and the journal logs
   `device_confirm_passkey: Operation not permitted`. So the confirmation is never completed and the
   half-paired link is torn down. This is a pairing-layer issue on this host's BlueZ 5.82 / kernel
   6.12 (aggravated by asymmetric bond state from heavy re-pairing, and possibly ESP32 SC-ECDH timing
   under the Ethernet board's concurrent network load) -- NOT an idle-link or notify policy.

**Conclusion:** there is no "notify-then-drop" behaviour to fix. When the central is on the RIGHT
board AND pairing completes, the link holds and delivery works (the byte-exact PASS above). The
remedies are correct board-address targeting (fixed) + the existing transient-retry for flaky
pairing. Registering the pairing agent as the DEFAULT agent (RequestDefaultAgent) is load-bearing:
the kernel auto-connect path routes the pairing confirmation to the default agent, and its outcome is
now recorded in the verdict (`steps.agent`).

## Update 2026-07-17 -- both drive modes (rpc rpc + raw frames) now exercised

The central self-test gained a `--mode {frames,rpc}` dimension (env `BT_HID_MODE`, default `frames`),
so the SAME host-radio-as-central proof runs for BOTH ways of driving the board (docs/bt-hid-modes.md)
and asserts the identical GATT notification bytes arrive either way:

- **mode=frames** (raw, the original): the host builds the exact report bytes and the transport
  relays them (`transport.send`). Unchanged.
- **mode=rpc** (rpc, "smart firmware"): the host sends a JSON-RPC `frames` call over the
  transport's **already-open** channel (`rpcClient(transport._ser)`) that relays the SAME pre-built
  report bytes; the board's firmware notifies them. Reusing the open channel is deliberate -- a second
  serial connection would reset the board / fight the first. The verdict records `result["mode"]`.
  rpc needs JSON-RPC firmware, so it applies to **esp32 / pico / net** only; the **dongle** backend (a
  local BlueZ daemon with no JSON-RPC) self-SKIPs `mode=rpc` with a clear reason.

The pure `frames`-string encoding (`central_core.frames_method_hex` -- `<reportID hex><body hex>`, the
shape a rpc dry run returns) is unit-tested offline (`test_bt_hid_central_core.py`,
`test_bt_hid_rpc.py`), so the frames<->rpc byte-equivalence is pinned with no board.

### How to run BOTH modes live (once a working central is available)

The pytest wrapper parametrizes over `(backend, mode)`; select a cell with `-k`:

```
# raw (frames) over the ESP32 -- the original path:
BT_HID_CENTRAL=1 BT_HID_CENTRAL_ADAPTER=hci1 BT_HID_CONNECT_METHOD=connectdevice \
    sudo -E python -m pytest tests/test_bt_hid_central_selftest.py -k "esp32 and frames" -rs
# rpc (rpc) over the ESP32 -- the board builds/relays the reports via JSON-RPC:
BT_HID_CENTRAL=1 BT_HID_MODE=rpc BT_HID_CENTRAL_ADAPTER=hci1 BT_HID_CONNECT_METHOD=connectdevice \
    sudo -E python -m pytest tests/test_bt_hid_central_selftest.py -k "esp32 and rpc" -rs

# or directly, JSON verdict (note --mode):
sudo /usr/bin/python3 -m hidbridge.central --backend esp32 --mode rpc \
    --central hci1 --connect-method connectdevice --json
```

**Honesty note:** live-green for either mode still needs a WORKING BLE central on this host, which it
does not reliably have (the hci0 auto-connect stall + flaky Just-Works pairing above; a full PASS was
only captured on mode=frames via the UB500 + ConnectDevice after several retries). The mode=rpc path
here is **code + offline-tested, NOT yet verified live** -- the on-glass both-modes run is deferred to
a host with a healthy central, exactly like the original single-mode live verify.

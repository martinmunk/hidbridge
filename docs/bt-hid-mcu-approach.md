# MCU HID backend -- robust, first-class (ESP32 / Pico with their own BLE stack)

Status: 2026-07-10. Branch `bt-hid-mcu-robust` (REVIEW DRAFT, do NOT merge yet). All findings below
are verified LIVE on wsb hardware unless marked otherwise.

The MCU boards run their OWN BLE stack (ESP32 = NimBLE HOG, Pico 2 W = BTstack HOG) and are driven
over USB serial, bypassing the host's BlueZ entirely. That makes them a valuable alternative HID
backend when BlueZ is flaky. This doc covers the robustness work that makes them first-class
alongside the dongle: the no-reset serial open, the connect-on-demand lifecycle, the
ESP32-Ethernet-Kit (FT2232H), the iOS-reconnect findings, and how the MCU backends plug behind the
shared `HidSession`.

## The core problem it fixes

Opening the ESP32's USB-serial bridge REBOOTED the chip, dropping its BLE link to the iPad -- so
every command we sent could kill the connection (one iPad "stayed conn=0 after our port-opens").
Root cause (verified): the Linux tty layer ASSERTS DTR+RTS when it activates a port on `open(2)`
(kernel `tty_port_raise_dtr_rts()`, gated on `C_BAUD(tty) != 0`). On an ESP32 dev board RTS is
wired to EN (reset) and DTR to GPIO0 (boot), so that assert pulses EN and reboots the chip.
pyserial's documented `dtr=False; rts=False` before `open()` does NOT help -- the kernel raise
happens INSIDE `os.open()`, before pyserial can clear the lines. (Live proof: the ESP32 ROM boot
log + `HIDBRIDGE-esp32 ready` banner reprints on every such open of /dev/ttyUSB1.)

## The no-reset open (task 1) -- `python/src/hidbridge/serial_link.py`

`RawSerialLink` opens the port at the raw fd level and never resets the board:

1. Leave the tty at baud **B0** on close. The next open then sees `C_BAUD(tty) == 0`, so the kernel
   SKIPS `tty_port_raise_dtr_rts()` -- DTR/RTS are never asserted -> RTS never pulses EN -> no
   reset. We open with `os.open()` (so pyserial's own baud reconfigure cannot re-trigger the raise),
   then raise the REAL baud (B115200) AFTER the tty is already active (post-activation baud changes
   do not re-raise the modem lines).
2. Clear DTR+RTS **atomically** in ONE `TIOCMBIC` ioctl (never one line at a time).

Verified live: 3+ consecutive opens of the CP2102 with NO boot banner and the bonded iPad's
`conn=1` held throughout. Hardware-gated regression: `python/tests/test_bt_hid_esp32_noreset.py`
(`BT_HID_HW_ESP32=1`).

CAVEAT: a truly COLD enumeration (board just physically plugged in) still resets ONCE -- the default
termios baud is nonzero and nothing has left B0 yet. The lifecycle opens once and HOLDS the port for
the whole session, always leaving B0 on close, so within a session and across our own sessions there
are ZERO further resets; only a cold plug-in costs the one unavoidable reset. `serial_link.prime_b0(port)`
can be run once (e.g. session start / udev hook) to pre-leave B0 so even the first open is clean.

### Two boards reset for OPPOSITE reasons -- one open path is safe for both

| Board | Bridge | Resets because... | Why RawSerialLink is safe |
|-------|--------|-------------------|---------------------------|
| ESP32 DevKit | CP2102 (10c4) | the DTR/RTS **raise** on open | B0 trick suppresses the raise |
| ESP32-Ethernet-Kit | FT2232H (0403) | a DTR/RTS **mismatch transient** (default open, lines left asserted, does NOT reset) | we clear both lines ATOMICALLY, so they never mismatch |

The FT2232H is the counter-intuitive one: a plain default open (dtr/rts left asserted) does NOT
reset it, but pyserial's `dtr=False; rts=False` DOES -- pyserial applies the two lines in separate
ioctls, so one drops before the other and the momentary mismatch pulses EN. Clearing both together
(as `RawSerialLink` does) avoids it. Verified: 4 reset-free FT2232H cycles, firmware alive.

## Connect-on-demand lifecycle (tasks 2 + 5)

The robust default is a PERSISTENT single open per session + a reconnect wait:

- `Esp32Transport` opens ONCE (no-reset) and holds the port; `wait_connected(timeout)` polls the
  board's `P` ping until the bonded iOS device reports `conn=1` before any HID is sent, so reports
  are never dropped on a down link. On close it leaves the tty B0 (reset-free next open).
- Firmware `X` command (added here): disconnect the current central but KEEP the bond, then resume
  advertising -- the connect-on-demand teardown so another bonded device can take the single HID
  link. `Esp32Transport.disconnect()` sends it. (GATT/report map unchanged, so existing bonds
  survive the reflash that adds `X`.)

### Shared, backend-agnostic API -- `HidSession` (one context manager, two backends)

The MCU boards plug in BEHIND the same `HidSession(target, ios_device)` the BlueZ dongle uses
(`python/src/hidbridge/conn.py`, merged from branch `bt-hid-multi-device`). Target grammar selects the
backend: `esp32:/dev/ttyUSB1`, `pico:/dev/ttyACM0`, or `hciN`/MAC for the dongle.

```python
from bt_hid.conn import HidSession
with HidSession("esp32:/dev/ttyUSB1", "My iPad") as hid:
    hid.type_text("hello"); hid.send_key("enter")
# exit -> firmware 'X' disconnect (bond kept) + serial close (tty left B0, reset-free next open)
```

The MCU backends are `bt_hid.link_esp32` / `bt_hid.link_pico`, each exposing
`make_link_backend(arg, **opts) -> LinkBackend` with `prepare / connect / disconnect /
is_connected / transport / capabilities / describe` (the `LinkBackend` contract in conn.py).
`conn.make_link_backend("esp32:...")` auto-loads them. Verified live end-to-end on the CP2102:
`HidSession("esp32:/dev/ttyUSB1","mini6")` -> connect (conn=1) -> `type_text`/`send_key` frames
NOTIFIED (ok, not `noconn`) -> clean disconnect+close. (Visual landing on the iPad screen is the
iPad-gated check, coordinated via 'main'.)

## Distinct BLE identity per iPad -- per-bond HOST SLOTS (IMPLEMENTED 2026-07-14)

The BlueZ verdict (docs/bt-hid-multi-device.md) is that a single adapter has ONE identity, so a
distinct identity per iPad is NOT feasible on the dongle. On an MCU we own the firmware, so the
board CAN advertise a distinct BLE address per bonded iOS device. The MCU backends report
`capabilities["per_device_identity"] = True` (the dongle reports `False`), AND a new
`capabilities["simultaneous_identities"]` (ESP32 `True`, Pico `False`, dongle `False`) that says
whether the board can hold >1 identity CONNECTED at the same time.

### The model: host slots (like a multi-host keyboard)

Each board holds a small table of BLE identities ("host slots"). **Slot 0 = the board's NATIVE
identity** (its efuse/OTP public address) so an EXISTING bond survives a reflash unchanged. **Slots
>=1 = deterministic STATIC-RANDOM addresses** derived from the public address (two MSBits of the
MSB forced to `0b11` = the BT static-random signature, slot index XORed into the LSB, so each slot
differs from public + from every other slot). Each slot advertises a TARGET-scoped BLE name
(`wsb-<board>-<ipad>`, e.g. `HIDBRIDGE-esp32-mini5`) so an iPad's Bluetooth settings show which entry is
meant for it (`python/src/hidbridge/naming.py:slot_identity_name`).

Serial protocol (both boards -- `transport_esp32.py` / `transport_pico.py`, offline-tested):

| cmd | meaning | reply |
|-----|---------|-------|
| `I<n>[:name]` | select/create slot n as the advertising identity (+ set its name) + advertise | `OK slot=<n> addr=<12hex> name=<name> conn=<0\|1>` |
| `T<n>` | route subsequent K/M/A/D reports to slot n's connection (send target) | `OK tgt=<n>` |
| `S` | slot table + per-slot state | `OK slots=<N> adv=<n> tgt=<n> multi=<0\|1> c0=.. s0=..` |

### Per-stack mechanism (cited APIs) + what each board can do

- **ESP32 / NimBLE-Arduino 2.x -- TRUE simultaneous per-bond identity.** `NimBLEDevice::setOwnAddr()`
  (-> `ble_hs_id_set_rnd`) sets the per-slot static-random address; `setOwnAddrType(PUBLIC/RANDOM)`
  picks native vs. slot id. NimBLE holds MULTIPLE centrals at once (`CONFIG_BT_NIMBLE_MAX_CONNECTIONS`
  = 3, pinned in `platformio.ini`; `static_assert >= NUM_SLOTS`) and `NimBLECharacteristic::notify(connHandle)`
  drives a SPECIFIC central, so the firmware records each connection against the slot it was
  advertising (`slotConn[]`) and `T<n>` picks which iPad the reports hit. The classic ESP32 (BT4.2)
  has **NO extended advertising** (confirmed: the ESP32 controller lacks BLE-5 ADV extensions), so
  only ONE identity ADVERTISES at a time -- a second/reconnecting central is served by selecting its
  slot and advertising -- but once connected, connections are held **simultaneously**. => ONE ESP32
  holds mini6 (slot 0) AND mini5 (slot 1) connected at the same time, no time-slice.
- **Pico 2 W / BTstack -- per-slot identity, but time-sliced.** `gap_random_address_set()` /
  `gap_random_address_set_mode(STATIC/OFF)` set the per-slot address; per-slot adv name via
  `gap_advertisements_set_data()`. BTstack the STACK supports extended advertising + multiple
  connections, BUT the arduino-pico `PicoBluetoothBLEHID` wrapper tracks a SINGLE `_con_handle`
  (`libraries/HID_Bluetooth/src/PicoBluetoothBLEHID.h`), so the Pico presents distinct identities
  **sequentially** (deterministic slot selection: pick a slot -> that iPad's identity advertises ->
  it connects), not simultaneously. It reports `multi=0`. Simultaneous hold on the Pico needs a
  wrapper bypass (drive `hids_device` per-connection directly) -- a tracked follow-up.

### Re-pairing after the flash (IMPORTANT)

Flashing this firmware CHANGES what identity each iPad sees, so bonds may need re-establishing:

- **ESP32:** slot 0 keeps the board's public address, so a bond made on the OLD single-identity
  firmware to the public address would in principle survive as slot 0. In practice the ESP32 had no
  usable mini5 bond (mini6 won the single slot in ~400ms), so the flash step RE-PAIRS both: mini6 on
  slot 0 (`HIDBRIDGE-esp32-mini6`) and mini5 on slot 1 (`HIDBRIDGE-esp32-mini5`, a static-random address = a
  NEW device to iOS, so a fresh pair).
- **Pico:** slot 0 keeps the CYW43439 public address, so the mini5 bond made NOW on the current
  `HIDBRIDGE-pico` firmware is PRESERVED as slot 0 -- no Pico re-pair required. A second iPad on Pico
  slot 1 (`HIDBRIDGE-pico-mini6`) is a fresh pair when/if wanted.

The live acceptance test is `python/tests/test_bt_hid_dual_identity_live.py` (`BT_HID_LIVE_DUAL=1`):
it brings slot 0 up (mini6 connects), slot 1 up (mini5 connects WITHOUT dropping slot 0), asserts
BOTH connected simultaneously per `S`, and drives each via `T<n>`.

### Live bring-up findings (2026-07-14, on the wsb rig)

Verified/learned flashing both boards + pairing mini5/mini6:

- **Bonds survive a firmware reflash.** slot 0 = the board's public address, so the existing bond
  reconnects unchanged. mini6 auto-reconnected to the ESP32 (slot 0) within seconds after every
  `pio run -t upload` (NimBLE bonds live in NVS, untouched by an app-partition flash; the Pico's
  BTstack TLV bond store likewise survives a UF2 flash). So slot-0 iPads need NO re-pair; only a
  NEW slot (>=1, a fresh static-random identity) is a fresh pair.
- **Two bugs found + fixed by btmon sniff** (never trust the firmware's own reply -- sniff the air):
  (1) the static-random address was built in the wrong byte order (`NimBLEAddress(uint8_t[6])`
  reverse-copies its input) so slot 1 came out `7B..` (top bits 01, invalid) instead of `E4..`;
  (2) the per-slot NAME did not update -- `NimBLEAdvertising`'s auto-build accumulated service UUIDs
  and kept the name in a stale scan response, so slot 1's address advertised slot 0's NAME. Fix:
  build the adv payload explicitly (`NimBLEAdvertisementData` + `clearData()`, name in the 31-byte
  main packet, no scan response). Regression: `tests/test_bt_hid_advert_name_live.py` sniffs the
  ACTUAL advertised name per slot.
- **Cross-board contention is real -> `X`/`O` park.** With an iPad bonded to BOTH boards and both
  advertising, iOS attaches it to whichever board it likes (mini6 jumped from the ESP32 to the Pico).
  To make "board A serves this iPad now" DETERMINISTIC, the firmware gained `X` (disconnect current
  central, keep bond, + park) and `O` (park: stop advertising, keep connections/bonds). Test flow:
  PARK the board you are not testing, so the iPad deterministically attaches to the board under test.
- **A simultaneous double-reflash drops both iPads at once**; they re-attach only when awake +
  scanning (a locked-but-unlocked iPad reconnects; a slept/BFU one needs a wake). Reflash boards ONE
  at a time and keep the iPads awake when re-demonstrating.

### Registry (targets.py)

Each `(board, iPad)` route carries a `slot`. Defaults: mini6 -> esp32 slot 0; mini5 -> pico slot 0
(native, bond preserved); `mini5-esp32` -> esp32 slot 1 (lets the ESP32 hold both); `mini6-pico` ->
pico slot 1. `TargetRegistry.simultaneous_hold_plan(target)` lists the distinct-slot routes on one
board and `can_hold_simultaneously(target)` is True only for the ESP32 (capability + >=2 distinct
bonds/slots).

## ESP32-Ethernet-Kit (FT2232H) notes (task 3)

- USB `0403:6010`, DUAL channel: `/dev/ttyUSB2` = interface 00 = channel A = **JTAG**;
  `/dev/ttyUSB3` = interface 01 = channel B = **the ESP32 UART**. Autodetect prefers the higher
  interface number for FTDI so it lands on the UART, and prefers a CP2102 over an FTDI when both are
  present (override with `BT_HID_ESP32_PORT`). Vendor-gated like everything else.
- esptool auto-reset into the bootloader WORKS via the FT2232H -- no BOOT button needed (it detected
  the chip, uploaded the stub, and flashed).
- The chip is a classic **ESP32-D0WD-V3, 4MB flash, NO PSRAM** (esptool report), NOT a WROVER -- so
  the SAME PlatformIO env `esp32dev` and the SAME `src/main.cpp` build + run unchanged. No
  board-specific change was needed. (If a future Ethernet-Kit unit is WROVER-based, add an
  `esp32-wrover-kit` env; our firmware does not use PSRAM so it is env-agnostic in practice.)
- Verified: our firmware boots, advertises as `HIDBRIDGE-esp32`, and answers the full serial protocol
  (`P`/`V`/`A`/`K`/`M`/abs/`D`/`X`) on the Ethernet-Kit. iPad-gated keystroke-lands test is pending
  a pairing (coordinate via 'main'); note both boards advertise the SAME name, so pair only one at a
  time until the per-device-identity firmware lands.

## Why an iPad sometimes does not reconnect after a reset (task 4)

Findings from the live reflash/disconnect cycles:

- mini6 (awake, unlocked) reconnects to the bonded peripheral within ~1 s of a reset OR an `X`
  disconnect -- consistently. The bond survives a firmware reflash (GATT/report map unchanged).
- The "one iPad stayed conn=0" symptom is DEVICE-STATE dependent, not a protocol bug. The most
  likely cause is iOS **Before-First-Unlock (BFU)**: a just-rebooted iPad does not auto-reconnect
  BLE HID peripherals until its FIRST unlock (the keybag/BLE bond material is sealed until then). A
  locked-but-already-unlocked-once device reconnects; a cold-booted, never-unlocked device does not.
- MITIGATION, in priority order:
  1. The no-reset open (above) means we rarely reset the board at all now, so the iPad rarely has to
     reconnect in the first place -- this removes most of the exposure.
  2. When a reset is unavoidable (cold plug-in), `wait_connected(timeout)` polls for the reconnect
     before sending HID, so we self-heal instead of firing reports into a down link.
  3. If the iPad is in BFU it must be unlocked once by hand -- there is no peripheral-side fix (the
     device will not reconnect until unlocked). Surface this to the operator rather than hanging.

## Readiness gate -- `__enter__` returns only when the link is READY TO USE (task: hidsession-ready)

"Connected" is NOT "ready". After a (re)connect, iOS spends ~1-3s reading the HID report map +
writing the report CCCDs (StartNotify) before it ACTS on input notifications. A keystroke fired in
that window is silently dropped -- so live-driving mini6 via the ESP32, keystrokes sent right after
`with HidSession(...) as hid:` were LOST, and callers papered over it with a manual `time.sleep(3.3)`.

`HidSession.__enter__` now BLOCKS until the link is genuinely ready, preferring a REAL per-backend
subscribe signal over a fixed sleep, then builds the controller. Tunables:

```python
HidSession(target, ios_device,
           ready_timeout=8.0,   # bounded wait for the REAL "iOS subscribed" signal
           settle=None,         # None=auto (0 once the signal fires, else backend READY_SETTLE
                                #   fallback); a number forces exactly that many seconds
           require_ready=False) # True => raise if a subscribe-capable backend never subscribes
```

`hid.ready_state` reports how readiness resolved: `"ready"` (real signal seen) or `"settle"` (fell
back to a timed delay). The gate is a small `LinkBackend` contract -- `readiness_supported()`,
`is_ready()`, `wait_ready(timeout)`, and a `READY_SETTLE` fallback -- so it is backend-agnostic and
offline-testable (`python/tests/test_bt_hid_readiness.py`, `test_bt_hid_ready_signal.py`).

The REAL readiness signal per backend:

- **ESP32 (NimBLE):** a `NimBLECharacteristicCallbacks::onSubscribe` on the KEYBOARD input report
  tracks when iOS writes its CCCD; the firmware exposes it as a new `subscribed=<0|1>` field in the
  `P` ping reply (`OK HIDBRIDGE-esp32 subscribed=<0|1> conn=<0|1>`) and an async `EVT ready` /
  `EVT unsubscribed`. `transport_esp32.wait_ready()` polls it; `link_esp32` reports
  `readiness_supported=True`. BACKWARD COMPATIBLE: the GATT / report map is UNCHANGED (existing iOS
  bonds survive a reflash -- only a serial field + a CCCD callback were added), and `subscribed=` is
  placed BEFORE `conn=` so any legacy `endswith("conn=1")` parser still holds; an old firmware that
  omits the field parses as "unknown" -> host uses the settle fallback.
- **Dongle (BlueZ):** the `ble_hid` daemon already logs `StartNotify` and flips each report char's
  `notifying`. A new `status` control command reports `{"subscribed": <keyboard char notifying>,
  "chars": {...}}`; `DongleLinkBackend` polls it via `SocketTransport.status()`. This is the real
  "iOS enabled the reports" signal (without it, `send_report` DROPS -- see `ble_hid.py`).
- **Pico (BTstack HOG):** the arduino-pico `PicoBluetoothBLEHID` wrapper does not currently expose a
  CCCD/subscribe callback to user code, so the Pico firmware does not yet emit `subscribed=`. The
  Pico backend therefore uses the `READY_SETTLE = 3.3s` fallback for now, but the host side already
  PARSES `subscribed=` if present (`link_pico._pong_subscribed`), so a future firmware that adds the
  field lights up the real path with NO host change. FOLLOW-UP: hook the BTstack `hids_device`
  input-report-enable event (needs the board + arduino-pico API to verify).

### Async: `AsyncHidSession` (verdict = yes, thin + honest)

The expensive part of a session is the connect + readiness WAIT (poll BlueZ `Connected` / poll a
serial `subscribed=` ack / a settle sleep) -- all I/O-bound. The underlying libs (dbus-python,
pyserial) are BLOCKING C bindings with no native-async API, so an async version cannot make the I/O
itself async; `AsyncHidSession` instead offloads `__enter__`/`__exit__` to a worker thread via
`asyncio.to_thread`. That is exactly enough VALUE to justify it: multiple iPads can be brought up
CONCURRENTLY (overlap the readiness waits with `asyncio.gather` / a multi-`async with`) instead of
serializing them -- directly useful for the multi-device work. Report calls stay synchronous
(sub-ms socket/serial writes; a thread hop per keystroke would add latency for no gain). If a future
backend grows a genuinely non-blocking transport, `AsyncHidSession` is where an await-per-report path
slots in. Offline-tested (`test_bt_hid_readiness.py::test_async_*`).

## Guardrails (unchanged, enforced)

NEVER open `/dev/ttyUSB0` (CH340 KVM, vendor 1a86 -- would drive the KVM). All serial is
vendor-gated: CP2102 = 10c4 (ttyUSB1), FT2232H = 0403 (ttyUSB2/3, UART = channel B/ttyUSB3),
Pico = 2e8a (ttyACM0). `transport_esp32.detect_port` hard-excludes 1a86 and is regression-pinned
(`test_bt_hid_esp32_portsafety.py` + the FTDI cases in `test_bt_hid_esp32_noreset.py`).

## Files

- `python/src/hidbridge/serial_link.py` -- the no-reset raw serial link (`RawSerialLink`, `prime_b0`,
  `open_serial`).
- `python/src/hidbridge/transport_esp32.py` -- no-reset open + `wait_connected` + `disconnect` + FTDI
  detection.
- `python/src/hidbridge/link_esp32.py`, `link_pico.py` -- MCU `LinkBackend`s behind `HidSession`
  (+ the readiness contract: `readiness_supported`/`is_ready`/`wait_ready`/`READY_SETTLE`).
- `python/src/hidbridge/conn.py` -- `HidSession` readiness gate (`ready_timeout`/`settle`/`require_ready`,
  `ready_state`) + `AsyncHidSession`.
- `python/src/hidbridge/controller.py` -- `SocketTransport.status()`/`request()` (dongle readiness query).
- `python/src/hidbridge/ble_hid.py` -- daemon `status` control command (report-char subscription state).
- `firmware/esp32/src/main.cpp` -- adds the `X` disconnect command (bond kept) + the
  `onSubscribe` readiness tracking (`subscribed=` ping field, `EVT ready`).
- `python/tests/test_bt_hid_esp32_noreset.py` -- offline FTDI/detection + HW-gated no-reset
  regression (`BT_HID_HW_ESP32=1`, `BT_HID_HW_ESP32_FTDI=1`).
- `python/tests/test_bt_hid_link_mcu.py` -- MCU backends behind HidSession (offline).
- `python/tests/test_bt_hid_readiness.py` -- the readiness gate + async, offline (FakeClock).
- `python/tests/test_bt_hid_ready_signal.py` -- per-backend real-signal parse (offline).
- `python/tests/test_bt_hid_readiness_live.py` -- HW-gated: a keystroke sent IMMEDIATELY after
  `__enter__` lands with no manual sleep (`BT_HID_HW_ESP32_LIVE=1` + `BT_HID_IPAD=`; coordinate the
  iPad via 'main').

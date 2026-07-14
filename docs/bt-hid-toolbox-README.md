# bt_hid -- Linux Bluetooth-HID peripheral (keyboard + mouse to an iPad/iPhone)

Emulate a **standard Bluetooth keyboard + mouse** from this Linux host to an iPad/iPhone over a USB
Bluetooth **dongle**, so we can programmatically:

- **wake** a device,
- **unlock** it (type the passcode at the lock screen),
- **type** text (Apple-ID / Trust passwords),
- **move + click** a pointer (tap "Trust", drive Settings),
- and, on an unlocked device, drive a **software reboot** (Settings > General > Shut Down, or
  AssistiveTouch) to clear wedges.

This removes the "the maintainer must physically poke the iPad" dependency.

> **Connecting an iPad/iPhone + the required iOS setup** (pairing per backend, Full Keyboard Access,
> AssistiveTouch + Pointer Devices, Mouse Keys, the "Restart" Shortcut, the no-touch reboot recipe):
> see the device-side manual **[`docs/bt-hid-ipad-connect-manual.md`](../../docs/bt-hid-ipad-connect-manual.md)**.

This is a **standard USB-HID** device (boot-style keyboard reportID 1 + mouse reportID 2), distinct
from Apple's private UniversalHID (`continuity/universalcontrol/`). The iPad talks standard HID to a
Bluetooth peripheral.

## Status (2026-07-10): WORKING END-TO-END, webcam-verified on mini6

Linux -> USB Bluetooth dongle -> BLE HID -> iPad, **keyboard + pointer both register**, Mac-free.
Confirmed live on mini6 (iPadOS, webcam-verified): the emulated keyboard is discoverable, mini6 pairs
(authenticated LE Secure Connections bond), the bond holds + auto-reconnects, iOS subscribes to the
HID report characteristics (`StartNotify 2a4d` on both keyboard + mouse chars), reports deliver
(`notify 2a4d`, zero dropped), and input registers:
- **Keyboard:** `Cmd+Space` opened Spotlight (modifier works) and typing **"hello bt hid" appeared in
  the Spotlight search field** on mini6 (alphanumeric + space work).
- **Pointer:** mouse move visibly moved the iPad cursor + click navigated the UI.

(The report layer is independently corroborated: the same `reports.py` keyboard descriptor drives an
ESP32 BLE-HID backend that types into iPad Spotlight. During bring-up the two iPads on the bench were
briefly mis-identified; the clean proof above is on the confirmed mini6.)

Getting here surfaced three real bugs, each fixed (see the commits + "Bond persistence"):
1. **Bond dropped after ~1s** -> dongle-scoped **auto-trust watcher** (mark Trusted on pair/connect).
2. **iOS "Insufficient Authentication (0x05)" on the HID reads** -> **DisplayYesNo authenticated
   pairing** (LE Secure Connections numeric comparison, agent auto-confirms).
3. **THE final blocker: iOS never subscribed to the HID report chars** (`send_report DROPPED`). Root
   cause found via btmon: the GATT Descriptor's `ReadValue` was on the wrong D-Bus interface
   (`GattCharacteristic1` instead of `GattDescriptor1`), so BlueZ returned ATT **Unlikely Error
   (0x0e)** when iOS read the HID **Report Reference** descriptor. Without it iOS could not learn each
   report's ID/type and refused to enable the reports. Fixed by putting `ReadValue`/`GetAll` on
   `org.bluez.GattDescriptor1`; verified by a local GATT client reading the Report References as
   `[1,1]`/`[2,1]`, then confirmed live.

Note: HID chars ended up on **plain** read/notify (not `encrypt-*`) -- the authenticated bond already
protects them at the link level, and `encrypt-notify` was blocking the iOS subscribe.

---

## Multiple dongles / multiple iPads

Run `bt_hid preflight` first: it audits every adapter and WARNS on the same-BD-address trap (the two
TP-Link UB500 dongles share `E8:48:B8:C8:20:00` -- a UB500 V1 defect that corrupts BlueZ routing; run
only one at a time). Each `ble_hid` daemon is now PER-ADAPTER (own control socket + D-Bus objects +
`wsb-hid-<hciN>` name), so `bt-hid@hci1` and `bt-hid@hci2` run side by side to two iPads. Target a
dongle by `hciN` / MAC / USB-path / product. Connect-on-demand for one dongle + several iPads:

```python
from bt_hid.conn import HidSession
with HidSession("hci1", "My iPad") as hid:   # connect on enter, disconnect on exit (bond kept)
    hid.unlock("1234"); hid.type_text("hi"); hid.send_key("enter")   # no manual sleep needed
```

`HidSession(target, ios_device)` is backend-agnostic (`"hci1"`, `"dongle:hci1"`, `"pico:/dev/ttyACM0"`,
...). Full design + the per-iPad-BLE-identity feasibility verdict: **`docs/bt-hid-multi-device.md`**.

**Readiness:** `__enter__` returns only when the link is genuinely READY TO USE (iOS has subscribed
to the HID reports), not merely "connected" -- so a keystroke fired immediately after the `with`
lands instead of being dropped in the ~1-3s post-reconnect activation window. It prefers each
backend's real subscribe signal (dongle: the daemon's `StartNotify`; ESP32: a firmware `subscribed=`
ack) over a fixed sleep. Tunables: `HidSession(..., ready_timeout=8.0, settle=None,
require_ready=False)`; `hid.ready_state` is `"ready"` (real signal) or `"settle"` (fallback). An
`AsyncHidSession` (`async with`) offloads the connect/readiness waits to a thread so several iPads
can be brought up concurrently. Design: **`docs/bt-hid-mcu-approach.md`** (Readiness gate).

## HID backends: minimize contributor hardware (dongle OR ESP32 OR Pico)

The report layer (`reports.py`) is the single source of truth for the HID bytes; a pluggable
**backend** turns those bytes into a real BLE HID peripheral. Pick whichever hardware you have:

| `--backend` | hardware | how it presents | advertises as |
|-------------|----------|-----------------|---------------|
| `dongle` (default) | a USB Bluetooth dongle + this host's BlueZ | `ble_hid` daemon (HOG) | `wsb-hid-<hciN>` |
| `esp32`     | any **ESP32** dev board (~$5) on USB | ESP32 NimBLE HOG firmware, driven over USB serial | `wsb-hid-esp32` |
| `pico`      | a **Raspberry Pi Pico 2 W** (~$6) on USB | Pico BTstack HOG firmware, driven over USB serial | `wsb-hid-pico` |

The Pico's BTstack HID-over-GATT stack is iOS-compatible out of the box (it handles the
CCCD / Report-Reference subscription the BlueZ path can get stuck on) and needs no dbus/daemon.
Pin its serial device with `--pico-port /dev/ttyACMx` / `BT_HID_PICO_PORT`; autodetect is
Pico-vendor-gated (USB vendor `2e8a`) so it never grabs an unrelated serial adapter. Firmware +
flashing: [`firmware/pico/README.md`](firmware/pico/README.md).

Select a backend with `--backend` (or `BT_HID_BACKEND`). Report commands
(`wake/unlock/type/key/move/click`) build the exact same frames from `reports.py` and route to the
chosen backend, so behaviour is identical across backends; only device management
(`adapters/devices/connect`) is dongle-specific.

### ESP32 backend (`--backend esp32`)

**Why:** an ESP32 is a cheap, common board, so a contributor who does not own a USB BT dongle can
still drive an iPad/iPhone/Mac. It is also more *reliable* on iOS: the dongle/BlueZ path currently
fights iOS over subscribing to the HID report characteristics (a CCCD / Report-Reference GATT
quirk). The ESP32's **NimBLE-Arduino** HID-over-GATT stack implements HOG correctly and
iOS-compatibly out of the box (encryption-required input reports + real CCCDs), so iOS enables the
input reports and keystrokes/pointer register. The host talks to the board over **USB serial** and
just streams the report bytes; the firmware relays them as BLE HID notifications.

**Hardware:** one ESP32 board (this repo verified a classic **ESP32-D0WDQ6** DevKit on a CP2102 USB
bridge; ESP32-S3 / C3 also supported -- see the extra envs in `platformio.ini`). Nothing else.

**Flash it (PlatformIO):**

```
# one-time: PlatformIO Core (pio). build_flash.sh installs it if missing (uv tool install platformio)
cd firmware/esp32
pio run                                  # compile (default env = esp32dev = classic ESP32)
pio run -t upload                        # compile + flash the connected board (auto-detect port)
pio device monitor -b 115200             # optional: watch the serial replies
```

or the wrapper (installs pio + toolchain if needed, auto-detects the chip when given a port):

```
firmware/esp32/build_flash.sh /dev/ttyUSB1
```

- **Detected board id on wsb:** `esp32dev` (classic ESP32-D0WDQ6). For another variant pass
  `PIO_ENV=esp32-s3-devkitc-1` (or `pio run -e <env>`); envs live in `platformio.ini`.
- The project uses the **pioarduino** platform fork (pinned release) because it ships the ESP32
  Arduino **core 3.x** that NimBLE-Arduino 2.x requires; PlatformIO's official `espressif32` still
  ships core 2.x. This is all in `platformio.ini` -- a plain `pio run -t upload` reproduces it.

**Use it:**

```
# no daemon needed -- the board advertises from firmware the moment it is powered
python/src/hidbridge/bt_hid --backend esp32 --port /dev/ttyUSB1 status      # ping the board
python/src/hidbridge/bt_hid --backend esp32 --port /dev/ttyUSB1 pair-mode   # kick advertising + pairing steps
python/src/hidbridge/bt_hid --backend esp32 --port /dev/ttyUSB1 type "hi"   # type
python/src/hidbridge/bt_hid --backend esp32 --port /dev/ttyUSB1 move 10 -5  # pointer
python/src/hidbridge/bt_hid --backend esp32 --port /dev/ttyUSB1 click left
```

Set `BT_HID_ESP32_PORT=/dev/ttyUSB1` to omit `--port` (the port is also auto-detected among
`/dev/ttyUSB*` / `/dev/ttyACM*`, preferring a CP210x/CH340 bridge).

**One-time pairing (physical -- the maintainer taps once):** with the board powered (advertising as
`wsb-hid-esp32`), on the iPad/iPhone open **Settings > Bluetooth**, tap **`wsb-hid-esp32`** under
*Other Devices*, and tap **Pair** (Just Works -- nothing to type). After that it reconnects on its
own and `type/key/move/click` register.

**Serial protocol** (115200 baud, newline-terminated ASCII; host -> ESP32; see
`firmware/esp32/src/main.cpp`): `K<hex>` = 8-byte keyboard report body, `M<hex>` = 4-byte mouse
report body, `P` = ping, `A` = start advertising. The board replies one `OK.../ERR...` line per
command and emits `EVT connected`/`EVT disconnected`. The firmware `REPORT_MAP` is byte-identical to
`reports.REPORT_DESCRIPTOR` (a pytest enforces it), so both backends present the same HID device.

## Route: BLE HID-over-GATT (HOG), not classic BT-HID

We use **BLE HID-over-GATT** (GATT service `0x1812`), not the classic BT-HID-device role. Why:

- The classic HID-device role needs an SDP record + the L2CAP HID PSMs `0x11`/`0x13`, which
  **collide with BlueZ's `input` plugin** (the HID *host* role). We are forbidden from disabling
  that plugin globally (it serves wsb's own BT input devices).
- BLE HOG has **no SDP and no HID PSMs**, so there is no `input`-plugin conflict, and every BlueZ
  object is **per-adapter** (`/org/bluez/hciN/...`) so we cleanly scope everything to the dongle.
- iPadOS/iOS fully support BLE HID keyboards + mice, including **lock-screen passcode entry**.

(If we ever need classic BT-HID's page-initiated switching, see "Multi-device" below -- but it would
reintroduce the `input`-plugin problem, so BLE is the default.)

---

## Which adapter it uses (dongle only, built-in refused)

**Hard rule: the tool uses the USB dongle, never the wsb built-in Bluetooth.** It auto-detects and
**refuses** the built-in unless explicitly forced.

Detection (`python/src/hidbridge/adapter.py`, pure + unit-tested) classifies each `hciN` by two signals:

1. **USB `removable` sysfs attribute** -- the built-in radio reads `fixed`; a hub-plugged dongle
   reads `removable`/`unknown`.
2. **USB vendor** -- Intel (`0x8087`) and common Broadcom (`0x0a5c`) are soldered-in combo radios
   -> always treated as built-in.

On wsb right now:

```
hci0  builtin  vendor=8087 removable=fixed       (built-in, refused)
hci1  dongle   vendor=2357 removable=unknown   TP-Link UB500 Adapter  <-- USED
```

(`hci1` = TP-Link UB500 = Realtek RTL8761B, `2357:0604`, on an external hub port.)

**Override:** pin an adapter with `--adapter hciN` (or a MAC), or `BT_HID_ADAPTER=hciN`. Pinning the
built-in **errors out** unless you pass `--force-builtin` (documented escape hatch only).

It never alters the built-in adapter. All changes are **scoped to the dongle** and **restored on
exit** (Powered/Discoverable/Pairable/Alias are saved and put back when the daemon stops).

---

## Running the daemon

Persistent (survives shells + reboots), branch-tracked systemd unit -- the reviewable, cleanly
removable way (unit template `bt-hid.service.in`, installer `service.sh`):

```
sudo python/src/hidbridge/service.sh install [ADAPTER] # install + enable + start (instance = hciN; default: auto dongle)
sudo python/src/hidbridge/service.sh status [ADAPTER]
sudo python/src/hidbridge/service.sh logs   [ADAPTER]
sudo python/src/hidbridge/service.sh stop   [ADAPTER]
sudo python/src/hidbridge/service.sh uninstall [ADAPTER]  # stop + disable + remove the unit file
```

The unit installs to `/etc/systemd/system/bt-hid@<NAME>.service` and runs
`bt_hid pair-mode --name <NAME>`. For a quick throwaway run without installing, use
`sudo python/src/hidbridge/pair-mode.sh` (foreground, Ctrl-C to stop).

## Pairing (the maintainer does this ONCE per device -- physical)

1. Make sure the daemon is running (see above); the dongle advertises as **`wsb-hid-kbd`**.
2. On the **iPad/iPhone**: **Settings > Bluetooth**.
   - If `wsb-hid-kbd` already appears under *My Devices*, tap the (i) and **Forget This Device**
     first (clears any stale/unauthenticated bond).
   - Under *Other Devices* tap **`wsb-hid-kbd`**.
   - A pairing dialog appears (LE Secure Connections; iOS may show a 6-digit code). **Tap "Pair".**
     Our agent auto-confirms on this side -- there is nothing to type.
   - It moves to *My Devices / Connected* and STAYS (the auto-trust watcher keeps the bond).

That is the only physical step. After it, the authenticated bond is stored on the dongle and the
device reconnects on demand without another tap.

---

## Multi-device: ONE dongle, bonded to many, active with one at a time

**Answer: one dongle suffices.** You do the one-time pair (above) with each iPad/iPhone. The dongle
stores an independent bond **per device**, and is actively linked to **one at a time** -- exactly how
a multi-host Bluetooth keyboard (e.g. Logitech K380, 3 hosts) works.

**Evidence:**

- BlueZ stores each central's bond in its own subdirectory under the **dongle's** address:
  `/var/lib/bluetooth/E8:48:B8:C8:20:00/<central-mac>/info`. Multiple centrals = multiple subdirs,
  all under the one dongle. (The built-in adapter's bonds live under its own separate MAC dir and
  are untouched.)
- A BLE HID peripheral holds a single active HID connection; the others remain bonded and idle.

**Switching (`bt_hid connect <name|mac>`):** because a BLE peripheral cannot *page* a central, the
switch is: drop any other active link, keep the dongle **advertising**, and the **target reconnects**
to us. In practice you point the target at us (it auto-reconnects to a bonded HID keyboard when it is
awake and in range). `connect` also issues a best-effort `Device1.Connect()`; the normal path is the
target reconnecting to our advertisement.

> If you ever need **deterministic push** switching (host initiates the link to a chosen device, like
> the K380 button), that is a property of **classic BT-HID** (role reversal / outgoing page), which
> we avoid here because of the `input`-plugin PSM conflict. The BLE approach covers the wake/unlock/
> type/click workflow, which is what the wedge-recovery use case needs.

---

## Target switching: pick WHICH backend drives WHICH paired device (`--device` + `targets`)

You rarely want to remember that `mini6` lives on `hci1` bonded as `mini6` while `mini5` lives on
`hci2`. The **targets registry** (`bt_hid targets`) maps a friendly device name to a `(target, bond)`
pair, and `--device <name>` resolves it automatically:

```bash
# register a device: <name> <HidSession target> <bond identity>   (target grammar = HidSession's)
bt_hid targets add mini6 hci1 mini6                 # a BlueZ dongle, bonded by BLE-name substring
bt_hid targets add mini5 pico:/dev/ttyACM0 AA:BB:CC:DD:EE:FF   # a Pico MCU backend, bonded by MAC
bt_hid targets add labpad esp32:/dev/ttyUSB1 "Lab iPad" --note "shared unit"

bt_hid targets                # or `targets list` -- shows resolved backend + capability + warnings
bt_hid targets remove labpad

# drive a registered device by name -- no --adapter/--backend/--port needed:
bt_hid --device mini6 click back
bt_hid --device mini5 type "hello"
bt_hid --device mini6 connect          # uses the registered bond as the (dis)connect target
```

The registry is plain JSON at `~/.config/bt_hid/targets.json` (override with `BT_HID_TARGETS` or
`XDG_CONFIG_HOME`; not secret -> `0644`). A fresh install seeds `mini6`/`mini5` (+ the board-qualified
routes below) defaults. Explicit `--adapter`/`--backend`/`--port` still override the registry.

### Both boards bonded to one iPad + per-board mini5<->mini6 switching (board-qualified routes)

An MCU board keeps its OWN bond database in firmware, so ONE board can be bonded to BOTH iPads and
switched between them. The registry expresses "which board reaches which iPad" with **board-qualified
route names** `<ipad>-<board>` alongside the primary `<ipad>` names. Defaults now seed all four:

```
mini5       -> pico:/dev/ttyACM0  bond=mini5   # mini5 on its PRIMARY board (Pico)
mini5-esp32 -> esp32:/dev/ttyUSB1 bond=mini5   # mini5 ALSO on the ESP32 (second board bonded to mini5)
mini6       -> esp32:/dev/ttyUSB1 bond=mini6   # mini6 on its PRIMARY board (ESP32)
mini6-pico  -> pico:/dev/ttyACM0  bond=mini6   # mini6 ALSO on the Pico
```

So `bt_hid --device mini5-esp32 move 0 -3` drives mini5 from the ESP32, and each board switches
mini5<->mini6 by driving its two routes in turn. `TargetRegistry.routes_for_bond("mini5")` /
`backends_for_bond("mini5")` enumerate every board bonded to mini5.

### Per-bond BLE identity (host slots) -- ONE ESP32 holds mini5 AND mini6 at once

Each board presents a **DISTINCT BLE identity/address per bonded iPad** ("host slots"), like a
multi-host keyboard. **Slot 0 = the board's NATIVE identity** (its public address, so an existing
bond survives a reflash); **slots >=1 = derived static-random addresses**. Each slot advertises a
**target-scoped name** so you can tell which entry is for which iPad in the iPad's Bluetooth settings:

```
wsb-esp32-mini6   ESP32 slot 0  (mini6)     wsb-pico-mini5   Pico slot 0 (mini5, native/preserved)
wsb-esp32-mini5   ESP32 slot 1  (mini5)     wsb-pico-mini6   Pico slot 1 (mini6)
```

The name is derived from `(board, target)` by `naming.slot_identity_name()` / `TargetEntry.identity_name()`.
Firmware serial commands (both boards): `I<n>[:name]` select+name+advertise a slot, `T<n>` pick the
send-target slot, `S` slot status (`multi=`, per-slot `c<i>`/`s<i>`), `X` disconnect the current
central (keep bond) + park, `O` park (stop advertising, keep connections). See `docs/bt-hid-mcu-approach.md`.

**Deterministic cross-board handoff (`X`/`O`).** Since an iPad can be bonded to BOTH boards, iOS
would otherwise attach it to whichever board it likes. `X` (disconnect+park) / `O` (park) let the host
RELEASE an iPad from one board and PARK that board so the iPad deterministically attaches to the other
-- this is what makes "board A serves this iPad now" reproducible (`transport.park()` / `.disconnect()`).

- **ESP32 (NimBLE):** holds MULTIPLE centrals connected AT ONCE (`CONFIG_BT_NIMBLE_MAX_CONNECTIONS`)
  and notifies a SPECIFIC one -> `capabilities["simultaneous_identities"]=True`. ONE ESP32 holds
  mini6 (slot 0) + mini5 (slot 1) **simultaneously**, no time-slice. Acceptance test:
  `python/tests/test_bt_hid_dual_identity_live.py` (`BT_HID_LIVE_DUAL=1`).
- **Pico (BTstack):** distinct per-slot identity via `gap_random_address_set()`, but the arduino-pico
  `PicoBluetoothBLEHID` wrapper is single-connection, so it switches slots **sequentially**
  (`simultaneous_identities=False`, `multi=0`). Deterministic slot selection, not simultaneous hold.

**Pairing per (board, iPad).** To bond a NEW iPad on slot n, the host selects that slot (`I<n>:name`)
so the board advertises that identity, then on the iPad open **Settings > Bluetooth**, tap the
slot's name (e.g. `wsb-esp32-mini5`) under *Other Devices*, and tap **Pair** ("Par" on a Danish iPad).
A just-restarted iPad will not auto-reconnect a bonded BLE-HID board until its FIRST unlock (BFU).

**Re-pair after flashing the per-bond-identity firmware.** Changing identities invalidates bonds
tied to the OLD identity. Slot 0 keeps the board's public address (bonds to it survive): the **Pico's
mini5 bond is PRESERVED as slot 0** (no Pico re-pair). On the **ESP32**, re-pair mini6 (slot 0) and
mini5 (slot 1, a fresh static-random identity) at the flash step. Details: `docs/bt-hid-mcu-approach.md`.

**Routing-ambiguity warning (the shared-MAC trap).** A BlueZ **dongle** exposes ONE BLE identity for
ALL its bonds, and the two wsb UB500 dongles even **share a BD address** -- so a dongle backend cannot
be steered to "the right iPad" by identity; it relies on exclusive-connect + a distinct bond, ONE
iPad at a time. When two registered devices both sit on the (identity-less) dongle family, `resolve`
prints a clear **WARNING** naming the collision. The clean fix for *simultaneous* two-iPad control is
a **per-device-identity MCU backend** (`pico:`/`esp32:` firmware picks the advertising address per
bond) or genuinely separate, distinctly-addressed dongles. MCU-backed devices each own their identity
and never trip the warning. From Python:

```python
from bt_hid import targets
reg = targets.TargetRegistry.load()
r = reg.resolve("mini6")                 # -> ResolvedTarget(target, bond, backend, per_device_identity, ...)
if r.shared_mac_warning:
    print(r.shared_mac_warning)
from bt_hid.conn import HidSession
with HidSession(r.target, r.bond) as hid:
    hid.type_text("hi")
```

---

## Bond persistence + authenticated pairing (the "bonds then drops" fix)

BLE HID on iOS needs two things that a naive peripheral gets wrong:

1. **Trusted / LTK persistence.** After bonding, iOS disconnects and reconnects *encrypted* using the
   stored key. If our side does not mark the device **Trusted**, that encrypted reconnect is refused
   and iOS drops the device back to *Other Devices* after ~1s. The daemon runs a **dongle-scoped
   auto-trust watcher** that marks a device `Trusted=True` the instant it pairs OR connects on hci1
   (it never touches the built-in adapter). Proven: mini6 then holds + auto-reconnects.
2. **Authenticated bond.** BlueZ elevates the HID service characteristics to require an
   **authenticated** key; a plain Just-Works (NoInputNoOutput) bond is unauthenticated, so the HID
   reads fail with *Insufficient Authentication* and iOS never subscribes to the input reports (no
   keys register, on-screen keyboard stays up). The agent therefore uses **DisplayYesNo** (LE Secure
   Connections numeric comparison = authenticated) and auto-confirms the code. Override with
   `BT_HID_IO=<capability>` if ever needed.

Both are dongle-scoped and reversible. Nothing on the built-in adapter is changed.

## Importable controller API (for ops AND as a Sidecar-HID test fixture)

The tool is API-first: `controller.HidController` is a scriptable HID input generator that the CLI is
a thin wrapper over. It builds deterministic report frames from the pure `reports` layer and sends
them via a pluggable transport, so it doubles as the injection fixture for a future closed-loop
Sidecar-HID relay e2e (engage the iPad as a Sidecar display, inject known key/pointer events with
this API, assert they arrive decoded over our Sidecar HIDRelay stream). It cross-checks the host->iPad
UniversalHID codec in `continuity/universalcontrol/hid_report.py` (Apple's private relay path).

```python
from bt_hid.controller import HidController          # default transport = the running daemon socket
c = HidController()
c.type_text("hello"); c.send_key("enter"); c.move(50, -20); c.click("left"); c.scroll(3)
c.play([{"after": 0.2, "action": "type", "text": "hi"},
        {"after": 0.1, "action": "click"}])          # scripted timeline (timing injectable for tests)

from bt_hid.controller import HidController, RecordingTransport   # offline / tests: assert exact bytes
c = HidController(RecordingTransport()); c.type_text("1"); # -> c.transport.frames == [(kind, bytes), ...]
```

## On-host loopback self-test (keyboard/mouse over-the-air proof, no iPad)

`loopback_selftest.py` uses the **built-in** adapter as a BLE central to connect to our peripheral on
the **dongle**, subscribe to the report characteristics, and assert the exact injected report bytes
arrive as notifications -- a closed loop without an iPad. Run the daemon in insecure mode for it
(`BT_HID_INSECURE=1`, which drops the HID chars to plain read/notify + a NoInputNoOutput accept-all
agent so a local Just-Works bond can form), then `BT_HID_LOOPBACK=1 sudo python/src/hidbridge/loopback_selftest.py`.
It is a **hardware-gated, best-effort** test (`python/tests/test_bt_hid_loopback.py`, skipped unless
`BT_HID_LOOPBACK=1`).

KNOWN LIMITATION: on a single host, one `bluetoothd` acting as BOTH the central (hci0) and the
peripheral (hci1) to itself does not reliably populate the central's GATT client view -- the central
discovers + connects + resolves services, but the report characteristics often come back empty, and
the connect itself is flaky. This is a bluetoothd same-host limitation, NOT a defect in our HID GATT
profile: a real external central (the iPad) reads the profile correctly (btmon during the mini6
session shows iOS reading our Report Map + writing the CCCDs). The authoritative byte-level proof of
the report layer is therefore the **offline unit suite** (`type("1234")` -> exact reports, mouse
move/click -> exact reports, passcode -> digit reports + Enter), and the profile's external
readability is proven by btmon, not by this same-host loopback.

## Usage

The daemon must be running (`pair-mode`) for the report commands to work; they are sent to it over a
Unix control socket (`$XDG_RUNTIME_DIR/bt_hid.sock`, mode 0600).

```
sudo python/src/hidbridge/bt_hid adapters            # show adapters + which dongle is used
sudo python/src/hidbridge/bt_hid pair-mode           # discoverable + advertising (run this first)
sudo python/src/hidbridge/bt_hid devices             # list bonded iPads/iPhones on the dongle
sudo python/src/hidbridge/bt_hid connect <name|mac>  # target a bonded device for reconnect
sudo python/src/hidbridge/bt_hid status              # daemon + link status

sudo python/src/hidbridge/bt_hid wake                # wake the connected device
sudo python/src/hidbridge/bt_hid unlock 1234         # type passcode + Enter (NEVER logged)
sudo python/src/hidbridge/bt_hid type "hello@x.com"  # type text
sudo python/src/hidbridge/bt_hid key enter           # named key (enter/esc/space/arrows/f1..)
sudo python/src/hidbridge/bt_hid key space --mod 0x08  # e.g. Cmd(GUI)+Space
sudo python/src/hidbridge/bt_hid move 120 -40        # relative pointer move
sudo python/src/hidbridge/bt_hid click left          # click (also: right, middle, back=btn4, forward=btn5)
sudo python/src/hidbridge/bt_hid click back          # button 4 ("Back") -- an "additional" button iOS can remap
sudo python/src/hidbridge/bt_hid move-abs 0.5 0.5    # ABSOLUTE pointer to screen centre (0..1)  [reportID 3]
sudo python/src/hidbridge/bt_hid tap 0.5 0.9         # absolute-position click at (x,y) fraction  [reportID 3]
sudo python/src/hidbridge/bt_hid touch 0.5 0.9       # digitizer touch tap at (x,y) fraction      [reportID 4]
sudo python/src/hidbridge/bt_hid tap-trust           # helper: nudge to Trust button + click (approx)
sudo python/src/hidbridge/bt_hid disconnect          # drop the link (device stays bonded)
```

**tap-trust caveat:** the *relative* mouse only nudges toward where the iOS Trust button usually is.
For reliable targeting, verify on screen (webcam) or drive it stepwise with `move`/`click`.

**5-button mouse -> deterministic reboot (reportID 2, buttons 4 + 5):** the relative mouse now
declares **5 buttons** -- the original left(0x01)/right(0x02)/middle(0x04) plus **button 4 "Back"
(0x08)** and **button 5 "Forward" (0x10)**. The buttons byte stays 1 byte (5 button bits + 3 const
padding bits); the body is unchanged `[buttons, dx, dy, wheel]` and buttons 1-3 are byte-identical to
the old 3-button device (regression-tested). This unblocks the autonomous mini6 reboot (task #35): a
3-button mouse cannot emit an "additional" button, which was the only thing blocking the AssistiveTouch
button-mapping. Once flashed/paired, iPadOS sees a 5-button mouse; map button 4 in
**Settings > Accessibility > Touch > AssistiveTouch > Pointer Devices > Customize Additional Buttons**
(pick button 4/Back -> action "Restart"/"Genstart"). Then a single `bt_hid click back` = a
deterministic software reboot. REMAINING MANUAL STEP: iOS caches the GATT report map per bond, so the
mini6 must be FORGOTTEN + RE-PAIRED once after flashing this change before it sees the extra buttons;
the AssistiveTouch mapping UI itself is already confirmed working.

**Absolute pointer + digitizer (reportIDs 3 + 4) -- iOS does NOT honour these (verdict 2026-07-10):**
`move-abs`/`tap` drive an *absolute* Generic-Desktop mouse (X/Y as u16 0..32767 = a 0.0..1.0 screen
fraction), and `touch` drives a single-finger *digitizer* (touchscreen). Webcam-verified on mini6:
iPadOS ACCEPTS + subscribes to both reports and they are DELIVERED (btmon-proven), but it does NOT
act on the absolute coordinate or the touch -- the cursor never snaps to the commanded point and the
digitizer tap registers nothing (iPadOS pointer is relative-only). A button *click* still fires, but
at the pointer's existing location, not the aimed coordinate. So these do NOT give exact-coordinate
taps; use the relative mouse + on-screen (webcam) verification instead. The reports are harmless +
ADDITIVE (relative mouse reportID 2 + keyboard reportID 1 unchanged) and stay behind their own report
IDs. NOTE: iOS caches the GATT report map per bond -- a report-map change needs a FORGET + RE-PAIR to
take effect. Full evidence + the one untested (dedicated-digitizer) hypothesis:
`docs/bt-hid-absolute-pointer.md`.

---

## Security

This tool types a **device passcode / Apple-ID password** on **the maintainer's own devices** for his own
workflow -- legitimate. Safeguards:

- `unlock <passcode>` expands the passcode to HID frames **locally in the CLI** and sends only the
  frames to the daemon; the passcode **string is never sent over the socket, never stored, never
  logged**.
- The daemon **never logs report bytes** (a report can carry a passcode digit).
- **Never** put a real passcode in a script, a commit, or a log. The tests use the placeholder
  `"0000"` only.
- The control socket is a user-owned Unix socket at mode `0600`.

---

## Files

- `reports.py` -- **pure** report layer (stdlib only): HID scancode map, keyboard/mouse report
  packing, `type_text` / `passcode_reports` / `mouse_move_reports`, and the combined USB-HID
  `REPORT_DESCRIPTOR`. Unit-tested offline (`python/tests/test_bt_hid_reports.py`).
- `adapter.py` -- **pure** dongle detection + the built-in-refusal guard. Unit-tested
  (`python/tests/test_bt_hid_adapter.py`).
- `ble_hid.py` -- the BLE HID-over-GATT daemon (system python: needs `dbus` + `gi`): full HID GATT
  profile, Just-Works/DisplayYesNo pairing agent, auto-trust watcher, control socket.
- `controller.py` -- the importable `HidController` API + transports (pure/socket/recording) + the
  backend-agnostic `make_transport(backend)` loader (`transport_<backend>` modules plug in here).
- `transport_esp32.py` -- the **ESP32 backend**: streams report frames to an ESP32 over USB serial
  (lazy pyserial; pure `encode_frame` is offline-tested). Selected by `--backend esp32`.
- `firmware/esp32/` -- the ESP32 NimBLE HOG firmware as a **PlatformIO** project (`platformio.ini`
  + `src/main.cpp`), plus `build_flash.sh`. Advertises as `wsb-hid-esp32`.
- `transport_pico.py` -- the **Pico backend** transport: ships reports over USB serial to a Pico 2 W
  running `firmware/pico/`. Pure framing helpers + Pico-vendor-gated port autodetect; pyserial is
  imported lazily so the framing + tests run offline
  (`python/tests/test_bt_hid_transport_pico.py`). Selected by `--backend pico`.
- `firmware/pico/` -- the Pico 2 W BLE HID firmware (PlatformIO / arduino-pico, advertises
  `wsb-hid-pico`). See `firmware/pico/README.md`.
- `cli.py` / `bt_hid` -- the CLI (system python: needs `dbus` for the dongle path; the serial
  backends `--backend esp32` / `--backend pico` need only `pyserial`).
- `pair-mode.sh` -- one-command foreground discoverable bring-up.
- `service.sh` + `bt-hid.service.in` -- install/manage the persistent systemd unit.
- `loopback_selftest.py` -- on-host built-in-central <- dongle loopback e2e (gated).

**Interpreter note:** the daemon + CLI run under the **system** `/usr/bin/python3` (it has `dbus`,
`gi`, and `AF_BLUETOOTH`). uv's managed CPython lacks all three, so it can only run the pure
`reports`/`adapter` modules + their tests -- which is exactly what the offline pytest suite does.

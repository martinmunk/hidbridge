# Connecting an iPad / iPhone to bt_hid (dongle / ESP32 / Pico) -- setup manual

This is the DEVICE-SIDE manual: how to pair an iPad or iPhone to each `bt_hid` backend and what to
turn on in iOS/iPadOS so the emulated keyboard + pointer actually drive the device (wake, unlock,
type, click, and a no-touch software Restart). It complements the developer/architecture notes in
[`docs/bt-hid-toolbox-README.md`](../docs/bt-hid-toolbox-README.md) -- read that for the daemon, the report layer,
adapter selection, and multi-device internals.

`bt_hid` presents a STANDARD Bluetooth keyboard + mouse (BLE HID-over-GATT). Every backend serves the
same HID reports (built from `reports.py`), so the on-device setup below is IDENTICAL across
backends; only the pairing name + the physical board differ.

--------------------------------------------------------------------------------------------------

## 0. Pick a backend

| backend | hardware | advertises as | notes |
|---------|----------|---------------|-------|
| `dongle` | USB Bluetooth dongle + this host's BlueZ | `wsb-hid-<hciN>` (default `wsb-hid-kbd`) | most integrated; the BlueZ HOG path needed the CCCD / Report-Reference fixes (see README) |
| `esp32` | any ESP32 dev board on USB serial | `wsb-hid-esp32` | most iOS-robust HOG stack (NimBLE); used for the reboot recipe |
| `pico`  | Raspberry Pi Pico 2 W on USB serial | `wsb-hid-pico` | BTstack HOG; renumbers the mouse to reportID 3 + pads a pan byte (handled transparently) |

All three are driven from Linux the same way (CLI or `HidSession`); see section 4.

--------------------------------------------------------------------------------------------------

## 1. Pair the device (one-time, physical -- a person taps once)

Pairing is Just Works / LE Secure Connections; nothing to type on the iPad. Do it ONCE per
(device, backend); the bond persists and the device auto-reconnects afterwards.

### dongle (`wsb-hid-kbd`)
1. Start the daemon so the dongle advertises: `sudo python/src/hidbridge/service.sh install` (or a throwaway
   `sudo python/src/hidbridge/pair-mode.sh`). It advertises as `wsb-hid-kbd`.
2. On the iPad: **Settings > Bluetooth**. If `wsb-hid-kbd` already shows under *My Devices*, tap the
   (i) and **Forget This Device** first (clears any stale/unauthenticated bond).
3. Under *Other Devices* tap **`wsb-hid-kbd`**, then **Pair**. Our agent auto-confirms; it moves to
   *My Devices / Connected* and stays (the dongle-scoped auto-trust watcher keeps the bond).

### ESP32 (`wsb-hid-esp32`)
1. Power the board (it advertises from firmware immediately). Verify: `bt_hid --backend esp32 --port
   /dev/ttyUSB1 status`. NEVER point `--port` at `/dev/ttyUSB0` (that is the CH340 KVM adapter -- the
   ESP32 backend hard-excludes USB vendor `1a86` and prefers the CP2102 `10c4`).
2. On the iPad: **Settings > Bluetooth > Other Devices > `wsb-hid-esp32` > Pair**.

### Pico (`wsb-hid-pico`)
1. Power the Pico 2 W (advertises from firmware). Verify: `bt_hid --backend pico status` (autodetect
   is Pico-vendor-gated to USB `2e8a`; pin with `--pico-port /dev/ttyACMx` / `BT_HID_PICO_PORT`).
2. On the iPad: **Settings > Bluetooth > Other Devices > `wsb-hid-pico` > Pair**.

> **Quirk -- one active central at a time.** A BLE HID peripheral serves ONE central. The ESP32 /
> Pico bond only ONE iPad at a time on their firmware; the dongle stores an independent bond per
> device but is actively linked to one at a time (like a multi-host BT keyboard). If a board seems
> to "grab itself" right after boot, a stale bond on another iPad is auto-reconnecting -- Forget the
> board on that iPad.

> **Quirk -- iOS caches the GATT report map per bond.** Any change to the report descriptor (adding
> a mouse button, the abs/digitizer reports, etc.) is INVISIBLE until you **Forget This Device** and
> **re-pair**. Do the forget/re-pair after flashing new firmware or changing `reports.py`.

--------------------------------------------------------------------------------------------------

## 2. One-time iOS/iPadOS setup for full control

Turn these on ONCE per device (they persist). Exact submenu names shift slightly across iPadOS
versions; the top-level path + the feature name are stable.

### 2a. Full Keyboard Access (FKA) -- keyboard-only navigation
**Settings > Accessibility > Keyboards > Full Keyboard Access -> ON.**
Lets the emulated keyboard move a focus ring (Tab / arrows / Space to activate) through UI elements,
so you can drive dialogs without a pointer. VERIFIED on mini6: the FKA focus ring is reliably
readable on the webcam for DIALOG BUTTONS (it is faint on home-screen icons). Spotlight
open-app-by-name (`Cmd+Space`, type a name, Enter) is deterministic and is the most reliable way to
launch something.

### 2b. AssistiveTouch -- the on-screen pointer + menu
**Settings > Accessibility > Touch > AssistiveTouch -> ON.**
Enables the software pointer that a Bluetooth mouse drives, plus the floating AssistiveTouch menu
(which contains an auto-returning **Restart** entry under Device > More).

### 2c. AssistiveTouch controlled by the connected pointer (Pointer Devices)
**Settings > Accessibility > Touch > AssistiveTouch > Pointer Devices.**
Confirms the paired `bt_hid` mouse appears here and drives the AssistiveTouch pointer. This is what
makes `bt_hid move` / `click` move the on-screen cursor + click. (Bench shorthand: "AssistiveTouch
controlled by controllers".)

### 2d. Customize Additional Buttons -> map button 4 to Restart (the no-touch reboot)
**Settings > Accessibility > Touch > AssistiveTouch > Pointer Devices > Customize Additional
Buttons.** Click **Button 4** (our mouse declares 5 buttons: left/right/middle + Back=btn4 +
Forward=btn5) and assign the action **Restart** (da: "Genstart"). After this, a single `bt_hid click
back` triggers a deterministic software reboot with NO on-screen touch. (A 3-button mouse cannot emit
an "additional" button -- that was the original blocker; the report layer now declares 5.)
Re-pair after flashing the 5-button descriptor so iOS re-reads the report map (see the cache quirk).

### 2e. Mouse Keys (optional) -- pointer via the numeric keypad
**Settings > Accessibility** -> Pointer Control / Keyboards (submenu varies) **> Mouse Keys.**
Lets a KEYBOARD move the pointer, a fallback for pointer motion when only the keyboard path is
reliable. Optional; the primary pointer path is the mouse reports (2c).

### 2f. The "Restart" Shortcut (Shortcuts app) -- keyboard-launchable reboot
In the **Shortcuts** app, create a shortcut (e.g. named `Restart`) whose single action is **Shut
Down**, then switch that action's toggle from *Shut Down* to **Restart**. This gives a reboot you can
launch BY NAME from the keyboard: `Cmd+Space`, type `Restart`, Enter. See the reboot recipe (section
5) for the current headless limitation.

--------------------------------------------------------------------------------------------------

## 3. Passcode / unlock note

`bt_hid unlock <passcode>` types the device passcode at the lock screen then Enter. The passcode
string is expanded to HID frames locally and is NEVER logged, stored, or sent over the control
socket. Never put a real passcode in a script or commit. (mini6's PIN is kept at
`~/.config/bt_hid/idevice.pin`, mode 0600, so a reboot that lands on the lock screen is recoverable.)

--------------------------------------------------------------------------------------------------

## 4. Driving it from Linux -- `HidSession` (recommended)

`HidSession` connects a specific bonded device on any backend for the duration of a `with`, and --
critically -- its `__enter__` returns only once the link is READY TO USE (iOS has subscribed to the
HID reports), so the first keystroke lands instead of being dropped in the ~1-3s post-reconnect
window. It is backend-agnostic:

```python
from bt_hid.conn import HidSession

# dongle:
with HidSession("hci1", "My iPad") as hid:
    hid.unlock("1234"); hid.type_text("hello"); hid.send_key("enter")
    hid.move(120, -40); hid.click("left")

# ESP32 / Pico (serial backends -- same API):
with HidSession("esp32:/dev/ttyUSB1", "mini6") as hid:
    hid.type_text("Restart"); hid.send_key("enter")
```

`target` grammar: `hciN` / MAC / USB-path / `None` (dongle), or `pico:PORT` / `esp32:PORT` /
`dongle:hciN`. Readiness tunables: `HidSession(..., ready_timeout=8.0, settle=None,
require_ready=False)`; `hid.ready_state` is `"ready"` (real subscribe signal) or `"settle"`
(fallback delay). `AsyncHidSession` (`async with`) overlaps the connect/readiness waits so several
iPads can be brought up concurrently. Full design: [`bt-hid-mcu-approach.md`](bt-hid-mcu-approach.md).

The one-shot CLI equivalents (`bt_hid wake/unlock/type/key/move/click ...`) are in the README's Usage
section.

### 4a. Switch which backend drives which device (`--device` + the targets registry)

To avoid hard-coding `(target, bond)` everywhere, register each device once and address it by a
friendly name. `bt_hid targets add <name> <target> <bond>` writes `~/.config/bt_hid/targets.json`
(override with `BT_HID_TARGETS`); then `bt_hid --device <name> ...` (or
`BT_HID_DEVICE=<name>`) resolves the backend target + bond automatically:

```bash
bt_hid targets add mini6 hci1 mini6                          # dongle, bonded by name substring
bt_hid targets add mini5 pico:/dev/ttyACM0 AA:BB:CC:DD:EE:FF  # Pico MCU, bonded by MAC
bt_hid targets                 # list -> resolved backend + capability + any routing warning
bt_hid --device mini6 click back
bt_hid --device mini5 type "hello"
```

From Python, resolve then open a `HidSession` on the resolved pair:

```python
from bt_hid import targets
from bt_hid.conn import HidSession
r = targets.TargetRegistry.load().resolve("mini6")
with HidSession(r.target, r.bond) as hid:
    hid.type_text("hi")
```

**Shared-MAC warning:** two devices both on the (identity-less) dongle family raise a routing-
ambiguity warning -- a dongle cannot pick the iPad by identity (the two UB500 dongles even share one
BD address), so only one drives at a time via exclusive-connect. For *simultaneous* two-iPad control
use per-device-identity MCU backends (`pico:`/`esp32:`) or separate distinctly-addressed dongles.
Full detail: the README "Target switching" section.

--------------------------------------------------------------------------------------------------

## 5. Reliable software-reboot recipe

Goal: reboot a wedged iPad from Linux with no physical touch. Two paths:

### Path A (deterministic, no on-screen touch) -- button-4 -> Restart
Prerequisites: 5-button mouse descriptor flashed + re-paired (section 1 quirk), and button 4 mapped
to **Restart** in AssistiveTouch (section 2d). Then:

```python
with HidSession("esp32:/dev/ttyUSB1", "mini6") as hid:
    hid.click("back")     # button 4 == Restart -> immediate software reboot, no dialog
```

This is the preferred recipe: AssistiveTouch fires Restart directly with no confirmation dialog.

### Path B (Restart Shortcut via Spotlight) -- ONE step not yet headless
Prerequisites: the `Restart` Shortcut (section 2f) + FKA (2a).

```python
with HidSession("esp32:/dev/ttyUSB1", "mini6") as hid:
    hid.send_key("space", modifiers=0x08)   # Cmd+Space -> Spotlight
    hid.type_text("Restart"); hid.send_key("enter")   # launch the Restart shortcut by name
```

**KNOWN LIMITATION (not yet headless):** iOS then shows a **confirmation dialog** ("Restart" /
"Cancel") that must be tapped. FKA CAN move a focus ring onto dialog buttons (verified readable on
the webcam), but reliably landing the confirm tap without on-screen verification is not yet solved.
So Path B currently needs one webcam-verified confirm (via FKA Tab->Space, or an absolute tap once
that path lands). Use Path A for a fully headless reboot; track Path B's confirm-tap in issue #35.

> **Pointer aiming caveat.** iPadOS pointer input is RELATIVE-only: the absolute-mouse (reportID 3)
> and digitizer (reportID 4) reports are accepted + delivered but iOS does NOT act on their
> coordinates (webcam-verified 2026-07-10; see [`bt-hid-absolute-pointer.md`](bt-hid-absolute-pointer.md)).
> So you cannot aim an exact-coordinate tap -- use the relative mouse + on-screen (webcam)
> verification, the FKA focus ring, or the button-4 mapping (Path A) which needs no aiming.

--------------------------------------------------------------------------------------------------

## 6. Per-backend quirk summary

- **dongle:** uses the USB dongle only, never the built-in adapter (auto-refused). Bond is
  dongle-scoped + restored on exit. The BlueZ HOG path required an authenticated (DisplayYesNo) bond
  + a Report-Reference descriptor fix for iOS to subscribe -- both are in the daemon now.
- **esp32:** most iOS-robust; the reboot recipe (section 5) is driven over it. Report map is
  byte-identical to `reports.py` (a pytest enforces it). Port is vendor-gated to CP2102 `10c4`; the
  CH340 KVM (`1a86`, `/dev/ttyUSB0`) is hard-excluded.
- **pico:** BTstack builds its OWN descriptor -- the mouse is reportID **3** (not 2) and its body has
  a trailing pan=0 byte. `bt_hid` handles both transparently (the central self-test's Pico profile
  remaps 2->3 and tolerates the pan pad), so behaviour is identical from the caller's side.

--------------------------------------------------------------------------------------------------

## 7. Validating a backend without an iPad (self-test)

`python/src/hidbridge/central.py` (behind `python/tests/test_bt_hid_central_selftest.py`, gated by
`BT_HID_CENTRAL=1`) uses this host's built-in adapter as a transient BLE CENTRAL to connect to an MCU
peripheral, byte-verify its Report Map + Report References against `reports.py`, and assert injected
report bytes arrive as GATT notifications -- a full proof with NO iPad. If the built-in adapter
stalls on connect (a known Intel `hci0` LE auto-connect issue), use `--connect-method connectdevice`
(needs bluetoothd Experimental) or pin a USB dongle as the central with `--central hciN`
(`BT_HID_CENTRAL_ADAPTER`); a clean reboot also clears the stall. Details:
[`bt-hid-central-selftest-status.md`](bt-hid-central-selftest-status.md).

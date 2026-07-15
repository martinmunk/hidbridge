# Prior-art & landscape survey (open-source projects overlapping hidbridge)

Compiled 2026-07-14 by a research agent. "Verified" = read from the cited primary page this session;
"Inferred" = interpretation, flagged inline. Left uncommitted by default -- move/edit/commit as you see
fit. Every claim is cited.

## Executive summary

hidbridge's individual capabilities all have prior art, but no single open-source project combines
them. The ecosystem splits into four disconnected clusters:

1. **Linux-as-BLE-HID-peripheral via BlueZ/HOG** -- thin, mostly keyboard-only reference code
   (HeadHodge). The mature "PC as a Bluetooth keyboard+mouse" tools (hidclient, EmuBTHID) are classic
   BR/EDR, not BLE, and several explicitly fail on Apple devices.
2. **ESP32/Pico BLE-HID firmware driven over serial** -- one strong match, `asterics/esp32_mouse_keyboard`
   (same "MCU running open HOG firmware + ASCII command protocol + host-switching" shape) -- but
   relative mouse only, single backend.
3. **Absolute-pointer / digitizer over BLE** -- a small cluster (sobrinho, iw20, topcoco, Renesas/Dialog,
   kshoji); each a single-library, single-backend artifact.
4. **Network -> real HID emitter** -- a rich cluster (PiKVM, TinyPilot, serial-hid-kvm, WiFiDuck,
   ESPHome-BLE-keyboard), but almost all emit USB HID, not BLE, and are web-KVM products, not a
   scriptable library with a shared report layer.

**Verdict: no project occupies hidbridge's niche.** The distinctive combination is *one shared HID
report layer feeding three interchangeable backends (BlueZ HOG / MCU-serial / network) that all target
BLE-HID hosts, with absolute-pointer + digitizer and per-bond identities.* Closest single competitors
to study/cite: **asterics/esp32_mouse_keyboard** (MCU-serial backend), **VKCOM/devicehub** (iPad-control
via ESP32 BLE-HID), **PiKVM** (network->real-HID architecture incl. absolute pointer),
**sobrinho/ESP32-BLE-Abs-Mouse** (absolute/digitizer over BLE).

## Dimension 1 -- BLE HID peripheral emulation on Linux via BlueZ / HOG

| Project | URL | Lang / License | Popularity | Overlap & where hidbridge differs |
|---|---|---|---|---|
| HeadHodge / Bluez-HID-over-GATT-Keyboard-Emulator-Example | https://github.com/HeadHodge/Bluez-HID-over-GATT-Keyboard-Emulator-Example | Python, MIT | ~26* | Truest BlueZ-HOG-via-D-Bus example. Keyboard + consumer only, no mouse/pointer/digitizer; reference code, single backend; iOS untested (author tested Android/Win/FireTV). |
| petzval / btferret | https://github.com/petzval/btferret | C+Python, MIT | ~230* | Keyboard+mouse over both classic BT and BLE from Linux -- most complete Linux BT-HID codebase found. But bypasses BlueZ (raw HCI), relative pointer only, no digitizer, no MCU/network backends. |
| hidclient family (xenogenesi / 505e06b2 / neo-hidclient) | https://github.com/xenogenesi/hidclient - https://github.com/505e06b2/HIDClient-Bluez5 - https://github.com/jascosius/neo-hidclient | C, GPL-2.0 | 77 / ~4* | Canonical "Linux PC as a Bluetooth keyboard+mouse." Classic BR/EDR, not BLE-HOG; relative pointer; forwards local input rather than a scriptable API. neo-hidclient names iOS as a target. |
| Alkaid-Benetnash / EmuBTHID | https://github.com/Alkaid-Benetnash/EmuBTHID | Python | ~162* | Linux BlueZ classic-BT HID keyboard+mouse. Explicitly does NOT work with Apple devices (verified); relative only, single backend. |
| AnesBenmerzoug / Bluetooth_HID | https://github.com/AnesBenmerzoug/Bluetooth_HID | Python, GPL-3.0 | ~114* | Raspberry Pi as classic-BT keyboard/mouse. Archived 2021. Classic BT, relative, single backend. |

**Key finding (verified):** No polished/popular Linux/BlueZ project does full BLE-HOG keyboard+mouse,
let alone absolute-pointer/digitizer over HOG. The mature "PC-as-BT-keyboard+mouse" tools are all
classic BR/EDR. This is hidbridge's clearest gap in the field.

## Dimension 2 -- ESP32/Pico BLE-HID firmware driven from a host over serial

| Project | URL | Lang / License | Popularity | Overlap & where hidbridge differs |
|---|---|---|---|---|
| **asterics / esp32_mouse_keyboard** (closest to the MCU backend) | https://github.com/asterics/esp32_mouse_keyboard | C (ESP-IDF), GPL-3.0 | ~897*, active | VERIFIED same shape as hidbridge's MCU backend -- open ESP32 BLE-HOG firmware driven over a UART ASCII `$`-command protocol (`$ID`,`$GP`,`$PM`), with multi-host bond switching (`$SW` by BT address, `$GP` list, `$DP` delete). Differs: relative mouse only, single backend, no shared report layer, no absolute/digitizer. Part of AsTeRICS/FLipMouse assistive-tech -- strong to cite. |
| KMChris / esp32-kvm | https://github.com/KMChris/esp32-kvm | C+Python, MIT | ~7* | Python host captures input -> USB serial -> ESP32 -> BLE HID. Relative only, tiny, no documented ASCII API, single backend. |
| T-vK / ESP32-BLE-Keyboard & -Mouse | https://github.com/T-vK/ESP32-BLE-Keyboard - https://github.com/T-vK/ESP32-BLE-Mouse | C++ (Arduino) | ~1k*, stale | Canonical Arduino BLE-HID libs. Autonomous (app compiled into MCU), single host, relative only (absolute is an open request, issue #19). The baseline the absolute forks branch from. |
| ESP32-BLE-Combo family (blackketter, TheNitek NimBLE, ...) | https://github.com/blackketter/ESP32-BLE-Combo - https://github.com/TheNitek/ESP32-NimBLE-Combo | C++ | ~4-145* | Keyboard+mouse combo libs, relative, autonomous, single host. |
| NimBLE-Arduino (substrate) | https://github.com/h2zero/NimBLE-Arduino | C++, Apache-2.0 | ~1.1k*, active | VERIFIED supports multiple simultaneous connections (default 3) -- the substrate that could enable "two tablets at once," but the HID libs on top don't expose it. |
| Pico W / BTstack path | https://github.com/bluekitchen/btstack - https://github.com/raspberrypi/pico-examples | C / MicroPython | mature | Canonical open BLE-HOG stack/demos for Pico 2 W. All autonomous -- no ready-made serial->BLE-HID Pico bridge found (which is exactly what hidbridge adds). |

**Key finding:** asterics/esp32_mouse_keyboard is the single best pre-existing match for the MCU-serial
backend *in isolation*. Cite it. But it is one board with a relative mouse; not a report layer shared
with a BlueZ or network backend.

## Dimension 3 -- Programmatic tablet/phone control via emulated HID

| Project | URL | Lang / License | Popularity | Overlap & where hidbridge differs |
|---|---|---|---|---|
| **VKCOM / devicehub** (closest use-case) | https://github.com/VKCOM/devicehub - doc: /blob/master/doc/ios-docs/esp32.md | TS + ESP32 fw | ~210*, active | VERIFIED clearest OSS example of tapping specific iPad coordinates over Bluetooth HID with no jailbreak/agent -- browser -> ESP32 emulating a BLE HID mouse -> iOS AssistiveTouch. Documents converting absolute coords to relative deltas + tracking the guessed cursor. Differs: device-farm product, single ESP32 backend, no shared report layer, no digitizer. |
| PiKVM (Bluetooth HID mode) | https://docs.pikvm.org/bluetooth_hid/ - https://github.com/pikvm/pikvm | Python/C, GPL | ~10k* | Docs explicitly: "control the cursor and keyboard of an iPad" via emulated Bluetooth HID. Relative mouse only in BT mode. Web-KVM tool, single Pi backend. |
| raghavk92 / Kontroller | https://github.com/raghavk92/Kontroller | Kotlin, Apache-2.0 | ~257* | Android BT HID Device profile to control "pc, mac, tv, iPad." Keyboard + relative pointer, single backend. |
| LiangLuDev / HidPeripheral | https://github.com/LiangLuDev/HidPeripheral | Kotlin/Java | ~74* | Android as BLE-HID peripheral, lists iOS as a target. Keyboard + relative mouse, single backend. |
| P4wnP1 A.L.O.A. | https://github.com/RoganDawes/P4wnP1_aloa | Go/Python (Pi Zero W) | large | HIDScript drives keyboard+mouse, primarily USB HID gadget. Relative, keyboard-centric; not tablet-coordinate automation. |

**Load-bearing design fact (verified, multiple sources):** iOS accepts only a *relative* HID pointer
(via AssistiveTouch); there is no absolute-positioning path iOS honors from an external HID host
(VK DeviceHub docs; PiKVM docs; Apple support 111775). Android/Windows *do* honor absolute/digitizer
descriptors. So hidbridge's absolute-pointer/digitizer lands on Android/desktop hosts, while precise
iPad tapping requires relative dead-reckoning -- worth stating honestly, since VK DeviceHub already
documents the workaround.

## Dimension 4 -- Absolute-pointer / digitizer BLE HID (the rare capability)

| Project | URL | Lang / License | Popularity | What it implements |
|---|---|---|---|---|
| **sobrinho / ESP32-BLE-Abs-Mouse** | https://github.com/sobrinho/ESP32-BLE-Abs-Mouse | C++ (Arduino) | ~105* | VERIFIED from BleAbsMouse.cpp: emits a Digitizers-page (0x0D) Touch Screen descriptor -- Tip Switch (0x42) + In-Range (0x32), 16-bit absolute X/Y, Logical Max 10000. Effectively a single-contact touch digitizer over BLE. Single library, single backend. |
| iw20 / ESP32-BLE-Touchscreen | https://github.com/iw20/ESP32-BLE-Touchscreen | C++ | fork | Fork of sobrinho; same digitizer descriptor (inferred from lineage). |
| topcoco / ESP32-BLE-HID | https://github.com/topcoco/ESP32-BLE-HID | C++ | ~108* | Keyboard + absolute mouse + touchscreen; aimed at Android automation. Digitizer-style descriptor (inferred). |
| kshoji / BLE-HID-Peripheral-for-Android | https://github.com/kshoji/BLE-HID-Peripheral-for-Android | Java, Apache-2.0 | ~260* | VERIFIED offers "Absolute Position Mouse" (Generic-Desktop absolute) + relative + joystick. Android peripheral, single backend. |
| Renesas/Dialog BLE_SDK6 -- HID-Gamepad-Digitizer | https://github.com/dialog-semiconductor/BLE_SDK6_examples/tree/main/interfaces/HID-Gamepad-Digitizer | C (DA1453x/1458x) | vendor SDK | VERIFIED an HOGPD digitizer mode "mainly for iOS devices" mapping input to absolute coords (0-100). Good descriptor reference to cite. |

**Key finding (verified):** Absolute-pointer/digitizer *over BLE* exists in ~5 open projects, each a
single-backend library/demo. None pairs it with a BlueZ-HOG Linux backend or a shared report layer.

## Dimension 5 -- HID reports over network / TCP

| Project | URL | Lang / License | Popularity | Overlap & where hidbridge differs |
|---|---|---|---|---|
| PiKVM | https://github.com/pikvm/pikvm | Python/C, GPL-3.0 | ~10.2k* | Strongest network->real-HID prior art. Web/WebRTC -> USB HID gadget (or Pico/Arduino/CH9329); both absolute & relative mouse. But USB-target IP-KVM product, not a BLE-host-driving library, no shared report layer, no mDNS-discovered BLE emitter. |
| **serial-hid-kvm (sunasaji)** (closest network shape) | https://github.com/sunasaji/serial-hid-kvm | Python, MIT | ~2*, active | VERIFIED clean TCP JSON-Lines API for automation -> CH9329 serial-to-USB-HID; absolute positioning. Same "HID-report-producer over a socket + hardware emitter" idea -- but emitter is USB-HID (not BLE), no mDNS, no auth token. |
| TinyPilot | https://github.com/tiny-pilot/tinypilot | Python/JS, MIT | ~3.5k* | WebSocket -> USB HID gadget, relative+absolute. Web-KVM, USB target. |
| ESPHome-espidf_ble_keyboard | https://github.com/markusg1234/ESPHome-espidf_ble_keyboard | C++/Py, GPL-3.0 | ~28* | Clearest network->real-BLE-HID match: HTTP/Home-Assistant over WiFi -> BLE HID kbd/mouse. Relative only, single ESP32 backend, no shared report layer. |
| WiFiDuck / UltraWiFiDuck | https://github.com/SpacehuhnTech/WiFiDuck - https://github.com/EmileSpecialProducts/UltraWiFiDuck | C/C++ | ~2.9k* | WiFi/web -> real USB (S2/S3 variants BLE) HID; two-chip network/emitter split mirrors hidbridge's producer/emitter decoupling. Keyboard-centric, autonomous payloads, not a scriptable report library. |

**Key finding:** Network->real-HID is mature but almost universally USB-HID targets, packaged as web-KVM
products. The one clean network->BLE-HID (ESPHome-BLE-keyboard) is relative-only, single-backend. No
project pairs mDNS discovery + token auth + a real BLE-HID emitter -- a gap hidbridge's network backend
fills.

## The 3-6 closest projects, ranked

1. **asterics/esp32_mouse_keyboard** -- closest because it independently built the MCU-serial backend
   shape (open ESP32 BLE-HOG firmware + ASCII command protocol + multi-host bond switching). Missing:
   absolute pointer; one board, not a shared report layer across backends.
2. **VKCOM/devicehub** -- closest because it's the mainstream open project doing hidbridge's headline
   use case (control an iPad by coordinate via an ESP32 BLE-HID mouse, no jailbreak). Missing: single
   backend, no shared report layer, no digitizer.
3. **PiKVM** -- closest because it's the reference architecture for "network transport -> a real HID
   emitter with both absolute and relative pointer," and even does BT-HID to an iPad. Missing:
   USB-target product, no unified multi-backend report layer.
4. **sobrinho/ESP32-BLE-Abs-Mouse** (+ iw20 / topcoco) -- closest because it's the concrete
   absolute-pointer/touch-digitizer descriptor over BLE. Missing: single Arduino library.
5. **serial-hid-kvm** -- closest because it embodies the network backend's philosophy (TCP socket
   protocol -> remote hardware HID emitter, absolute positioning). Missing: CH9329/USB emitter, no
   mDNS/token.
6. **petzval/btferret** -- closest because it's the most complete Linux keyboard+mouse-over-Bluetooth
   codebase that actually does BLE. Missing: raw HCI (not BlueZ), relative only, not multi-backend.

## Coverage matrix

Legend: Y full - ~ partial/adjacent - - none.

| Project | BlueZ-HOG peripheral | MCU-serial firmware | Network transport | Abs-pointer + digitizer | Cross-platform input automation | Per-bond identity | One report layer -> many backends |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| **hidbridge** | Y | Y | Y | Y | Y | Y | Y |
| asterics/esp32_mouse_keyboard | - | Y | - | - | ~ | Y | - |
| VKCOM/devicehub | - | ~ | Y | - | Y | - | - |
| PiKVM | ~ | ~ | Y | ~ | ~ | - | ~ |
| sobrinho/ESP32-BLE-Abs-Mouse | - | ~ | - | Y | ~ | - | - |
| serial-hid-kvm | - | ~ | Y | Y | ~ | - | - |
| petzval/btferret | ~ | - | - | - | ~ | - | ~ |
| HeadHodge Bluez-HID-over-GATT | Y | - | - | - | - | - | - |
| ESPHome-espidf_ble_keyboard | - | ~ | Y | - | ~ | - | - |
| kshoji BLE-HID-Peripheral (Android) | - | - | - | ~ | ~ | - | - |
| hidclient / EmuBTHID | - | - | - | - | ~ | - | - |
| Renesas/Dialog HID-Digitizer | - | - | - | Y | ~ | - | - |

No row other than hidbridge has more than three columns checked, and no other project checks the two
structural columns -- per-bond identity (only asterics, partially) and one-report-layer-many-backends
(nobody).

## Honest verdict

No single project occupies hidbridge's niche. Every close competitor is a single-backend artifact:

- The **multi-backend + one-shared-report-layer** architecture (identical HID bytes from BlueZ, an MCU
  over serial, and an ESP32 over TCP) has no equivalent. PiKVM is the only project supporting multiple
  emitter *types*, and it's USB-target and product-shaped.
- **BLE-HOG peripheral emulation on Linux** exists only as thin keyboard-only reference code or raw-HCI
  libraries; mature PC-as-BT-keyboard tools are classic BR/EDR and often fail on Apple devices.
- **Absolute-pointer + touch-digitizer over BLE** exists but never combined with the other backends.
- **Per-bond BLE identities / two tablets at once** is essentially unmatched.
- The niche engineering (reset-free B0 serial open, multi-adapter isolation, host-radio self-test)
  turned up no prior art -- treat as novel until shown otherwise.

**Prior art to study/cite before publishing:** asterics/esp32_mouse_keyboard (MCU-serial + command
protocol; EZKey/AsTeRICS lineage); VKCOM/devicehub (iPad control + the iOS relative-dead-reckoning
workaround); PiKVM + TinyPilot + serial-hid-kvm (network->real-HID + absolute/relative dual mode);
sobrinho/ESP32-BLE-Abs-Mouse + Renesas/Dialog HID-Digitizer (absolute/digitizer over BLE);
hidclient/neo-hidclient/EmuBTHID (classic-BT lineage + the works-on-Apple contrast).

**Positioning recommendation:** lead with the two things nobody else has -- *one report layer across
three interchangeable BLE-HID backends* and *absolute-pointer/digitizer + per-bond identities over
BLE-HOG* -- and be upfront that iOS caps you at relative pointing (VK DeviceHub already sets that
expectation publicly).

### Verification notes
Verified from primary source this session: asterics `$`-protocol + `$SW`/`$GP`/`$DP` + relative +
GPL-3.0/897*; sobrinho digitizer descriptor bytes (Digitizers page 0x0D, Logical Max 10000, Tip Switch
0x42 + In-Range 0x32); VK DeviceHub relative-dead-reckoning + iOS AssistiveTouch requirement; PiKVM
abs+relative + iPad control; HeadHodge keyboard-only/iOS-untested; EmuBTHID Apple-incompatibility.
Inferred / not re-verified: star counts/licenses marked "--"; iw20/topcoco descriptors (from lineage);
some Dimension-3/5 metadata from search snippets.

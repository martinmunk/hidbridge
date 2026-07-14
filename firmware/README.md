# hidbridge firmware

MCU firmware that turns a cheap microcontroller into an **iOS-compatible BLE HID keyboard + pointer**
peripheral, driven by the [`hidbridge`](../python/) host over a **USB serial** link. Two boards are
supported; pick whichever you have.

| Board | Dir | BLE stack | Env / toolchain | Advertises as |
|-------|-----|-----------|-----------------|---------------|
| **Raspberry Pi Pico 2 W** (RP2350 + CYW43439) | [`pico/`](pico/) | BTstack HOG (earlephilhower arduino-pico core) | PlatformIO `rpipico2w` | `HIDBRIDGE-pico` |
| **ESP32** (classic / S3 / C3) | [`esp32/`](esp32/) | NimBLE-Arduino 2.x (esp32 core 3.x) | PlatformIO `esp32dev` (+ variants) | `HIDBRIDGE-esp32` |

Both implement the **same HID report map** -- keyboard (report ID 1), relative mouse (ID 2),
**absolute mouse (ID 3)**, and **digitizer/touch (ID 4)** -- installed **byte-for-byte from the host's
`python/src/hidbridge/reports.py` `REPORT_DESCRIPTOR`**. A parity test
(`python/tests/test_bt_hid_*_report_map*`) fails if firmware and host ever drift apart, so **do not
edit the report map in one place without the other**.

## Why an MCU (not just a USB dongle)?

The Linux BlueZ dongle path fights iOS over the HID input-report **CCCD / Report-Reference**
subscription (iOS won't enable our input reports). BTstack (Pico) and NimBLE (ESP32) serve
HID-over-GATT the iOS-compatible way out of the box, so the MCU backend delivers keystrokes/pointer
reliably where BlueZ stalls -- for a ~USD 5-6 board and a USB cable.

## Serial wire protocol (host -> MCU, 115200 baud, newline-terminated ASCII)

```
K<hex>       keyboard report body      (8 bytes  = 16 hex)  -> BLE notify keyboard input report
M<hex>       relative-mouse body       (4 bytes  =  8 hex)  -> BLE notify mouse input report
A<hex>       absolute-mouse body       (5 bytes  = 10 hex)  -> BLE notify abs-mouse input report
D<hex>       digitizer/touch body      (6 bytes  = 12 hex)  -> BLE notify digitizer input report
P            ping                                            -> "OK ... subscribed=<0|1> conn=<0|1>"
A            (bare 'A') (re)start advertising                -> "OK adv"
X            disconnect the target's central, keep bond (+park) -> "OK disc" / "OK noconn"
O            park: stop advertising, keep connections + bonds -> "OK parked"
I<n>[:name]  select host SLOT n as the advertising identity (+optional per-slot name) + advertise
                                                              -> "OK slot=<n> addr=<12hex> name=.. conn=.."
T<n>         set the send-target slot (route K/M/A/D to that connection) -> "OK tgt=<n>"
S            slots status              -> "OK slots=<N> adv=<n> tgt=<n> multi=.. c0=.. c1=.. s0=.. s1=.."
```

Async link events arrive unsolicited as `EVT connected` / `EVT disconnected` and, for the HID report
subscription, `EVT ready` / `EVT unsubscribed` (iOS wrote/cleared the report CCCDs). The host uses
`EVT ready` (or `subscribed=1`) as the **readiness** signal so the first keystroke after (re)connect
is not dropped.

## Per-bond identity ("host slots")

Each board can present a **distinct BLE identity/address and a target-scoped name per bonded device**.
Slot 0 is the board's native address (an existing bond survives a firmware update); slots 1..N are
derived static-random addresses. This is what lets one board be bonded to several iPads and switch
between them deterministically -- and, on the ESP32, hold **two iPads connected simultaneously** on
distinct addresses (route reports with `T<n>`). See [`../docs/bt-hid-mcu-approach.md`](../docs/bt-hid-mcu-approach.md).

## Build + flash (PlatformIO)

Install PlatformIO Core once (`pip install platformio`, or `uv tool install platformio`), then:

```bash
# Pico 2 W  (hold BOOTSEL for the very first flash; the core reboots it over USB afterwards)
cd pico
pio run                 # compile
pio run -t upload       # compile + flash
pio device monitor -b 115200

# ESP32
cd esp32
pio run                 # compile the default env (esp32dev = classic ESP32)
pio run -t upload       # compile + flash (auto-detects the port)
pio run -e esp32-s3-devkitc-1 -t upload    # a different variant
```

Convenience wrappers with one-time toolchain setup + chip auto-detect: `pico/build.sh` and
`esp32/build_flash.sh [PORT]`. See each board's own notes ([`pico/README.md`](pico/README.md)) and
the header comment in each `src/main.cpp`.

## Network (Ethernet) -- drive the ESP32 with no USB cable

An ESP32 with a wired Ethernet PHY (e.g. the **ESP32-Ethernet-Kit**) runs the *same* firmware, built
with `-DHIDBRIDGE_NET`, and additionally serves the identical line protocol over **TCP 3333** with
mDNS discovery (`hidbridge-esp32.local`). Same BLE bonds, same host slots, same report bytes.

```bash
cd esp32
pio run -e esp32-ethernet-kit -t upload        # flash the network build (default esp32dev unchanged)
hidbridge net-provision --esp32-port /dev/ttyUSB3   # one-time: set the auth token over USB
hidbridge --backend net --host hidbridge-esp32.local type "hello"
```

The TCP path is **token-authenticated and fails closed** (no token provisioned ⇒ no network HID; the
token is settable only over the physical serial cable). It is a trusted-network interface: report
bytes cross the LAN in the clear. PHY pins are build flags (default = the ESP32-Ethernet-Kit's
IP101GRI); override for another board. Full detail:
[`../docs/bt-hid-network-backend.md`](../docs/bt-hid-network-backend.md).

## Pairing

The MCU advertises on its own; pair from the **target's Bluetooth settings** (there is no host-side
BlueZ pairing for the MCU backend). Then drive it from the host, e.g.
`hidbridge --backend pico type "hello"`.

# HIDBRIDGE-esp32 -- ESP32 BLE HID firmware

iOS-compatible **BLE HID keyboard + pointer** firmware for an **ESP32** (classic / S3 / C3), the
low-cost **ESP32 backend** for `hidbridge`. It presents four HID reports -- **keyboard (ID 1)**,
**relative mouse (ID 2)**, **absolute mouse (ID 3)**, and a **digitizer/touch (ID 4)** -- advertises
as **`HIDBRIDGE-esp32`**, and relays the report bytes the host streams over USB serial as BLE HID
input notifications.

The HID report map is a **byte-for-byte copy of `python/src/hidbridge/reports.py`
`REPORT_DESCRIPTOR`** (a parity test, `python/tests/test_bt_hid_esp32.py`, guards it -- do not edit
one without the other).

## Why an ESP32?

`hidbridge` needs *some* BLE HID peripheral. The ESP32 backend uses **NimBLE-Arduino 2.x**, which
serves HID-over-GATT (HOG) the iOS-compatible way out of the box (correct CCCD / Report-Reference
handling) -- so it "just works" where the Linux/BlueZ dongle path fights iOS. It is the backend that
can hold **two iPads connected at once** on distinct per-bond addresses (see the per-bond-identity
notes in [`../README.md`](../README.md) and [`../../docs/bt-hid-mcu-approach.md`](../../docs/bt-hid-mcu-approach.md)).

## Hardware

- An **ESP32** dev board. The default PlatformIO env `esp32dev` targets the classic ESP32; envs for
  `esp32-s3-devkitc-1` and `esp32-c3-devkitm-1` are provided (pass `-e <env>` or `PIO_ENV=<env>`).
- A USB cable to the host (also carries the serial command channel).

## Toolchain: PlatformIO

The firmware needs esp32 Arduino **core 3.x** (IDF 5.x) for NimBLE-Arduino 2.x, so `platformio.ini`
uses the maintained community platform **pioarduino** (pinned) instead of the official
`espressif32` (which still ships core 2.x). Everything else is fetched by PlatformIO on the first
build.

```bash
cd firmware/esp32
pio run                  # compile the default env (esp32dev)
pio run -t upload        # compile + flash the connected board (auto-detects the port)
pio device monitor -b 115200
```

One-time-setup + chip-mismatch-warning wrapper: `./build_flash.sh [PORT]`.

## Serial protocol

See the header comment in [`src/main.cpp`](src/main.cpp) and the shared protocol table in
[`../README.md`](../README.md). Pair from the target's Bluetooth settings, then drive from the host,
e.g. `hidbridge --backend esp32 --port /dev/ttyUSB0 type "hello"`.

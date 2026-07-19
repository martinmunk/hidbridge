# hidbridge firmware

MCU firmware that turns a cheap microcontroller into a **BLE HID keyboard + pointer** peripheral,
driven by the [`hidbridge`](../python/) host over a **USB serial** link (or, on a networked ESP32,
over TCP). Two boards are supported; pick whichever you have.

| Board | Dir | BLE stack | Env / toolchain | Advertises as |
|-------|-----|-----------|-----------------|---------------|
| **Raspberry Pi Pico 2 W** (RP2350 + CYW43439) | [`pico/`](pico/) | BTstack HOG (earlephilhower arduino-pico core) | PlatformIO `rpipico2w` | `HIDBRIDGE-pico` |
| **ESP32** (classic / S3 / C3) | [`esp32/`](esp32/) | NimBLE-Arduino 2.x (esp32 core 3.x) | PlatformIO `esp32dev` (+ variants) | `HIDBRIDGE-esp32` |

Both implement the **same HID report map** -- keyboard (report ID 1) + relative mouse (ID 2), the
relative-only report set -- installed **byte-for-byte from the host's
`python/src/hidbridge/reports.py` `REPORT_DESCRIPTOR`**. A parity test
(`python/tests/test_bt_hid_*_report_map*`) fails if firmware and host ever drift apart, so **do not
edit the report map in one place without the other**. (Absolute-mouse + digitizer/touch reports were
removed deliberately -- they made desktop hosts hide the cursor; see the top-level README / CLAUDE.md.)

## Why an MCU (not just a USB dongle)?

The Linux BlueZ dongle path fights some hosts over the HID input-report **CCCD / Report-Reference**
subscription (the host won't enable our input reports). BTstack (Pico) and NimBLE (ESP32) serve
HID-over-GATT the way those hosts expect out of the box, so the MCU backend delivers keystrokes/pointer
reliably where BlueZ stalls -- for a ~USD 5-6 board and a USB cable.

## Wire protocol: one versioned JSON-RPC 2.0 protocol (host -> MCU, 115200 baud / TCP, newline ASCII)

The MCU wire is **one JSON-RPC 2.0 protocol** over the same channel (serial or TCP): the board builds
every HID report AND owns device management, and the host sends **intent**. Since **proto 2** the wire
is uniformly JSON -- requests, replies, errors, and async `event` notifications are all JSON objects; a
non-JSON line yields a JSON-RPC parse error `{id:null}`. (The earlier host-built `K<hex>`/`M<hex>` frame
relay AND the single-char device commands `P/A/I/T/S/N/V/W/X/O` were folded into JSON-RPC and
**removed**; the plain-text `EVT`/`ERR` frames and the TCP `@<token>` auth line were removed at proto 2 --
the TLS-PSK handshake is now the sole network auth.) The full method vocabulary + params, the `caps`
list, the `proto` version, and the version-independence policy live in
[`../docs/bt-hid-protocol.md`](../docs/bt-hid-protocol.md).

Request `{"jsonrpc":"2.0","id":<n>,"method":<m>,"params":{...}}` -> `{..,"result":{...}}` or
`{..,"error":{"code":..,"message":..}}`. A params `"dry":true` returns the built report bytes as hex
WITHOUT notifying. Methods:

```
hello                          proto + a COMPREHENSIVE caps list (the host feature-detects on caps)
type/key/wake/move/click/          the HID report methods -- the board builds the reports
  press/release/scroll/frames
identities                     the host-slot table: [{slot,client,addr,name,conn,sub,bonded,layout}]
connect {slot|client}          bring an identity up + advertise it + claim it (was I<n>)
target  {slot|client}          claim a slot as the send-target WITHOUT re-advertising (was T<n>)
disconnect {slot|client}       drop the target's central, keep the bond, park (was X)
park                           stop advertising, keep connections + bonds (was O)
advertise                      (re)start advertising the current slot (was A)
pair {client} / unpair {..}    assign / forget a client's identity slot
nickname {name}                get/set the board's advertised-name nickname
layout {name,slot|client}      get/set a slot's per-device keyboard layout (us/da/uk)
ping                           liveness + readiness: {device, subscribed, conn} (was P)
net_status                     Ethernet/TCP state: {eth,ip,port,mdns,token,clients} (was N)
provision {token}              set the network auth token -- SERIAL ONLY, refused over TCP (was W)
```

Async link/readiness state arrives unsolicited as an id-less JSON-RPC **notification**
`{"jsonrpc":"2.0","method":"event","params":{"event":"connected|disconnected|ready|unsubscribed","slot":N}}`
(`ready`/`unsubscribed` = the host wrote/cleared the report CCCDs). The host uses `event:"ready"` (or
`ping`'s `subscribed`) as the **readiness** signal so the first keystroke after (re)connect is not
dropped. (proto 1 firmware emitted these as plain-text `EVT ...` lines; the wheel still reads both.)

## Per-bond identity ("host slots")

Each board can present a **distinct BLE identity/address and a target-scoped name per bonded host**.
Slot 0 is the board's native address (an existing bond survives a firmware update); slots 1..N are
derived static-random addresses. This is what lets one board be bonded to several hosts and switch
between them deterministically -- the ESP32 remembers up to ten and can hold **two connected
simultaneously** on distinct addresses (route reports with the `target` method). See
[`../docs/bt-hid-mcu-approach.md`](../docs/bt-hid-mcu-approach.md).

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

## Network (Ethernet / WiFi) -- drive the ESP32 with no USB cable

An ESP32 with a wired Ethernet PHY (e.g. the **ESP32-Ethernet-Kit**) or WiFi runs the *same* firmware,
built with `-DHIDBRIDGE_NET` (Ethernet) or `-DHIDBRIDGE_NET -DHIDBRIDGE_WIFI` (WiFi), and additionally
serves the identical line + JSON-RPC protocol over **TCP 3333** with mDNS discovery
(`hidbridge-<nick>.local`), plus a self-contained control web page on :80. Same BLE bonds, same host
slots, same report bytes.

```bash
cd esp32
pio run -e esp32-ethernet-kit -t upload        # flash the network build (default esp32dev unchanged)
hidbridge net-provision --esp32-port /dev/ttyUSB3   # one-time: set the auth token over USB
hidbridge --backend net --host hidbridge-<nick>.local type "hello"
```

The TCP path is **TLS-PSK encrypted and fails closed**: the provisioned token is the pre-shared key,
so no token => no TLS port opens => no network HID, and the report bytes are encrypted on the wire.
The token is settable only over the physical serial cable. The control web page on :80 stays plain
HTTP (browsers cannot speak TLS-PSK) -- the one cleartext surface. PHY pins are build flags
(default = the ESP32-Ethernet-Kit's IP101GRI); override for another board. Full detail:
[`../docs/bt-hid-network-backend.md`](../docs/bt-hid-network-backend.md).

## Pairing

The MCU advertises on its own; pair from the **host's Bluetooth settings** (there is no host-side
BlueZ pairing for the MCU backend). Then drive it from the host, e.g.
`hidbridge --backend pico type "hello"`.

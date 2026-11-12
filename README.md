# hidbridge

> **Note:** `hidbridge` is a **temporary placeholder name** for this project pending a rename.
> It is used for the repo folder and the Python package throughout; expect it to change.

**Drive any Bluetooth-LE HID host from Linux.** Type text, press keys, move and click the pointer,
and enter a lock-screen passcode -- all programmatically from a Linux host, over a standard
HID-over-GATT (HOG) link that any BLE-HID central accepts (phones, tablets, laptops, desktops).

hidbridge presents the HID keyboard + relative pointer to the host one of two ways:

- **BlueZ backend (`dongle`)** -- the Linux host's *own* Bluetooth stack (a USB adapter). A small
  daemon exports a HID-over-GATT peripheral; the CLI drives it.
- **MCU backend (`pico` / `esp32`)** -- a **Raspberry Pi Pico 2 W** or an **ESP32** running its own
  BLE stack (firmware in [`firmware/`](firmware/)), reached over a **USB serial** link (or, on an
  Ethernet/WiFi ESP32, over the network). The MCU owns its BLE bonds, so it sidesteps the
  HID-over-GATT input-report subscription quirk (CCCD / Report-Reference) that the BlueZ path fights
  with some hosts -- it "just works" for keystroke/pointer delivery.

Both backends emit the **exact same report bytes** (from one report descriptor), so behaviour is
identical regardless of transport -- and the MCU firmware installs that descriptor byte-for-byte, a
parity test guards it.

## Architecture

```
   Linux host                         serial            MCU (Pico 2W / ESP32)      BLE
  +-------------------+          (USB, 115200)         +--------------------+
  |  hidbridge CLI    |  ---- JSON-RPC intent -------->|  BTstack / NimBLE  | ~~~ BLE HID ~~> any
  |  (hidbridge.*)    |  <---- result / EVT ready ------|  HOG peripheral    |               BLE-HID
  |  board builds     |    (the BOARD builds reports)   |  own bond database |                host
  +---------+---------+                                 +--------------------+
            |
            |  (alternative backend: the host's OWN BlueZ stack)
            v
  +-------------------+   D-Bus    +-----------+   BLE
  |  ble_hid daemon   |<---------->|  BlueZ    | ~~~ BLE HID ~~> host
  |  (HID-over-GATT)  |            |  adapter  |
  +-------------------+            +-----------+
```

## Headline features

- **One report layer, three transports.** Keyboard + relative pointer -- identical bytes over BlueZ,
  Pico, or ESP32.
- **Per-bond identities.** An MCU board presents a **distinct BLE address + a target-scoped
  advertised name per bonded host** ("host slots"), so it can be bonded to several devices and switch
  between them deterministically -- no re-pairing. The ESP32 remembers up to ten.
- **Simultaneous dual-hold (ESP32).** One ESP32 holds **two hosts connected at once** on distinct
  addresses (no time-slicing); it routes report frames to a chosen target.
- **Encrypted network path (ESP32).** An Ethernet/WiFi ESP32 also serves the same protocol over
  **TLS-PSK** on TCP (the provisioned token is the pre-shared key), plus a self-contained control
  web page.
- **Deterministic park / handoff.** Cleanly disconnect a target while keeping the bond, or park (stop
  advertising, keep links + bonds), for reliable cross-board / cross-host handoff.
- **Reset-free serial link.** The transport avoids the DTR/RTS auto-reset that would reboot a running
  MCU mid-session; a readiness handshake (`subscribed=` / `EVT ready`) means the first keystroke
  after (re)connect actually lands instead of being dropped in the host's post-connect activation
  window.
- **Multi-adapter / multi-daemon isolation.** Per-adapter socket + D-Bus object naming lets several
  BlueZ dongles run side by side (even the TP-Link UB500 defect that makes dongles share a BD
  address).

## Repository layout

```
hidbridge/
  README.md            <- you are here (what it is, architecture, quick start)
  LICENSE              MIT
  python/              the Python package + tests  (has its own README)
    pyproject.toml     buildable to a wheel; console_script `hidbridge`
    src/hidbridge/     the package
    tests/             pytest suite (offline by default; hardware tests skip)
  firmware/            the MCU firmware  (has its own README)
    pico/              Raspberry Pi Pico 2 W  (PlatformIO, BTstack HOG)
    esp32/             ESP32  (PlatformIO, NimBLE-Arduino)
  docs/                deep-dive notes (report format, MCU approach, multi-device, network path)
```

Why one repo? Python and firmware share a strict contract -- the HID report descriptor and the
serial wire protocol -- and a parity test asserts the firmware's report map equals the Python
descriptor byte-for-byte. Keeping them together makes that contract, and the whole project, easy to
review as a unit. The two halves still have independent toolchains (pip/pytest vs PlatformIO) in
separate subtrees, so a later split into `hidbridge-python` + `hidbridge-firmware` is
straightforward if desired.

## Install in your project (consumers)

Released wheels are published to **PyPI** as `hidbridge`. Install the extra for the backend you use:

```bash
pip install "hidbridge[mcu]"      # Pico / ESP32 over USB serial (pulls pyserial)
pip install hidbridge             # core only: reports + the net (TCP) backend, stdlib-only
pip install "hidbridge[dongle]"   # BlueZ backend; needs dbus + PyGObject (see python/README.md)
```

or with uv: `uv add "hidbridge[mcu]"`. Firmware images for the boards are attached to the
[GitHub Releases](https://github.com/martinmunk/hidbridge/releases) (`fw/release/<X.Y.Z>` tags).
The maintainer's private Forgejo registry carries the same versions; see
[`docs/releasing.md`](docs/releasing.md).

Then drive a target in a few lines (the board self-identifies from the location you pass):

```python
from hidbridge import HidBridge, session

# open the board (a serial ESP32 here), connect one bonded host, type into it:
with HidBridge("/dev/ttyUSB3") as bridge, bridge.connect("laptop3") as s:
    s.type("hello")
    s.key("enter")
    s.move(40, 0); s.click("left")

# single-host shorthand -- open board + connect + yield the session in one call:
with session("/dev/ttyUSB3", "laptop3") as s:
    s.type("hi")
```

Releasing new versions (the `wheel/release/*` / `fw/release/*` tag flow) is documented in
[`docs/releasing.md`](docs/releasing.md).

## Quick start

**MCU backend (no root, no daemon)** -- flash a Pico or ESP32 (see [`firmware/`](firmware/)), then:

```bash
cd python
pip install -e ".[mcu]"
hidbridge --backend pico type "hello world"
hidbridge --backend pico move 40 0
hidbridge --backend pico click left
```

**BlueZ backend (needs a USB BT adapter + root):**

```bash
cd python
pip install -e ".[dongle]"      # or: sudo apt install python3-dbus python3-gi
sudo hidbridge pair-mode        # advertise as a keyboard; pair from the host's Bluetooth settings
sudo hidbridge connect mini5
sudo hidbridge type "hello"
```

See [`python/README.md`](python/README.md) for the full CLI, the `targets` registry, and the
lock-screen passcode file, and [`firmware/README.md`](firmware/README.md) for building + flashing
the boards.

## Security / secrets

- The host **passcode PIN** is read at run time from `~/.config/bt_hid/idevice.pin` -- a `0600`
  file **outside** this repo, **never committed and never logged**. The passcode is expanded to HID
  frames locally; only frames go over the wire.
- The **network auth token** (also the TLS pre-shared key) lives in `~/.config/bt_hid/net.token`
  (`0600`, git-ignored), is provisioned onto the board over USB only, and is never logged.
- No tokens, credentials, captures, or real host identities are committed. `.gitignore` blocks
  `*.pin`, `*.token`, `targets.json`, and build artifacts.

## License

**MIT** (see [`LICENSE`](LICENSE)).

## Status

A self-contained copy of the BT-HID toolbox with no dependency on its source project. Development
happens on the maintainer's Forgejo, which is the source of truth; this GitHub repository is a push
mirror of `main` (please file issues here). CI runs on both: `.forgejo/workflows/` on Forgejo (an
offline gate on every push/PR, firmware compile checks, tag-driven releases to the private registry)
and `.github/workflows/` on GitHub (the offline test matrix, plus the public releases). The wheel
(`wheel/release/*` tags -> PyPI) and the firmware (`fw/release/*` tags -> GitHub Releases) release
**independently**, both starting at 1.0.0. See [`docs/releasing.md`](docs/releasing.md) for the
SemVer scheme and tag conventions, and [`PUBLISHING.md`](PUBLISHING.md) for the public half.

# hidbridge (Python)

The Python side of **hidbridge** -- a Linux toolbox that presents a Bluetooth-LE HID
keyboard + relative pointer to any BLE-HID (HOG) host and drives it programmatically: type text,
press keys, move/click the pointer, and enter a lock-screen passcode.

Two backends, one report layer:

- **dongle** -- the host's own **BlueZ** stack (a USB Bluetooth adapter). A small daemon
  (`ble_hid`) exports the HID-over-GATT peripheral; the CLI talks to it over a Unix socket.
  Needs `dbus` + `PyGObject` (system python) and usually root.
- **mcu** -- a **Raspberry Pi Pico 2 W** or an **ESP32** running the firmware in
  `../firmware/`, reached over **USB serial**. The MCU owns its own BLE stack + bond database,
  so it sidesteps the HID-over-GATT CCCD/Report-Reference subscription quirk that trips BlueZ with
  some hosts. Needs only `pyserial`; no dbus, no daemon, no root.
- **net** -- an **Ethernet/WiFi ESP32** running that same firmware, reached over **TCP** (no USB
  cable): `--backend net --host hidbridge-<nick>.local`. **TLS-PSK encrypted** and fails closed -- the
  provisioned token is the pre-shared key; set it on the board over USB once with
  `hidbridge net-provision`. Needs Python 3.13+ (ssl PSK). Stdlib-only (sockets + ssl); no pyserial,
  dbus, daemon, or root. See [`../docs/bt-hid-network-backend.md`](../docs/bt-hid-network-backend.md).

Both backends emit the **exact same report bytes** via `hidbridge.reports` (the single source of
truth for the HID report descriptor), so behaviour is identical regardless of transport.

## Install

**As a consumer** (released wheels are on PyPI):

```bash
pip install "hidbridge[mcu]"      # + pyserial for the Pico/ESP32 serial backend
pip install hidbridge             # core only: reports + the net (TCP) backend
```

Source, issues and firmware releases: <https://github.com/martinmunk/hidbridge>. The release flow
(wheel and firmware version independently; the git tag is the version):
[`docs/releasing.md`](https://github.com/martinmunk/hidbridge/blob/main/docs/releasing.md).

**From source** (this checkout):

```bash
# from this directory (hidbridge/python)
pip install -e ".[mcu]"        # Pico / ESP32 serial backend (pyserial)
pip install -e ".[dongle]"     # BlueZ backend (dbus-python + PyGObject; system python)
pip install -e ".[dev]"        # + pytest, ruff, build
```

The **dongle** backend must run under the **system python** (`/usr/bin/python3`) because
`dbus` + `AF_BLUETOOTH` sockets are only available there. On Debian/Ubuntu install the system
deps with apt instead of pip:

```bash
sudo apt install python3-dbus python3-gi
```

For a venv that still sees them, create it with `--system-site-packages`
(`uv venv --system-site-packages` or `python3 -m venv --system-site-packages`).

## Quick start

```bash
# MCU backend (a Pico on /dev/ttyACM0) -- no root, no daemon
hidbridge --backend pico type "hello world"
hidbridge --backend pico move 40 0
hidbridge --backend pico click left

# dongle backend (BlueZ) -- needs the daemon up + root
sudo hidbridge pair-mode                 # advertise as a keyboard; pair from the host's Bluetooth settings
sudo hidbridge connect mini5             # (re)connect a bonded target
sudo hidbridge type "hello"
sudo hidbridge unlock                    # types the passcode from ~/.config/bt_hid/idevice.pin (never logged)

# named targets (device -> backend map in ~/.config/bt_hid/targets.json)
hidbridge targets add labpad pico:/dev/ttyACM0 AA:BB:CC:DD:EE:FF
hidbridge --target labpad type "hi"
```

Run `hidbridge --help` for the full subcommand list.

## Python API

The driving surface is two context managers that mirror the hardware: a **`HidBridge`** is the board
channel; **`bridge.connect(name)`** advertises a bonded identity, waits until it is ready, and hands
back a **`HidSession`** you drive. You pass a *location* ("/dev/ttyUSB3", a net host, "hci1", or
nothing to autodetect) -- never a board type; the board self-identifies into `bridge.info` /
`bridge.capabilities`.

```python
from hidbridge import HidBridge

with HidBridge("/dev/ttyUSB3") as bridge, bridge.connect("laptop3") as s:
    s.type("hello world")
    s.key("enter")
    s.move(40, 0)          # relative pointer
    s.click("left")
    s.scroll(-3)
# leaving the `with` disconnects but keeps the bond -- no idle connections
```

The session verbs are the whole vocabulary: `type(text)`, `key(name, mod=0)`, `move(dx, dy)`,
`click(button="left")`, `scroll(clicks)`. Who builds the report bytes is a backend detail -- an MCU
board delegates each verb to the firmware (the target slot's keyboard layout applies board-side); a
dongle builds the bytes host-side. Either way the surface is identical. "Unlock an iPad" is just
typing the passcode: `s.type(pin); s.key("enter")`.

For the single-host case, `hidbridge.session(location, device)` opens the board, connects the one
device, and yields the session (tearing both down on exit):

```python
from hidbridge import session

with session("/dev/ttyUSB3", "laptop3") as s:
    s.type("hi")
```

Errors are typed under `HidbridgeError`: `NotBonded`, `ConnectTimeout`, `Unsupported`,
`CapacityError`, `IncompatibleFirmware`. Multiple sessions can be open at once on a board that
supports it (the ESP32 holds several identities); otherwise a second `connect()` raises. The wheel
version is `hidbridge.__version__`.

## The passcode PIN (secret, external)

`hidbridge unlock` (and the trust-overlay helper) read the device passcode from
`~/.config/bt_hid/idevice.pin` -- a `0600` file that lives **outside** this repo and is **never
committed or logged**. Create it yourself:

```bash
mkdir -p ~/.config/bt_hid && umask 077 && printf '%s' 1234 > ~/.config/bt_hid/idevice.pin
```

The passcode is expanded to HID frames locally; only frames are ever sent, never the string.

## Layout

```
python/
  pyproject.toml          PEP 621 metadata, console_script `hidbridge`, ruff + pytest config
  src/hidbridge/          the package
    reports.py            PURE HID report layer (stdlib only) -- the report-byte source of truth
    controller.py         backend-agnostic report driver (type/key/move/click/unlock/timeline)
    conn.py               HidSession: connect-on-demand context manager, readiness gate
    targets.py            device -> backend registry + per-bond identity slots
    naming.py             per-adapter runtime names (multi-daemon isolation) + slot names
    adapter.py            BlueZ adapter discovery / dongle selection (sysfs + dbus)
    ble_hid.py            the HID-over-GATT peripheral daemon (dongle backend)
    central.py / central_core.py   on-host BLE-central HID self-test (dongle/MCU parity)
    transport_pico.py / link_pico.py     Pico serial transport + link backend
    transport_esp32.py / link_esp32.py   ESP32 serial transport + link backend
    dismiss_trust.py      hands-free "Trust This Computer" overlay dismissal
    cli.py                the `hidbridge` command
    _scripts/             reference launcher + systemd unit + pair-mode/service shell helpers
  tests/                  pytest suite (offline by default; hardware tests skip)
```

## Tests

```bash
pytest                    # offline suite (hardware/live tests skip automatically)
```

Hardware-gated tests opt in via env vars, e.g. `BT_HID_LOOPBACK=1` (on-host BLE loopback,
needs two adapters + root) or `BT_HID_CENTRAL=1` (central self-test, needs the built-in adapter
+ an MCU). `*_live.py` tests need a real bonded host.

## Lint

```bash
ruff check .
```

The ruleset is deliberately scoped (see `pyproject.toml`): it enforces the pyflakes correctness
rules but tolerates this codebase's dense one-liner report/parser style.

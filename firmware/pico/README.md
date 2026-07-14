# wsb-hid-pico -- Raspberry Pi Pico 2 W BLE HID firmware

BLE HID firmware for a **Raspberry Pi Pico 2 W** (RP2350 + CYW43439), the low-cost **Pico backend**
for `bt_hid`. It presents four HID reports -- **keyboard (ID 1)**, **relative mouse (ID 2)**,
**absolute mouse (ID 3)**, and a **digitizer / touchscreen (ID 4)** -- advertises as
**`wsb-hid-pico`**, and lets this Linux host wake / unlock / type / move / click / tap-at-exact-
coordinates an iPad, iPhone, or Mac over Bluetooth LE -- an alternative to the USB Bluetooth dongle
backend.

The HID report map is installed **directly from `python/src/hidbridge/reports.py` `REPORT_DESCRIPTOR`**
(byte-identical -- a test guards the parity), NOT the arduino-pico auto-generated map. That is why
the relative mouse is report ID 2 here (the auto map would make it ID 3) and why the absolute mouse
(ID 3) and digitizer (ID 4) exist at all.

## Why a Pico (contributor hardware flexibility)

`bt_hid` needs *some* Bluetooth HID peripheral. The two supported backends are interchangeable, so
a contributor uses whichever hardware they already have:

- **`--backend dongle`** -- a USB Bluetooth dongle driven via Linux BlueZ.
- **`--backend pico`** -- a ~USD 6 Raspberry Pi Pico 2 W running this firmware.

The Pico is not just a cheaper option: its BTstack **HID-over-GATT (HOG)** stack (via the
earlephilhower arduino-pico core) implements HOG the **iOS-compatible way out of the box** --
including the CCCD / Report-Reference handling that the Linux BlueZ path currently gets stuck on
(iOS not subscribing to our input reports). So the Pico backend is expected to Just Work for
keystroke/pointer delivery where BlueZ fights us.

## Hardware needed

- A **Raspberry Pi Pico 2 W** (board id `rpipico2w`; RP2350 + CYW43439 wireless). The non-W Pico 2
  has no radio and will not work.
- A USB cable to this host (the same cable carries the serial command channel).

## Toolchain: PlatformIO

This is a **PlatformIO** project (`platformio.ini` + `src/main.cpp`). Install PlatformIO Core:

```
pip install platformio           # provides the `pio` CLI
```

First `pio run` downloads the platform (`maxgerhardt/platform-raspberrypi`), the arduino-pico core,
and the ARM toolchain automatically -- no manual SDK setup.

## Build + flash

```
cd firmware/pico
pio run                 # compile -> .pio/build/rpipico2w/firmware.uf2
pio run -t upload       # compile + flash over USB
```

`pio run -t upload` uses `picotool` and needs the Pico in **BOOTSEL** (hold BOOTSEL while plugging
in) plus udev permission for RP2350 in bootsel. If picotool cannot connect (a permissions error),
flash by **copying the UF2** to the BOOTSEL mass-storage drive instead -- this always works:

```
pio run                                             # build the .uf2
# hold BOOTSEL, plug in the Pico -> it mounts as an "RP2350" USB drive
sudo mount /dev/sdX1 /mnt/rp2350                     # (or let your desktop auto-mount it)
sudo cp .pio/build/rpipico2w/firmware.uf2 /mnt/rp2350/ ; sync
```

The Pico flashes, reboots into the app, disconnects the drive, and comes up as a USB-CDC serial
device (`/dev/ttyACM*`, by-id `usb-Raspberry_Pi_Pico_2W_*`) while BLE-advertising as `wsb-hid-pico`.

Watch its log:

```
pio device monitor -b 115200    # prints "READY name=wsb-hid-pico", EV lines, OK/ERR per report
```

## Serial command protocol (host <-> Pico)

The host ships **one HID report per line**; the Pico re-emits it as a BLE HID notification. This
keeps `python/src/hidbridge/reports.py` the single source of truth for the report bytes. Lines are ASCII,
`\n`-terminated:

| Line           | Meaning                                                                   |
|----------------|---------------------------------------------------------------------------|
| `K <16 hex>`   | keyboard report, 8-byte boot body `mod,resv,k1..k6` -> ID 1                |
| `M <8 hex>`    | relative mouse, 4-byte body `buttons,dx,dy,wheel` -> ID 2                  |
| `A <10 hex>`   | absolute mouse, 5-byte body `buttons,Xlo,Xhi,Ylo,Yhi` -> ID 3              |
| `D <12 hex>`   | digitizer/touch, 6-byte body `flags,contactID,Xlo,Xhi,Ylo,Yhi` -> ID 4     |
| `P`            | ping -> `PONG name=wsb-hid-pico conn=<0|1>`                                |

Reply per line: `OK` on success, `ERR <reason>` (`notconnected` / `badframe` / `unknowncmd` /
`toolong`). The firmware prepends only the report ID; the body bytes are exactly what
`reports.py` builds (`abs_mouse_report` / `digitizer_report` etc.), so `reports.py` stays the single
source of truth. X/Y are u16 little-endian in `0..32767` (a screen-fraction full scale).

The host side of this protocol is `python/src/hidbridge/transport_pico.py`; you normally never type these
lines by hand -- use the `bt_hid` CLI (below).

## One-time pairing (the maintainer, physical -- on the iPad)

While the Pico is powered it advertises `wsb-hid-pico` automatically (no daemon on the host). Pair
once:

1. On the iPad/iPhone: **Settings > Bluetooth**.
2. Under *Other Devices* tap **`wsb-hid-pico`**.
3. Approve the pairing prompt (**Pair**).

After that the bond is stored and the device reconnects on demand.

## Use it

```
bt_hid --backend pico status          # ping the Pico (PONG + connection state)
bt_hid --backend pico wake            # nudge the connected device awake
bt_hid --backend pico type "hello"    # type text
bt_hid --backend pico key enter
bt_hid --backend pico move 120 -40    # relative pointer move
bt_hid --backend pico click left
bt_hid --backend pico unlock 1234     # type passcode + Enter (never logged)
```

Pin a specific port with `--pico-port /dev/ttyACMx` or `BT_HID_PICO_PORT`. Autodetection is
**Pico-vendor-gated** (USB vendor `2e8a`, ttyACM only) so it never grabs an unrelated serial device
(e.g. the CH340 KVM controller on `/dev/ttyUSB*`).

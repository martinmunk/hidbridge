# Network HID backend -- an Ethernet-equipped ESP32 over TCP

Status: 2026-07-14. Drives the same NimBLE HID firmware as the USB `esp32` backend, but over the LAN
instead of a USB cable, so the host can wake / unlock / type / point at a bonded iPad with nothing but
a network path to the board. USB serial keeps working unchanged; the two channels are interchangeable
and may be used at once.

## Why / what

The ESP32-Ethernet-Kit (and any ESP32 with a wired PHY) has an Ethernet MAC alongside its BLE radio.
The firmware (`firmware/esp32/`, build env `esp32-ethernet-kit`, flag `-DHIDBRIDGE_NET`) brings the
PHY up over DHCP, advertises itself as `hidbridge-esp32.local` (mDNS), and serves the **exact same
newline-terminated line protocol** (`K`/`M`/`A`/`D` reports, `P`/`I`/`T`/`S`/`X`/`O`, plus the new
`N`/`W`/`@`) on **TCP 3333**. Because the wire protocol is identical, the host reuses the whole
`transport_esp32` command layer: `transport_net.NetTransport` is a thin subclass whose only additions
are opening a TCP channel (`socket_link.SocketLine`) instead of a tty and doing the auth handshake.
`reports.py` remains the single source of truth for the report bytes on the network path too.

```
  Linux host                              LAN               ESP32-Ethernet-Kit         BLE
 +-------------------+   TCP 3333   +----------------+
 | hidbridge --backend|<----------->| line server    |   NimBLE HOG  ~~~ BLE HID ~~> iPad / iPhone
 |   net --host ...  |  K/M/A/D +   | + Ethernet PHY |   (same bonds + host slots as the USB path)
 | transport_net.py  |  @auth       | (DHCP + mDNS)  |
 +-------------------+              +----------------+
         |  (or, unchanged, the USB serial path: transport_esp32 over /dev/ttyUSB*)
```

## Security model -- input injection over the network

A TCP port that streams HID reports into a bonded, possibly **unlocked** iPad (and can type its
lock-screen passcode via `unlock`) is a remote **input-injection** surface: anyone who can reach the
port could type into the target. So the network path is authenticated and **fails closed**:

- **Shared secret in NVS, set over USB only.** `W<token>` (serial command; `hidbridge net-provision`)
  stores a token in the board's flash. The firmware **refuses `W` over TCP**, so the secret can only
  be provisioned across the physical cable, and it is never echoed, logged, or readable back.
- **Every TCP session must authenticate.** A fresh connection may only ping (`P`/`V`/`N`) and send
  `@<token>`. Every report (`K`/`M`/`A`/`D`) and all control (`I`/`T`/`S`/`X`/`O`) is rejected with
  `ERR auth` until `@<token>` succeeds. The compare is constant-time; 5 failures drop the session.
- **Unprovisioned = no network HID.** With no token set, `@` can never succeed, so a board merely
  plugged into the LAN cannot be driven over it. `hidbridge net-provision` mints a strong random
  token into `~/.config/bt_hid/net.token` (0600, outside the repo, git-ignored) if you don't supply
  one, and writes the same token to the board -- so `--backend net` then just works.

**What this does NOT do:** the channel is not confidential. The report bytes -- **including the
passcode frames `unlock` emits** -- and the `@<token>` line itself cross the LAN in the clear. Treat
TCP 3333 as a **trusted-network-only** interface (a LAN/VLAN/VPN you control), and prefer the USB path
for `unlock` on an untrusted network. A TLS- or Noise-framed channel is the obvious next step if that
is not enough; the auth gate and fail-closed behaviour are in place to build on.

## Using it

```bash
# one-time, board on USB: set the auth token (writes ~/.config/bt_hid/net.token + the board's NVS)
hidbridge net-provision --esp32-port /dev/ttyUSB3

# then drive it with no USB cable -- address by mDNS name or IP
hidbridge --backend net --host hidbridge-esp32.local type "hello ipad"
hidbridge --backend net --host 192.0.2.42 move-abs 0.5 0.5
hidbridge --backend net --host hidbridge-esp32.local net-status   # link / IP / token / clients

# or via HidSession (interchangeable with the dongle / esp32 backends)
python -c 'from hidbridge.conn import HidSession
with HidSession("net:hidbridge-esp32.local", "the maintainer'\''s iPad") as h: h.type_text("hi")'
```

Token resolution order: `--token` > `BT_HID_NET_TOKEN` > `~/.config/bt_hid/net.token`. Host:
`--host` / `BT_HID_NET_HOST`, or the `net:<host>[:port]` target string.

## Firmware build + porting to another Ethernet board

```bash
cd firmware/esp32
pio run -e esp32-ethernet-kit                 # compile (default env esp32dev is unchanged)
pio run -e esp32-ethernet-kit -t upload       # flash over the FT2232H UART channel (ttyUSB3 on wsb)
```

The PHY wiring is board-specific and lives entirely in build flags (defaults = ESP32-Ethernet-Kit:
IP101GRI, MDC 23 / MDIO 18, PHY power GPIO5, 50 MHz RMII clock **in** on GPIO0). A wrong PHY/clock
config is the usual "no link light / never gets an IP". Override in `platformio.ini` rather than
editing `main.cpp`, e.g. Olimex ESP32-POE:
`-DHB_ETH_PHY_TYPE=ETH_PHY_LAN8720 -DHB_ETH_PIN_POWER=12 -DHB_ETH_CLK_MODE=ETH_CLOCK_GPIO17_OUT`.

The network build fits the default app partition (~900 KB / 1.31 MB). The serial-only `esp32dev`
image is byte-for-byte unaffected -- all network code is behind `#ifdef HIDBRIDGE_NET`, and the
firmware↔host report-map parity test still guards both.

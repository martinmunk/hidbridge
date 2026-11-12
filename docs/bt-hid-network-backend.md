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
port could type into the target, and anyone who can sniff the wire could read the passcode. So TCP
3333 is **TLS-PSK encrypted** and **fails closed**:

- **Shared secret in NVS, set over USB only.** `W<token>` (serial command; `hidbridge net-provision`)
  stores a token in the board's flash. The firmware **refuses `W` over TCP**, so the secret can only
  be provisioned across the physical cable, and it is never echoed, logged, or readable back.
- **The token IS the TLS pre-shared key.** Port 3333 speaks TLS 1.2 with the PSK cipher
  `PSK-AES128-GCM-SHA256` and no certificates -- the client proves it holds the token by completing
  the handshake, so **the secret never crosses the wire** and the report bytes (including passcode
  frames) are **encrypted**. There is no plaintext path: a non-TLS connection gets no reply. Once the
  handshake completes the session is authenticated; the legacy `@<token>` line is still accepted over
  the encrypted channel for the existing client flow but is redundant.
- **Unprovisioned = no network HID.** With no token set the board does not even open the TLS port, so
  a board merely plugged into the LAN cannot be driven over it. `hidbridge net-provision` mints a
  strong random token into `~/.config/bt_hid/net.token` (0600, outside the repo, git-ignored) if you
  don't supply one, and writes the same token to the board -- so `--backend net` then just works.

**Scope / caveats:** encryption is TLS-PSK, so security rests entirely on the token secrecy (it is
both the auth secret and the encryption key) -- provision a strong random one and keep
`~/.config/bt_hid/net.token` private. The client pins TLS 1.2 (mbedTLS on the ESP32 does PSK over
1.2; TLS 1.3 external-PSK is a different handshake). The **one** cleartext surface is the optional
control **webpage on :80** (browsers cannot do TLS-PSK) -- HTTP with an `X-Auth-Token` header,
LAN-only, and no passcode path uses it; skip the web build (or firewall :80) if that matters. Needs
Python 3.13+ on the host (`ssl.set_psk_client_callback`).

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
with HidSession("net:hidbridge-esp32.local", "My iPad") as h: h.type_text("hi")'
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

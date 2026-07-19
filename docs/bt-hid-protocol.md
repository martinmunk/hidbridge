# The host <-> MCU protocol: one versioned JSON-RPC 2.0 contract

The hidbridge MCU firmware (ESP32 / Pico) is driven over one channel (USB serial, or TCP on a
networked ESP32) by **one JSON-RPC 2.0 protocol**. The board builds every HID report AND owns device
management; the host sends **intent**. This is the entire contract between the Python wheel and the
firmware -- and it is **negotiated**, so the two version independently.

Since **proto 2** the wire is *uniformly* JSON-RPC: requests, replies, errors, and asynchronous
link/readiness state (id-less `event` **notifications**) are all JSON objects. A line that is not valid
JSON gets a JSON-RPC parse error (`{id:null}`). There are **no plain-text frames** on the wire.

> History: the firmware also used to accept a host-built `K<hex>`/`M<hex>` frame relay (the host
> builds the exact report bytes, the board is "just a radio") and a set of single-char DEVICE commands
> (`P A I T S N V W X O`). Both were folded into JSON-RPC and **removed**.
>
> **proto 1 -> 2 (breaking):** async state was emitted as plain-text `EVT ready`/`EVT connected`/
> `EVT disconnected`/`EVT unsubscribed` lines and errors as `ERR ...` lines; a TCP session
> additionally authenticated with a raw `@<token>` line (`OK auth`/`ERR auth`). proto 2 replaces the
> `EVT`/`ERR` frames with JSON (see below) and **removes the `@<token>` line entirely** -- the TLS-PSK
> handshake is now the sole network authentication. The Python wheel drives **both** proto 1 and
> proto 2 boards (it gates the event/auth format on `hello.proto`); `MIN_FIRMWARE_PROTO` stays 1.

## Wire format

One JSON object per line, newline-terminated ASCII:

```
request   {"jsonrpc":"2.0","id":<n>,"method":"<m>","params":{...}}
response  {"jsonrpc":"2.0","id":<n>,"result":{...}}
error     {"jsonrpc":"2.0","id":<n>,"error":{"code":<c>,"message":"<m>"}}
event     {"jsonrpc":"2.0","method":"event","params":{"event":"<e>","slot":<n>}}
```

A params `"dry":true` returns the built report bytes as hex in `result.frames` WITHOUT notifying (the
firmware<->`reports.py` byte-parity guard uses this). Over TCP the channel is TLS-PSK encrypted (the
provisioned token is the pre-shared key) and the completed handshake **is** the authentication -- there
is no app-layer auth line; over serial every line is trusted.

### Async `event` notifications (proto 2)

Link and readiness state is broadcast as an **id-less JSON-RPC notification** (not a reply to any one
command) to serial + every authenticated network client + the WS control page:

```
{"jsonrpc":"2.0","method":"event","params":{"event":"connected",   "slot":N}}
{"jsonrpc":"2.0","method":"event","params":{"event":"disconnected","slot":N}}
{"jsonrpc":"2.0","method":"event","params":{"event":"ready",       "slot":N}}   # iOS enabled the report CCCDs
{"jsonrpc":"2.0","method":"event","params":{"event":"unsubscribed","slot":N}}
{"jsonrpc":"2.0","method":"event","params":{"event":"net","link":true,"ip":"1.2.3.4"}}  # net bring-up (serial)
```

`event:"ready"` is the readiness signal a host should await before the first keystroke (some hosts drop
reports in a post-connect activation window). The wheel's JSON-RPC demux skips notifications when
matching a reply (exposing the latest as `RpcClient.last_event`); the `board.McuBoard` readiness gate
polls `identities()` for the target slot's `sub` flag, so it does not depend on catching the event.

## The `hello` handshake (version negotiation)

`hello` is always allowed (unauthenticated) and self-identifies the board:

```
result: { "proto": <int>, "device": "HIDBRIDGE-esp32", "nick": "...", "name": "...", "fw": "...",
          "modes": ["frames","rpc"], "layouts": ["us","da","uk"],
          "caps": [ ...every supported method/feature... ]  (+ "mdns" on a net build) }
```

- **`proto`** -- an integer protocol version. The host does **not** branch on it; it is a floor.
- **`caps`** -- the COMPREHENSIVE list of methods/features this firmware supports. The host
  **feature-detects on `caps`**: it checks a cap BEFORE calling an optional method, and treats caps it
  does not recognise as inert.

The Python `HidBridge.__enter__` reads `proto` into `bridge.info` and `caps` into
`bridge.capabilities`, then applies the compat policy below.

## Method vocabulary

| Method | Params | Result / effect | Cap |
|--------|--------|-----------------|-----|
| `hello` | -- | proto + caps (above) | always |
| `type` | `{text}` | build+notify keystrokes for `text` (target slot's layout) | `keyboard` |
| `key` | `{name,mod}` | one named key (or single char) + modifier | `keyboard` |
| `wake` | -- | a benign wake keystroke (space) | `keyboard` |
| `move` | `{dx,dy}` | relative pointer move | `mouse` |
| `click`/`press`/`release` | `{button}` | click / hold / release (click-and-drag) | `mouse` |
| `scroll` | `{clicks}` | wheel | `mouse` |
| `frames` | `{frames:[hex,...]}` | relay PRE-BUILT report frames (dry-run shape) | `frames` |
| `identities` | -- | host-slot table `[{slot,client,addr,name,conn,sub,bonded,layout}]` | `connect` |
| `connect` | `{slot|client}` | bring an identity up + advertise + claim it (was `I<n>`) | `connect` |
| `target` | `{slot|client}` | claim a slot as send-target, no re-advertise (was `T<n>`) | `target` |
| `disconnect` | `{slot|client}` | drop central, keep bond, park (was `X`) | `connect` |
| `pair`/`unpair` | `{client}` | assign / forget a client's identity slot | `connect` |
| `nickname` | `{name}` | get/set the advertised-name nickname | `nickname` |
| `layout` | `{name,slot|client}` | get/set a slot's per-device keyboard layout | `layout` |
| `advertise` | -- | (re)start advertising the current slot (was `A`) | `advertise` |
| `park` | -- | stop advertising, keep connections + bonds (was `O`) | `park` |
| `ping` | -- | liveness + readiness `{device,subscribed,conn}` (was `P`) | `ping` |
| `net_status` | -- | Ethernet/TCP state `{eth,ip,port,mdns,token,clients}` (was `N`) | `net_status` |
| `provision` | `{token}` | set the network auth token -- **SERIAL ONLY** (was `W`) | `provision` |

The ESP32 (NimBLE) advertises all of these (`provision` only on the `-DHIDBRIDGE_NET` build); the Pico
(single-connection BTstack wrapper) advertises `keyboard mouse slots connect target advertise park
layout ping` and addresses slots by index.

### The `provision` serial-only gate (security)

`provision` sets the network auth token (== the TLS pre-shared key). The firmware **honors it only on
the serial channel and REFUSES it over TCP/TLS and over the WS control channel** (error code -32001,
"provision is serial-only"). So the secret can only be set across the physically-attached cable -- an
already-authenticated network peer cannot rotate it. The token is never echoed back. This preserves
the exact gate the old `W<token>` command had.

## Compat policy: the wheel and the firmware version independently

- The protocol is **ADDITIVE**: methods and result fields are **never repurposed or removed** in place.
  A new optional feature is announced by **adding a `caps` entry** -- `proto` is left UNCHANGED.
- `proto` is **incremented only on a BREAKING change** (a method/field whose meaning changed, or a
  removal). The Python wheel declares `bridge.MIN_FIRMWARE_PROTO`, the oldest firmware it supports; a
  board whose `proto` is below that is refused with the typed `IncompatibleFirmware`.
- The host **feature-detects on `caps`**, never on the version number. Before an optional method it
  checks the cap (and translates a firmware "method not found" reply the same way), raising
  `IncompatibleFirmware` with a clear "upgrade the firmware or pin an older wheel" message -- **never a
  silent no-op**.
- **New wheel + old firmware** degrades cleanly (caps/proto refuse what the board can't serve).
  **Old wheel + new firmware** ignores caps it does not know. Both directions are covered offline in
  `test_bt_hid_bridge.py` (a fake low-proto / trimmed-caps / method-not-found board).

`reports.py` and the HID **report descriptor** are firmware<->target internals (and the dongle
backend's own host-side path) -- they are **NOT** part of this host<->MCU contract. That is exactly why
the wheel no longer depends on firmware report internals, and why the wheel and the firmware image can
ship on separate cadences.

> Firmware note: `firmware/shared/hidbridge_rpc.h` is the shared native C++ report layer + JSON-RPC
> dispatch, a deliberate duplicate of `reports.py` guarded against drift by
> `test_bt_hid_rpc.py`. The per-board slot/identity + network methods are firmware-specific `custom`
> handlers (`esp32Custom` / `picoCustom`). `hbmode1::PROTO_VERSION` is the `proto` the firmware
> reports (**now 2** -- the uniform-JSON wire); bump it only on a breaking change (add a cap for an
> additive one).

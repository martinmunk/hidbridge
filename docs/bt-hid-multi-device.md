# bt_hid multi-dongle / multi-iOS-device support

Status: review-draft (branch `bt-hid-multi-device`, do NOT merge to main yet). Offline suite green
(`pytest python/tests/`), ruff clean, live-verified on wsb 2026-07-10 (per-adapter isolation +
SIGTERM cleanup on a CSR dongle, coexisting with the hci1 daemon).

This document is the architecture + justification, the same-MAC (UB500) detection, the honest
per-device-BLE-identity feasibility verdict, and the runbook for multiple dongles to multiple iPads.

---

## 1. The problem (what bit us the night of 2026-07-10)

- **Duplicate-MAC dongles.** The two TP-Link UB500 (RTL8761B, USB `2357:0604`) dongles on wsb SHARE
  the BD address `E8:48:B8:C8:20:00` -- a known UB500 V1 defect. The address is NOT runtime-
  reprogrammable (TP-Link confirms). With BOTH plugged in, BlueZ + the kernel index adapters and
  connections partly by BD_ADDR, so routing corrupts and the iPad never recognises a stable HID
  peripheral.
- **Multi-adapter confusion.** The old `adapter.select_adapter` just took "the lowest-index USB
  dongle"; with 4 dongles present (2x UB500 + 3x CSR, minus the built-in) there was no deterministic
  way to target a SPECIFIC (dongle <-> iPad) pairing.
- **Single-daemon design.** `ble_hid` hard-coded ONE control socket (`/run/bt_hid.sock`), ONE D-Bus
  application path (`/org/martinmunk/bthid`), ONE agent path, ONE advertised name. Two daemons
  COLLIDE on the system bus + the socket, so you could not run two dongles to two iPads at once.
- **SIGTERM socket leak.** The daemon only cleaned up on SIGINT (Ctrl-C); `systemctl stop` /
  `timeout` / `pkill` send SIGTERM, which killed it without unlinking the socket -- a stale socket
  then looks like a live daemon.

---

## 2. Architecture decision: host bluetoothd, scoped per-adapter (NOT a private bluez instance)

**Decision: use the HOST `bluetoothd` via D-Bus, and scope EVERY operation to the target adapter
object `/org/bluez/hciN`.** `HidSession` (and each `ble_hid` daemon) does NOT run its own bluez.

Justification:

- Every BlueZ D-Bus object is ALREADY per-adapter: the adapter (`Adapter1`), its advertising manager
  (`LEAdvertisingManager1`), its GATT manager (`GattManager1`), and its devices
  (`/org/bluez/hciN/dev_*`). Scoping a daemon/session to one adapter is just "only ever address
  objects under `/org/bluez/hciN`". That is what the code does.
- A **private bluetoothd instance is not viable**: a second system `bluetoothd` cannot own the
  `org.bluez` bus name (only one owner), and a kernel HCI adapter is claimed by the one running
  daemon. You would have to detach the adapter from bluetoothd and drive raw HCI/mgmt yourself,
  losing the GATT server + pairing plumbing we depend on. No upside, large cost.
- So host-daemon-scoped is both the SIMPLEST and the only clean option. The only real BlueZ
  limitation it inherits is the single default-agent slot (see 4.3), which does not affect steady-
  state concurrent use.

What is per-adapter now (all derived from the adapter slug = `hciN`, see `python/src/hidbridge/naming.py`):

| artifact            | before (single)                | after (per-adapter)                     |
|---------------------|--------------------------------|-----------------------------------------|
| control socket      | `/run/bt_hid.sock`             | `/run/bt_hid/hci1.sock`, `hci2.sock`... |
| D-Bus app path      | `/org/martinmunk/bthid`        | `/org/martinmunk/bthid/hci1`, `/hci2`   |
| D-Bus agent path    | `/org/martinmunk/bthid/agent`  | `/org/martinmunk/bthid/hci1/agent`      |
| advertised name     | `wsb-hid-kbd`                  | `wsb-hid-hci1`, `wsb-hid-hci2` (default)|

Why `hciN` is the slug, not the BD address: the two UB500s SHARE a BD address, so the address is not
unique; the kernel still assigns each physical dongle a DISTINCT `hciN`. Runtime artifacts live in
`/run` (per-boot) and on the bus (per-session), and `hciN` is stable + collision-free within a boot.
Operators still TARGET a dongle by a stable id (MAC / USB path / product / hciN) via
`adapter.resolve_dongle`, which resolves to the concrete `hciN` at runtime.

---

## 3. Same-MAC detection (the UB500 trap)

`adapter.duplicate_address_groups()` returns `{BD_ADDR: [hciA, hciB, ...]}` for every address shared
by more than one present adapter. `adapter.preflight()` folds that into a report with loud warnings,
surfaced by `bt_hid preflight` (exit code 3 on a collision) and flagged in `bt_hid adapters`.

```
$ bt_hid preflight
bt_hid preflight -- adapter audit
  hci0  builtin  AA:BB:CC:DD:EE:02
  hci1  dongle   E8:48:B8:C8:20:00  TP-Link UB500 Adapter
  hci2  dongle   E8:48:B8:C8:20:00  TP-Link UB500 Adapter
...
WARNINGS:
  - DUPLICATE BD ADDRESS E8:48:B8:C8:20:00 shared by hci1, hci2 -- BlueZ/kernel routing can corrupt
    and the iPad may never recognise the HID dongle. Run ONLY ONE of these at a time ...
PREFLIGHT FAILED (duplicate-address hazard present)
```

Targeting a dongle by the SHARED address is itself rejected: `resolve_dongle("E8:48:B8:C8:20:00")`
raises `AmbiguousDongleError` (it matches both UB500s). Disambiguate with the unique `hciN` or a USB
path substring (`resolve_dongle("3-2")`).

Guidance: for CONCURRENT multi-device use, use dongles with DISTINCT MACs (the CSR `0a12:0001`
dongles each have their own MAC). Keep at most ONE UB500 of a duplicate pair powered at a time.

---

## 4. Running multiple dongles to multiple iPads

### 4.1 Bring-up (one daemon per dongle)

```
# audit first
python/src/hidbridge/bt_hid preflight

# a persistent daemon per adapter (instance = the hciN):
sudo python/src/hidbridge/service.sh install hci2      # daemon on hci2, advertises wsb-hid-hci2
sudo python/src/hidbridge/service.sh install hci3      # daemon on hci3, advertises wsb-hid-hci3
# or a throwaway foreground daemon:
BT_HID_ADAPTER=hci2 python/src/hidbridge/pair-mode.sh
```

Each daemon binds its own `/run/bt_hid/hciN.sock`, exports `/org/martinmunk/bthid/hciN`, and
advertises `wsb-hid-hciN` -- so they never collide. Verified live: an `hci2` daemon ran alongside
the existing `hci1` daemon with no D-Bus/socket conflict.

### 4.2 Pairing (one iPad per dongle)

Bring up the daemon for the dongle, then on that iPad open Settings > Bluetooth and tap
`wsb-hid-hciN`. `bt_hid --adapter hciN devices` lists what is bonded on that dongle. Because the
CLI now resolves the PER-ADAPTER socket from `--adapter`, `bt_hid --adapter hci2 type ...` reaches
the hci2 daemon, not a global one.

### 4.3 The single default-agent caveat (pair one dongle at a time)

BlueZ has ONE default-agent slot per system bus, and each daemon's auto-accept agent is scoped to
its own adapter (it REJECTS pairings on other adapters, so we never authorise something on the
built-in). Consequences:

- **Pairing** (a one-time bond) needs the relevant daemon to hold the default agent. Only the LAST
  daemon that called `RequestDefaultAgent` holds it. So PAIR one dongle's iPad at a time -- start /
  restart that daemon last (it grabs the slot), tap Pair on its iPad, done.
- **Steady-state concurrent use is unaffected**: connecting an ALREADY-bonded device needs no agent,
  so dongleA<->iPadA and dongleB<->iPadB run concurrently with no agent contention.

The daemon logs whether it holds the default agent; a non-holder logs a clear hint instead of
failing.

### 4.4 One dongle, several iPads: `HidSession` connect-on-demand

`python/src/hidbridge/conn.py` `HidSession` bonds several iPads to one dongle but CONNECTS only the one in
use, disconnecting on exit (the bond stays):

```python
from bt_hid.conn import HidSession

with HidSession("hci1", "My iPad") as hid:   # enter: connect that iPad on hci1
    hid.unlock("1234")
    hid.type_text("hello")
    hid.send_key("enter")
# exit: that iPad is DISCONNECTED (bond intact); next time reconnect is instant
```

Enter resolves the dongle, checks its daemon is up, disconnects any OTHER connected central
(exclusive, there is one HID link per adapter), connects the target, and returns a driver whose
report calls go to that daemon's per-adapter socket. Exit calls `Device1.Disconnect()` -- never
`RemoveDevice`, so the bond is kept.

BLE reality: we are the PERIPHERAL, the iPad is the CENTRAL -- we cannot page it. "Connect on demand"
therefore = keep advertising (the daemon does) + disconnect others + nudge `Device1.Connect()` +
WAIT for the iPad to reconnect to our advertisement. `require_connect=False` proceeds without the
wait (frames just queue until the central subscribes).

---

## 5. Backend-agnostic `HidSession` (dongle OR MCU behind the same API)

`HidSession(target, ios_device)` selects a BACKEND from `target`, mirroring the report-layer
`make_transport("dongle"/"pico"/"esp32")` pattern:

| target                    | backend                                              |
|---------------------------|------------------------------------------------------|
| `"hci1"` / MAC / USB-path / `None` | BlueZ dongle (auto-picks if `None`)         |
| `"dongle:hci1"`           | BlueZ dongle, explicit                                |
| `"pico:/dev/ttyACM0"`     | MCU over serial (its own BLE stack)                  |
| `"esp32:/dev/ttyUSB1"`    | MCU over serial                                       |
| `"<name>:<arg>"`          | any future `bt_hid.link_<name>` backend              |

A small `LinkBackend` interface (`prepare / connect / disconnect / is_connected / transport /
capabilities / describe`) expresses the connect-on-demand semantics for BOTH a BlueZ dongle
(`DongleLinkBackend`) and an MCU. The MCU work (branch `bt-hid-mcu-robust`) supplies
`bt_hid.link_pico` / `link_esp32` conforming to this interface; until then a generic
`SerialMcuLinkBackend` wraps the existing `transport_<name>` modules so `HidSession("pico:...")`
already relays reports. **The MCU subagent conforms to THIS `HidSession` / `LinkBackend`
definition.**

---

## 6. Per-iPad distinct BLE identity -- feasibility verdict (researched, honest)

Question (the maintainer): give ONE physical dongle a SEPARATE BLE address per associated iPad, to sidestep
the duplicate-MAC problem and self-heal without extra hardware. "Maybe maybe not."

**Verdict for the BlueZ dongle backend: NOT feasible via the stock stack, and it would NOT fix the
duplicate-MAC problem. Do not pursue it on BlueZ.** Reasons (BlueZ 5.82, RTL8761B, verified by D-Bus
introspection on wsb 2026-07-10):

1. **No per-advertisement address in the D-Bus API.** `LEAdvertisement1` exposes only
   `Type / ServiceUUIDs / LocalName / Appearance / Discoverable / IncludeTxPower / ...` -- there is
   NO address (public/random/static) property. You cannot ask BlueZ to advertise a chosen address
   per registered advertisement. (`SupportedInstances=3` allows 3 concurrent adv SETS, but not a
   per-set address via D-Bus.)
2. **One adapter = one bonding identity.** The adapter has a single identity address (here public
   `E8:48:...`) and BlueZ manages ONE local IRK per adapter. LE Secure-Connections bonding exchanges
   the IDENTITY address; a peer using resolvable private addresses resolves via the single local IRK
   to that one identity. So even distinct advertising addresses would resolve to ONE bond identity --
   every iPad still bonds the same dongle identity. Distinct advertising !=  distinct identity.
3. **It would not fix duplicate-MAC anyway.** The UB500 collision is two PHYSICAL adapters both
   claiming the same PUBLIC BD_ADDR at the HCI/kernel level; the kernel routes on that. LE random
   advertising addresses do not change the underlying public identity the kernel indexes on, and the
   public address is not runtime-reprogrammable on UB500 V1.
4. **The only ways to get true per-peer identity on BlueZ** are to patch `bluetoothd`, or to detach
   the adapter from bluetoothd and drive raw HCI/mgmt (each adv set with its own random address +
   your own per-peer IRK/bond database) -- which throws away the GATT server + pairing plumbing we
   rely on. Not sensible for this project.

**The MCU route IS the clean way to the SAME capability.** An MCU (Pico/ESP32) runs its own BLE
stack in firmware, so it can pick the advertising address AND keep its own bond database per peer --
i.e. present a distinct identity to each iPad from one physical board. So we treat "distinct identity
per iPad" as a per-backend CAPABILITY, not a dongle feature: `HidSession.supports(
"per_device_identity")` is `False` for the dongle backend and can be `True` for an MCU backend whose
firmware implements it (the `bt-hid-mcu-robust` branch owns that firmware work). Two routes to one
capability: BlueZ = no, MCU = yes.

Practical recommendation for the duplicate-MAC hardware today: detect + warn (done), run at most one
UB500 of a duplicate pair at a time, and use the distinct-MAC CSR dongles (or an MCU) for concurrent
multi-device.

---

## 7. Code map

- `python/src/hidbridge/naming.py` -- pure per-adapter names (socket / app path / agent path / default name).
- `python/src/hidbridge/adapter.py` -- `duplicate_address_groups`, `resolve_dongle` (hciN/MAC/USB/product,
  raises `AmbiguousDongleError` on a shared id), `adapter_slug`, `preflight`.
- `python/src/hidbridge/ble_hid.py` -- per-adapter D-Bus app/agent/socket/name; best-effort default agent;
  SIGTERM/SIGINT clean shutdown.
- `python/src/hidbridge/conn.py` -- `HidSession` + `LinkBackend` / `DongleLinkBackend` /
  `SerialMcuLinkBackend` + `make_link_backend`.
- `python/src/hidbridge/cli.py` -- `preflight` subcommand; per-adapter socket resolution; `adapters` shows
  addresses + duplicate flag.
- `python/src/hidbridge/service.sh` + `bt-hid.service.in` -- systemd instance is the ADAPTER (`bt-hid@hciN`).
- Tests: `python/tests/test_bt_hid_multi_adapter.py`, `test_bt_hid_naming.py`,
  `test_bt_hid_hidsession.py` (offline), `test_bt_hid_multi_daemon_live.py` (device-gated:
  `sudo BT_HID_LIVE_ADAPTER=hciN ...`).

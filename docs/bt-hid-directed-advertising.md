# BLE directed advertising (ADV_DIRECT_IND) -- feasibility for the shared-identity model

Status: 2026-07-18. Research spike on branch `feat/directed-advertising`. Findings are from reading
the vendored stack sources (BTstack 1.6.2 shipped by arduino-pico, NimBLE-Arduino 2.x under
`.pio/libdeps/`) plus primary-source web corroboration. NOT verified on live hardware (build-only per
the spike's guardrails -- the boards are in use).

## Why we looked

Today each remembered device gets its OWN BLE identity (a distinct advertiser address = a "slot";
the ESP32 remembers ten). Advertising slot N with undirected `ADV_IND` means only the device bonded
to address N reconnects; the others never see their address, so they stay quiet. That isolation is
WHY we use distinct identities (see `docs/bt-hid-mcu-approach.md`).

The alternative evaluated here: ONE shared BLE identity, many bonds, and **directed advertising**
(`ADV_DIRECT_IND`) aimed at a specific bonded peer, so only that one reconnects and the other bonded
hosts are not disturbed. The make-or-break question is whether directed advertising works against a
modern host that connects with a **Resolvable Private Address (RPA)** -- the peer's address rotates,
so the peripheral must put the peer's CURRENT RPA in the directed PDU's TargetA field. Only the
controller can do that, from the bond's IRK via its **resolving list** (LL Privacy). Directing to a
fixed identity address is the historical footgun: an RPA-using host ignores it.

## Verdict at a glance

| Stack | Directed adv API | High + low duty | RPA-capable out of the box | Net |
| --- | --- | --- | --- | --- |
| Pico / BTstack 1.6.2 (CYW43439) | Yes -- `gap_advertisements_set_params` | Yes (adv_type 1 and 4) | **No** -- resolving list compiled OUT of the core | API present, RPA blocked by the vendored build config |
| ESP32 / NimBLE-Arduino 2.x (classic ESP32) | Yes -- `NimBLEAdvertising::start(dur, &peer)` | Low only via the wrapper | **No** -- classic ESP32 controller has no resolving list | Cleanly buildable, but not RPA-viable on this silicon |

Neither stack gives RPA-directed advertising to a modern (iOS/Android/macOS) host without work
outside the project tree. The per-slot identity model remains the correct approach for now.

## Pico / BTstack 1.6.2

BTstack drives directed advertising through the SAME call the per-slot path already uses:

```c
void gap_advertisements_set_params(uint16_t adv_int_min, uint16_t adv_int_max, uint8_t adv_type,
    uint8_t direct_address_type, bd_addr_t direct_address, uint8_t channel_map, uint8_t filter_policy);
```
(`src/gap.h`). The per-slot path passes `adv_type = 0` (`ADV_IND`) with a null `direct_address`
(`firmware/pico/src/main.cpp::select_slot_advertise`). Directed advertising just changes two args:

- `adv_type = 1` -- `ADV_DIRECT_IND`, HIGH duty cycle (fast, <= 1.28 s), or
- `adv_type = 4` -- `ADV_DIRECT_IND`, LOW duty cycle;
- `direct_address_type` + `direct_address` -- the peer's identity address.

Both duty cycles are supported: `hci.c::hci_le_advertisements_set_params` stores `adv_type` verbatim
and `hci.c` (~line 6578) passes it straight to the HCI `LE Set Advertising Parameters` command. Both
duty-cycle symbols are present in the precompiled core (`gap_advertisements_set_params` is in
`liblwip-bt.a`, verified with `arm-none-eabi-nm`).

**RPA: the API exists but is compiled out.** BTstack has a full controller resolving list --
`gap_load_resolving_list_from_le_device_db()`, `gap_set_peer_privacy_mode()` (`src/gap.h`) -- and per
BTstack's own docs the **CYW43439 radio supports controller privacy address resolution**, so on paper
the Pico is the more promising stack. BUT the whole resolving-list state machine in `hci.c` is guarded
by `#ifdef ENABLE_LE_PRIVACY_ADDRESS_RESOLUTION`, and arduino-pico's vendored
`framework-arduinopico/include/btstack_config.h` defines only `ENABLE_LE_PERIPHERAL` +
`ENABLE_LE_CENTRAL` -- NOT that macro. BTstack ships precompiled into `liblwip-bt.a`, so:

- `gap_load_resolving_list_from_le_device_db` is **absent** from every core archive (verified with
  `nm` across all `framework-arduinopico/lib/*/*.a`) -- calling it would fail to link;
- a project-level `-D` in `platformio.ini` cannot re-enable it (the object was already compiled
  without the define).

So the Pico can directed-advertise to a FIXED identity address today, but RPA targeting needs the
arduino-pico core rebuilt with `ENABLE_LE_PRIVACY_ADDRESS_RESOLUTION` (and `gap_load_resolving_list_*`
called after bonds are loaded). That is a core-package change, out of scope for a project build.

Also note: `PicoBluetoothBLEHID` owns advertising, but `gap_advertisements_set_params` is a global
BTstack call the firmware already reaches directly, so the wrapper is not an obstacle.

## ESP32 / NimBLE-Arduino 2.x (classic ESP32, esp32-ethernet-kit)

NimBLE-Arduino exposes directed advertising cleanly on `NimBLEAdvertising`:

```cpp
bool start(uint32_t duration = 0, const NimBLEAddress* dirAddr = nullptr);
bool setConnectableMode(uint8_t mode);   // BLE_GAP_CONN_MODE_DIR == 1
```
(`src/NimBLEAdvertising.h`). `start()` forwards `dirAddr` as the `peer_addr` argument of
`ble_gap_adv_start(ownAddrType, peer_addr, duration, &m_advParams, cb, this)`
(`src/NimBLEAdvertising.cpp` ~line 226). With `conn_mode = BLE_GAP_CONN_MODE_DIR`, the host maps it to
`BLE_HCI_ADV_TYPE_ADV_DIRECT_IND_LD` (0x04) in `ble_gap.c::ble_gap_adv_type`. The peer address is
copied straight into the HCI `LE Set Advertising Parameters` `peer_addr` field
(`ble_gap.c::ble_gap_adv_params_tx`) -- the host does NO RPA substitution for TargetA.

Caveats:

- **Low duty only via the wrapper.** `ble_gap_adv_params.high_duty_cycle` selects
  `ADV_DIRECT_IND_HD` (0x01), but `NimBLEAdvertising` never sets that bit, so the wrapper only reaches
  the low-duty variant. High duty needs poking `m_advParams` directly.
- **No RPA on the classic ESP32.** This is the make-or-break failure. (a) NimBLE-Arduino compiles in
  NO privacy: `MYNEWT_VAL(BLE_HOST_BASED_PRIVACY)` is undefined here (grep of `syscfg.h` +
  `nimconfig.h`), so the entire RPA branch of `NimBLEDevice::setOwnAddrType` is `#if`-d out and
  `ble_host_rpa_enabled()` is false. (b) The classic ESP32 CONTROLLER has no resolving list at all --
  NimBLE-Arduino's own source comments "esp32 controller does not support RPA" (`NimBLEDevice.cpp`
  ~line 1131), and Espressif documents `esp_ble_gap_add_device_to_resolving_list()` as "currently not
  supported on the ESP32." So the peer identity address goes on air as TargetA with no RPA generation:
  a peer that bonded with a FIXED identity would reconnect, but an iOS/modern host on a rotating RPA
  never answers. Newer parts (ESP32-S3/C3/C6) DO support the resolving list per Espressif, so this
  verdict is specific to the classic ESP32 on the bench (esp32-ethernet-kit).

## The RPA rule, stated plainly

`ADV_DIRECT_IND` puts TargetA = the intended recipient on air. A modern host connects with a
Resolvable Private Address that rotates on its own schedule; it only answers a directed advert whose
TargetA it can resolve to itself with its local IRK. The peripheral therefore has to emit the peer's
CURRENT RPA in TargetA, which only the CONTROLLER can generate -- from the bond's peer IRK held in the
controller's resolving list, with address resolution enabled. No resolving list => you can only target
a fixed identity address => a modern host ignores it. That is the single fact that sinks both stacks
here: BTstack has the machinery but arduino-pico ships it compiled out; the classic ESP32 controller
lacks the machinery entirely.

## What was prototyped (build-only, OFF by default)

Both spikes are behind `-DHIDBRIDGE_DIRECTED_ADV`, wired into a dedicated opt-in env, and kept
entirely OFF the per-slot path so the shipped firmware is byte-for-byte unchanged. They prove the
directed-adv APIs compile + link; they were NOT flashed and are NOT RPA-capable (see above).

- **ESP32** (`firmware/esp32`): `startDirectedAdvToPeer()` + a `Y<n>` command that directed-advertises
  to bonded peer index n. Build: `pio run -e esp32-directed-adv`.
- **Pico** (`firmware/pico`): `directed_advertise_to()` + a `Y<12hex>` command that directed-advertises
  (`ADV_DIRECT_IND`, high duty) to a given peer identity address. Build:
  `pio run -e rpipico2w-directed-adv`.

## Recommendation

Keep the per-slot identity model. If shared-identity + directed advertising is ever wanted for its
isolation benefit, the Pico/BTstack path is the one to pursue -- the CYW43439 supports controller RPA
resolution -- but it requires rebuilding the arduino-pico core with
`ENABLE_LE_PRIVACY_ADDRESS_RESOLUTION` and loading the resolving list from the bond DB, then a live
iOS reconnect test. The classic ESP32 cannot do it at all; an ESP32-S3/C3/C6 would need re-checking
against its (supported) resolving list.

## Sources

- BTstack directed adv + resolving list: `src/gap.h`, `src/hci.c` (vendored 1.6.2), and
  <https://github.com/bluekitchen/btstack/blob/master/CHANGELOG.md> (43439 controller privacy support).
- NimBLE directed adv + RPA on ESP32: `src/NimBLEAdvertising.{h,cpp}`, `src/NimBLEDevice.cpp`,
  `src/nimble/nimble/host/src/ble_gap.c` (vendored), the ESP32 forum thread
  <https://esp32.com/viewtopic.php?t=29549>, and Espressif's GAP API note that
  `esp_ble_gap_add_device_to_resolving_list()` is not supported on the classic ESP32:
  <https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/bluetooth/esp_gap_ble.html>.

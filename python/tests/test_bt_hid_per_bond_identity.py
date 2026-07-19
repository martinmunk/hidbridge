"""Offline TDD for per-bond BLE identity (host-slot) support on the MCU HID boards.

GOAL (docs/bt-hid-mcu-approach.md follow-up): give each MCU board a DISTINCT BLE identity/address
PER bonded iPad (a "host slot", like a multi-host keyboard's slots), so ONE board can hold two
iPads at once on distinct addresses instead of time-slicing a single identity.

These tests are HARDWARE-FREE. They pin the parts that survive the JSON-RPC migration:
  1. the link backends advertise the simultaneous-identity capability;
  2. the target registry expresses a distinct identity (slot) per (board, iPad) and can say whether a
     given board can hold BOTH iPads simultaneously;
  3. the firmware sources declare the slot protocol (now JSON-RPC methods) + a >1 slot table.

The host-side single-char slot framing (I/T/S) + its transport senders were REMOVED when the firmware
folded the device protocol into JSON-RPC, so the framing/parser tests that pinned them are gone.

The LIVE dual-hold acceptance test (ONE ESP32 holding mini5 AND mini6 at once) is device-gated in
tests/bt_hid/test_bt_hid_dual_identity_live.py (BT_HID_LIVE_DUAL=1).
"""

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

from hidbridge import conn as C  # noqa: E402
from hidbridge import naming as N  # noqa: E402
from hidbridge import targets as T  # noqa: E402


# --------------------------------------------------------------------------- #
# 1. link backends expose the simultaneous-identity capability
# --------------------------------------------------------------------------- #
def test_esp32_backend_reports_simultaneous_identities():
    be = C.make_link_backend("esp32:/dev/ttyUSB1")
    assert be.capabilities["per_device_identity"] is True
    # the ESP32 (NimBLE, MAX_CONNECTIONS>1 + per-conn notify) can hold multiple centrals AT ONCE
    assert be.capabilities["simultaneous_identities"] is True


def test_pico_backend_single_identity_at_a_time():
    be = C.make_link_backend("pico:/dev/ttyACM0")
    assert be.capabilities["per_device_identity"] is True
    # the arduino-pico PicoBluetoothBLEHID wrapper tracks ONE con handle -> not simultaneous (yet)
    assert be.capabilities["simultaneous_identities"] is False


# --------------------------------------------------------------------------- #
# 2. registry: distinct identity (slot) per (board, iPad) + dual-hold query
# --------------------------------------------------------------------------- #
def test_registry_entries_carry_a_slot():
    reg = T.TargetRegistry.load(path="/nonexistent-seed-defaults")
    # mini6 + mini5 both reachable from the ESP32 on DISTINCT slots
    e_m6 = reg.get("mini6")
    e_m5_esp = reg.get("mini5-esp32")
    assert e_m6.target.startswith("esp32:")
    assert e_m5_esp.target.startswith("esp32:")
    assert e_m6.slot != e_m5_esp.slot          # distinct identities on the same board


def test_registry_slot_persists_roundtrip(tmp_path):
    reg = T.TargetRegistry.load(path="/nonexistent-seed-defaults")
    p = str(tmp_path / "targets.json")
    reg.save(p)
    reg2 = T.TargetRegistry.load(path=p)
    assert reg2.get("mini5-esp32").slot == reg.get("mini5-esp32").slot


def test_registry_esp32_can_hold_both_ipads_simultaneously():
    reg = T.TargetRegistry.load(path="/nonexistent-seed-defaults")
    # The ESP32 backend holds distinct slots for mini5 + mini6 AND advertises simultaneous identity
    plan = reg.simultaneous_hold_plan("esp32:/dev/ttyUSB1")
    bonds = {r.bond for r in plan}
    slots = {r.slot for r in plan}
    assert bonds == {"mini5", "mini6"}          # BOTH iPads
    assert len(slots) == len(plan)              # each on its OWN distinct slot
    assert reg.can_hold_simultaneously("esp32:/dev/ttyUSB1") is True


def test_registry_pico_cannot_hold_both_simultaneously():
    reg = T.TargetRegistry.load(path="/nonexistent-seed-defaults")
    # the Pico maps both iPads to distinct slots too, but the wrapper is single-connection
    assert reg.can_hold_simultaneously("pico:/dev/ttyACM0") is False


def test_resolved_target_exposes_slot_and_simultaneous_capability():
    reg = T.TargetRegistry.load(path="/nonexistent-seed-defaults")
    r = reg.resolve("mini5-esp32")
    assert r.slot == reg.get("mini5-esp32").slot
    assert r.simultaneous_identities is True


# --------------------------------------------------------------------------- #
# 2b. per-slot BLE name names the TARGET iPad (so you can tell which identity is for which iPad)
# --------------------------------------------------------------------------- #
def test_naming_slot_identity_name():
    assert N.slot_identity_name("esp32", "mini6") == "HIDBRIDGE-esp32-mini6"
    assert N.slot_identity_name("esp32", "mini5") == "HIDBRIDGE-esp32-mini5"
    assert N.slot_identity_name("pico", "mini5") == "HIDBRIDGE-pico-mini5"
    assert N.slot_identity_name("pico", "mini6") == "HIDBRIDGE-pico-mini6"


def test_registry_entry_identity_name_is_target_scoped():
    reg = T.TargetRegistry.load(path="/nonexistent-seed-defaults")
    assert reg.get("mini6").identity_name() == "HIDBRIDGE-esp32-mini6"
    assert reg.get("mini5-esp32").identity_name() == "HIDBRIDGE-esp32-mini5"
    assert reg.get("mini5").identity_name() == "HIDBRIDGE-pico-mini5"
    assert reg.resolve("mini5-esp32").identity_name == "HIDBRIDGE-esp32-mini5"


@pytest.mark.parametrize("rel", [
    "firmware/esp32/src/main.cpp",
    "firmware/pico/src/main.cpp",
])
def test_firmware_sets_per_slot_name(rel):
    src = (ROOT / rel).read_text()
    # the firmware must be able to set a per-slot advertised name and echo it back as name=
    assert "name=" in src, "firmware must emit the per-slot name in the I<n> reply"


# --------------------------------------------------------------------------- #
# 3. firmware sources declare the slot protocol + a >1 slot table (host<->fw parity)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rel", [
    "firmware/esp32/src/main.cpp",
    "firmware/pico/src/main.cpp",
])
def test_firmware_declares_slot_protocol(rel):
    src = (ROOT / rel).read_text()
    assert "NUM_SLOTS" in src, "firmware must define a host-slot table"
    # The slot protocol is now JSON-RPC methods (the single-char I/T/S/X/O commands were folded in +
    # removed). Each must be handled + emit the identity fields the host reads.
    for method in ('"connect"', '"target"', '"identities"', '"park"', '"disconnect"'):
        assert method in src, f"firmware missing slot JSON-RPC method {method}"
    assert '"slot"' in src and '"addr"' in src, "firmware must emit the identity reply fields"
    # park + disconnect = the deterministic cross-board handoff control
    assert '"parked"' in src, "firmware must implement the park method"


def _num_slots(rel):
    m = re.search(r"#define\s+NUM_SLOTS\s+(\d+)", (ROOT / rel).read_text())
    assert m, f"{rel}: no #define NUM_SLOTS"
    return int(m.group(1))


def test_both_firmwares_default_to_16_slots():
    # 16 = the Pico's precompiled arduino-pico BTstack bond ceiling (MAX_NR_LE_DEVICE_DB_ENTRIES /
    # NVM_NUM_DEVICE_DB_ENTRIES = 16); the ESP32 matches it so REMEMBERED-host capacity is UNIFORM
    # across boards. NUM_SLOTS is firmware-internal (the wheel never hardcodes a slot count), so this
    # is proto-neutral -- a 1.0.x wheel drives 1.1.0 firmware unchanged. (fw 1.1.0)
    assert _num_slots("firmware/esp32/src/main.cpp") == 16
    assert _num_slots("firmware/pico/src/main.cpp") == 16


def test_esp32_bond_and_cccd_stores_cover_all_slots():
    # NimBLE's bond/CCCD stores must hold every slot's bond + its HID input-report CCCD subscriptions,
    # else the 11th..16th device cannot persist. MAX_BONDS >= NUM_SLOTS and MAX_CCCDS >= NUM_SLOTS.
    ini = (ROOT / "firmware/esp32/platformio.ini").read_text()
    slots = _num_slots("firmware/esp32/src/main.cpp")
    bonds = int(re.search(r"MAX_BONDS=(\d+)", ini).group(1))
    cccds = int(re.search(r"MAX_CCCDS=(\d+)", ini).group(1))
    assert bonds >= slots, f"MAX_BONDS={bonds} < NUM_SLOTS={slots}"
    assert cccds >= slots, f"MAX_CCCDS={cccds} < NUM_SLOTS={slots}"


def test_esp32_firmware_supports_multiple_connections():
    src = (ROOT / "firmware/esp32/src/main.cpp").read_text()
    # per-connection notify (drive a specific central) + a >1 connection budget = simultaneous hold
    assert "CONFIG_BT_NIMBLE_MAX_CONNECTIONS" in src
    assert "slotConn" in src                      # per-slot connection-handle table
    assert "getConnHandle" in src                 # records each central's handle against its slot
    assert "->notify(h)" in src or "notify(h)" in src  # notify a SPECIFIC connection (one iPad)

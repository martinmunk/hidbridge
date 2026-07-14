"""Offline TDD for per-bond BLE identity (host-slot) support on the MCU HID boards.

GOAL (docs/bt-hid-mcu-approach.md follow-up): give each MCU board a DISTINCT BLE identity/address
PER bonded iPad (a "host slot", like a multi-host keyboard's slots), so ONE board can hold two
iPads at once on distinct addresses instead of time-slicing a single identity.

These tests are HARDWARE-FREE. They pin:
  1. the serial framing for the new slot commands (I<n> select+advertise, T<n> send-target, S status)
     on BOTH transports (esp32 + pico) via the fake-serial harness;
  2. the reply parsers (parse_slot_reply / parse_slots_status);
  3. the link backends expose select/target/status + advertise the simultaneous-identity capability;
  4. the target registry expresses a distinct identity (slot) per (board, iPad) and can say whether a
     given board can hold BOTH iPads simultaneously;
  5. the firmware sources declare the slot protocol + a >1 slot table (parity with the host).

The LIVE dual-hold acceptance test (ONE ESP32 holding mini5 AND mini6 at once) is device-gated in
tests/bt_hid/test_bt_hid_dual_identity_live.py (BT_HID_LIVE_DUAL=1).
"""

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

from hidbridge import conn as C  # noqa: E402
from hidbridge import link_esp32  # noqa: E402
from hidbridge import naming as N  # noqa: E402
from hidbridge import targets as T  # noqa: E402
from hidbridge import transport_esp32 as TE  # noqa: E402
from hidbridge import transport_pico as TP  # noqa: E402


class FakeSerial:
    """Duck-typed serial port: records written lines, returns scripted replies (or 'OK')."""

    def __init__(self, replies=None):
        self.written = []
        self._replies = list(replies) if replies is not None else None
        self.port = "/dev/fake"

    def write(self, data: bytes):
        self.written.append(data)
        return len(data)

    def flush(self):
        pass

    def readline(self) -> bytes:
        if self._replies is not None:
            return self._replies.pop(0) if self._replies else b""
        return b"OK\n"

    def close(self):
        pass


# --------------------------------------------------------------------------- #
# 1. pure serial framing -- the new slot commands, both transports
# --------------------------------------------------------------------------- #
def test_esp32_encode_slot_select():
    assert TE.encode_slot_select(0) == b"I0\n"
    assert TE.encode_slot_select(1) == b"I1\n"


def test_esp32_encode_slot_target():
    assert TE.encode_slot_target(0) == b"T0\n"
    assert TE.encode_slot_target(1) == b"T1\n"


def test_esp32_encode_slots_query():
    assert TE.encode_slots_query() == b"S\n"


def test_esp32_encode_slot_rejects_negative():
    with pytest.raises(TE.Esp32Error):
        TE.encode_slot_select(-1)
    with pytest.raises(TE.Esp32Error):
        TE.encode_slot_target(-1)


def test_pico_encode_slot_select():
    assert TP.encode_slot_select(0) == b"I0\n"
    assert TP.encode_slot_select(1) == b"I1\n"


def test_pico_encode_slot_target_and_query():
    assert TP.encode_slot_target(1) == b"T1\n"
    assert TP.encode_slots_query() == b"S\n"


# --------------------------------------------------------------------------- #
# 2. reply parsers
# --------------------------------------------------------------------------- #
def test_parse_slot_reply():
    got = TE.parse_slot_reply("OK slot=1 addr=c2a1b2c3d4e5 conn=1")
    assert got["ok"] is True
    assert got["slot"] == 1
    assert got["addr"] == "c2a1b2c3d4e5"
    assert got["connected"] is True


def test_parse_slot_reply_disconnected():
    got = TE.parse_slot_reply("OK slot=0 addr=aabbccddeeff conn=0")
    assert got["slot"] == 0 and got["connected"] is False


def test_parse_slots_status():
    st = TE.parse_slots_status("OK slots=2 adv=1 tgt=0 multi=1 c0=1 c1=1 s0=1 s1=0")
    assert st["ok"] is True
    assert st["slots"] == 2
    assert st["adv"] == 1
    assert st["tgt"] == 0
    assert st["multi"] is True
    assert st["conn"] == {0: True, 1: True}
    assert st["sub"] == {0: True, 1: False}


def test_parse_slots_status_pico_single_identity():
    st = TE.parse_slots_status("OK slots=2 adv=0 tgt=0 multi=0 c0=1 c1=0 s0=1 s1=0")
    assert st["multi"] is False
    assert st["conn"] == {0: True, 1: False}


# --------------------------------------------------------------------------- #
# 3. transport methods drive the serial lines + return parsed state
# --------------------------------------------------------------------------- #
def test_esp32_select_slot_writes_line_and_parses():
    fake = FakeSerial(replies=[b"OK slot=1 addr=c2a1b2c3d4e5 conn=1\n"])
    tp = TE.Esp32Transport(port=fake)
    got = tp.select_slot(1)
    assert fake.written == [b"I1\n"]
    assert got["slot"] == 1 and got["connected"] is True and got["addr"] == "c2a1b2c3d4e5"


def test_esp32_target_slot_writes_line():
    fake = FakeSerial(replies=[b"OK tgt=1\n"])
    tp = TE.Esp32Transport(port=fake)
    got = tp.target_slot(1)
    assert fake.written == [b"T1\n"]
    assert got["ok"] is True and got["tgt"] == 1


def test_esp32_slots_status_writes_line_and_parses():
    fake = FakeSerial(replies=[b"OK slots=2 adv=0 tgt=0 multi=1 c0=1 c1=1 s0=1 s1=1\n"])
    tp = TE.Esp32Transport(port=fake)
    st = tp.slots_status()
    assert fake.written == [b"S\n"]
    assert st["multi"] is True and st["conn"] == {0: True, 1: True}


def test_pico_select_slot_writes_line():
    fake = FakeSerial(replies=[b"OK slot=1 addr=c2a1b2c3d4e5 conn=0\n"])
    tp = TP.PicoSerialTransport(port="x", open_factory=lambda *a: fake)
    got = tp.select_slot(1)
    assert fake.written == [b"I1\n"]
    assert got["slot"] == 1


# --------------------------------------------------------------------------- #
# 3b. park / disconnect -- the deterministic cross-board handoff control
# --------------------------------------------------------------------------- #
def test_esp32_park_writes_line():
    fake = FakeSerial(replies=[b"OK parked\n"])
    tp = TE.Esp32Transport(port=fake)
    got = tp.park()
    assert fake.written == [b"O\n"] and got["ok"] is True


def test_esp32_disconnect_writes_line():
    fake = FakeSerial(replies=[b"OK disc\n"])
    tp = TE.Esp32Transport(port=fake)
    got = tp.disconnect()
    assert fake.written == [b"X\n"] and got["ok"] is True


def test_pico_disconnect_and_park_write_lines():
    fake = FakeSerial(replies=[b"OK disc\n", b"OK parked\n"])
    tp = TP.PicoSerialTransport(port="x", open_factory=lambda *a: fake)
    assert tp.disconnect()["ok"] is True
    assert tp.park()["ok"] is True
    assert fake.written == [b"X\n", b"O\n"]


# --------------------------------------------------------------------------- #
# 4. link backends expose slot control + the simultaneous-identity capability
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


def test_esp32_backend_select_and_target_delegate(monkeypatch):
    fake = FakeSerial(replies=[b"OK slot=1 addr=c2a1b2c3d4e5 conn=1\n", b"OK tgt=1\n"])
    be = link_esp32.Esp32LinkBackend(port="/dev/ttyUSB1")
    monkeypatch.setattr(link_esp32, "make_transport", lambda name, **o: TE.Esp32Transport(port=fake))
    be.prepare()
    sel = be.select_identity(1)
    tgt = be.target_identity(1)
    assert sel["slot"] == 1
    assert tgt["tgt"] == 1
    assert fake.written == [b"I1\n", b"T1\n"]


# --------------------------------------------------------------------------- #
# 5. registry: distinct identity (slot) per (board, iPad) + dual-hold query
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
# 5b. per-slot BLE name names the TARGET iPad (so you can tell which identity is for which iPad)
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


def test_encode_slot_select_with_name_sets_advertised_name():
    assert TE.encode_slot_select(1, "HIDBRIDGE-esp32-mini5") == b"I1:HIDBRIDGE-esp32-mini5\n"
    assert TP.encode_slot_select(1, "HIDBRIDGE-pico-mini6") == b"I1:HIDBRIDGE-pico-mini6\n"
    # no name -> bare form (backward compatible)
    assert TE.encode_slot_select(0) == b"I0\n"


def test_parse_slot_reply_includes_name():
    got = TE.parse_slot_reply("OK slot=1 addr=c2a1b2c3d4e5 name=HIDBRIDGE-esp32-mini5 conn=1")
    assert got["name"] == "HIDBRIDGE-esp32-mini5" and got["slot"] == 1


def test_select_slot_transport_passes_name():
    fake = FakeSerial(replies=[b"OK slot=1 addr=c2a1b2c3d4e5 name=HIDBRIDGE-esp32-mini5 conn=0\n"])
    tp = TE.Esp32Transport(port=fake)
    got = tp.select_slot(1, "HIDBRIDGE-esp32-mini5")
    assert fake.written == [b"I1:HIDBRIDGE-esp32-mini5\n"]
    assert got["name"] == "HIDBRIDGE-esp32-mini5"


@pytest.mark.parametrize("rel", [
    "firmware/esp32/src/main.cpp",
    "firmware/pico/src/main.cpp",
])
def test_firmware_sets_per_slot_name(rel):
    src = (ROOT / rel).read_text()
    # the firmware must be able to set a per-slot advertised name and echo it back as name=
    assert "name=" in src, "firmware must emit the per-slot name in the I<n> reply"


# --------------------------------------------------------------------------- #
# 6. firmware sources declare the slot protocol + a >1 slot table (host<->fw parity)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rel", [
    "firmware/esp32/src/main.cpp",
    "firmware/pico/src/main.cpp",
])
def test_firmware_declares_slot_protocol(rel):
    src = (ROOT / rel).read_text()
    assert "NUM_SLOTS" in src, "firmware must define a host-slot table"
    # the three new serial commands must be handled
    for token in ("'I'", "'T'", "'S'"):
        assert token in src, f"firmware missing slot command {token} handler"
    assert "slot=" in src and "addr=" in src, "firmware must emit the slot reply fields"
    # park (O) + disconnect (X) = the deterministic cross-board handoff control
    assert "'O'" in src and "OK parked" in src, "firmware must implement the O (park) command"
    assert "'X'" in src, "firmware must implement the X (disconnect) command"


def test_esp32_firmware_supports_multiple_connections():
    src = (ROOT / "firmware/esp32/src/main.cpp").read_text()
    # per-connection notify (drive a specific central) + a >1 connection budget = simultaneous hold
    assert "CONFIG_BT_NIMBLE_MAX_CONNECTIONS" in src
    assert "slotConn" in src                      # per-slot connection-handle table
    assert "getConnHandle" in src                 # records each central's handle against its slot
    assert "->notify(h)" in src or "notify(h)" in src  # notify a SPECIFIC connection (one iPad)

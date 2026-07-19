"""DEVICE-GATED live acceptance: ONE ESP32 holds mini5 AND mini6 SIMULTANEOUSLY; Pico switches.

Acceptance for the per-bond-identity feature (docs/bt-hid-mcu-approach.md):
  * the ESP32 presents a DISTINCT BLE identity per bonded iPad (host slots) and holds BOTH iPads
    connected AT ONCE on distinct addresses -- no time-slice -- and drives EACH independently;
  * the Pico switches identities deterministically (single-connection: one iPad at a time).

DETERMINISM: both iPads are bonded to BOTH boards, so to test one board cleanly the OTHER board is
PARKED (disconnect the held central + stop advertising) -- the deterministic cross-board handoff.
Without that, iOS attaches each iPad to whichever board it likes and the hold is not reproducible.

DRIVEN OVER THE CURRENT API: the MCU wire is JSON-RPC only (the board builds every report). This test
drives an `rpc.RpcClient` directly -- `connect`/`disconnect`/`park`/`identities`/`target` for slot
orchestration and `type` for input. Slots are addressed by INDEX: `connect {"slot":n}` brings that
identity up + advertises it; `target n` routes THIS session's next report to that slot's central.

PRECONDITIONS (all physical; skipped unless BT_HID_LIVE_DUAL=1):
  ESP32 (BT_HID_ESP32_PORT or autodetect) bonded: slot0<->mini6, slot1<->mini5.
  Pico  (BT_HID_PICO_PORT or autodetect) bonded: slot0<->mini5, slot1<->mini6.
  both iPads awake + unlocked + in range.

Run:
  BT_HID_LIVE_DUAL=1 BT_HID_ESP32_PORT=/dev/ttyUSB1 BT_HID_PICO_PORT=/dev/ttyACM0 \
      uv run --with pyserial --with pytest pytest tests/test_bt_hid_dual_identity_live.py -v -s
"""

import os
import pathlib
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    os.environ.get("BT_HID_LIVE_DUAL") != "1",
    reason="live dual-identity test; set BT_HID_LIVE_DUAL=1 (+ a bonded ESP32/Pico + both iPads)")

from hidbridge import naming as N  # noqa: E402
from hidbridge import transport_esp32 as TE  # noqa: E402
from hidbridge import transport_pico as TP  # noqa: E402
from hidbridge.rpc import open_client  # noqa: E402

SLOT_MINI6_ESP = 0   # ESP32 native identity (existing bond preserved)
SLOT_MINI5_ESP = 1   # ESP32 static-random identity
SLOT_MINI5_PICO = 0  # Pico native identity (existing bond preserved)
SLOT_MINI6_PICO = 1  # Pico static-random identity


def _slot(rpc, slot) -> dict:
    """The board's identity-table row for `slot` (or {} if absent): {slot,client,addr,name,conn,sub}."""
    for s in rpc.identities().get("slots", []):
        if s.get("slot") == slot:
            return s
    return {}


def _wait_slot(rpc, slot, want=True, timeout=40.0, poll=1.0):
    end = time.time() + timeout
    while time.time() < end:
        if bool(_slot(rpc, slot).get("conn")) == want:
            return True
        time.sleep(poll)
    return bool(_slot(rpc, slot).get("conn")) == want


def _bring_up(rpc, slot):
    """Bring host `slot`'s identity up + advertise it so the bonded iPad reconnects (JSON `connect`,
    addressed by slot index). On the ESP32 this does NOT drop other slots (it holds several at once)."""
    return rpc.call("connect", {"slot": slot})


def _type_slot(rpc, slot, text):
    """Drive a keystroke to a specific host slot: `target` that slot (so the board notifies THAT
    slot's central), then `type` -- the board builds the report. Returns the rpc result ({sent,conn})."""
    rpc.target(slot)
    return rpc.type_text(text)


def _drive_slot_honestly(rpc, slot):
    """Verify slot is CONNECTED (so a report really lands) then type a keystroke to it; assert the board
    built + notified the report (sent>0, conn true on the target slot)."""
    assert _slot(rpc, slot).get("conn"), f"slot {slot} not connected -- cannot drive it"
    res = _type_slot(rpc, slot, "x")
    assert res.get("sent"), f"drive slot {slot} reported nothing sent: {res}"
    assert res.get("conn", True), f"drive slot {slot}: board had no central on the target slot: {res}"


@pytest.fixture()
def esp32():
    port = os.environ.get("BT_HID_ESP32_PORT") or TE.detect_port()
    if not port:
        pytest.skip("no ESP32 serial port (set BT_HID_ESP32_PORT)")
    cli = open_client(f"esp32:{port}")
    yield cli
    cli.close()


@pytest.fixture()
def pico():
    port = os.environ.get("BT_HID_PICO_PORT") or TP.autodetect_port()
    if not port:
        yield None
        return
    cli = open_client(f"pico:{port}")
    yield cli
    cli.close()


def test_esp32_holds_mini5_and_mini6_simultaneously(esp32, pico):
    # Park the Pico so it releases mini6 (bonded to both) -> the ESP32 can hold it deterministically:
    # drop the Pico's current central, then stop it advertising.
    if pico is not None:
        pico.disconnect()   # drop the Pico's held central (bond kept)
        pico.park()         # stop advertising so mini6 is free to attach to the ESP32

    # slot 0 = mini6 (native identity): bring it up + advertise
    _bring_up(esp32, SLOT_MINI6_ESP)
    assert _wait_slot(esp32, SLOT_MINI6_ESP), "mini6 did not connect on ESP32 slot 0"
    row6 = _slot(esp32, SLOT_MINI6_ESP)
    assert row6.get("name") == N.slot_identity_name("esp32", "mini6"), \
        f"slot 0 advertises {row6.get('name')!r}, expected mini6's identity name"

    # slot 1 = mini5 (static-random identity) WITHOUT dropping slot 0
    _bring_up(esp32, SLOT_MINI5_ESP)
    row5 = _slot(esp32, SLOT_MINI5_ESP)
    assert row5.get("addr") and row6.get("addr") and row5["addr"] != row6["addr"], "slots must differ"
    msb = int(row5["addr"][:2], 16)
    assert (msb & 0xC0) == 0xC0, f"slot1 addr {row5['addr']} not static-random (MSB {msb:#04x})"
    assert _wait_slot(esp32, SLOT_MINI5_ESP), "mini5 did not connect on ESP32 slot 1"

    # SUSTAINED simultaneous hold: sample repeatedly; BOTH must stay connected (no time-slice). Two
    # slots connected at once IS the "multi" proof (the ESP32's simultaneous-identities capability).
    both_samples = 0
    for _ in range(6):
        time.sleep(1)
        if _slot(esp32, SLOT_MINI6_ESP).get("conn") and _slot(esp32, SLOT_MINI5_ESP).get("conn"):
            both_samples += 1
    assert both_samples >= 5, f"ESP32 did not SUSTAIN both connected (only {both_samples}/6 samples)"

    # drive EACH iPad independently via its slot (honest: connection verified before each)
    _drive_slot_honestly(esp32, SLOT_MINI6_ESP)
    _drive_slot_honestly(esp32, SLOT_MINI5_ESP)

    assert _slot(esp32, SLOT_MINI6_ESP).get("conn") and _slot(esp32, SLOT_MINI5_ESP).get("conn"), \
        "a slot dropped after driving"


def test_pico_time_sliced_switch(esp32, pico):
    if pico is None:
        pytest.skip("no Pico serial port (set BT_HID_PICO_PORT)")
    # Release BOTH iPads from the ESP32 + park it, so they are free to attach to the Pico.
    for slot in (SLOT_MINI6_ESP, SLOT_MINI5_ESP):
        esp32.call("disconnect", {"slot": slot})   # disconnect that slot's central (bond kept)
    esp32.park()

    # mini5 (Pico slot 0): bring it up -> it connects -> drive it
    _bring_up(pico, SLOT_MINI5_PICO)
    assert _wait_slot(pico, SLOT_MINI5_PICO), "mini5 did not connect to the Pico slot0"
    addr5 = _slot(pico, SLOT_MINI5_PICO).get("addr")
    assert _type_slot(pico, SLOT_MINI5_PICO, "x").get("sent"), "driving mini5 on the Pico failed"

    # switch to mini6 (Pico slot 1) -> single-connection, so slot0 releases
    _bring_up(pico, SLOT_MINI6_PICO)
    assert _wait_slot(pico, SLOT_MINI6_PICO), "mini6 did not connect to the Pico slot1"
    addr6 = _slot(pico, SLOT_MINI6_PICO).get("addr")
    assert addr6 and addr6 != addr5, "pico slots must have distinct addresses"
    assert _type_slot(pico, SLOT_MINI6_PICO, "x").get("sent"), "driving mini6 on the Pico failed"

    # single-connection: only ONE slot connected at a time
    connected = [s for s in pico.identities().get("slots", []) if s.get("conn")]
    assert len(connected) <= 1, f"Pico held >1 at once: {connected}"

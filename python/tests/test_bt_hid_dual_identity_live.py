"""DEVICE-GATED live acceptance: ONE ESP32 holds mini5 AND mini6 SIMULTANEOUSLY; Pico switches.

Acceptance for the per-bond-identity feature (docs/bt-hid-mcu-approach.md):
  * the ESP32 presents a DISTINCT BLE identity per bonded iPad (host slots) and holds BOTH iPads
    connected AT ONCE on distinct addresses -- no time-slice -- and drives EACH independently;
  * the Pico switches identities deterministically (single-connection: one iPad at a time).

DETERMINISM: both iPads are bonded to BOTH boards, so to test one board cleanly the OTHER board is
PARKED (X = disconnect+park / O = park) -- the deterministic cross-board handoff. Without that, iOS
attaches each iPad to whichever board it likes and the hold is not reproducible.

PRECONDITIONS (all physical; skipped unless BT_HID_LIVE_DUAL=1):
  ESP32 (BT_HID_ESP32_PORT or autodetect) bonded: slot0<->mini6, slot1<->mini5.
  Pico  (BT_HID_PICO_PORT or autodetect) bonded: slot0<->mini5, slot1<->mini6.
  both iPads awake + unlocked + in range.

Run:
  BT_HID_LIVE_DUAL=1 BT_HID_ESP32_PORT=/dev/ttyUSB1 BT_HID_PICO_PORT=/dev/ttyACM0 \
      uv run --with pyserial --with pytest pytest tests/bt_hid/test_bt_hid_dual_identity_live.py -v -s
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
from hidbridge.rpc import RpcClient  # noqa: E402

SLOT_MINI6_ESP = 0   # ESP32 native identity (existing bond preserved)
SLOT_MINI5_ESP = 1   # ESP32 static-random identity
SLOT_MINI5_PICO = 0  # Pico native identity (existing bond preserved)
SLOT_MINI6_PICO = 1  # Pico static-random identity


def _wait_slot(status_fn, slot, want=True, timeout=40.0, poll=1.0):
    end = time.time() + timeout
    while time.time() < end:
        if bool(status_fn()["conn"].get(slot)) == want:
            return True
        time.sleep(poll)
    return bool(status_fn()["conn"].get(slot)) == want


def _rpc_type_slot(tp, slot, text):
    """Drive a keystroke to a specific host slot over the MCU's JSON-RPC channel (the board builds the
    report -- the MCU wire no longer accepts host-built K/M frames). Targets `slot` (T<n>) first so the
    report notifies that slot's central, then types. Returns the rpc result."""
    tp.target_slot(slot)
    ch = getattr(tp, "_ser", None)
    if ch is None:                        # nudge the transport to open its serial channel
        (getattr(tp, "_open", None) or getattr(tp, "_get", None))()
        ch = tp._ser
    return RpcClient(ch).type_text(text)


def _drive_slot_honestly(esp32, slot):
    """Verify slot is CONNECTED (so a report really lands) then type a keystroke to it; assert the board
    built + notified the report (sent>0, conn true on the target slot)."""
    assert esp32.slots_status()["conn"].get(slot), f"slot {slot} not connected -- cannot drive it"
    res = _rpc_type_slot(esp32, slot, "x")
    assert res.get("sent"), f"drive slot {slot} reported nothing sent: {res}"
    assert res.get("conn", True), f"drive slot {slot}: board had no central on the target slot: {res}"


@pytest.fixture()
def esp32():
    port = os.environ.get("BT_HID_ESP32_PORT") or TE.detect_port()
    if not port:
        pytest.skip("no ESP32 serial port (set BT_HID_ESP32_PORT)")
    tp = TE.Esp32Transport(port=port)
    yield tp
    tp.close()


@pytest.fixture()
def pico():
    port = os.environ.get("BT_HID_PICO_PORT") or TP.autodetect_port()
    if not port:
        yield None
        return
    tp = TP.PicoSerialTransport(port=port)
    yield tp
    tp.close()


def test_esp32_holds_mini5_and_mini6_simultaneously(esp32, pico):
    # Park the Pico so it releases mini6 (bonded to both) -> the ESP32 can hold it deterministically.
    if pico is not None:
        pico.disconnect()   # X: disconnect current central + park

    # slot 0 = mini6 (native identity)
    sel6 = esp32.select_slot(SLOT_MINI6_ESP, N.slot_identity_name("esp32", "mini6"))
    assert sel6["ok"], f"slot 0 select failed: {sel6}"
    assert _wait_slot(esp32.slots_status, SLOT_MINI6_ESP), "mini6 did not connect on ESP32 slot 0"

    # slot 1 = mini5 (static-random identity) WITHOUT dropping slot 0
    sel5 = esp32.select_slot(SLOT_MINI5_ESP, N.slot_identity_name("esp32", "mini5"))
    assert sel5["ok"] and sel5["addr"] and sel5["addr"] != sel6["addr"], "slots must differ"
    msb = int(sel5["addr"][:2], 16)
    assert (msb & 0xC0) == 0xC0, f"slot1 addr {sel5['addr']} not static-random (MSB {msb:#04x})"
    assert _wait_slot(esp32.slots_status, SLOT_MINI5_ESP), "mini5 did not connect on ESP32 slot 1"

    # SUSTAINED simultaneous hold: sample repeatedly; BOTH must stay connected (no time-slice).
    both_samples = 0
    for _ in range(6):
        time.sleep(1)
        st = esp32.slots_status()
        if st["conn"].get(SLOT_MINI6_ESP) and st["conn"].get(SLOT_MINI5_ESP):
            both_samples += 1
    assert both_samples >= 5, f"ESP32 did not SUSTAIN both connected (only {both_samples}/6 samples)"
    assert esp32.slots_status()["multi"] is True

    # drive EACH iPad independently via its slot (honest: connection verified before each)
    _drive_slot_honestly(esp32, SLOT_MINI6_ESP)
    _drive_slot_honestly(esp32, SLOT_MINI5_ESP)

    st2 = esp32.slots_status()
    assert st2["conn"].get(SLOT_MINI6_ESP) and st2["conn"].get(SLOT_MINI5_ESP), (
        f"a slot dropped after driving; conn={st2['conn']}")


def test_pico_time_sliced_switch(esp32, pico):
    if pico is None:
        pytest.skip("no Pico serial port (set BT_HID_PICO_PORT)")
    # Release BOTH iPads from the ESP32 + park it, so they are free to attach to the Pico.
    for slot in (SLOT_MINI6_ESP, SLOT_MINI5_ESP):
        esp32.target_slot(slot)
        esp32.disconnect()   # X: disconnect that slot's central + park the ESP32
    esp32.park()

    # select mini5 (Pico slot 0) -> it connects -> drive it
    p5 = pico.select_slot(SLOT_MINI5_PICO, N.slot_identity_name("pico", "mini5"))
    assert p5["ok"], f"pico slot0 select failed: {p5}"
    assert _wait_slot(pico.slots_status, SLOT_MINI5_PICO), "mini5 did not connect to the Pico slot0"
    assert _rpc_type_slot(pico, SLOT_MINI5_PICO, "x").get("sent"), "driving mini5 on the Pico failed"

    # switch to mini6 (Pico slot 1) -> single-connection, so slot0 releases
    p6 = pico.select_slot(SLOT_MINI6_PICO, N.slot_identity_name("pico", "mini6"))
    assert p6["ok"] and p6["addr"] != p5["addr"], "pico slots must have distinct addresses"
    assert _wait_slot(pico.slots_status, SLOT_MINI6_PICO), "mini6 did not connect to the Pico slot1"
    assert _rpc_type_slot(pico, SLOT_MINI6_PICO, "x").get("sent"), "driving mini6 on the Pico failed"

    # single-connection: only ONE slot connected at a time (multi=0)
    st = pico.slots_status()
    assert st["multi"] is False
    assert sum(1 for v in st["conn"].values() if v) <= 1, f"Pico held >1 at once: {st['conn']}"

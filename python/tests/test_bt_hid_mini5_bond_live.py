"""HARDWARE-GATED live test: BOTH MCU boards bonded to mini5 + per-board mini5<->mini6 switching.

Task (2026-07-14): the Pico (wsb-hid-pico, /dev/ttyACM0) AND the ESP32 (wsb-hid-esp32, /dev/ttyUSB1)
must each be BONDED to iPad mini5 and able to DRIVE it, and each board must switch its target between
mini5 and mini6 WITHOUT re-pairing. The registry (bt_hid.targets) expresses this with board-qualified
routes: mini5 on the Pico = `mini5`, mini5 on the ESP32 = `mini5-esp32`; mini6 on the ESP32 = `mini6`,
mini6 on the Pico = `mini6-pico`.

The registry-enumeration half runs OFFLINE (no hardware) and always executes -- it proves both boards
enumerate a mini5 target + bond. The connect/drive/switch half needs the real boards + a bonded, awake
mini5, so it SKIPS unless the operator opts in:

    BT_HID_LIVE_BOND=1 uv run --extra test pytest -q \
        tests/bt_hid/test_bt_hid_mini5_bond_live.py

FIRMWARE-CAPABILITY NOTE (see docs/bt-hid-mcu-approach.md + targets.py): the shipped firmware gives
each board ONE fixed BLE identity with undirected advertising + a multi-bond DB. Switching which
bonded iPad a board talks to is TIME-SLICED (disconnect the current central, let the other reconnect),
NOT deterministic when both iPads are simultaneously awake+in-range. These tests drive ONE route at a
time, matching that reality. Bonding a board to mini5 is a one-time physical tap on mini5's Bluetooth
settings ("Par"); until that is done for a board, its connect assertion is expected to fail.
"""

import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

from hidbridge import targets as TG  # noqa: E402

_LIVE = os.environ.get("BT_HID_LIVE_BOND") == "1"

# The board-qualified routes under test: (registry name, expected backend, iPad bond).
MINI5_ROUTES = [("mini5", "pico", "mini5"), ("mini5-esp32", "esp32", "mini5")]
# Per board: the two switch targets (mini5 route, mini6 route) that must both work without re-pair.
SWITCH_PAIRS = [
    ("pico", "mini5", "mini6-pico"),      # Pico switches mini5 <-> mini6
    ("esp32", "mini5-esp32", "mini6"),    # ESP32 switches mini5 <-> mini6
]


# --------------------------------------------------------------------------- #
# OFFLINE: both boards enumerate a mini5 target + bond (always runs, no hardware)
# --------------------------------------------------------------------------- #
def test_both_boards_enumerate_a_mini5_target_and_bond():
    """The registry must resolve a mini5 route on BOTH the Pico and the ESP32, each with a bond."""
    reg = TG.TargetRegistry.load()
    routes = {r.backend: r for r in reg.routes_for_bond("mini5")}
    assert set(routes) == {"pico", "esp32"}, (
        f"both boards must enumerate a mini5 route; got {sorted(routes)}")
    for backend, r in routes.items():
        assert r.bond == "mini5"
        assert r.target and r.per_device_identity is True
        assert backend in r.target  # e.g. 'pico:/dev/ttyACM0' / 'esp32:/dev/ttyUSB1'


@pytest.mark.parametrize("name,backend,bond", MINI5_ROUTES)
def test_mini5_route_resolves_per_board(name, backend, bond):
    reg = TG.TargetRegistry.load()
    r = reg.resolve(name)
    assert r.backend == backend
    assert r.bond == bond
    assert r.shared_mac_warning is None  # MCU backends never trip the shared-dongle-MAC warning


# --------------------------------------------------------------------------- #
# LIVE (gated): each board actually bonds + drives mini5, and switches mini5<->mini6
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _LIVE, reason="set BT_HID_LIVE_BOND=1 (needs both boards + an awake bonded "
                                      "mini5) to run the live bond/drive/switch checks")
@pytest.mark.parametrize("name,backend,bond", MINI5_ROUTES)
def test_board_bonds_and_drives_mini5(name, backend, bond):
    """Open a HidSession on the board's mini5 route: it must CONNECT (bond present + mini5 awake)
    and accept a benign pointer nudge without raising."""
    from hidbridge.conn import HidSession

    r = TG.TargetRegistry.load().resolve(name)
    with HidSession(r.target, r.bond, require_connect=True, require_ready=False) as hid:
        assert hid.backend.is_connected(), f"{name}: mini5 did not connect via {r.backend}"
        hid.move(0, -3)   # benign 3px pointer nudge -- proves the link carries HID to mini5
        hid.move(0, 3)    # nudge back


@pytest.mark.skipif(not _LIVE, reason="set BT_HID_LIVE_BOND=1 to run the per-board switch test")
@pytest.mark.parametrize("backend,mini5_name,mini6_name", SWITCH_PAIRS)
def test_board_switches_mini5_and_mini6(backend, mini5_name, mini6_name):
    """The SAME board drives mini5 then mini6 (time-sliced) with NO re-pairing -- the multi-bond
    switch. Each route is opened in turn; both must connect + accept a nudge."""
    from hidbridge.conn import HidSession

    reg = TG.TargetRegistry.load()
    for name in (mini5_name, mini6_name):
        r = reg.resolve(name)
        assert r.backend == backend, f"{name} should be on {backend}, got {r.backend}"
        with HidSession(r.target, r.bond, require_connect=True, require_ready=False) as hid:
            assert hid.backend.is_connected(), f"{name}: no connect via {backend} (bonded+awake?)"
            hid.move(0, -3)
            hid.move(0, 3)

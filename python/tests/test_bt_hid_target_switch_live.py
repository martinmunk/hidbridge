"""HARDWARE-GATED live test: switch bt_hid backends across TWO paired iOS devices via the registry.

This is the end-to-end proof of task #34 -- pick WHICH backend drives WHICH paired device and switch
between them. It needs real hardware that is NOT present on CI / the offline dev loop (two bonded
iPads reachable via two dongles, OR an MCU + a dongle), so it SKIPS unless the operator opts in:

    BT_HID_LIVE_SWITCH=1 \
    BT_HID_TARGETS=/path/to/targets.json \
    BT_HID_LIVE_DEVICE_A=mini6 BT_HID_LIVE_DEVICE_B=mini5 \
    uv run --extra test pytest -q tests/bt_hid/test_bt_hid_target_switch_live.py

Both names must already exist in the registry (bt_hid targets add ...) and both iPads must be awake
+ in range. It resolves each device through the registry, opens a HidSession on the RESOLVED
(target, bond) for each in turn, and types a per-device marker -- proving the switch routes to the
right device. Verify on-glass which iPad received which marker (or a webcam, per docs/hardware.md).
"""

import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

_LIVE = os.environ.get("BT_HID_LIVE_SWITCH") == "1"
_A = os.environ.get("BT_HID_LIVE_DEVICE_A", "")
_B = os.environ.get("BT_HID_LIVE_DEVICE_B", "")

pytestmark = pytest.mark.skipif(
    not (_LIVE and _A and _B),
    reason="set BT_HID_LIVE_SWITCH=1 + BT_HID_LIVE_DEVICE_A/B=<registered names> (needs 2 bonded "
           "iPads + backends) to run this live target-switch test")


@pytest.mark.parametrize("device", [_A, _B])
def test_switch_backend_to_registered_device(device):
    """Resolve `device` via the registry and drive it -- run for BOTH A and B to prove switching."""
    from hidbridge import targets as TG
    from hidbridge.conn import HidSession

    reg = TG.TargetRegistry.load()
    r = reg.resolve(device)                       # KeyError here => device not registered
    marker = f"bt_hid switch -> {device}\n"
    with HidSession(r.target, r.bond) as hid:
        hid.type_text(marker)                     # lands on THIS device only
    # No assert on pixels here (that is the human/webcam check); reaching here means the resolved
    # backend connected the right bond, became ready, and accepted input without raising.


def test_registry_resolves_both_live_devices():
    """Cheap guard that both env-named devices are registered + their targets parse to a backend."""
    from hidbridge import targets as TG

    reg = TG.TargetRegistry.load()
    for name in (_A, _B):
        r = reg.resolve(name)
        assert r.target and r.bond
        assert r.backend  # parsed to a concrete backend family

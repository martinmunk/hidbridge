"""Hardware-gated on-host loopback e2e for bt_hid (built-in adapter <- dongle peripheral).

This is the closed-loop proof that keyboard + mouse HID reports actually flow over BLE: the built-in
adapter acts as the central (the "iPad"), connects to our HID peripheral on the dongle, subscribes to
the report characteristics, and we assert the exact injected report bytes arrive as GATT
notifications. It needs BOTH real adapters, root, the daemon up on the dongle, and the dongle free to
advertise (no iPad currently bonded/connected), so it is SKIPPED by default.

Enable with:  BT_HID_LOOPBACK=1 sudo -E .venv/bin/python -m pytest tests/bt_hid/test_bt_hid_loopback.py
(or run hidbridge/loopback_selftest.py directly). It shells out to the self-test under the SYSTEM
python (uv's CPython lacks AF_BLUETOOTH/dbus).
"""

import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    os.environ.get("BT_HID_LOOPBACK") != "1",
    reason="hardware-gated: set BT_HID_LOOPBACK=1 (needs both adapters + root + daemon up)")


def test_loopback_keyboard_and_mouse_reports_arrive():
    env = dict(os.environ, BT_HID_SOCK=os.environ.get("BT_HID_SOCK", "/run/bt_hid.sock"))
    proc = subprocess.run(
        [os.environ.get("BT_HID_SYS_PY", sys.executable), "-m", "hidbridge.loopback_selftest", "--json"],
        capture_output=True, text=True, env=env, timeout=90)
    line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "{}"
    result = json.loads(line)
    assert result.get("ok"), f"loopback failed: {result}"
    assert result["received"]["keyboard"] == result["expected"]["keyboard"]
    assert result["received"]["mouse"] == result["expected"]["mouse"]

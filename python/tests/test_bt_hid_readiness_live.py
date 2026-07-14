"""HARDWARE-GATED live proof: a keystroke sent IMMEDIATELY after HidSession.__enter__ LANDS.

This is the end-to-end signal for the readiness gate. Before it, live-driving mini6 via the ESP32
lost keystrokes fired right after `with HidSession(...) as hid:` -- iOS needs ~1-3s after (re)connect
to enable the HID reports (write the report CCCDs), so callers hand-added a `time.sleep(3.3)`. With
the gate, __enter__ blocks until the firmware reports iOS SUBSCRIBED (ready_state == "ready"), so the
very next report is delivered with NO manual sleep.

GATING (all required; skips otherwise):
  BT_HID_HW_ESP32_LIVE=1     opt in to touching real hardware
  BT_HID_ESP32_PORT=/dev/ttyUSBx  the flashed CP2102/FT2232H ESP32 (NEVER the CH340 KVM; asserted)
  BT_HID_IPAD=<name/MAC>     the BONDED iOS device to drive

COORDINATE iPad use via 'main' before running (the iPad is a shared resource). This test drives a
real bonded iPad: it wakes it, so run it only when 'main' has granted the device.

VERIFY: the automated assertion is ready_state == "ready" (the REAL subscribe signal was observed
inside __enter__, not the settle fallback) AND the immediate keystroke was accepted by the firmware
while subscribed. The ultimate pixel-level confirmation (the character appears on the iPad) is visual
-- watch the glass / webcam, per docs/hardware.md; this test types a distinctive marker string to
make that easy.
"""

import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

_LIVE = os.environ.get("BT_HID_HW_ESP32_LIVE") == "1"
_PORT = os.environ.get("BT_HID_ESP32_PORT", "/dev/ttyUSB1")
_IPAD = os.environ.get("BT_HID_IPAD", "")
CP2102 = "10c4"
FTDI = "0403"


@pytest.mark.skipif(not (_LIVE and _IPAD),
                    reason="set BT_HID_HW_ESP32_LIVE=1 + BT_HID_IPAD=<device> (+ a flashed ESP32, "
                           "coordinate iPad via 'main') to run")
def test_keystroke_lands_immediately_after_enter():
    from hidbridge.conn import HidSession
    from hidbridge.transport_esp32 import _tty_usb_vendor

    # GUARDRAIL: never the KVM CH340 (vendor 1a86) -- only a genuine ESP32 bridge.
    vid = _tty_usb_vendor(_PORT)
    assert vid in (CP2102, FTDI), f"{_PORT} vendor {vid!r} is not an ESP32 bridge; refusing"

    # No manual settle: the readiness gate must make __enter__ return only when iOS is subscribed.
    with HidSession(f"esp32:{_PORT}", _IPAD, connect_timeout=30.0, ready_timeout=15.0,
                    require_ready=True) as hid:
        # __enter__ returned -> the firmware reported iOS enabled the keyboard report (real signal).
        assert hid.ready_state == "ready", (
            f"expected a real subscribe signal, got {hid.ready_state!r} (settle fallback means the "
            f"firmware did not report subscribed= -- reflash the ESP32 with the readiness firmware)")

        # Fire IMMEDIATELY -- no sleep. Under the old behavior these were dropped.
        marker = "hidready"
        res = hid.type_text(marker)
        assert res.get("ok"), f"immediate keystroke rejected: {res!r}"

        # Cross-check the link is genuinely subscribed right now (belt-and-suspenders).
        st = hid.backend.transport().ping()
        assert st.get("subscribed") is True and st.get("connected") is True, st

    print(f"\n[readiness-live] typed {marker!r} immediately after enter with NO manual sleep; "
          f"confirm it appears on {_IPAD} (webcam/glass).")

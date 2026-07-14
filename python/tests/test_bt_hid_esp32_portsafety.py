"""Regression: ESP32 serial autodetect must NEVER select the KVM CH340 (/dev/ttyUSB0, vendor 1a86).

BUG (found live 2026-07-10 during the first central self-test run): on wsb, /dev/ttyUSB0 is the KVM
control adapter (CH340, USB vendor 1a86) and the ESP32 is a CP2102 (vendor 10c4) on /dev/ttyUSB1.
transport_esp32.detect_port() ranked CH340 as a valid ESP32 bridge (its hint list contained "ch34")
and returned /dev/ttyUSB0 -- so streaming HID frames to the "ESP32" would have written them to the
KVM. CLAUDE.md is explicit: NEVER write to /dev/ttyUSB0 (KVM CH340, vendor 1a86); vendor-gate the
ESP32 to the CP2102 (10c4). This test pins that guarantee: the KVM vendor is hard-excluded and the
CP2102 is preferred. The autodetect is made injectable (candidates + vendor_of) so it is unit-tested
with no hardware.
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]

from hidbridge import transport_esp32 as TE  # noqa: E402

KVM_CH340 = "1a86"   # the KVM control adapter on wsb -- BANNED as an ESP32 port
CP2102 = "10c4"      # Silicon Labs CP2102 -- the ESP32 dev-board bridge


def test_detect_port_never_returns_kvm_ch340_prefers_cp2102():
    # wsb reality: ttyUSB0 = CH340 KVM (banned), ttyUSB1 = CP2102 ESP32.
    vendors = {"/dev/ttyUSB0": KVM_CH340, "/dev/ttyUSB1": CP2102}
    got = TE.detect_port(candidates=["/dev/ttyUSB0", "/dev/ttyUSB1"],
                         vendor_of=lambda d: vendors[d])
    assert got == "/dev/ttyUSB1"


def test_detect_port_excludes_kvm_even_as_sole_candidate():
    # If the ONLY serial device is the KVM CH340, detect must return None -- never the KVM.
    got = TE.detect_port(candidates=["/dev/ttyUSB0"], vendor_of=lambda d: KVM_CH340)
    assert got is None


def test_detect_port_prefers_cp2102_over_other_vendor():
    vendors = {"/dev/ttyUSB1": "1234", "/dev/ttyACM0": CP2102}
    got = TE.detect_port(candidates=["/dev/ttyUSB1", "/dev/ttyACM0"],
                         vendor_of=lambda d: vendors[d])
    assert got == "/dev/ttyACM0"


def test_detect_port_env_override_wins(monkeypatch):
    monkeypatch.setenv("BT_HID_ESP32_PORT", "/dev/ttyUSB9")
    assert TE.detect_port() == "/dev/ttyUSB9"


def test_detect_port_none_when_no_candidates():
    assert TE.detect_port(candidates=[], vendor_of=lambda d: "") is None

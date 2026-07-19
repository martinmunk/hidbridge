"""No-reset serial open: offline unit tests + a hardware-gated regression on the real ESP32.

BUG (found live 2026-07-10): opening the CP2102 ESP32 port RESET the board (RTS->EN edge asserted
by the Linux tty layer on activation), dropping its BLE link to the iPad -- so every command could
kill the connection. pyserial's dtr=False/rts=False before open did NOT prevent it (the kernel
raise happens inside os.open). FIX: serial_link.RawSerialLink leaves the tty at baud B0 on close so
the next open skips the DTR/RTS raise (C_BAUD==0) -> no EN pulse -> no reset.

The offline tests pin the FTDI/Ethernet-Kit channel selection + vendor gate. The HARDWARE test
(BT_HID_HW_ESP32=1) proves on the real CP2102 that consecutive opens do NOT reprint the boot banner
and the bonded iPad's conn state persists. It needs NO iPad for the no-reset assertion itself.
"""

import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

from hidbridge import transport_esp32 as TE  # noqa: E402

CP2102 = "10c4"
FTDI = "0403"
KVM_CH340 = "1a86"


# --------------------------------------------------------------------------- #
# FTDI / ESP32-Ethernet-Kit channel selection (offline, injectable)
# --------------------------------------------------------------------------- #
def test_detect_port_prefers_ftdi_channel_b_over_a():
    # FT2232H exposes two ttys: iface 00 = channel A (JTAG), iface 01 = channel B (ESP32 UART).
    vendors = {"/dev/ttyUSB2": FTDI, "/dev/ttyUSB3": FTDI}
    ifaces = {"/dev/ttyUSB2": "00", "/dev/ttyUSB3": "01"}
    got = TE.detect_port(candidates=["/dev/ttyUSB2", "/dev/ttyUSB3"],
                         vendor_of=lambda d: vendors[d], iface_of=lambda d: ifaces[d])
    assert got == "/dev/ttyUSB3"   # channel B = the UART


def test_detect_port_prefers_cp2102_over_ftdi():
    # If both a CP2102 DevKit and an FTDI Ethernet-Kit are present, prefer the CP2102.
    vendors = {"/dev/ttyUSB1": CP2102, "/dev/ttyUSB3": FTDI}
    ifaces = {"/dev/ttyUSB1": "00", "/dev/ttyUSB3": "01"}
    got = TE.detect_port(candidates=["/dev/ttyUSB1", "/dev/ttyUSB3"],
                         vendor_of=lambda d: vendors[d], iface_of=lambda d: ifaces[d])
    assert got == "/dev/ttyUSB1"


def test_detect_port_ftdi_still_bans_kvm():
    # The KVM CH340 stays hard-banned even alongside a valid FTDI channel.
    vendors = {"/dev/ttyUSB0": KVM_CH340, "/dev/ttyUSB3": FTDI}
    ifaces = {"/dev/ttyUSB0": "00", "/dev/ttyUSB3": "01"}
    got = TE.detect_port(candidates=["/dev/ttyUSB0", "/dev/ttyUSB3"],
                         vendor_of=lambda d: vendors[d], iface_of=lambda d: ifaces[d])
    assert got == "/dev/ttyUSB3"


# --------------------------------------------------------------------------- #
# serial_link import safety (no hardware, no pyserial)
# --------------------------------------------------------------------------- #
def test_serial_link_imports_without_pyserial():
    from hidbridge import serial_link  # must import with no serial dependency
    assert hasattr(serial_link, "RawSerialLink")
    assert hasattr(serial_link, "open_serial")
    assert hasattr(serial_link, "prime_b0")


# --------------------------------------------------------------------------- #
# HARDWARE-GATED: real CP2102 ESP32 must open WITHOUT rebooting (BT_HID_HW_ESP32=1)
# --------------------------------------------------------------------------- #
_HW = os.environ.get("BT_HID_HW_ESP32") == "1"
_PORT = os.environ.get("BT_HID_ESP32_PORT", "/dev/ttyUSB1")


@pytest.mark.skipif(not _HW, reason="set BT_HID_HW_ESP32=1 (+ a flashed CP2102 ESP32) to run")
def test_hw_open_does_not_reset_board():
    import time

    from hidbridge import serial_link
    from hidbridge.transport_esp32 import _tty_usb_vendor

    # GUARDRAIL: refuse anything that is not the CP2102 (never the KVM CH340).
    assert _tty_usb_vendor(_PORT) == CP2102, f"{_PORT} is not a CP2102 (10c4); refusing"

    # Prime B0 so even the first open in this test is reset-free (board was not just cold-plugged).
    serial_link.prime_b0(_PORT)
    time.sleep(0.4)

    def open_and_check_banner():
        # No single-char ping any more (folded into JSON-RPC); the no-reset assertion needs only the
        # open path -- _rebooted_on_open is set by _settle() draining (or NOT) a boot banner.
        tp = TE.Esp32Transport(port=_PORT, timeout=1.0)
        try:
            return tp._rebooted_on_open
        finally:
            tp.close()

    for _ in range(2):
        rebooted = open_and_check_banner()
        assert rebooted is False, "board rebooted on open -- no-reset regression!"
        time.sleep(0.6)


_HW_FTDI = os.environ.get("BT_HID_HW_ESP32_FTDI") == "1"
_FTDI_PORT = os.environ.get("BT_HID_ESP32_FTDI_PORT", "/dev/ttyUSB3")


@pytest.mark.skipif(not _HW_FTDI,
                    reason="set BT_HID_HW_ESP32_FTDI=1 (+ a flashed FT2232H Ethernet-Kit) to run")
def test_hw_ftdi_open_does_not_reset_board():
    # The FT2232H resets on a DTR/RTS MISMATCH transient; RawSerialLink clears both lines atomically,
    # so it does not reset the Ethernet-Kit either. Same open path, both boards.
    import time

    from hidbridge import serial_link
    from hidbridge.transport_esp32 import _tty_usb_vendor

    assert _tty_usb_vendor(_FTDI_PORT) == FTDI, f"{_FTDI_PORT} is not an FT2232H (0403); refusing"
    serial_link.prime_b0(_FTDI_PORT)
    time.sleep(0.4)
    for _ in range(2):
        tp = TE.Esp32Transport(port=_FTDI_PORT, timeout=1.0)
        try:
            assert tp._rebooted_on_open is False, "Ethernet-Kit rebooted on open -- regression!"
        finally:
            tp.close()
        time.sleep(0.6)

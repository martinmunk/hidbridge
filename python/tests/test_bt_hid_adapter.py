"""Offline tests for bt_hid adapter selection (the built-in-vs-dongle guard).

HARD REQUIREMENT (the maintainer): the tool must use the USB Bluetooth DONGLE, never the wsb built-in
adapter. `classify_adapter` + `select_adapter` are pure functions over adapter-info dicts so the
guard is unit-tested without touching real hardware.
"""

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

from hidbridge import adapter as A  # noqa: E402


# Real values captured on wsb 2026-07-09:
#   hci0 = Intel 8087:0026 removable=fixed  (BUILT-IN -> refuse)
#   hci1 = TP-Link UB500 / Realtek 2357:0604 removable=unknown, on an external hub (DONGLE)
BUILTIN = {"hci": "hci0", "address": "00:11:22:33:44:55", "usb_vendor": "8087",
           "usb_product": "0026", "product_name": "", "removable": "fixed"}
DONGLE = {"hci": "hci1", "address": "E8:48:B8:C8:20:00", "usb_vendor": "2357",
          "usb_product": "0604", "product_name": "TP-Link UB500 Adapter", "removable": "unknown"}


def test_classify_builtin_by_removable_fixed():
    assert A.classify_adapter(BUILTIN) == "builtin"


def test_classify_builtin_by_intel_vendor_even_if_removable_unknown():
    info = dict(BUILTIN, removable="unknown")
    assert A.classify_adapter(info) == "builtin"


def test_classify_dongle():
    assert A.classify_adapter(DONGLE) == "dongle"


def test_select_prefers_dongle_over_builtin():
    picked = A.select_adapter([BUILTIN, DONGLE])
    assert picked["hci"] == "hci1"


def test_select_refuses_when_only_builtin():
    with pytest.raises(A.NoDongleError):
        A.select_adapter([BUILTIN])


def test_select_honours_explicit_pin_to_dongle():
    picked = A.select_adapter([BUILTIN, DONGLE], prefer="hci1")
    assert picked["hci"] == "hci1"


def test_select_refuses_explicit_pin_to_builtin_unless_forced():
    with pytest.raises(A.BuiltinRefusedError):
        A.select_adapter([BUILTIN, DONGLE], prefer="hci0")
    # forced override works (documented escape hatch)
    picked = A.select_adapter([BUILTIN, DONGLE], prefer="hci0", allow_builtin=True)
    assert picked["hci"] == "hci0"


def test_select_pin_by_mac_address():
    picked = A.select_adapter([BUILTIN, DONGLE], prefer="E8:48:B8:C8:20:00")
    assert picked["hci"] == "hci1"

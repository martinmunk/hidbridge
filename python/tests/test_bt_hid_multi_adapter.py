"""Offline tests for bt_hid MULTI-adapter support: same-MAC detection, flexible dongle resolution,
and the preflight audit.

Motivated by the wsb hardware trap (2026-07-10): two TP-Link UB500 dongles SHARE a BD address
(a UB500 V1 defect, not runtime-reprogrammable). When both are plugged in, BlueZ/kernel routing
corrupts and the iPad never recognises the HID dongle. The tool must DETECT + WARN, and must let an
operator target a SPECIFIC dongle by a unique id (hciN / USB path) rather than the shared address.
Pure functions over adapter-info dicts so this is unit-tested with no hardware.
"""

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

from hidbridge import adapter as A  # noqa: E402

# Real wsb topology (2026-07-10): two same-MAC UB500 dongles, three distinct-MAC CSR dongles, the
# refused built-in. usb_path distinguishes the two UB500s even though the address does not.
UB500_A = {"hci": "hci1", "address": "E8:48:B8:C8:20:00", "usb_vendor": "2357",
           "usb_product": "0604", "product_name": "TP-Link UB500 Adapter",
           "removable": "unknown", "usb_path": "/sys/devices/pci/usb3/3-1"}
UB500_B = {"hci": "hci2", "address": "E8:48:B8:C8:20:00", "usb_vendor": "2357",
           "usb_product": "0604", "product_name": "TP-Link UB500 Adapter",
           "removable": "unknown", "usb_path": "/sys/devices/pci/usb3/3-2"}
CSR = {"hci": "hci3", "address": "00:66:77:88:99:AA", "usb_vendor": "0a12",
       "usb_product": "0001", "product_name": "CSR Bluetooth Dongle",
       "removable": "unknown", "usb_path": "/sys/devices/pci/usb3/3-3"}
BUILTIN = {"hci": "hci0", "address": "00:11:22:33:44:55", "usb_vendor": "8087",
           "usb_product": "0026", "product_name": "", "removable": "fixed",
           "usb_path": "/sys/devices/pci/usb3/3-0"}

ALL = [BUILTIN, UB500_A, UB500_B, CSR]


# ---- duplicate BD-address detection (the UB500 trap) ----
def test_duplicate_address_group_detected():
    groups = A.duplicate_address_groups(ALL)
    assert groups == {"E8:48:B8:C8:20:00": ["hci1", "hci2"]}


def test_no_duplicates_when_all_unique():
    assert A.duplicate_address_groups([BUILTIN, CSR]) == {}


def test_duplicate_ignores_all_zero_address():
    a = dict(CSR, address="00:00:00:00:00:00")
    b = dict(UB500_A, hci="hci9", address="00:00:00:00:00:00")
    assert A.duplicate_address_groups([a, b]) == {}


# ---- flexible dongle resolution ----
def test_resolve_by_hci():
    assert A.resolve_dongle("hci3", ALL)["hci"] == "hci3"


def test_resolve_by_usb_path_disambiguates_same_mac():
    # The shared address cannot pick one UB500, but the USB path can.
    assert A.resolve_dongle("3-2", ALL)["hci"] == "hci2"


def test_resolve_by_product_substring_is_ambiguous_for_two_ub500():
    with pytest.raises(A.AmbiguousDongleError):
        A.resolve_dongle("UB500", ALL)


def test_resolve_by_shared_mac_is_ambiguous():
    # Targeting the shared BD address must NOT silently pick one -- it is the whole trap.
    with pytest.raises(A.AmbiguousDongleError):
        A.resolve_dongle("E8:48:B8:C8:20:00", ALL)


def test_resolve_unique_mac_ok():
    assert A.resolve_dongle("00:66:77:88:99:AA", ALL)["hci"] == "hci3"


def test_resolve_no_match_raises_nodongle():
    with pytest.raises(A.NoDongleError):
        A.resolve_dongle("hci7", ALL)


def test_resolve_builtin_refused_unless_forced():
    with pytest.raises(A.BuiltinRefusedError):
        A.resolve_dongle("hci0", ALL)
    assert A.resolve_dongle("hci0", ALL, allow_builtin=True)["hci"] == "hci0"


def test_resolve_none_autopicks_lowest_dongle():
    # No id -> select_adapter semantics (lowest-index dongle, builtin refused).
    assert A.resolve_dongle(None, ALL)["hci"] == "hci1"


def test_adapter_slug_is_hci():
    assert A.adapter_slug(UB500_A) == "hci1"
    assert A.adapter_slug(UB500_B) == "hci2"  # distinct even though the address collides


# ---- preflight audit ----
def test_preflight_fails_on_duplicate_address():
    rep = A.preflight(ALL)
    assert rep["ok"] is False
    assert rep["duplicate_address_groups"] == {"E8:48:B8:C8:20:00": ["hci1", "hci2"]}
    assert any("DUPLICATE BD ADDRESS" in w for w in rep["warnings"])
    assert set(rep["dongles"]) == {"hci1", "hci2", "hci3"}
    assert rep["builtin"] == ["hci0"]


def test_preflight_ok_when_clean():
    rep = A.preflight([BUILTIN, CSR])
    assert rep["ok"] is True
    assert rep["warnings"] == []


def test_preflight_warns_when_no_dongle():
    rep = A.preflight([BUILTIN])
    assert rep["ok"] is True  # no duplicate hazard, but...
    assert any("no USB Bluetooth dongle" in w for w in rep["warnings"])

"""Regression test for the bt_hid BLE GATT D-Bus interface wiring.

BUG (2026-07-10, found live on mini6): the GATT Descriptor's ReadValue method was decorated with
the org.bluez.GattCharacteristic1 interface instead of org.bluez.GattDescriptor1. BlueZ therefore
could not serve the descriptor's value, so when iOS read the HID Report Reference (0x2908) descriptor
it got "Unlikely Error (0x0e)" -> iOS could not determine each report's ID/type -> it never enabled
the HID report characteristics and never subscribed (no CCCD write on 2a4d), so no keystroke/pointer
ever registered. Fix: put ReadValue (and GetAll) on org.bluez.GattDescriptor1. Verified live: a local
GATT client then reads the Report Reference descriptors as [1,1] (keyboard) and [2,1] (mouse).

Needs dbus + gi (system python); skips under uv's CPython which lacks them.
"""

import pathlib

import pytest

dbus = pytest.importorskip("dbus")          # skips under uv (no dbus)
pytest.importorskip("gi")

ROOT = pathlib.Path(__file__).resolve().parents[2]

from hidbridge import ble_hid as B  # noqa: E402

GATT_DESC = "org.bluez.GattDescriptor1"
GATT_CHRC = "org.bluez.GattCharacteristic1"


def _iface(method):
    return getattr(method, "_dbus_interface", None)


def test_descriptor_readvalue_on_gatt_descriptor_interface():
    # THE regression: must be GattDescriptor1, never GattCharacteristic1.
    assert _iface(B.Descriptor.ReadValue) == GATT_DESC
    assert _iface(B.Descriptor.GetAll) == "org.freedesktop.DBus.Properties"


def test_characteristic_methods_on_gatt_characteristic_interface():
    assert _iface(B.Characteristic.ReadValue) == GATT_CHRC
    assert _iface(B.Characteristic.WriteValue) == GATT_CHRC
    assert _iface(B.Characteristic.StartNotify) == GATT_CHRC
    assert _iface(B.Characteristic.StopNotify) == GATT_CHRC

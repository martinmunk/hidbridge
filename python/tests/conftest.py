"""Pytest configuration for the hidbridge test suite.

The suite is backend-agnostic and mostly OFFLINE (pure report-layer + parser + resolver logic).
Tests that need real hardware -- a BlueZ adapter, an MCU on a serial port, or an iPad to bond --
gate themselves on an environment variable (e.g. BT_HID_LOOPBACK / BT_HID_CENTRAL) or a `*_live.py`
filename and SKIP by default, so `pytest` on a dev box with no hardware runs clean.

`hidbridge` must be importable (install it: `pip install -e .`); there is no sys.path shim.
"""

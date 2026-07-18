"""Offline lifecycle tests for the BlueZ dongle LINK layer (bt_hid.conn.DongleLinkBackend).

The connect-on-demand context manager moved to hidbridge.bridge (HidBridge/HidSession); the dongle's
BLE link logic still lives in DongleLinkBackend, so these drive it DIRECTLY with a fake BlueZ manager
and a RecordingTransport (no hardware). Asserts the real contract:
  * connect() (re)connects the target on the right adapter,
  * exclusive connect DISCONNECTS any other connected central first,
  * report calls reach the transport as the exact reports.py bytes,
  * disconnect() drops the target but KEEPS the bond (never RemoveDevice),
  * a never-connecting device is reported, and the backend-agnostic target grammar dispatches.
"""

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

from hidbridge import conn as C  # noqa: E402
from hidbridge import reports as R  # noqa: E402
from hidbridge.controller import HidController, RecordingTransport  # noqa: E402

ADAPTER_PATH = "/org/bluez/hci1"
DONGLE = {"hci": "hci1", "address": "E8:48:B8:C8:20:00", "product_name": "TP-Link UB500 Adapter"}


class FakeLinkManager:
    """In-memory BlueZ stand-in. Devices keyed by object path; tracks connect/disconnect calls."""

    def __init__(self, devices):
        self.devices = devices
        self.connect_calls = []
        self.disconnect_calls = []
        self.removed = []  # would-be RemoveDevice -- must stay empty (bond kept)

    def find_device(self, adapter_path, ios_device):
        t = ios_device.lower()
        for path, props in self.devices.items():
            if not path.startswith(adapter_path + "/"):
                continue
            if t == str(props.get("Address", "")).lower() or t in str(props.get("Alias", "")).lower():
                return path, props
        return None, None

    def all_devices(self, adapter_path):
        return [(p, pr) for p, pr in self.devices.items() if p.startswith(adapter_path + "/")]

    def connected_devices(self, adapter_path):
        return [(p, pr) for p, pr in self.devices.items()
                if p.startswith(adapter_path + "/") and pr.get("Connected")]

    def is_connected(self, device_path):
        return bool(self.devices.get(device_path, {}).get("Connected"))

    def connect(self, device_path, timeout=20.0):
        self.connect_calls.append(device_path)
        self.devices[device_path]["Connected"] = True
        return True

    def disconnect(self, device_path):
        self.disconnect_calls.append(device_path)
        if device_path in self.devices:
            self.devices[device_path]["Connected"] = False
        return True


@pytest.fixture
def patch_resolve(monkeypatch):
    monkeypatch.setattr(C._adapter, "resolve_dongle", lambda *a, **k: DONGLE)


def _backend(link, transport):
    be = C.DongleLinkBackend("hci1", link_manager=link, transport=transport)
    be.prepare()
    return be


def test_connect_then_disconnect_keeps_bond(patch_resolve):
    ipad = f"{ADAPTER_PATH}/dev_AA_11"
    link = FakeLinkManager({ipad: {"Address": "AA:11", "Alias": "My iPad", "Connected": False}})
    be = _backend(link, RecordingTransport())
    assert be.connect("My iPad", 20.0, exclusive=True) is True
    assert link.is_connected(ipad)
    be.disconnect()
    assert link.connect_calls == [ipad]
    assert link.disconnect_calls == [ipad]
    assert link.removed == []               # bond intact -- never removed
    assert not link.is_connected(ipad)


def test_exclusive_disconnects_other_central_first(patch_resolve):
    target = f"{ADAPTER_PATH}/dev_AA_11"
    other = f"{ADAPTER_PATH}/dev_BB_22"
    link = FakeLinkManager({
        target: {"Address": "AA:11", "Alias": "Target iPad", "Connected": False},
        other: {"Address": "BB:22", "Alias": "Other iPad", "Connected": True},
    })
    be = _backend(link, RecordingTransport())
    be.connect("Target iPad", 20.0, exclusive=True)
    # the other central was disconnected so the target could take the single HID link
    assert other in link.disconnect_calls
    assert target in link.connect_calls


def test_reports_reach_transport_as_report_bytes(patch_resolve):
    ipad = f"{ADAPTER_PATH}/dev_AA_11"
    link = FakeLinkManager({ipad: {"Address": "AA:11", "Alias": "iPad", "Connected": False}})
    rec = RecordingTransport()
    be = _backend(link, rec)
    be.connect("iPad", 20.0, exclusive=True)
    ctl = HidController(be.transport())
    ctl.type_text("hi")
    ctl.send_key("enter")
    expected = [(r.kind, r.data) for r in R.type_text("hi")] + \
               [(r.kind, r.data) for r in R.key_reports("enter")]
    assert rec.frames == expected


def test_unlock_frames_flow_through(patch_resolve):
    ipad = f"{ADAPTER_PATH}/dev_AA_11"
    link = FakeLinkManager({ipad: {"Address": "AA:11", "Alias": "iPad", "Connected": False}})
    rec = RecordingTransport()
    be = _backend(link, rec)
    be.connect("iPad", 20.0, exclusive=True)
    HidController(be.transport()).unlock("1234")
    assert rec.frames == [(r.kind, r.data) for r in R.passcode_reports("1234", press_enter=True)]


def test_not_bonded_device_raises(patch_resolve):
    link = FakeLinkManager({})  # nothing bonded
    be = _backend(link, RecordingTransport())
    with pytest.raises(C.BtHidError):
        be.connect("Ghost iPad", 20.0, exclusive=True)


def test_never_connects_returns_false(patch_resolve):
    ipad = f"{ADAPTER_PATH}/dev_AA_11"
    link = FakeLinkManager({ipad: {"Address": "AA:11", "Alias": "iPad", "Connected": False}})
    link.connect = lambda device_path, timeout=20.0: False   # simulate the iPad never reconnecting
    be = _backend(link, RecordingTransport())
    assert be.connect("iPad", 0.1, exclusive=True) is False


# ---- backend-agnostic target grammar + capabilities ----
def test_dongle_capability_no_per_device_identity():
    b = C.make_link_backend("hci1")
    assert isinstance(b, C.DongleLinkBackend)
    assert b.capabilities["per_device_identity"] is False


def test_target_grammar_dispatch():
    assert isinstance(C.make_link_backend(None), C.DongleLinkBackend)
    assert isinstance(C.make_link_backend("hci2"), C.DongleLinkBackend)
    assert isinstance(C.make_link_backend("E8:48:B8:C8:20:00"), C.DongleLinkBackend)  # a MAC
    assert isinstance(C.make_link_backend("dongle:hci2"), C.DongleLinkBackend)
    from hidbridge.link_pico import PicoLinkBackend
    pico = C.make_link_backend("pico:/dev/ttyACM0")
    assert isinstance(pico, PicoLinkBackend)
    assert pico._port == "/dev/ttyACM0"
    mcu = C.make_link_backend("madeupmcu:/dev/ttyACM9")
    assert isinstance(mcu, C.SerialMcuLinkBackend)
    assert mcu.name == "madeupmcu"
    assert mcu._opts.get("port") == "/dev/ttyACM9"

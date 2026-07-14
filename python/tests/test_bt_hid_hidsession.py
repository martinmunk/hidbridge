"""Offline lifecycle tests for bt_hid.conn.HidSession (connect-on-demand context manager).

Exercised with NO hardware: a FakeLinkManager models BlueZ device state, a RecordingTransport
captures relayed frames, and adapter.resolve_dongle is monkeypatched to a fixed dongle. Asserts the
real end-to-end contract:
  * enter CONNECTS the target iOS device on the right adapter,
  * exclusive mode DISCONNECTS any other connected central first,
  * report calls (type_text/unlock/...) reach the transport as the exact reports.py bytes,
  * exit DISCONNECTS the target but KEEPS the bond (never RemoveDevice),
  * a never-connecting device raises,
  * the backend-agnostic target grammar + capabilities surface correctly.
"""

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

from hidbridge import conn as C  # noqa: E402
from hidbridge import reports as R  # noqa: E402
from hidbridge.controller import RecordingTransport  # noqa: E402

ADAPTER_PATH = "/org/bluez/hci1"
DONGLE = {"hci": "hci1", "address": "E8:48:B8:C8:20:00", "product_name": "TP-Link UB500 Adapter"}


class FakeLinkManager:
    """In-memory BlueZ stand-in. Devices keyed by object path; tracks connect/disconnect calls."""

    def __init__(self, devices):
        # devices: {path: {"Address":.., "Alias":.., "Connected": bool}}
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


def _dongle_backend(link, transport):
    return C.DongleLinkBackend("hci1", link_manager=link, transport=transport)


def test_enter_connects_and_exit_disconnects_keeping_bond(patch_resolve):
    ipad = f"{ADAPTER_PATH}/dev_AA_11"
    link = FakeLinkManager({ipad: {"Address": "AA:11", "Alias": "My iPad", "Connected": False}})
    rec = RecordingTransport()
    with C.HidSession("hci1", "My iPad", backend=_dongle_backend(link, rec)) as hid:
        assert link.is_connected(ipad)
        assert hid.controller is not None
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
    with C.HidSession("hci1", "Target iPad", backend=_dongle_backend(link, RecordingTransport())):
        pass
    # the other central was disconnected so the target could take the single HID link
    assert other in link.disconnect_calls
    assert target in link.connect_calls


def test_reports_reach_transport_as_report_bytes(patch_resolve):
    ipad = f"{ADAPTER_PATH}/dev_AA_11"
    link = FakeLinkManager({ipad: {"Address": "AA:11", "Alias": "iPad", "Connected": False}})
    rec = RecordingTransport()
    with C.HidSession("hci1", "iPad", backend=_dongle_backend(link, rec)) as hid:
        hid.type_text("hi")
        hid.send_key("enter")
    expected = [(r.kind, r.data) for r in R.type_text("hi")] + \
               [(r.kind, r.data) for r in R.key_reports("enter")]
    assert rec.frames == expected


def test_unlock_frames_flow_through(patch_resolve):
    ipad = f"{ADAPTER_PATH}/dev_AA_11"
    link = FakeLinkManager({ipad: {"Address": "AA:11", "Alias": "iPad", "Connected": False}})
    rec = RecordingTransport()
    with C.HidSession("hci1", "iPad", backend=_dongle_backend(link, rec)) as hid:
        hid.unlock("1234")
    assert rec.frames == [(r.kind, r.data) for r in R.passcode_reports("1234", press_enter=True)]


def test_not_bonded_device_raises(patch_resolve):
    link = FakeLinkManager({})  # nothing bonded
    with pytest.raises(C.BtHidError):
        with C.HidSession("hci1", "Ghost iPad", backend=_dongle_backend(link, RecordingTransport())):
            pass


def test_never_connects_raises_when_required(patch_resolve):
    ipad = f"{ADAPTER_PATH}/dev_AA_11"
    link = FakeLinkManager({ipad: {"Address": "AA:11", "Alias": "iPad", "Connected": False}})

    def _fail_connect(device_path, timeout=20.0):
        return False  # simulate the iPad never reconnecting
    link.connect = _fail_connect
    with pytest.raises(C.BtHidError):
        with C.HidSession("hci1", "iPad", require_connect=True,
                          backend=_dongle_backend(link, RecordingTransport())):
            pass


def test_require_connect_false_proceeds(patch_resolve):
    ipad = f"{ADAPTER_PATH}/dev_AA_11"
    link = FakeLinkManager({ipad: {"Address": "AA:11", "Alias": "iPad", "Connected": False}})
    link.connect = lambda device_path, timeout=20.0: False
    rec = RecordingTransport()
    with C.HidSession("hci1", "iPad", require_connect=False,
                      backend=_dongle_backend(link, rec)) as hid:
        hid.type_text("x")
    assert rec.frames == [(r.kind, r.data) for r in R.type_text("x")]


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
    # A backend name with a dedicated bt_hid.link_<name> module loads THAT (the MCU-robust work
    # supplies link_pico / link_esp32); a truly-unknown name falls back to the generic MCU wrapper.
    from hidbridge.link_pico import PicoLinkBackend
    pico = C.make_link_backend("pico:/dev/ttyACM0")
    assert isinstance(pico, PicoLinkBackend)
    assert pico._port == "/dev/ttyACM0"
    mcu = C.make_link_backend("madeupmcu:/dev/ttyACM9")
    assert isinstance(mcu, C.SerialMcuLinkBackend)
    assert mcu.name == "madeupmcu"
    assert mcu._opts.get("port") == "/dev/ttyACM9"


def test_hidsession_exposes_capabilities_via_fake_backend():
    class FakeBackend(C.LinkBackend):
        name = "fake"
        capabilities = {"per_device_identity": True}

        def __init__(self):
            self.events = []
            self._tx = RecordingTransport()

        def prepare(self):
            self.events.append("prepare")

        def connect(self, ios_device, timeout=20.0, exclusive=True):
            self.events.append(("connect", ios_device))
            return True

        def disconnect(self):
            self.events.append("disconnect")

        def is_connected(self):
            return True

        def transport(self):
            return self._tx

    fake = FakeBackend()
    sess = C.HidSession("fake:x", "iPad", backend=fake)
    assert sess.supports("per_device_identity") is True
    with sess as hid:
        hid.type_text("q")
    assert fake.events[0] == "prepare"
    assert fake.events[1] == ("connect", "iPad")
    assert fake.events[-1] == "disconnect"
    assert fake._tx.frames == [(r.kind, r.data) for r in R.type_text("q")]

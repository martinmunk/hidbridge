"""Offline tests: the MCU link backends (link_esp32 / link_pico) plug behind the shared HidSession.

These prove the ESP32/Pico boards are INTERCHANGEABLE with the dongle behind bt_hid.conn.HidSession:
conn.make_link_backend("esp32:/dev/ttyUSB1") auto-loads bt_hid.link_esp32; the LinkBackend contract
(prepare/connect/disconnect/is_connected/transport/capabilities/describe) drives a connect-on-demand
lifecycle with a FAKE transport (no hardware); and per_device_identity is advertised as an MCU
capability (the clean route the BlueZ backend cannot offer).
"""

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

from hidbridge import conn as C  # noqa: E402
from hidbridge import link_esp32, link_pico  # noqa: E402


class FakeMcuTransport:
    """Duck-typed MCU transport: records frames, scriptable connect state + disconnect/close."""

    def __init__(self, connected=True, pico=False, subscribed=None):
        self.frames = []
        self._connected = connected
        # Model the firmware readiness ack (iOS subscribed to the HID reports); defaults to the link
        # state so a "connected" board reports ready and the HidSession readiness gate resolves fast.
        self._subscribed = connected if subscribed is None else subscribed
        self._pico = pico
        self.closed = False
        self.disconnected = False
        self.advertised = False

    def send(self, frames):
        self.frames.extend(frames)
        return {"ok": True, "sent": len(frames)}

    def advertise(self):
        self.advertised = True
        return {"ok": True}

    def wait_connected(self, timeout=20.0, poll=1.0):
        return self._connected

    def ping(self):
        sub = f"subscribed={int(self._subscribed)} "
        if self._pico:
            return {"ok": True,
                    "reply": f"PONG name=wsb-hid-pico {sub}conn={int(self._connected)}"}
        return {"ok": True, "connected": self._connected, "subscribed": self._subscribed,
                "resp": f"OK wsb-hid-esp32 {sub}conn={int(self._connected)}"}

    def is_ready(self):
        return bool(self._subscribed and self._connected)

    def wait_ready(self, timeout=8.0, poll=0.5):
        return self.is_ready()

    def is_connected(self):
        return self._connected

    def disconnect(self):
        self.disconnected = True
        return {"ok": True, "resp": "OK disc"}

    def close(self):
        self.closed = True


# --------------------------------------------------------------------------- #
# conn.make_link_backend auto-loads the dedicated MCU modules (not the generic fallback)
# --------------------------------------------------------------------------- #
def test_conn_loads_dedicated_esp32_backend():
    be = C.make_link_backend("esp32:/dev/ttyUSB1")
    assert isinstance(be, link_esp32.Esp32LinkBackend)
    assert be._port == "/dev/ttyUSB1"


def test_conn_loads_dedicated_pico_backend():
    be = C.make_link_backend("pico:/dev/ttyACM0")
    assert isinstance(be, link_pico.PicoLinkBackend)
    assert be._port == "/dev/ttyACM0"


def test_mcu_backends_advertise_per_device_identity_capability():
    assert C.make_link_backend("esp32:/dev/ttyUSB1").capabilities["per_device_identity"] is True
    assert C.make_link_backend("pico:/dev/ttyACM0").capabilities["per_device_identity"] is True
    # the dongle backend cannot (BlueZ: one identity per adapter)
    assert C.make_link_backend("hci0").capabilities["per_device_identity"] is False


# --------------------------------------------------------------------------- #
# Full connect-on-demand lifecycle through HidSession with a fake transport (no hardware)
# --------------------------------------------------------------------------- #
def test_esp32_hidsession_lifecycle(monkeypatch):
    fake = FakeMcuTransport(connected=True)
    monkeypatch.setattr(link_esp32, "make_transport", lambda name, **o: fake)
    with C.HidSession("esp32:/dev/ttyUSB1", "mini6") as hid:
        assert fake.advertised is True          # connect kicked advertising
        hid.type_text("1")                       # frames flow through reports.py
        assert fake.frames[0][0] == "keyboard"
    # exit -> disconnect (firmware 'X') + close (leaves tty B0)
    assert fake.disconnected is True
    assert fake.closed is True


def test_esp32_hidsession_raises_if_never_connects(monkeypatch):
    fake = FakeMcuTransport(connected=False)
    monkeypatch.setattr(link_esp32, "make_transport", lambda name, **o: fake)
    with pytest.raises(C.BtHidError):
        with C.HidSession("esp32:/dev/ttyUSB1", "mini6", connect_timeout=0.1):
            pass


def test_pico_hidsession_lifecycle(monkeypatch):
    fake = FakeMcuTransport(connected=True, pico=True)
    monkeypatch.setattr(link_pico, "make_transport", lambda name, **o: fake)
    with C.HidSession("pico:/dev/ttyACM0", "mini6") as hid:
        hid.send_key("space")
        assert fake.frames[0][0] == "keyboard"
    assert fake.closed is True


def test_pico_connected_parse():
    assert link_pico._pong_connected("PONG name=wsb-hid-pico conn=1") is True
    assert link_pico._pong_connected("PONG name=wsb-hid-pico conn=0") is False
    assert link_pico._pong_connected("") is False

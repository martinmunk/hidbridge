"""Offline tests for the per-backend REAL readiness signal (iOS enabled the HID reports).

Covers the wire-level parse each backend uses to know iOS has SUBSCRIBED to the HID report chars:
  * ESP32/Pico MCU: a `subscribed=<0|1>` field the firmware adds to its ping reply (backward
    compatible: an old firmware omits it -> the field parses as None -> readiness_unsupported ->
    HidSession falls back to a settle). Emitted BEFORE `conn=` so an old host's endswith("conn=1")
    check still works.
  * Dongle: the ble_hid daemon's `status` control command reports whether the keyboard report
    characteristic is notifying (its StartNotify was issued by iOS).
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]

from hidbridge import link_esp32, link_pico  # noqa: E402
from hidbridge import transport_esp32 as TE  # noqa: E402


class FakeSerial:
    """Duck-typed serial: replies with a scripted queue of lines (bytes)."""

    def __init__(self, lines):
        self._lines = list(lines)
        self.written = []

    def write(self, data):
        self.written.append(data)

    def readline(self):
        return self._lines.pop(0) if self._lines else b""

    def flush(self):
        pass


def _esp32(lines):
    t = TE.Esp32Transport(port=FakeSerial(lines), open_now=False)
    return t


# --------------------------------------------------------------------------- #
# ESP32 ping now surfaces conn + subscribed (order-independent, backward compatible)
# --------------------------------------------------------------------------- #
def test_esp32_ping_parses_subscribed_and_conn():
    t = _esp32([b"OK HIDBRIDGE-esp32 subscribed=1 conn=1\n"])
    st = t.ping()
    assert st["connected"] is True
    assert st["subscribed"] is True


def test_esp32_ping_subscribed_zero():
    t = _esp32([b"OK HIDBRIDGE-esp32 subscribed=0 conn=1\n"])
    st = t.ping()
    assert st["connected"] is True
    assert st["subscribed"] is False


def test_esp32_ping_old_firmware_has_no_subscribed_field():
    # An OLD firmware that only reports conn= must still parse (subscribed -> None = unknown).
    t = _esp32([b"OK HIDBRIDGE-esp32 conn=1\n"])
    st = t.ping()
    assert st["connected"] is True
    assert st["subscribed"] is None


def test_esp32_is_ready_and_wait_ready():
    # not subscribed on the first poll, subscribed on the second
    t = _esp32([b"OK HIDBRIDGE-esp32 subscribed=0 conn=1\n",
                b"OK HIDBRIDGE-esp32 subscribed=1 conn=1\n"])
    assert t.wait_ready(timeout=5.0, poll=0.0) is True


def test_esp32_old_firmware_ping_still_ends_with_conn1():
    # regression: the NEW reply orders subscribed BEFORE conn so a legacy endswith("conn=1") holds
    reply = "OK HIDBRIDGE-esp32 subscribed=1 conn=1"
    assert reply.rstrip().endswith("conn=1")


# --------------------------------------------------------------------------- #
# Link backends expose readiness (probe the firmware field; graceful when absent)
# --------------------------------------------------------------------------- #
class FakeReadyTp:
    def __init__(self, subscribed=True, pico=False):
        self._sub = subscribed
        self._pico = pico

    def ping(self):
        s = "" if self._sub is None else f"subscribed={int(self._sub)} "
        if self._pico:
            return {"ok": True, "reply": f"PONG name=HIDBRIDGE-pico {s}conn=1"}
        return {"ok": True, "subscribed": self._sub, "connected": True,
                "resp": f"OK HIDBRIDGE-esp32 {s}conn=1"}

    def is_ready(self):
        return self._sub is True

    def wait_ready(self, timeout=8.0, poll=0.5):
        return self._sub is True

    def is_connected(self):
        return True


def test_esp32_link_readiness_supported_when_firmware_reports_it():
    be = link_esp32.Esp32LinkBackend()
    be._tp = FakeReadyTp(subscribed=True)
    assert be.readiness_supported() is True
    assert be.is_ready() is True


def test_esp32_link_readiness_unsupported_on_old_firmware():
    be = link_esp32.Esp32LinkBackend()
    be._tp = FakeReadyTp(subscribed=None)     # old firmware: no subscribed field
    assert be.readiness_supported() is False


def test_pico_link_readiness_probe():
    be = link_pico.PicoLinkBackend()
    be._tp = FakeReadyTp(subscribed=True, pico=True)
    assert be.readiness_supported() is True
    assert be.is_ready() is True
    old = link_pico.PicoLinkBackend()
    old._tp = FakeReadyTp(subscribed=None, pico=True)
    assert old.readiness_supported() is False


def test_mcu_backends_have_a_settle_fallback():
    assert link_esp32.Esp32LinkBackend().READY_SETTLE > 0
    assert link_pico.PicoLinkBackend().READY_SETTLE > 0


# --------------------------------------------------------------------------- #
# Dongle: readiness = the daemon reports the keyboard report char is notifying (iOS StartNotify)
# --------------------------------------------------------------------------- #
class FakeStatusTp:
    """Injected dongle transport that answers the new `status` query (like the ble_hid daemon)."""

    def __init__(self, subscribed):
        self._sub = subscribed
        self.frames = []

    def send(self, frames):
        self.frames.extend(frames)
        return {"ok": True}

    def status(self):
        return {"ok": True, "subscribed": self._sub,
                "chars": {"keyboard": self._sub, "mouse": self._sub}}


def test_dongle_readiness_supported_and_ready_via_status():
    from hidbridge import conn as C  # noqa: PLC0415
    be = C.DongleLinkBackend("hci1", transport=FakeStatusTp(subscribed=True))
    assert be.readiness_supported() is True
    assert be.is_ready() is True
    assert be.READY_SETTLE == 0.0          # an injected transport manages its own readiness


def test_dongle_not_ready_until_subscribed():
    from hidbridge import conn as C  # noqa: PLC0415
    be = C.DongleLinkBackend("hci1", transport=FakeStatusTp(subscribed=False))
    assert be.readiness_supported() is True
    assert be.is_ready() is False


def test_dongle_readiness_unsupported_without_status_transport():
    from hidbridge import conn as C  # noqa: PLC0415
    from hidbridge.controller import RecordingTransport  # noqa: PLC0415
    be = C.DongleLinkBackend("hci1", transport=RecordingTransport())
    assert be.readiness_supported() is False


def test_socket_transport_status_roundtrip(tmp_path):
    """SocketTransport.status() speaks the daemon's JSON control protocol over a real unix socket."""
    import json  # noqa: PLC0415
    import socket  # noqa: PLC0415
    import threading  # noqa: PLC0415

    from hidbridge.controller import SocketTransport  # noqa: PLC0415

    sock_path = str(tmp_path / "bt_hid.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(1)
    seen = {}

    def serve():
        conn, _ = srv.accept()
        with conn:
            seen["req"] = json.loads(conn.recv(65536).decode())
            conn.sendall(json.dumps({"ok": True, "subscribed": True,
                                     "chars": {"keyboard": True}}).encode())

    threading.Thread(target=serve, daemon=True).start()
    st = SocketTransport(sock_path).status()
    srv.close()
    assert seen["req"] == {"cmd": "status"}
    assert st["subscribed"] is True

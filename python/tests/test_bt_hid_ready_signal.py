"""Offline tests for the per-backend REAL readiness signal (iOS enabled the HID reports).

Covers the readiness signal the dongle backend uses to know iOS has SUBSCRIBED to the HID report
chars: the ble_hid daemon's `status` control command reports whether the keyboard report
characteristic is notifying (its StartNotify was issued by iOS).

NOTE: the MCU (ESP32/Pico) readiness tests that exercised the single-char `P` ping's `subscribed=`
field on the transport + link backends were REMOVED -- the firmware no longer speaks the single-char
protocol, so those senders (transport.ping/is_ready/wait_ready and the link-backend readiness probes)
are gone. MCU readiness now rides the JSON-RPC path (board.McuBoard / rpc.py). Only the MCU backends'
static settle fallback survives here.
"""

from hidbridge import link_esp32, link_pico


# --------------------------------------------------------------------------- #
# MCU link backends keep a settle fallback (a class constant, no transport I/O)
# --------------------------------------------------------------------------- #
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

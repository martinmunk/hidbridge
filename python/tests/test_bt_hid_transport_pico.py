"""Offline tests for the Pico HID backend (hidbridge/transport_pico.py).

The Pico backend ships each HID report as one ASCII line over USB serial to a Raspberry Pi Pico 2 W
running the BTstack HOG firmware (firmware/pico/); the Pico re-emits it as a BLE HID notification.
These tests pin the line-protocol framing + the transport's send/ack behaviour with a MOCK serial
port, so they run with no pyserial and no hardware. They also prove the host encoder and the
firmware's parser agree (decode_frame mirrors the firmware's parse), and that reports.py stays the
single source of truth for the report BYTES (the exact frames a real timeline emits).
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]

import pytest  # noqa: E402

from hidbridge import reports as R  # noqa: E402
from hidbridge.controller import HidController  # noqa: E402
from hidbridge import transport_pico as TP  # noqa: E402
from hidbridge.transport_pico import (  # noqa: E402
    PicoSerialTransport,
    ack_is_ok,
    autodetect_port,
    decode_frame,
    encode_frame,
    encode_ping,
)


# --------------------------------------------------------------------------- #
# Mock serial port
# --------------------------------------------------------------------------- #
class FakeSerial:
    """Minimal pyserial-like stand-in: records writes, replies with queued lines."""

    def __init__(self, replies=None):
        self.written = b""
        self._replies = list(replies) if replies else []
        self.closed = False
        # default: an "OK\n" per written line, generated lazily if no explicit replies given
        self._auto_ok = replies is None

    def write(self, data):
        self.written += data
        return len(data)

    def flush(self):
        pass

    def readline(self):
        if self._replies:
            return self._replies.pop(0)
        if self._auto_ok:
            return b"OK\n"
        return b""

    def close(self):
        self.closed = True


def _transport(replies=None, **kw):
    fake = FakeSerial(replies)
    t = PicoSerialTransport(port="/dev/fake", open_factory=lambda *a, **k: fake, **kw)
    return t, fake


# --------------------------------------------------------------------------- #
# Pure framing
# --------------------------------------------------------------------------- #
def test_encode_keyboard_frame_bytes():
    body = bytes([0x00, 0x00, 0x04, 0, 0, 0, 0, 0])  # 'a' down
    assert encode_frame("keyboard", body) == b"K 0000040000000000\n"


def test_encode_mouse_frame_bytes():
    body = bytes([0x01, 5, (-3) & 0xFF, 0x00])  # left button, dx=5, dy=-3
    assert encode_frame("mouse", body) == b"M 0105fd00\n"


def test_encode_rejects_bad_kind_and_length():
    with pytest.raises(ValueError):
        encode_frame("joystick", bytes(8))
    with pytest.raises(ValueError):
        encode_frame("keyboard", bytes(7))   # keyboard must be 8
    with pytest.raises(ValueError):
        encode_frame("mouse", bytes(5))       # mouse must be 4


def test_encode_abs_mouse_frame_bytes():
    # buttons=left, X=0x1234, Y=0x0056 (little-endian on the wire) -> 5-byte body
    body = bytes([R.BTN_LEFT, 0x34, 0x12, 0x56, 0x00])
    assert encode_frame("abs_mouse", body) == b"A 0134125600\n"


def test_encode_digitizer_frame_bytes():
    # tip+in_range, contactID=0, X=0x00FF, Y=0x7FFF -> 6-byte body
    body = bytes([R.TIP_SWITCH | R.IN_RANGE, 0x00, 0xFF, 0x00, 0xFF, 0x7F])
    assert encode_frame("digitizer", body) == b"D 0300ff00ff7f\n"


def test_decode_is_inverse_of_encode():
    cases = [
        ("keyboard", bytes(range(8))),
        ("mouse", bytes([2, 1, 255, 7])),
        ("abs_mouse", bytes([1, 0x34, 0x12, 0x56, 0x00])),
        ("digitizer", bytes([3, 0, 0xFF, 0x00, 0xFF, 0x7F])),
    ]
    for kind, body in cases:
        assert decode_frame(encode_frame(kind, body)) == (kind, body)


def test_encode_rejects_wrong_abs_and_digitizer_length():
    with pytest.raises(ValueError):
        encode_frame("abs_mouse", bytes(4))     # abs_mouse must be 5
    with pytest.raises(ValueError):
        encode_frame("digitizer", bytes(5))     # digitizer must be 6


def test_encode_ping_and_ack_parsing():
    assert encode_ping() == b"P\n"
    assert ack_is_ok(b"OK\n") is True
    assert ack_is_ok(b"ok\r\n") is True
    assert ack_is_ok(b"ERR notconnected\n") is False
    assert ack_is_ok(b"") is False


# --------------------------------------------------------------------------- #
# Transport behaviour (mock serial)
# --------------------------------------------------------------------------- #
def test_send_writes_one_line_per_frame_and_counts_acks():
    t, fake = _transport()
    frames = [("keyboard", bytes([0, 0, 0x04, 0, 0, 0, 0, 0])), ("keyboard", bytes(8))]
    resp = t.send(frames)
    assert resp == {"ok": True, "sent": 2}
    assert fake.written == b"K 0000040000000000\n" + b"K 0000000000000000\n"


def test_send_stops_and_reports_on_err_ack():
    t, fake = _transport(replies=[b"OK\n", b"ERR notconnected\n"])
    frames = [("mouse", bytes([1, 0, 0, 0])), ("mouse", bytes(4))]
    resp = t.send(frames)
    assert resp["ok"] is False
    assert resp["sent"] == 1
    assert "notconnected" in resp["error"]


def test_send_without_ack_does_not_read():
    t, fake = _transport(ack=False)
    resp = t.send([("mouse", bytes([0, 1, 2, 3]))])
    assert resp == {"ok": True, "sent": 1}
    assert fake.written == b"M 00010203\n"


def test_ping_returns_pong_line():
    t, fake = _transport(replies=[b"PONG name=wsb-hid-pico conn=1\n"])
    resp = t.ping()
    assert resp["ok"] is True
    assert resp["reply"] == "PONG name=wsb-hid-pico conn=1"
    assert fake.written == b"P\n"


def test_persist_false_closes_after_send():
    t, fake = _transport(persist=False)
    t.send([("keyboard", bytes(8))])
    assert fake.closed is True


# --------------------------------------------------------------------------- #
# reports.py remains the single source of truth (controller -> pico wire bytes)
# --------------------------------------------------------------------------- #
def test_controller_type_text_over_pico_emits_expected_wire_lines():
    t, fake = _transport()
    ctl = HidController(t)
    ctl.type_text("1")
    # 'K' down (usage 0x1E for '1') then all-up
    assert fake.written == b"K 00001e0000000000\n" + b"K 0000000000000000\n"


def test_autodetect_matches_pico_byid_name(monkeypatch):
    monkeypatch.setattr(TP.glob, "glob", lambda p: (
        ["/dev/serial/by-id/usb-Raspberry_Pi_Pico_2_W_E123-if00"]
        if "by-id" in p else []))
    assert autodetect_port() == "/dev/serial/by-id/usb-Raspberry_Pi_Pico_2_W_E123-if00"


def test_autodetect_never_selects_a_non_pico_serial_device(monkeypatch):
    # This host has a CH340 KVM controller + a CP2102 on ttyUSB and possibly a non-Pico ttyACM.
    # Autodetect MUST NOT return any of them (writing HID lines to the KVM would be harmful).
    def fake_glob(p):
        if "by-id" in p:
            return ["/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0",
                    "/dev/serial/by-id/usb-Silicon_Labs_CP2102_...-if00-port0"]
        if "ttyACM" in p:
            return ["/dev/ttyACM0"]
        return []
    monkeypatch.setattr(TP.glob, "glob", fake_glob)
    monkeypatch.setattr(TP, "_tty_usb_vendor", lambda dev: "1234")  # not 2e8a
    assert autodetect_port() is None


def test_autodetect_picks_pico_vendor_ttyacm(monkeypatch):
    monkeypatch.setattr(TP.glob, "glob",
                        lambda p: ["/dev/ttyACM0", "/dev/ttyACM1"] if "ttyACM" in p else [])
    monkeypatch.setattr(TP, "_tty_usb_vendor",
                        lambda dev: "2e8a" if dev == "/dev/ttyACM1" else "1a86")
    assert autodetect_port() == "/dev/ttyACM1"


def test_controller_mouse_click_over_pico_uses_reports_bytes():
    t, fake = _transport()
    ctl = HidController(t)
    ctl.click("right")
    down = encode_frame("mouse", bytes([R.BTN_RIGHT, 0, 0, 0]))
    up = encode_frame("mouse", bytes([0, 0, 0, 0]))
    assert fake.written == down + up


# --------------------------------------------------------------------------- #
# Absolute pointer + digitizer: reports.py stays the single source of truth. The framed body a real
# timeline sends over serial MUST byte-match the report body reports.py builds -- so the Pico, which
# only prepends the reportID, emits exactly the bytes reports.py defines.
# --------------------------------------------------------------------------- #
def test_abs_mouse_frame_byte_matches_reports_body():
    rep = R.abs_mouse_report(buttons=R.BTN_LEFT, x=0x1234, y=0x0056)
    assert rep.kind == "abs_mouse"
    assert encode_frame(rep.kind, rep.data) == b"A " + rep.data.hex().encode() + b"\n"
    # the wire body is exactly reports.py's ABS_MOUSE_BODY_LEN bytes (buttons, X-LE, Y-LE)
    assert len(rep.data) == R.ABS_MOUSE_BODY_LEN
    assert rep.data == bytes([R.BTN_LEFT, 0x34, 0x12, 0x56, 0x00])


def test_digitizer_frame_byte_matches_reports_body():
    rep = R.digitizer_report(tip=True, contact_id=0, x=0x00FF, y=0x7FFF)
    assert rep.kind == "digitizer"
    assert encode_frame(rep.kind, rep.data) == b"D " + rep.data.hex().encode() + b"\n"
    assert len(rep.data) == R.DIGITIZER_BODY_LEN
    assert rep.data == bytes([R.TIP_SWITCH | R.IN_RANGE, 0x00, 0xFF, 0x00, 0xFF, 0x7F])


def test_controller_tap_abs_over_pico_uses_reports_bytes():
    t, fake = _transport()
    ctl = HidController(t)
    ctl.tap_abs(0.5, 0.25, "left")
    expect = b"".join(encode_frame(r.kind, r.data) for r in R.abs_click_reports(0.5, 0.25, "left"))
    assert fake.written == expect


def test_controller_touch_tap_over_pico_uses_reports_bytes():
    t, fake = _transport()
    ctl = HidController(t)
    ctl.touch_tap(0.5, 0.25)
    expect = b"".join(encode_frame(r.kind, r.data) for r in R.digitizer_tap_reports(0.5, 0.25))
    assert fake.written == expect


# --------------------------------------------------------------------------- #
# Firmware report map == reports.py REPORT_DESCRIPTOR (byte-identical). The Pico firmware embeds the
# HID report map as a C array between two markers; extract it and assert equality, so the firmware
# descriptor can never silently drift from the Python single-source-of-truth.
# --------------------------------------------------------------------------- #
_FIRMWARE_MAIN = ROOT / "firmware" / "pico" / "src" / "main.cpp"


def _extract_firmware_report_map() -> bytes:
    import re
    text = _FIRMWARE_MAIN.read_text()
    m = re.search(r"WSB-HID-REPORT-MAP-BEGIN(.*?)WSB-HID-REPORT-MAP-END", text, re.S)
    assert m, "report-map markers not found in firmware main.cpp"
    # drop // comment tails so commented byte counts / labels are ignored
    stripped = "\n".join(line.split("//", 1)[0] for line in m.group(1).splitlines())
    return bytes(int(h, 16) for h in re.findall(r"0x([0-9A-Fa-f]{2})", stripped))


def test_firmware_report_map_matches_reports_descriptor():
    assert _extract_firmware_report_map() == R.REPORT_DESCRIPTOR


# --------------------------------------------------------------------------- #
# Generic backend loader entry point: `bt_hid --backend pico` routes through
# controller.make_transport("pico") -> transport_pico.make_transport(), so the Pico must expose the
# same module-level factory the ESP32 backend does (else the CLI raises AttributeError at runtime).
# --------------------------------------------------------------------------- #
def test_module_make_transport_returns_pico_transport():
    tp = TP.make_transport(port="/dev/ttyACM9")
    assert isinstance(tp, PicoSerialTransport)
    assert tp.port == "/dev/ttyACM9"


def test_controller_make_transport_selects_pico():
    from hidbridge.controller import make_transport
    tp = make_transport("pico", port="/dev/ttyACM9")
    assert isinstance(tp, PicoSerialTransport)
    assert tp.port == "/dev/ttyACM9"

"""Offline tests for the ESP32 HID backend (hidbridge/transport_esp32.py).

The ESP32 backend streams the SAME report bytes that reports.py produces to an ESP32 over USB serial
(the ESP32 relays them as BLE HID notifications). These tests assert the serial framing + the exact
report bytes WITHOUT any hardware, using a fake serial port, and verify the controller routes through
the backend so reports.py stays the single source of truth.
"""

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

from hidbridge import reports as R  # noqa: E402
from hidbridge import transport_esp32 as TE  # noqa: E402
from hidbridge.controller import HidController, make_transport  # noqa: E402


class FakeSerial:
    """Duck-typed serial port: records written lines, replies 'OK' to each command."""

    def __init__(self, replies=None):
        self.written = []
        self._replies = list(replies) if replies is not None else None
        self.port = "/dev/fake"

    def write(self, data: bytes):
        self.written.append(data)
        return len(data)

    def flush(self):
        pass

    def readline(self) -> bytes:
        if self._replies is not None:
            return self._replies.pop(0) if self._replies else b""
        return b"OK\n"

    def close(self):
        pass


# --------------------------------------------------------------------------- #
# encode_frame: pure serial framing
# --------------------------------------------------------------------------- #
def test_encode_keyboard_frame():
    body = bytes([0x00, 0x00, 0x1E, 0, 0, 0, 0, 0])   # 'type 1' down report body (8 bytes)
    assert TE.encode_frame("keyboard", body) == b"K00001e0000000000\n"


def test_encode_mouse_frame():
    body = bytes([0x00, 5, (-3) & 0xFF, 0x00])
    assert TE.encode_frame("mouse", body) == b"M0005fd00\n"


def test_encode_abs_mouse_frame():
    # abs-mouse body = buttons, X(u16 LE), Y(u16 LE); 5 bytes -> 10 hex, "A" prefix
    body = bytes([R.BTN_LEFT, 0xFF, 0x7F, 0x00, 0x40])   # left button, X=32767, Y=16384
    assert TE.encode_frame("abs_mouse", body) == b"A01ff7f0040\n"


def test_encode_digitizer_frame():
    # digitizer body = flags, contactID, X(u16 LE), Y(u16 LE); 6 bytes -> 12 hex, "D" prefix
    body = bytes([R.TIP_SWITCH | R.IN_RANGE, 0x00, 0x00, 0x00, 0xFF, 0x7F])  # tip+inrange, X=0, Y=32767
    assert TE.encode_frame("digitizer", body) == b"D03000000ff7f\n"


def test_encode_rejects_unknown_kind():
    with pytest.raises(TE.Esp32Error):
        TE.encode_frame("touch", b"\x00")


def test_encode_rejects_wrong_length():
    with pytest.raises(TE.Esp32Error):
        TE.encode_frame("keyboard", bytes(4))
    with pytest.raises(TE.Esp32Error):
        TE.encode_frame("mouse", bytes(8))
    with pytest.raises(TE.Esp32Error):
        TE.encode_frame("abs_mouse", bytes(4))    # must be exactly 5
    with pytest.raises(TE.Esp32Error):
        TE.encode_frame("digitizer", bytes(5))    # must be exactly 6


# --------------------------------------------------------------------------- #
# Esp32Transport.send: writes exactly the report bytes reports.py builds
# --------------------------------------------------------------------------- #
def test_transport_send_writes_report_lines():
    fake = FakeSerial()
    tp = TE.Esp32Transport(port=fake)
    frames = [(r.kind, r.data) for r in R.type_text("1")]
    resp = tp.send(frames)
    assert resp["ok"] and resp["sent"] == 2
    # 'type 1' = key-down then all-up, both keyboard frames
    assert fake.written == [b"K00001e0000000000\n", b"K0000000000000000\n"]


def test_transport_send_mouse_and_keyboard_mixed():
    fake = FakeSerial()
    tp = TE.Esp32Transport(port=fake)
    tp.send([("mouse", bytes([R.BTN_LEFT, 0, 0, 0])), ("keyboard", bytes(8))])
    assert fake.written == [b"M01000000\n", b"K0000000000000000\n"]


def test_transport_send_abs_mouse_matches_reports_bytes():
    # An absolute click at screen-center: move -> button-down -> button-up, all at (16383,16383).
    fake = FakeSerial()
    tp = TE.Esp32Transport(port=fake)
    reps = R.abs_click_reports(0.5, 0.5)
    resp = tp.send([(r.kind, r.data) for r in reps])
    assert resp["ok"] and resp["sent"] == len(reps)
    # Each frame is "A" + the exact reports.py body hex + newline (byte-match the single source).
    for r, line in zip(reps, fake.written, strict=True):
        assert r.kind == "abs_mouse"
        assert line == b"A" + r.data.hex().encode("ascii") + b"\n"
        assert len(r.data) == R.ABS_MOUSE_BODY_LEN


def test_transport_send_digitizer_matches_reports_bytes():
    # A single-finger tap at (0.25, 0.75): finger-down at (x,y) then finger-up (all zero).
    fake = FakeSerial()
    tp = TE.Esp32Transport(port=fake)
    reps = R.digitizer_tap_reports(0.25, 0.75)
    resp = tp.send([(r.kind, r.data) for r in reps])
    assert resp["ok"] and resp["sent"] == len(reps)
    for r, line in zip(reps, fake.written, strict=True):
        assert r.kind == "digitizer"
        assert line == b"D" + r.data.hex().encode("ascii") + b"\n"
        assert len(r.data) == R.DIGITIZER_BODY_LEN


def test_controller_abs_and_touch_route_through_esp32():
    fake = FakeSerial()
    ctl = HidController(TE.Esp32Transport(port=fake))
    ctl.move_abs(0.0, 1.0)       # top-left X, bottom Y -> X=0, Y=32767
    ctl.touch_tap(1.0, 0.0)      # right edge X, top Y
    # move_abs -> 1 abs frame; touch_tap -> 2 digitizer frames (down, up)
    kinds = [line[:1] for line in fake.written]
    assert kinds == [b"A", b"D", b"D"]
    # first abs frame: buttons=0, X=0 (0000 LE), Y=32767 (ff7f LE)
    assert fake.written[0] == b"A00" + b"0000" + b"ff7f" + b"\n"


def test_transport_reports_error_when_esp32_rejects():
    fake = FakeSerial(replies=[b"ERR badkbd\n"])
    tp = TE.Esp32Transport(port=fake)
    resp = tp.send([("keyboard", bytes(8))])
    assert resp["ok"] is False and "rejected" in resp["error"]


def test_transport_skips_async_evt_lines():
    # An EVT line may arrive before the OK; the reader must skip it.
    fake = FakeSerial(replies=[b"EVT connected\n", b"OK\n"])
    tp = TE.Esp32Transport(port=fake)
    resp = tp.send([("keyboard", bytes(8))])
    assert resp["ok"] and resp["sent"] == 1


def test_ping_parses_connection_state():
    fake = FakeSerial(replies=[b"OK HIDBRIDGE-esp32 conn=1\n"])
    tp = TE.Esp32Transport(port=fake)
    resp = tp.ping()
    assert resp["ok"] and resp["connected"] is True


# --------------------------------------------------------------------------- #
# Controller + backend registry route through the ESP32 transport
# --------------------------------------------------------------------------- #
def test_make_transport_selects_esp32():
    fake = FakeSerial()
    tp = make_transport("esp32", port=fake)   # non-str port -> uses the fake serial directly
    assert isinstance(tp, TE.Esp32Transport)
    tp.send([("keyboard", bytes(8))])
    assert fake.written == [b"K0000000000000000\n"]


def test_controller_over_esp32_backend_emits_same_bytes():
    fake = FakeSerial()
    ctl = HidController(TE.Esp32Transport(port=fake))
    ctl.type_text("1")
    assert fake.written == [b"K00001e0000000000\n", b"K0000000000000000\n"]


def test_make_transport_unknown_backend_raises():
    with pytest.raises(ValueError):
        make_transport("nonesuch_backend")


# --------------------------------------------------------------------------- #
# Firmware REPORT_MAP must stay byte-identical to reports.py REPORT_DESCRIPTOR
# --------------------------------------------------------------------------- #
def _parse_firmware_report_map() -> bytes:
    src = (ROOT / "firmware/esp32/src/main.cpp").read_text()
    marker = "REPORT_MAP[] = {"
    start = src.index(marker) + len(marker)
    end = src.index("};", start)
    body = src[start:end]
    out = bytearray()
    for line in body.splitlines():
        line = line.split("//")[0]          # strip the trailing comment PER LINE
        for tok in line.split(","):
            tok = tok.strip()
            if tok:
                out.append(int(tok, 16))
    return bytes(out)


def test_firmware_report_map_matches_reports_descriptor():
    fw = _parse_firmware_report_map()
    assert fw == bytes(R.REPORT_DESCRIPTOR), (
        "firmware REPORT_MAP drifted from reports.REPORT_DESCRIPTOR -- they MUST match")

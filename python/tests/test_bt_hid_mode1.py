"""Tests for the Mode 1 client (JSON-RPC 2.0 over JSON-lines) -- hidbridge/mode1.py.

Offline tests use a fake channel that answers each request with a handler, so the client's request
shaping + response/error/notification handling are covered with no hardware. One hardware-gated test
(BT_HID_MODE1=1) drives a real board's dry-run and asserts the bytes it BUILDS equal reports.py's --
the firmware-native <-> host parity check for the duplicated report layer.
"""

import json
import os

import pytest

from hidbridge import mode1
from hidbridge import reports as R


class RpcFake:
    """Fake write/readline channel. `handler(method, params) -> result-dict | {"error": {...}}`.
    `noise` lines are emitted BEFORE each reply (async events / boot banners the client must skip)."""

    def __init__(self, handler=None, noise=None):
        self.requests = []
        self.raw_writes = []
        self._queue = []
        self._handler = handler
        self._noise = list(noise or [])

    def write(self, b):
        try:
            req = json.loads(b.decode())
        except ValueError:
            self.raw_writes.append(b)         # non-JSON line (e.g. the @token auth line)
            return len(b)
        self.requests.append(req)
        for ln in self._noise:
            self._queue.append(ln)
        self._noise = []
        if self._handler is not None:
            res = self._handler(req["method"], req.get("params", {}))
            env = {"jsonrpc": "2.0", "id": req["id"]}
            env.update({"error": res["error"]} if isinstance(res, dict) and "error" in res
                       else {"result": res})
            self._queue.append(env)
        return len(b)

    def readline(self):
        if not self._queue:
            return b""
        item = self._queue.pop(0)
        if isinstance(item, bytes):
            return item
        if isinstance(item, str):
            return (item + "\n").encode()
        return (json.dumps(item) + "\n").encode()

    def close(self):
        pass


# --------------------------------------------------------------------------- #
# request shaping -- each verb produces the right JSON-RPC method + params
# --------------------------------------------------------------------------- #
def test_requests_are_valid_jsonrpc_with_expected_method_params():
    seen = []
    fake = RpcFake(handler=lambda m, p: (seen.append((m, p)), {"sent": 1})[1])
    c = mode1.Mode1Client(fake)
    c.type_text("hi")
    c.key("enter", 8)
    c.wake()
    c.move(3, -4)
    c.click("back")
    c.scroll(-2)
    c.move_abs(0.5, 0.25)
    c.tap(0.1, 0.9, "left")
    c.touch(0.3, 0.7)

    # every request is well-formed JSON-RPC 2.0 with an increasing id
    for i, req in enumerate(fake.requests, start=1):
        assert req["jsonrpc"] == "2.0" and req["id"] == i and "method" in req and "params" in req
    assert seen == [
        ("type", {"text": "hi"}),
        ("key", {"name": "enter", "mod": 8}),
        ("wake", {}),
        ("move", {"dx": 3, "dy": -4}),
        ("click", {"button": "back"}),
        ("scroll", {"clicks": -2}),
        ("moveabs", {"x": 0.5, "y": 0.25}),
        ("tap", {"x": 0.1, "y": 0.9, "button": "left"}),
        ("touch", {"x": 0.3, "y": 0.7}),
    ]


def test_dry_flag_adds_dry_param():
    seen = []
    fake = RpcFake(handler=lambda m, p: (seen.append(p), {"n": 0, "frames": []})[1])
    mode1.Mode1Client(fake).type_text("x", dry=True)
    assert seen[0] == {"text": "x", "dry": True}


# --------------------------------------------------------------------------- #
# response + error + notification handling
# --------------------------------------------------------------------------- #
def test_returns_result_dict():
    fake = RpcFake(handler=lambda m, p: {"sent": 5, "conn": True})
    assert mode1.Mode1Client(fake).move(1, 1) == {"sent": 5, "conn": True}


def test_jsonrpc_error_raises():
    fake = RpcFake(handler=lambda m, p: {"error": {"code": -32601, "message": "method not found"}})
    with pytest.raises(mode1.Mode1Error) as ei:
        mode1.Mode1Client(fake).call("frobnicate")
    assert "method not found" in str(ei.value) and "-32601" in str(ei.value)


def test_skips_notifications_and_banners_before_reply():
    # an async event notification (id=null) + a boot banner precede the real reply
    noise = [{"jsonrpc": "2.0", "method": "event", "params": {"event": "connected"}},
             "OK HIDBRIDGE-esp32 ready"]
    fake = RpcFake(handler=lambda m, p: {"sent": 1}, noise=noise)
    assert mode1.Mode1Client(fake).wake() == {"sent": 1}


def test_skips_mismatched_id_reply():
    # a stale reply with the wrong id must be ignored, not returned
    noise = [{"jsonrpc": "2.0", "id": 999, "result": {"stale": True}}]
    fake = RpcFake(handler=lambda m, p: {"sent": 1}, noise=noise)
    assert mode1.Mode1Client(fake).wake() == {"sent": 1}


def test_no_response_raises():
    with pytest.raises(mode1.Mode1Error):
        mode1.Mode1Client(RpcFake(handler=None)).wake()   # handler=None -> nothing queued


# --------------------------------------------------------------------------- #
# hello / capabilities + auth + target parsing
# --------------------------------------------------------------------------- #
def test_hello_and_supports():
    caps = {"proto": 1, "caps": ["keyboard", "mouse", "abs", "digitizer", "slots", "net"]}
    fake = RpcFake(handler=lambda m, p: caps)
    c = mode1.Mode1Client(fake)
    assert c.hello()["proto"] == 1
    assert c.supports("digitizer") and not c.supports("nope")


def test_authenticate_sends_token_line_and_accepts():
    fake = RpcFake()
    fake._queue.append("OK auth")           # firmware answers the @token line (Mode 2 auth)
    mode1.Mode1Client(fake).authenticate("supersecret")
    assert fake.requests == []              # auth is NOT a JSON-RPC call...
    assert fake.raw_writes == [b"@supersecret\n"]   # ...it's the @<token> line


def test_authenticate_rejected_raises():
    fake = RpcFake()
    fake._queue.append("ERR auth")
    with pytest.raises(mode1.Mode1Error):
        mode1.Mode1Client(fake).authenticate("bad")


@pytest.mark.parametrize("target,expect", [
    (None, ("serial", None)),
    ("/dev/ttyUSB3", ("serial", "/dev/ttyUSB3")),
    ("esp32:/dev/ttyUSB3", ("esp32", "/dev/ttyUSB3")),
    ("pico:/dev/ttyACM0", ("pico", "/dev/ttyACM0")),
    ("net:board.local", ("net", "board.local")),
    ("net:board.local:4000", ("net", "board.local:4000")),
])
def test_split_target(target, expect):
    assert mode1._split_target(target) == expect


# --------------------------------------------------------------------------- #
# HARDWARE-GATED: firmware-native builders == reports.py (the parity check)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(os.environ.get("BT_HID_MODE1") != "1",
                    reason="set BT_HID_MODE1=1 (+ BT_HID_ESP32_PORT / a flashed board) to run")
def test_firmware_dry_run_matches_reports_py():
    """Drive a real board's dry-run for every command; assert the bytes it BUILDS equal reports.py.

    This is the drift guard for the deliberate reports.py <-> firmware duplication."""
    c = mode1.open_client("esp32:" + os.environ.get("BT_HID_ESP32_PORT", "/dev/ttyUSB3"))
    try:
        def frames(rl):
            return ["%02x" % r.report_id + r.data.hex() for r in rl]
        checks = [
            (c.type_text("Hello, World!", dry=True), R.type_text("Hello, World!")),
            (c.type_text("aA1!@ [];'/", dry=True), R.type_text("aA1!@ [];'/")),
            (c.key("enter", dry=True), R.key_reports("enter")),
            (c.key("tab", 8, dry=True), R.key_reports("tab", 8)),
            (c.wake(dry=True), R.key_reports("space")),
            (c.move(300, 5, dry=True), R.mouse_move_reports(300, 5)),
            (c.click("back", dry=True), R.mouse_click_reports("back")),
            (c.scroll(-3, dry=True), R.mouse_scroll_reports(-3)),
            (c.move_abs(0.5, 0.25, dry=True), R.abs_move_reports(0.5, 0.25)),
            (c.tap(0.5, 0.5, "left", dry=True), R.abs_click_reports(0.5, 0.5, "left")),
            (c.touch(0.3, 0.7, dry=True), R.digitizer_tap_reports(0.3, 0.7)),
        ]
        for res, rl in checks:
            assert res.get("frames") == frames(rl), f"drift: {res} != {frames(rl)}"
    finally:
        c.close()

"""Tests for the RPC client (JSON-RPC 2.0 over JSON-lines) -- hidbridge/rpc.py.

Offline tests use a fake channel that answers each request with a handler, so the client's request
shaping + response/error/notification handling are covered with no hardware. One hardware-gated test
(BT_HID_RPC=1, or the legacy BT_HID_MODE1=1) drives a real board's dry-run and asserts the bytes it
BUILDS equal reports.py's -- the firmware-native <-> host parity check for the duplicated report layer.
"""

import json
import os

import pytest

from hidbridge import reports as R
from hidbridge import rpc


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
    c = rpc.RpcClient(fake)
    c.type_text("hi")
    c.key("enter", 8)
    c.wake()
    c.move(3, -4)
    c.click("back")
    c.press("left")
    c.release("left")
    c.scroll(-2)

    # every request is well-formed JSON-RPC 2.0 with an increasing id
    for i, req in enumerate(fake.requests, start=1):
        assert req["jsonrpc"] == "2.0" and req["id"] == i and "method" in req and "params" in req
    assert seen == [
        ("type", {"text": "hi"}),
        ("key", {"name": "enter", "mod": 8}),
        ("wake", {}),
        ("move", {"dx": 3, "dy": -4}),
        ("click", {"button": "back"}),
        ("press", {"button": "left"}),
        ("release", {"button": "left"}),
        ("scroll", {"clicks": -2}),
    ]


_FULL_CAPS = ["keyboard", "mouse", "slots", "nickname", "connect", "frames"]


def _cap_handler(caps, seen=None):
    """A RpcFake handler that answers hello with `caps` and everything else with {ok:1}."""
    def h(m, p):
        if seen is not None:
            seen.append((m, p))
        return {"proto": 1, "caps": list(caps)} if m == "hello" else {"ok": 1}
    return h


def test_connection_and_frames_methods():
    seen = []
    c = rpc.RpcClient(RpcFake(handler=_cap_handler(_FULL_CAPS, seen)))
    c.nickname("labkit")
    c.nickname()
    c.identities()
    c.pair("mini5")
    c.connect("mini5")
    c.disconnect("mini5")
    c.disconnect()
    c.unpair("mini5")
    c.frames(["0100001e0000000000", "010000000000000000"])
    # a capability-gated call fetches hello ONCE (cached), then issues the method
    assert seen == [
        ("hello", {}),
        ("nickname", {"name": "labkit"}),
        ("nickname", {}),
        ("identities", {}),
        ("pair", {"client": "mini5"}),
        ("connect", {"client": "mini5"}),
        ("disconnect", {"client": "mini5"}),
        ("disconnect", {}),
        ("unpair", {"client": "mini5"}),
        ("frames", {"frames": ["0100001e0000000000", "010000000000000000"]}),
    ]


_CONSOLIDATED_CAPS = ["keyboard", "mouse", "slots", "nickname", "connect", "frames", "layout",
                      "target", "advertise", "park", "net_status", "provision"]


def test_consolidated_device_methods_shape_json():
    """The single-char device commands folded into JSON-RPC: target/ping/advertise/park/net_status/
    provision each produce the right method + params, capability-gated (except the bare ping probe)."""
    seen = []
    c = rpc.RpcClient(RpcFake(handler=_cap_handler(_CONSOLIDATED_CAPS, seen)))
    c.target(2)
    c.ping()
    c.advertise()
    c.park()
    c.net_status()
    c.provision("supersecrettoken")
    assert seen == [
        ("hello", {}),
        ("target", {"slot": 2}),
        ("ping", {}),
        ("advertise", {}),
        ("park", {}),
        ("net_status", {}),
        ("provision", {"token": "supersecrettoken"}),
    ]


def test_consolidated_methods_gate_on_capability():
    # an older board missing these caps errors clearly rather than sending a method it would reject
    c = rpc.RpcClient(RpcFake(handler=_cap_handler(["keyboard", "mouse"])))
    for call in (lambda: c.advertise(), lambda: c.park(), lambda: c.net_status(),
                 lambda: c.provision("supersecrettoken"), lambda: c.target(0)):
        with pytest.raises(rpc.RpcError, match="lacks capability"):
            call()


def test_ping_is_allowed_without_capability():
    # ping is a bare liveness probe -- not capability-gated (works on any JSON-RPC board)
    c = rpc.RpcClient(RpcFake(handler=_cap_handler(["keyboard", "mouse"])))
    assert c.ping() == {"ok": 1}


def test_missing_capability_raises_clearly():
    # a board that does NOT advertise 'connect' -> a clear error, not a cryptic firmware reject
    c = rpc.RpcClient(RpcFake(handler=_cap_handler(["keyboard", "mouse"])))
    with pytest.raises(rpc.RpcError) as ei:
        c.connect("mini5")
    assert "connect" in str(ei.value) and "lacks capability" in str(ei.value)


def test_newer_protocol_warns_not_crashes():
    fake = RpcFake(handler=lambda m, p: {"proto": 99, "caps": _FULL_CAPS})
    c = rpc.RpcClient(fake)
    with pytest.warns(UserWarning, match="protocol 99"):
        c.hello()
    # still usable: a supported cap works despite the newer version
    assert c.supports("connect")


def test_info_is_cached():
    calls = []
    c = rpc.RpcClient(RpcFake(handler=lambda m, p: (calls.append(m), {"proto": 1, "caps": []})[1]))
    c.info(); c.info(); c.supports("x")
    assert calls == ["hello"]   # fetched exactly once


def test_hello_exposes_fw_tag():
    fake = RpcFake(handler=lambda m, p: {"proto": 1, "caps": [], "device": "HIDBRIDGE-esp32",
                                         "fw": "abc123"})
    c = rpc.RpcClient(fake)
    c.hello()
    assert c.info().fw == "abc123" and c.info().device == "HIDBRIDGE-esp32"


def test_dry_flag_adds_dry_param():
    seen = []
    fake = RpcFake(handler=lambda m, p: (seen.append(p), {"n": 0, "frames": []})[1])
    rpc.RpcClient(fake).type_text("x", dry=True)
    assert seen[0] == {"text": "x", "dry": True}


def test_frames_relays_known_drive_report_bytes():
    """The EXACT shape central._inject uses for the rpc drive: relay the KNOWN drives as `frames`.

    Locks the raw-frames <-> rpc equivalence offline: central_core.frames_method_hex builds the hex,
    RpcClient.frames relays it verbatim as a JSON-RPC `frames` call. On a real board this is the
    smart-firmware path building the same notification the raw transport would."""
    from hidbridge import central_core as C
    seen = []

    def handler(m, p):
        seen.append((m, p))
        if m == "hello":
            return {"proto": 1, "caps": ["frames"]}
        return {"sent": 1, "conn": True}
    c = rpc.RpcClient(RpcFake(handler=handler))
    kbd = C.frames_method_hex(1, R.keyboard_report(0, [0x04]).data)   # 'a' down
    mouse = C.frames_method_hex(2, R.mouse_report(dx=3).data)         # move dx=3
    assert c.frames([kbd]) == {"sent": 1, "conn": True}
    c.frames([mouse])
    assert seen == [("hello", {}), ("frames", {"frames": [kbd]}), ("frames", {"frames": [mouse]})]
    assert kbd == "010000040000000000" and mouse == "0200030000"


# --------------------------------------------------------------------------- #
# response + error + notification handling
# --------------------------------------------------------------------------- #
def test_returns_result_dict():
    fake = RpcFake(handler=lambda m, p: {"sent": 5, "conn": True})
    assert rpc.RpcClient(fake).move(1, 1) == {"sent": 5, "conn": True}


def test_jsonrpc_error_raises():
    fake = RpcFake(handler=lambda m, p: {"error": {"code": -32601, "message": "method not found"}})
    with pytest.raises(rpc.RpcError) as ei:
        rpc.RpcClient(fake).call("frobnicate")
    assert "method not found" in str(ei.value) and "-32601" in str(ei.value)


def test_skips_notifications_and_banners_before_reply():
    # an async event notification (id=null) + a boot banner precede the real reply
    noise = [{"jsonrpc": "2.0", "method": "event", "params": {"event": "connected"}},
             "OK HIDBRIDGE-esp32 ready"]
    fake = RpcFake(handler=lambda m, p: {"sent": 1}, noise=noise)
    assert rpc.RpcClient(fake).wake() == {"sent": 1}


def test_skips_mismatched_id_reply():
    # a stale reply with the wrong id must be ignored, not returned
    noise = [{"jsonrpc": "2.0", "id": 999, "result": {"stale": True}}]
    fake = RpcFake(handler=lambda m, p: {"sent": 1}, noise=noise)
    assert rpc.RpcClient(fake).wake() == {"sent": 1}


def test_no_response_raises():
    with pytest.raises(rpc.RpcError):
        rpc.RpcClient(RpcFake(handler=None)).wake()   # handler=None -> nothing queued


def test_silence_raises_rpc_timeout():
    # nothing at all comes back (empty readline) -> RpcTimeout: 'nothing there', distinct from a board
    # that answered with non-JSON (legacy firmware). Both remain RpcError subclasses.
    with pytest.raises(rpc.RpcTimeout):
        rpc.RpcClient(RpcFake(handler=None)).hello()


def test_non_json_reply_raises_rpc_protocol_error():
    # a legacy / pre-JSON-RPC board answers `ERR unknowncmd` (not JSON) -> RpcProtocolError, so a
    # caller can tell 'answered but wrong protocol' from 'unreachable'.
    fake = RpcFake(handler=None)
    fake._queue.append("ERR unknowncmd")
    with pytest.raises(rpc.RpcProtocolError):
        rpc.RpcClient(fake).hello()


# --------------------------------------------------------------------------- #
# robust demux under the post-connect window (async EVT burst + a brief stall)
# --------------------------------------------------------------------------- #
class QueueChannel:
    """A minimal write/readline channel: readline() yields the queued items IN ORDER; a b"" item is
    an empty read (a stall / timeout window). Exhausted -> b"" (silence). Records the last request."""

    def __init__(self, items):
        self._items = list(items)
        self.req = None

    def write(self, b):
        self.req = json.loads(b.decode())
        return len(b)

    def readline(self):
        return self._items.pop(0) if self._items else b""


def _line(obj):
    return (json.dumps(obj) + "\n").encode()


def test_call_survives_notification_flood_exceeding_max_reads():
    # MORE THAN max_reads async EVT notifications (id=null) precede the real reply. A fixed
    # max_reads loop would exhaust its budget on the notifications and never see the reply.
    note = _line({"jsonrpc": "2.0", "method": "event", "params": {"event": "ready"}})
    reply = _line({"jsonrpc": "2.0", "id": 1, "result": {"ok": 1}})
    ch = QueueChannel([note] * 20 + [reply])     # 20 >> the default max_reads (12)
    assert rpc.RpcClient(ch, max_reads=12).wake() == {"ok": 1}


def test_call_survives_notification_flood_with_no_id_key():
    # notifications may omit the id key entirely (not just id=null) -- still skipped, not counted.
    note = _line({"jsonrpc": "2.0", "method": "event", "params": {"event": "ready"}})
    del_note = _line({"jsonrpc": "2.0", "params": {"event": "ready"}})  # no id key at all
    reply = _line({"jsonrpc": "2.0", "id": 1, "result": {"ok": 2}})
    ch = QueueChannel(([note, del_note] * 10) + [reply])
    assert rpc.RpcClient(ch, max_reads=12).wake() == {"ok": 2}


def test_call_tolerates_stall_empty_reads_before_reply():
    # the board goes quiet briefly (empty reads) after the EVT burst, then answers. Breaking on the
    # first empty read (the old behaviour) would mistake the stall for silence -> RpcTimeout.
    reply = _line({"jsonrpc": "2.0", "id": 1, "result": {"ok": 3}})
    ch = QueueChannel([b"", b"", reply])         # two stall windows, then the reply
    assert rpc.RpcClient(ch).wake() == {"ok": 3}


def test_call_tolerates_evt_burst_then_stall_then_reply():
    # the full post-connect shape: several async EVT ready lines, a stall, then the matching reply.
    note = _line({"jsonrpc": "2.0", "method": "event", "params": {"event": "ready"}})
    reply = _line({"jsonrpc": "2.0", "id": 1, "result": {"subscribed": 1}})
    ch = QueueChannel([note, note, note, b"", reply])
    assert rpc.RpcClient(ch).wake() == {"subscribed": 1}


def test_call_silence_still_raises_rpc_timeout():
    # a channel that ONLY ever stalls (b"") must still be classified as silence -> RpcTimeout, not
    # spin forever (regression guard for the RpcTimeout vs RpcProtocolError distinction).
    with pytest.raises(rpc.RpcTimeout):
        rpc.RpcClient(QueueChannel([])).wake()


def test_call_noise_after_stall_still_raises_protocol_error():
    # a board that answers only with non-JSON noise (interleaved with stalls) still raises
    # RpcProtocolError -- it ANSWERED, just not with JSON-RPC.
    ch = QueueChannel([b"", b"ERR unknowncmd\n", b""])
    with pytest.raises(rpc.RpcProtocolError):
        rpc.RpcClient(ch).wake()


# --------------------------------------------------------------------------- #
# hello / capabilities + auth + target parsing
# --------------------------------------------------------------------------- #
def test_hello_and_supports():
    caps = {"proto": 1, "caps": ["keyboard", "mouse", "slots", "net"]}
    fake = RpcFake(handler=lambda m, p: caps)
    c = rpc.RpcClient(fake)
    assert c.hello()["proto"] == 1
    assert c.supports("keyboard") and not c.supports("nope")


def test_authenticate_sends_token_line_and_accepts():
    fake = RpcFake()
    fake._queue.append("OK auth")           # firmware answers the @token line (raw-path auth)
    rpc.RpcClient(fake).authenticate("supersecret")
    assert fake.requests == []              # auth is NOT a JSON-RPC call...
    assert fake.raw_writes == [b"@supersecret\n"]   # ...it's the @<token> line


def test_authenticate_rejected_raises():
    fake = RpcFake()
    fake._queue.append("ERR auth")
    with pytest.raises(rpc.RpcError):
        rpc.RpcClient(fake).authenticate("bad")


@pytest.mark.parametrize("target,expect", [
    (None, ("serial", None)),
    ("/dev/ttyUSB3", ("serial", "/dev/ttyUSB3")),
    ("esp32:/dev/ttyUSB3", ("esp32", "/dev/ttyUSB3")),
    ("pico:/dev/ttyACM0", ("pico", "/dev/ttyACM0")),
    ("net:board.local", ("net", "board.local")),
    ("net:board.local:4000", ("net", "board.local:4000")),
])
def test_split_target(target, expect):
    assert rpc._split_target(target) == expect


# --------------------------------------------------------------------------- #
# HARDWARE-GATED: firmware-native builders == reports.py (the parity check)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(os.environ.get("BT_HID_RPC") != "1" and os.environ.get("BT_HID_MODE1") != "1",
                    reason="set BT_HID_RPC=1 (+ BT_HID_ESP32_PORT / a flashed board) to run")
def test_firmware_dry_run_matches_reports_py():
    """Drive a real board's dry-run for every command; assert the bytes it BUILDS equal reports.py.

    This is the drift guard for the deliberate reports.py <-> firmware duplication."""
    c = rpc.open_client("esp32:" + os.environ.get("BT_HID_ESP32_PORT", "/dev/ttyUSB3"))
    prev_tgt = None
    borrowed = None
    try:
        # Claim a send-target OURSELVES so this works from a CLEAN board (tgt=-1, no pre-claimed slot):
        # `layout` with no slot arg addresses the CURRENT TARGET slot, so without a target the layout
        # round-trip below would have nothing to set. Borrow a KNOWN-UNUSED slot (no client/bond) so we
        # never clobber a real device's persisted layout; restore the prior target in the finally.
        ident = c.identities()
        prev_tgt = ident.get("tgt")
        slots = ident.get("slots", [])
        borrowed = next((s["slot"] for s in slots
                         if not s.get("client") and not s.get("bonded") and not s.get("conn")), None)
        if borrowed is None:
            borrowed = max((s["slot"] for s in slots), default=0)
        c.target(borrowed)

        def frames(rl):
            return ["%02x" % r.report_id + r.data.hex() for r in rl]
        checks = [
            (c.type_text("Hello, World!", dry=True), R.type_text("Hello, World!")),
            (c.type_text("aA1!@ [];'/", dry=True), R.type_text("aA1!@ [];'/")),
            (c.key("enter", dry=True), R.key_reports("enter")),
            (c.key("tab", 8, dry=True), R.key_reports("tab", 8)),
            (c.key("c", 8, dry=True), R.key_reports("c", 8)),   # Cmd+C: single-char key + modifier
            (c.wake(dry=True), R.key_reports("space")),
            (c.move(300, 5, dry=True), R.mouse_move_reports(300, 5)),
            (c.click("back", dry=True), R.mouse_click_reports("back")),
            (c.scroll(-3, dry=True), R.mouse_scroll_reports(-3)),
        ]
        for res, rl in checks:
            assert res.get("frames") == frames(rl), f"drift: {res} != {frames(rl)}"

        # Per-slot Danish layout: set the CURRENT TARGET slot -> da (no slot arg == tgtSlot), so the
        # firmware's active layout for `type` becomes Danish; assert its dry-run bytes equal
        # reports.type_text(..., layout="da"). Restore the slot to us afterwards + re-assert the
        # default-US path, so the board is left in its default state.
        try:
            c.layout("da")                                     # target slot -> Danish
            da = "æøåÆØÅ@{}[]|<>$"                              # ae/oe/aa + AltGr @ { } [ ] | $ , ISO <>
            assert c.type_text(da, dry=True).get("frames") == frames(R.type_text(da, layout="da")), \
                "drift: firmware da-layout type != reports.type_text(layout='da')"
        finally:
            c.layout("us")                                     # restore default
        # default-US path still holds after the round-trip
        assert c.type_text("aA@'", dry=True).get("frames") == frames(R.type_text("aA@'")), \
            "drift: firmware default-us type != reports.type_text() after layout round-trip"
    finally:
        # Restore the board's prior send-target (a clean board had none: tgt=-1 -> leave the borrow).
        if prev_tgt is not None and prev_tgt >= 0 and prev_tgt != borrowed:
            try:
                c.target(prev_tgt)
            except rpc.RpcError:
                pass
        c.close()

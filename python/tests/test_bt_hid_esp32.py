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


def test_encode_rejects_unknown_kind():
    with pytest.raises(TE.Esp32Error):
        TE.encode_frame("abs_mouse", b"\x00")


def test_encode_rejects_wrong_length():
    with pytest.raises(TE.Esp32Error):
        TE.encode_frame("keyboard", bytes(4))
    with pytest.raises(TE.Esp32Error):
        TE.encode_frame("mouse", bytes(8))


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


def _firmware_function_body(name: str) -> str:
    """Return the brace-balanced body of C function `name` from main.cpp (its definition, not a call)."""
    import re
    src = (ROOT / "firmware/esp32/src/main.cpp").read_text()
    m = re.search(r"\bvoid\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", src)
    assert m, f"no definition of {name}() found in main.cpp"
    b = src.index("{", m.start())
    depth = 0
    for j in range(b, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[b:j + 1]
    raise AssertionError(f"unterminated body for {name}()")


def _strip_ws(s: str) -> str:
    return "".join(s.split())


# --------------------------------------------------------------------------- #
# WS DRIVE CHANNEL lifecycle -- STRUCTURAL source-parse guards.
#
# The WebSocket drive channel (port 81) can only be exercised end-to-end with a real board + a live
# BLE peer + a browser, none of which exist offline. These tests therefore parse main.cpp and pin the
# two load-bearing wiring facts that a refactor (or a bad merge) could silently drop -- they can NOT
# run the firmware, only assert its shape. The invariants:
#   (a) page-closed -> park: a WStype_DISCONNECTED of the OWNING client must parkTargetSlot() so a
#       closed/crashed control page deterministically releases the target it was driving; and
#   (b) fail-closed auth: an UNauthenticated WS client can reach only `hello` -- every other method is
#       refused ("auth required") -- the same fail-closed gate the HTTP /rpc path enforces.
# --------------------------------------------------------------------------- #
def test_ws_disconnect_of_owner_parks_target():
    """(a) wsEvent's WStype_DISCONNECTED case, for the OWNING client (num == g_wsOwner), must call
    parkTargetSlot(g_wsOwnerSlot) -- the page-closed -> park wiring. Without it, a closed browser tab
    would leave its target live and connected with nothing driving it."""
    body = _strip_ws(_firmware_function_body("wsEvent"))
    assert "caseWStype_DISCONNECTED:" in body, "wsEvent must handle WStype_DISCONNECTED"
    assert "(int)num==g_wsOwner" in body, (
        "the DISCONNECTED handler must check whether the leaving client OWNS the driven target")
    assert "parkTargetSlot(g_wsOwnerSlot)" in body, (
        "a WStype_DISCONNECTED of the owning WS client must parkTargetSlot(g_wsOwnerSlot) -- the "
        "page-closed -> park wiring (deterministic release, no timeout guessing)")


def test_ws_text_handler_is_fail_closed_auth_gated():
    """(b) wsOnText must be fail-closed: while a client is NOT authed (!wsAuthed[num]) the ONLY method
    that reaches the dispatcher is `hello`; any other method is refused with an 'auth required' error,
    and the single dispatch reachable while unauthed is the hello-only call (authed=false). This is the
    WS mirror of the HTTP /rpc token gate -- WS drive must not be a way around authentication."""
    body = _strip_ws(_firmware_function_body("wsOnText"))
    assert "if(!wsAuthed[num])" in body, "wsOnText must gate on the per-client authed flag"
    # non-hello while unauthed is rejected
    assert 'strcmp(m,"hello")' in body, "the unauthed branch must special-case only `hello`"
    # the message text lives inside a JSON string literal, so its quotes are backslash-escaped in C;
    # match the whitespace-stripped message body ("auth required" -> authrequired).
    assert "authrequired" in body, "a non-hello method while unauthed must be refused (auth required)"
    # the ONLY dispatch reachable before auth passes authed=false (hello-only); the post-auth dispatch
    # passes true. Both call sites must be present with those exact trust flags.
    assert "handleJsonRpc(line,out,false," in body, (
        "the pre-auth dispatch must pass authed=false (so handleJsonRpc answers hello only)")
    assert "handleJsonRpc(line,out,true," in body, "the post-auth dispatch passes authed=true"


def test_ws_authed_dispatch_sets_target_slot_layout():
    """Merge-critical wiring guard: the WS text handler must setActiveLayout(slotLayout[tgtSlot])
    BEFORE the authed handleJsonRpc dispatch, exactly like the serial/TCP path (handleLine), so typing
    driven over the WS honors the per-slot keyboard layout (feat/keyboard-layouts + feat/ws-drive-channel
    must stay integrated). If this drops, WS `type` would emit US scancodes to a `da` host."""
    body = _strip_ws(_firmware_function_body("wsOnText"))
    setlay = body.index("setActiveLayout(")
    authed_dispatch = body.index("handleJsonRpc(line,out,true,")
    assert setlay < authed_dispatch, (
        "wsOnText must setActiveLayout(slotLayout[tgtSlot]) BEFORE the authed handleJsonRpc dispatch")
    assert "setActiveLayout((tgtSlot>=0&&tgtSlot<NUM_SLOTS)?slotLayout[tgtSlot]" in body, (
        "the WS layout gate must select the TARGET slot's layout (matching the serial/TCP path)")


def test_firmware_gap_name_and_advertised_name_share_one_source():
    """The GAP Device Name characteristic (0x2A00, read after connecting) and the advertised Complete
    Local Name (seen while scanning) must come from the SAME per-slot source. If they diverge, a peer
    sees two different names -- the per-slot advert while scanning, then a stale fixed name once
    connected (the bug where a paired device showed as bare 'HIDBRIDGE-esp32' with no -<nick>-<client>
    suffix). Guard: startAdvertisingSlot sets BOTH from slotName[advSlot]."""
    body = _firmware_function_body("startAdvertisingSlot").replace(" ", "")
    assert "setDeviceName(slotName[advSlot])" in body, (
        "startAdvertisingSlot must sync the GAP Device Name (0x2A00) to the FULL slotName[advSlot] "
        "(else a connected peer reads a different name than it saw while scanning)")
    # the advertised Complete Local Name is the same per-slot name, length-capped into `advName` to
    # fit the 31-byte advertising packet (an over-long name would silently fail to advertise).
    assert 'snprintf(advName,sizeof(advName),"%s",slotName[advSlot])' in body, (
        "the advertised name must derive from slotName[advSlot] (capped to fit the advert packet)")
    assert "setName(advName)" in body, (
        "startAdvertisingSlot must advertise the length-capped per-slot name")

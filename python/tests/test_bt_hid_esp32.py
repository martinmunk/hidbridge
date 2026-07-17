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
    """Return the brace-balanced body of C function `name` from main.cpp (its definition, not a call).

    Matches a definition: an (optional `static`) return type, the name, a parenthesised arg list, then
    the opening brace. A CALL site ends in `;`/`)` not `{`, so only the definition matches -- this works
    for any return type (void/bool/int/...), not just void.
    """
    import re
    src = (ROOT / "firmware/esp32/src/main.cpp").read_text()
    m = re.search(r"(?:static\s+)?[A-Za-z_][\w:<>\*&]*\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", src)
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
# PER-SESSION MULTI-DRIVER TARGETING -- STRUCTURAL source-parse guards.
#
# Each driver SESSION (WS client / TCP net session / serial) owns ONE BLE claim; its commands route to
# ITS claimed slot, and a slot is parked only when its LAST claiming session goes away (ref-counted).
# None of this can be exercised offline (it needs a real board + live BLE peers + two drivers), so these
# tests parse main.cpp and pin the load-bearing wiring a refactor / bad merge could silently drop. They
# assert the SHAPE, not runtime behaviour. The invariants:
#   (a) per-session routing: notifyTarget notifies g_activeTarget (the IN-FLIGHT session's claimed
#       slot the dispatcher loaded), NOT a bare global set only by connect;
#   (b) ref-counted session-end park: a session ending (WS disconnect / net session close) releases its
#       claim and parks the slot only when slotClaimCount() hits 0 (never yanks a slot another driver
#       still holds);
#   (c) no stateless driving: the HTTP /rpc drive endpoint is gone (driving is WS/TCP/serial only);
#   (d) fail-closed auth: an UNauthenticated WS client can still reach only `hello`.
# --------------------------------------------------------------------------- #
def test_notify_target_routes_via_active_session_target():
    """(a) notifyTarget must route to g_activeTarget -- the slot the dispatcher set from the CALLING
    session's claim before this line -- so two sessions driving two slots each hit their own central.
    It must NOT read a single global drive target (the old tgtSlot). Paired with the dispatcher wiring:
    handleLine loads the session's stored slot into g_activeTarget before dispatch and stores it back
    after, which is what makes the routing per-session instead of one board-wide target."""
    body = _strip_ws(_firmware_function_body("notifyTarget"))
    assert "slotConn[g_activeTarget]" in body, (
        "notifyTarget must notify the ACTIVE session target slotConn[g_activeTarget], not a global tgtSlot")
    assert "tgtSlot" not in body, "the single global drive target (tgtSlot) must be gone from notifyTarget"
    # the dispatcher sets g_activeTarget from the session's stored claim BEFORE dispatch and stores the
    # (possibly moved) claim back AFTER -- the before/after-dispatch wiring that makes routing per-session.
    disp = _strip_ws(_firmware_function_body("handleLine"))
    assert "g_activeTarget=old" in disp and "dispatchLine(line,out,sess)" in disp, (
        "handleLine must load the session's claim into g_activeTarget BEFORE dispatchLine (per-session routing)")
    assert "finishRoute(store,old)" in disp, (
        "handleLine must store the (possibly moved) claim back AFTER dispatch (finishRoute, ref-counted park)")


def test_session_end_ref_counts_the_park():
    """(b) A session ENDING must release its claim and park the slot ONLY when it was the last claimer.
    Both session-end paths must ref-count via slotClaimCount(): the WS client leaving (wsEvent's
    WStype_DISCONNECTED) and a TCP net session closing (netCloseSession). Without the count a second
    tab/client driving the same slot would have its central yanked when the first one leaves."""
    ws = _strip_ws(_firmware_function_body("wsEvent"))
    assert "caseWStype_DISCONNECTED:" in ws, "wsEvent must handle WStype_DISCONNECTED"
    assert "g_wsDrivenSlot[num]=-1" in ws, (
        "the DISCONNECTED handler must release THIS client's claim (g_wsDrivenSlot[num] = -1)")
    assert "slotClaimCount(slot)==0" in ws and "parkTargetSlot(slot)" in ws, (
        "a WS client leaving must park its slot only when slotClaimCount(slot)==0 (ref-counted release)")
    net = _strip_ws(_firmware_function_body("netCloseSession"))
    assert "slotClaimCount(slot)==0" in net and "parkTargetSlot(slot)" in net, (
        "a closing TCP net session must ref-count-park its driven slot too (slotClaimCount==0)")


def test_no_stateless_http_rpc_drive_endpoint():
    """(c) Driving is WS/TCP/serial only -- each a persistent SESSION with a BLE claim. There must be NO
    stateless HTTP /rpc drive endpoint (it had no session to route to). Guard: neither the handler nor
    its route survive; the page HTML is still served at GET /."""
    src = (ROOT / "firmware/esp32/src/main.cpp").read_text()
    assert "handleWebRpc" not in src, "the stateless HTTP /rpc drive handler must be removed"
    assert '"/rpc"' not in src, "the /rpc route registration must be removed"
    assert 'webServer.on("/", handleWebRoot)' in src, "GET / must still serve the control page HTML"


def test_ws_text_handler_is_fail_closed_auth_gated():
    """(d) wsOnText must be fail-closed: while a client is NOT authed (!wsAuthed[num]) the ONLY method
    that reaches the dispatcher is `hello`; any other method is refused with an 'auth required' error,
    and the single dispatch reachable while unauthed is the hello-only call (authed=false). WS drive
    must not be a way around authentication."""
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


def test_ws_authed_dispatch_sets_active_target_layout():
    """Merge-critical wiring guard: the WS text handler must setActiveLayout for the caller's claimed
    slot (g_activeTarget) BEFORE the authed handleJsonRpc dispatch, exactly like the serial/TCP path
    (dispatchLine), so typing driven over the WS honors the per-slot keyboard layout. If this drops, WS
    `type` would emit US scancodes to a `da` host."""
    body = _strip_ws(_firmware_function_body("wsOnText"))
    setlay = body.index("setActiveLayout(")
    authed_dispatch = body.index("handleJsonRpc(line,out,true,")
    assert setlay < authed_dispatch, (
        "wsOnText must setActiveLayout(slotLayout[g_activeTarget]) BEFORE the authed handleJsonRpc dispatch")
    assert "setActiveLayout((g_activeTarget>=0&&g_activeTarget<NUM_SLOTS)?slotLayout[g_activeTarget]" in body, (
        "the WS layout gate must select the caller's ACTIVE target slot's layout (matching the serial/TCP path)")


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

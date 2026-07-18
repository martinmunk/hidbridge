"""Offline tests for hidbridge.board -- location->family dispatch + the MCU adapter mapping.

No hardware: `resolve_board` builds a board object but does no I/O until prepare(), and the MCU
adapter is exercised with an injected fake RPC client + fake transport, so the identities/pair/connect/
disconnect/transport(raw|rpc)/wait_ready mapping is pinned in the offline suite. Live behaviour stays
gated (a flashed board / a bonded host).
"""

import pytest

from hidbridge import board as B
from hidbridge.bridge import HidBridge, NotBonded, Unsupported
from hidbridge.controller import HidController, RecordingTransport


# --------------------------------------------------------------------------- #
# location classification + resolver dispatch (no board TYPE passed in)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("loc,family", [
    (None, "auto"),
    ("/dev/ttyUSB3", "serial"),
    ("/dev/ttyACM0", "serial"),
    ("hci1", "dongle"),
    ("AA:BB:CC:DD:EE:FF", "dongle"),
    ("192.0.2.102", "net"),
    ("hidbridge-espeth.local", "net"),
    ("espeth", "net"),
])
def test_classify(loc, family):
    fam, _arg = B._classify(loc)
    assert fam == family


def test_resolve_serial_and_net_build_mcu_boards_without_io():
    assert isinstance(B.resolve_board("/dev/ttyUSB3"), B.McuBoard)
    assert isinstance(B.resolve_board("192.0.2.102", token="secrettoken"), B.McuBoard)
    # mode threads through
    assert B.resolve_board("/dev/ttyUSB3", mode="rpc").mode == "rpc"


def test_resolve_dongle_builds_dongle_board():
    assert isinstance(B.resolve_board("hci1"), B.DongleBoard)


def test_dongle_rejects_rpc_mode():
    with pytest.raises(Unsupported):
        B.resolve_board("hci1", mode="rpc")


# --------------------------------------------------------------------------- #
# fakes for the MCU adapter
# --------------------------------------------------------------------------- #
class FakeRpc:
    """Records calls; scriptable identities + hello. Duck-types rpc.RpcClient's surface used by board."""

    def __init__(self, slots=None, hello=None, caps=("keyboard", "mouse", "connect", "nickname",
                                                     "frames", "layout")):
        self.calls = []
        self._slots = list(slots or [])
        self._caps = set(caps)
        self._hello = hello or {"device": "HIDBRIDGE-esp32", "fw": "testfw", "proto": 1,
                                "nick": "espeth", "caps": list(caps), "layouts": ["us", "da", "uk"]}
        self.frames_sent = []

    def hello(self):
        self.calls.append(("hello", {}))
        return dict(self._hello)

    def require_cap(self, cap):
        if cap not in self._caps:
            from hidbridge.rpc import RpcError
            raise RpcError(f"board lacks capability {cap!r}")

    def identities(self):
        self.calls.append(("identities", {}))
        return {"slots": [dict(s) for s in self._slots], "adv": 0, "tgt": 0}

    def call(self, method, params=None):
        self.calls.append((method, dict(params or {})))
        if method == "connect":
            # mark the named/indexed slot connected + subscribed (simulate the iPad reattaching)
            self._set(params, conn=True, sub=True)
        return {"ok": True, **(params or {})}

    def pair(self, client):
        self.calls.append(("pair", {"client": client}))
        self._slots.append({"slot": len(self._slots), "client": client, "bonded": True,
                            "conn": False, "sub": False, "layout": "us"})
        return {"ok": True}

    def nickname(self, name=None):
        self.calls.append(("nickname", {"name": name} if name else {}))
        return {"ok": True, "nick": name}

    def frames(self, hexframes, *, dry=False):
        self.frames_sent.append(list(hexframes))
        return {"sent": len(hexframes), "conn": True}

    # helper: flip a slot's state by client name or slot index
    def _set(self, params, **kw):
        for s in self._slots:
            if (("client" in params and s.get("client") == params["client"])
                    or ("slot" in params and s.get("slot") == params["slot"])):
                s.update(kw)


class FakeTransport:
    BACKEND = "esp32"

    def __init__(self):
        self._ser = object()          # a truthy channel; board reuses it for the RPC client
        self.frames = []
        self.closed = False

    def send(self, frames):
        self.frames.extend(frames)
        return {"ok": True, "sent": len(frames)}

    def close(self):
        self.closed = True


def _mcu(slots, mode="raw", rpc=None, tp=None):
    rpc = rpc or FakeRpc(slots=slots)
    tp = tp or FakeTransport()
    b = B.McuBoard(mode=mode, transport=tp, rpc_client=rpc)
    b.prepare()
    return b, rpc, tp


# --------------------------------------------------------------------------- #
# probe -> board_info + capabilities
# --------------------------------------------------------------------------- #
def test_probe_self_identifies_board_and_fw():
    b, rpc, _ = _mcu([])
    assert b.board_info()["board"] == "esp32"      # from hello.device, NOT passed in
    assert b.board_info()["fw"] == "testfw"
    assert b.capabilities["max_sessions"] == 2     # esp32 holds several identities
    assert b.capabilities["per_device_identity"] is True
    assert b.capabilities["layouts"] is True


def test_pico_probe_single_session():
    rpc = FakeRpc(hello={"device": "HIDBRIDGE-pico", "fw": "picofw", "proto": 1, "caps": ["keyboard"]},
                  caps=["keyboard"])
    b = B.McuBoard(transport=FakeTransport(), rpc_client=rpc)
    b.prepare()
    assert b.board_info()["board"] == "pico"
    assert b.capabilities["max_sessions"] == 1


# --------------------------------------------------------------------------- #
# device management maps to the right RPC
# --------------------------------------------------------------------------- #
def test_list_devices_from_identities():
    slots = [
        {"slot": 0, "client": "mini5", "bonded": True, "conn": True, "sub": True, "layout": "da"},
        {"slot": 1, "client": "", "bonded": False, "conn": False, "sub": False, "layout": "us"},
        {"slot": 2, "client": "laptop3", "bonded": True, "conn": False, "sub": False, "layout": "us"},
    ]
    b, _, _ = _mcu(slots)
    devs = {d.name: d for d in b.list_devices()}
    assert set(devs) == {"mini5", "laptop3"}       # the empty/unbonded slot is not a device
    assert devs["mini5"].connected is True and devs["mini5"].layout == "da"
    assert devs["laptop3"].connected is False


def test_pair_unpair_rename_map_to_rpc():
    b, rpc, _ = _mcu([{"slot": 0, "client": "mini5", "bonded": True, "conn": False, "sub": False}])
    b.pair("newpad")
    b.rename("labkit")
    b.unpair("mini5")
    methods = [m for m, _ in rpc.calls]
    assert ("pair", {"client": "newpad"}) in rpc.calls
    assert ("nickname", {"name": "labkit"}) in rpc.calls
    assert ("unpair", {"client": "mini5"}) in rpc.calls
    assert "identities" in methods                 # unpair resolved the name->slot first


def test_connect_calls_rpc_connect_and_waits_ready():
    slots = [{"slot": 3, "client": "mini5", "bonded": True, "conn": False, "sub": False}]
    b, rpc, _ = _mcu(slots)
    assert b.connect("mini5", timeout=1.0) is True
    assert ("connect", {"client": "mini5"}) in rpc.calls
    assert b.wait_ready("mini5", timeout=1.0) is True


def test_wait_ready_blocks_until_subscribed_then_times_out():
    # not subscribed yet -> wait_ready returns False at the timeout (best-effort readiness gate)
    slots = [{"slot": 0, "client": "mini5", "bonded": True, "conn": True, "sub": False}]
    b, rpc, _ = _mcu(slots)
    assert b.wait_ready("mini5", timeout=0.05) is False
    # flip subscribed -> wait_ready resolves True
    rpc._slots[0]["sub"] = True
    assert b.wait_ready("mini5", timeout=1.0) is True


def test_connect_unknown_device_raises_not_bonded():
    b, _, _ = _mcu([{"slot": 0, "client": "mini5", "bonded": True, "conn": False, "sub": False}])
    with pytest.raises(NotBonded):
        b.connect("ghost", timeout=0.1)


def test_disconnect_maps_to_rpc():
    b, rpc, _ = _mcu([{"slot": 0, "client": "mini5", "bonded": True, "conn": True, "sub": True}])
    b.disconnect("mini5")
    assert ("disconnect", {"client": "mini5"}) in rpc.calls


# --------------------------------------------------------------------------- #
# transport: raw relays K/M frames; rpc relays via the `frames` method
# --------------------------------------------------------------------------- #
def test_raw_transport_is_the_frame_relay():
    b, _, tp = _mcu([{"slot": 0, "client": "mini5", "bonded": True, "conn": True, "sub": True}],
                    mode="raw")
    HidController(b.transport("mini5")).type_text("hi")
    assert tp.frames                                # frames went to the raw transport
    assert tp.frames[0][0] == "keyboard"


def test_rpc_transport_relays_prebuilt_frames_via_frames_method():
    b, rpc, tp = _mcu([{"slot": 0, "client": "mini5", "bonded": True, "conn": True, "sub": True}],
                      mode="rpc")
    HidController(b.transport("mini5")).type_text("hi")
    assert tp.frames == []                           # NOT the raw transport
    assert rpc.frames_sent                           # relayed as JSON-RPC `frames`
    # frames are hex '<ridHex><bodyHex>' with the keyboard report id 01
    ref = RecordingTransport()
    HidController(ref).type_text("hi")
    flat = [f for batch in rpc.frames_sent for f in batch]
    assert flat[0].startswith("01") and flat[0][2:] == ref.frames[0][1].hex()


# --------------------------------------------------------------------------- #
# end-to-end through HidBridge with a real McuBoard driven by the fakes (backend=)
# --------------------------------------------------------------------------- #
def test_hidbridge_over_mcuboard_full_lifecycle():
    slots = [{"slot": 0, "client": "mini5", "bonded": True, "conn": False, "sub": False}]
    rpc = FakeRpc(slots=slots)
    tp = FakeTransport()
    board = B.McuBoard(mode="raw", transport=tp, rpc_client=rpc)
    with HidBridge("/dev/ttyUSB3", backend=board) as bridge:
        assert bridge.info["board"] == "esp32"
        assert [d.name for d in bridge.list_devices()] == ["mini5"]
        with bridge.connect("mini5") as s:
            s.type("hi")
        assert ("disconnect", {"client": "mini5"}) in rpc.calls
    assert tp.closed


# --------------------------------------------------------------------------- #
# discovery (vendor-gated + handshake), all injected
# --------------------------------------------------------------------------- #
def test_discover_returns_boards_that_answer_hello():
    cands = ["/dev/ttyUSB0", "/dev/ttyUSB3"]

    def prober(p):
        if p == "/dev/ttyUSB3":
            return {"port": p, "board": "esp32", "fw": "x", "device": "HIDBRIDGE-esp32"}
        return None
    found = B.discover(candidates=cands, prober=prober)
    assert [f["port"] for f in found] == ["/dev/ttyUSB3"]

"""Offline tests for hidbridge.board -- location->family dispatch + the MCU adapter mapping.

No hardware: `resolve_board` builds a board object but does no I/O until prepare(), and the MCU
adapter is exercised with an injected fake RPC client + fake transport, so the identities/pair/connect/
disconnect/drive/wait_ready mapping is pinned in the offline suite. The MCU board DELEGATES the drive
verbs (type/key/move/click/scroll) to the firmware's high-level JSON-RPC methods (the board builds the
reports); the dongle board builds them host-side via reports.py. Live behaviour stays gated (a flashed
board / a bonded host).
"""

import json

import pytest

from hidbridge import board as B
from hidbridge import rpc
from hidbridge.bridge import (
    HidBridge,
    IncompatibleFirmware,
    NotBonded,
    ProbeError,
    Unsupported,
)
from hidbridge.controller import HidController, RecordingTransport


# --------------------------------------------------------------------------- #
# location classification + resolver dispatch (no board TYPE passed in)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("loc,family,arg,hint", [
    (None, "auto", None, None),
    # bare forms: shape decides
    ("/dev/ttyUSB3", "serial", "/dev/ttyUSB3", None),
    ("/dev/ttyACM0", "serial", "/dev/ttyACM0", None),
    ("hci1", "dongle", "hci1", None),
    ("AA:BB:CC:DD:EE:FF", "dongle", "AA:BB:CC:DD:EE:FF", None),
    ("192.0.2.102", "net", "192.0.2.102", None),
    ("hidbridge-espeth.local", "net", "hidbridge-espeth.local", None),
    ("espeth", "net", "espeth", None),
    # explicit backend prefixes: family named + prefix stripped (+ a serial board pin)
    ("net:hidbridge-esp32up.local", "net", "hidbridge-esp32up.local", None),
    ("net:192.0.2.140", "net", "192.0.2.140", None),
    ("net:192.0.2.140:3333", "net", "192.0.2.140:3333", None),
    ("esp32:/dev/ttyUSB3", "serial", "/dev/ttyUSB3", "esp32"),
    ("pico:/dev/ttyACM0", "serial", "/dev/ttyACM0", "pico"),
    ("serial:/dev/ttyUSB9", "serial", "/dev/ttyUSB9", None),
])
def test_classify(loc, family, arg, hint):
    fam, a, h = B._classify(loc)
    assert (fam, a, h) == (family, arg, hint)


def test_resolve_serial_and_net_build_mcu_boards_without_io():
    assert isinstance(B.resolve_board("/dev/ttyUSB3"), B.McuBoard)
    assert isinstance(B.resolve_board("192.0.2.102", token="secrettoken"), B.McuBoard)


def test_resolve_strips_prefix_and_pins_serial_board(monkeypatch):
    # net: prefix -> net family, host stripped (no more DNS on the literal 'net:host')
    seen = {}
    monkeypatch.setattr(B, "_net_transport_factory",
                        lambda host, **k: seen.update(net_host=host) or (lambda: None))
    B.resolve_board("net:hidbridge-esp32up.local", token="t")
    assert seen["net_host"] == "hidbridge-esp32up.local"
    # pico: prefix -> serial family, path stripped, board pinned to pico
    monkeypatch.setattr(B, "_serial_transport_factory",
                        lambda port, *, board_hint=None, **k:
                        seen.update(ser_port=port, ser_hint=board_hint) or (lambda: None))
    B.resolve_board("pico:/dev/ttyACM0")
    assert seen["ser_port"] == "/dev/ttyACM0" and seen["ser_hint"] == "pico"
    # esp32: prefix pins esp32
    B.resolve_board("esp32:/dev/ttyUSB3")
    assert seen["ser_port"] == "/dev/ttyUSB3" and seen["ser_hint"] == "esp32"


def test_resolve_dongle_builds_dongle_board():
    assert isinstance(B.resolve_board("hci1"), B.DongleBoard)


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

    def call(self, method, params=None, *, dry=False):
        self.calls.append((method, dict(params or {})))
        if method == "connect":
            # mark the named/indexed slot connected + subscribed (simulate the iPad reattaching)
            self._set(params, conn=True, sub=True)
        return {"ok": True, **(params or {})}

    # high-level drive verbs (mirror rpc.RpcClient: shape params + record via call)
    def type_text(self, text, *, dry=False):
        return self.call("type", {"text": text}, dry=dry)

    def key(self, name, mod=0, *, dry=False):
        return self.call("key", {"name": name, "mod": int(mod)}, dry=dry)

    def move(self, dx, dy, *, dry=False):
        return self.call("move", {"dx": int(dx), "dy": int(dy)}, dry=dry)

    def click(self, button="left", *, dry=False):
        return self.call("click", {"button": button}, dry=dry)

    def scroll(self, clicks, *, dry=False):
        return self.call("scroll", {"clicks": int(clicks)}, dry=dry)

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


def _mcu(slots, rpc=None, tp=None):
    rpc = rpc or FakeRpc(slots=slots)
    tp = tp or FakeTransport()
    b = B.McuBoard(transport=tp, rpc_client=rpc)
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
# drive verbs: the MCU board tells the firmware WHAT (high-level JSON-RPC); the board builds
# --------------------------------------------------------------------------- #
def test_drive_verbs_delegate_to_firmware_rpc_methods():
    b, rpc, tp = _mcu([{"slot": 0, "client": "mini5", "bonded": True, "conn": True, "sub": True}])
    b.type("hi")
    b.key("enter", 8)
    b.move(3, -4)
    b.click("back")
    b.scroll(-2)
    # each verb is a high-level JSON-RPC method -- the BOARD builds the report bytes (per-slot layout
    # applies board-side); nothing is relayed as pre-built frames over the raw transport
    assert ("type", {"text": "hi"}) in rpc.calls
    assert ("key", {"name": "enter", "mod": 8}) in rpc.calls
    assert ("move", {"dx": 3, "dy": -4}) in rpc.calls
    assert ("click", {"button": "back"}) in rpc.calls
    assert ("scroll", {"clicks": -2}) in rpc.calls
    assert tp.frames == []                           # NOT host-built + relayed as raw frames


def test_transport_accessor_returns_the_raw_frame_channel():
    # the raw frame transport stays available for the board-level radio ops (advertise/net_status/
    # ping in the CLI); it is NOT the drive path anymore
    b, _, tp = _mcu([])
    assert b.transport() is tp


# --------------------------------------------------------------------------- #
# end-to-end through HidBridge with a real McuBoard driven by the fakes (backend=)
# --------------------------------------------------------------------------- #
def test_hidbridge_over_mcuboard_full_lifecycle():
    slots = [{"slot": 0, "client": "mini5", "bonded": True, "conn": False, "sub": False}]
    rpc = FakeRpc(slots=slots)
    tp = FakeTransport()
    board = B.McuBoard(transport=tp, rpc_client=rpc)
    with HidBridge("/dev/ttyUSB3", backend=board) as bridge:
        assert bridge.info["board"] == "esp32"
        assert [d.name for d in bridge.list_devices()] == ["mini5"]
        with bridge.connect("mini5") as s:
            s.type("hi")
        assert ("type", {"text": "hi"}) in rpc.calls     # board built it via the `type` RPC method
        assert ("disconnect", {"client": "mini5"}) in rpc.calls
    assert tp.closed


# --------------------------------------------------------------------------- #
# DongleBoard adapter (BlueZ), fake link manager (no dbus)
# --------------------------------------------------------------------------- #
class _FakeDongleLink:
    def __init__(self, devices):
        self.devices = devices           # {path: props}
        self.disconnected = []

    def all_devices(self, adapter_path):
        return [(p, pr) for p, pr in self.devices.items() if p.startswith(adapter_path + "/")]

    def find_device(self, adapter_path, name):
        for p, pr in self.devices.items():
            if name.lower() in str(pr.get("Alias", "")).lower():
                return p, pr
        return None, None

    def connected_devices(self, adapter_path):
        return [(p, pr) for p, pr in self.devices.items() if pr.get("Connected")]

    def is_connected(self, path):
        return bool(self.devices.get(path, {}).get("Connected"))

    def connect(self, path, timeout=20.0):
        self.devices[path]["Connected"] = True
        return True

    def disconnect(self, path):
        self.disconnected.append(path)
        return True


def test_dongle_board_lists_bonded_devices(monkeypatch):
    from hidbridge import conn as C
    monkeypatch.setattr(C._adapter, "resolve_dongle",
                        lambda *a, **k: {"hci": "hci1", "product_name": "UB500"})
    link = _FakeDongleLink({
        "/org/bluez/hci1/dev_AA": {"Alias": "iPad A", "Paired": True, "Connected": True},
        "/org/bluez/hci1/dev_BB": {"Alias": "iPad B", "Paired": True, "Connected": False},
    })
    board = B.DongleBoard("hci1", link_manager=link, transport=RecordingTransport())
    board.prepare()
    assert board.board_info()["board"] == "dongle"
    assert board.capabilities["max_sessions"] == 1
    devs = {d.name: d for d in board.list_devices()}
    assert set(devs) == {"iPad A", "iPad B"}
    assert devs["iPad A"].connected is True and devs["iPad A"].bonded is True


def test_dongle_board_drives_host_built_reports(monkeypatch):
    # a dongle has no board to delegate to, so type/key/... build the exact reports.py bytes host-side
    from hidbridge import conn as C
    monkeypatch.setattr(C._adapter, "resolve_dongle", lambda *a, **k: {"hci": "hci1"})
    rec = RecordingTransport()
    board = B.DongleBoard("hci1", link_manager=_FakeDongleLink({}), transport=rec)
    board.prepare()
    board.type("hi")
    board.key("enter", 8)
    board.move(3, -4)
    board.click("back")
    board.scroll(-2)
    ref = RecordingTransport()
    c = HidController(ref)
    c.type_text("hi"); c.send_key("enter", 8); c.move(3, -4); c.click("back"); c.scroll(-2)
    assert rec.frames == ref.frames                  # byte-for-byte what a bare controller sends


def test_dongle_board_pair_unpair_rename_unsupported(monkeypatch):
    from hidbridge import conn as C
    monkeypatch.setattr(C._adapter, "resolve_dongle", lambda *a, **k: {"hci": "hci1"})
    board = B.DongleBoard("hci1", link_manager=_FakeDongleLink({}), transport=RecordingTransport())
    for op in (lambda: board.pair("x"), lambda: board.unpair("x"), lambda: board.rename("x")):
        with pytest.raises(Unsupported):
            op()


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


def test_discover_includes_pico_ports(monkeypatch):
    # a Pico native-USB port (ttyACM) that answers hello must show up alongside the ESP32; a legacy
    # Pico (ttyACM0) that does not answer hello is dropped, as is the KVM (ttyUSB0).
    cands = ["/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyUSB3"]

    def prober(p):
        if p == "/dev/ttyACM1":
            return {"port": p, "board": "pico", "fw": "1.0.0", "device": "HIDBRIDGE-pico"}
        if p == "/dev/ttyUSB3":
            return {"port": p, "board": "esp32", "fw": "x", "device": "HIDBRIDGE-esp32"}
        return None                                    # ttyACM0 = legacy, no hello
    found = B.discover(candidates=cands, prober=prober)
    assert [f["port"] for f in found] == ["/dev/ttyACM1", "/dev/ttyUSB3"]
    assert {f["board"] for f in found} == {"pico", "esp32"}


# --------------------------------------------------------------------------- #
# Bug 1/2: per-device transport selection (Pico native USB vs ESP32 bridge) + the unified channel
# --------------------------------------------------------------------------- #
def test_is_pico_port_only_for_ttyacm_raspberry_vid(monkeypatch):
    import hidbridge.transport_pico as TP
    monkeypatch.setattr(TP, "_tty_usb_vendor", lambda dev: "2e8a")
    assert B._is_pico_port("/dev/ttyACM1") is True
    monkeypatch.setattr(TP, "_tty_usb_vendor", lambda dev: "10c4")   # a CP210x on ttyACM (rare)
    assert B._is_pico_port("/dev/ttyACM1") is False
    assert B._is_pico_port("/dev/ttyUSB3") is False                  # ttyUSB is never a Pico


def test_is_pico_port_resolves_by_id_symlink(tmp_path, monkeypatch):
    import os
    import hidbridge.transport_pico as TP
    # /dev/serial/by-id/usb-Raspberry_Pi_Pico_2W_...-if00 is a SYMLINK to /dev/ttyACMx (the stable path
    # we hand out). _is_pico_port must resolve it BEFORE the ttyACM basename check -- else a by-id
    # location falls back to the ESP32 transport (DTR de-asserted) and the Pico's CDC never transmits.
    link = tmp_path / "usb-Raspberry_Pi_Pico_2W_0169ABE90D273D42-if00"
    os.symlink("/dev/ttyACM7", link)
    monkeypatch.setattr(TP, "_tty_usb_vendor", lambda dev: "2e8a")
    assert B._is_pico_port(str(link)) is True


def test_serial_factory_selects_pico_transport_for_pico_port(monkeypatch):
    import hidbridge.transport_pico as TP
    monkeypatch.setattr(TP, "_tty_usb_vendor", lambda dev: "2e8a")
    tp = B._serial_transport_factory("/dev/ttyACM1")()               # no hardware: pico opens lazily
    assert isinstance(tp, TP.PicoSerialTransport)


def test_serial_factory_selects_esp32_transport_for_ttyusb():
    import hidbridge.transport_esp32 as TE
    tp = B._serial_transport_factory("/dev/ttyUSB3", open_now=False)()
    assert isinstance(tp, TE.Esp32Transport)


def test_board_hint_pico_forces_pico_transport():
    import hidbridge.transport_pico as TP
    tp = B._serial_transport_factory("/dev/ttyUSB9", board_hint="pico")()
    assert isinstance(tp, TP.PicoSerialTransport)


class _JsonChannel:
    """Minimal write/readline JSON-RPC channel: answers `hello` from a script, else {"ok":true}."""

    def __init__(self, hello):
        self._hello = hello
        self._out = []

    def write(self, b):
        req = json.loads(b.decode("ascii"))
        res = dict(self._hello) if req.get("method") == "hello" else {"ok": True}
        self._out.append((json.dumps({"jsonrpc": "2.0", "id": req.get("id"), "result": res})
                          + "\n").encode("ascii"))

    def readline(self):
        return self._out.pop(0) if self._out else b""

    def close(self):
        pass


class _LazyPicoTransport:
    """Mimics PicoSerialTransport's SHAPE: lazy, exposes only `open_channel()` (no eager `_ser`, no
    `_open()`), so it proves McuBoard drives over the unified transport interface (Bug 2)."""

    BACKEND = "pico"

    def __init__(self, hello, port="/dev/ttyACM1"):
        self.port = port
        self._hello = hello
        self._ser = None
        self.closed = False

    def open_channel(self):
        if self._ser is None:
            self._ser = _JsonChannel(self._hello)
        return self._ser

    def close(self):
        self.closed = True


def test_mcuboard_drives_over_lazy_pico_transport_interface():
    hello = {"device": "HIDBRIDGE-pico", "fw": "1.0.0", "proto": 1, "caps": ["keyboard", "mouse"]}
    tp = _LazyPicoTransport(hello)
    b = B.McuBoard(transport_factory=lambda: tp)
    b.prepare()                                        # builds RpcClient over open_channel(), probes
    assert b.board_info()["board"] == "pico"
    assert b.board_info()["fw"] == "1.0.0"
    assert b.capabilities["max_sessions"] == 1
    b.type("hi")                                       # a drive verb reaches the firmware JSON-RPC
    b.close()
    assert tp.closed


# --------------------------------------------------------------------------- #
# Bug 3/4: a bad probe RAISES (never a degraded, capability-less board)
# --------------------------------------------------------------------------- #
class _RaisingRpc:
    def __init__(self, exc):
        self._exc = exc

    def hello(self):
        raise self._exc


def test_probe_timeout_raises_probe_error_not_degraded_board():
    from hidbridge.rpc import RpcTimeout
    b = B.McuBoard(transport=FakeTransport(), rpc_client=_RaisingRpc(RpcTimeout("silence")))
    with pytest.raises(ProbeError):
        b.prepare()


def test_probe_legacy_firmware_raises_incompatible():
    from hidbridge.rpc import RpcProtocolError
    b = B.McuBoard(transport=FakeTransport(),
                   rpc_client=_RaisingRpc(RpcProtocolError("board replied 'ERR unknowncmd'")))
    with pytest.raises(IncompatibleFirmware):
        b.prepare()


def test_hidbridge_legacy_board_raises_incompatible_on_enter():
    from hidbridge.rpc import RpcProtocolError
    board = B.McuBoard(transport=FakeTransport(),
                       rpc_client=_RaisingRpc(RpcProtocolError("ERR unknowncmd")))
    with pytest.raises(IncompatibleFirmware):
        with HidBridge("/dev/ttyACM0", backend=board):
            pass


def test_hidbridge_unreachable_board_raises_probe_error_on_enter():
    from hidbridge.rpc import RpcTimeout
    board = B.McuBoard(transport=FakeTransport(), rpc_client=_RaisingRpc(RpcTimeout("silence")))
    with pytest.raises(ProbeError):
        with HidBridge("/dev/ttyACM9", backend=board):
            pass


# --------------------------------------------------------------------------- #
# END-TO-END through the REAL rpc.RpcClient: HidBridge -> McuBoard -> RpcClient over a FAKE CHANNEL.
#
# Every other MCU test above injects `FakeRpc`, which REPLACES rpc.RpcClient and so bypasses the real
# demux + proto gating. Here the board is driven by a genuine `rpc.RpcClient` over a scriptable
# write/readline channel, so the ACTUAL response demux (proto-1 plain-text `EVT` skip vs proto>=2 id-less
# `event` notification capture) + the `hello` version handling run INSIDE the full stack. Parametrized
# over proto {1, 2, 3=newer-than-KNOWN} the channel simulates a board at that proto and interleaves a
# proto-appropriate async readiness event before its replies; we then run the whole
# connect/type/move/disconnect lifecycle and assert the drive verbs still reached the board unbroken.
# --------------------------------------------------------------------------- #
class FakeBoardChannel:
    """A REAL write/readline JSON-RPC channel simulating a board at `proto`. Stateful: `connect` flips
    the one slot conn+sub, so wait_ready/_slot_connected pass without hardware. Before EVERY reply it
    interleaves a proto-appropriate async readiness event -- plain-text `EVT ...` bytes on proto 1, an
    id-less JSON `event` notification on proto>=2 -- exactly the post-connect burst the demux must
    tolerate. Records (method, params) of each request so the test can assert the drive verbs arrived."""

    _CAPS = ["keyboard", "mouse", "connect", "nickname", "frames", "layout"]

    def __init__(self, proto, name="mini5"):
        self.proto = proto
        self.name = name
        self.requests = []          # (method, params) for every JSON-RPC request seen
        self.raw_writes = []        # non-JSON lines (e.g. a proto-1 @token) -- unused on this path
        self._queue = []
        self._slot = {"slot": 0, "client": name, "bonded": True, "conn": False, "sub": False,
                      "layout": "us"}

    def _events(self):
        # proto 1: unsolicited plain-text EVT lines (bytes); proto>=2: id-less JSON event notifications
        if self.proto == 1:
            return [b"EVT connected\n", b"EVT ready\n"]
        return [{"jsonrpc": "2.0", "method": "event", "params": {"event": "ready", "slot": 0}}]

    def _reply(self, method, params):
        if method == "hello":
            return {"proto": self.proto, "caps": list(self._CAPS), "device": "HIDBRIDGE-esp32",
                    "fw": "fakefw", "nick": "espeth", "layouts": ["us", "da"]}
        if method == "identities":
            return {"slots": [dict(self._slot)], "adv": 0, "tgt": 0}
        if method == "connect":
            self._slot.update(conn=True, sub=True)     # the iPad reattached + enabled the reports
        if method == "ping":
            return {"device": self.name, "subscribed": self._slot["sub"], "conn": self._slot["conn"]}
        return {"ok": True, **params}

    def write(self, b):
        try:
            req = json.loads(b.decode())
        except ValueError:
            self.raw_writes.append(b)
            return len(b)
        m, p = req["method"], req.get("params", {})
        self.requests.append((m, p))
        for ev in self._events():                       # async event burst BEFORE the matching reply
            self._queue.append(ev)
        self._queue.append({"jsonrpc": "2.0", "id": req["id"], "result": self._reply(m, p)})
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


@pytest.mark.parametrize("proto", [1, 2, 3])
def test_hidbridge_real_rpcclient_drives_and_tolerates_proto_events(proto):
    ch = FakeBoardChannel(proto)
    rpc_client = rpc.RpcClient(ch)                       # the REAL client -- real demux + proto gating
    board = B.McuBoard(transport=FakeTransport(), rpc_client=rpc_client)

    def run():
        with HidBridge("/dev/ttyUSB3", backend=board) as bridge:
            assert bridge.info["board"] == "esp32" and bridge.info["proto"] == proto
            assert [d.name for d in bridge.list_devices()] == ["mini5"]
            with bridge.connect("mini5") as s:          # connect + wait_ready cross the event burst
                s.type("hi")
                s.move(3, 4)

    if proto > rpc.KNOWN_PROTO:
        # a board NEWER than the client understands must WARN on hello (never crash) but still drive
        with pytest.warns(UserWarning, match=f"protocol {proto}"):
            run()
    else:
        run()

    # the drive verbs REACHED the board through the whole stack (real client built the JSON-RPC requests)
    assert ("type", {"text": "hi"}) in ch.requests
    assert ("move", {"dx": 3, "dy": 4}) in ch.requests
    assert ("connect", {"client": "mini5"}) in ch.requests
    assert ("disconnect", {"client": "mini5"}) in ch.requests   # session exit disconnected, bond kept

    # the interleaved async events did NOT break the lifecycle -- and on proto>=2 the id-less `event`
    # notification was captured by the real demux; on proto 1 the plain-text EVT lines are skipped, not
    # captured (last_event stays None), proving BOTH formats flowed through end-to-end.
    if proto >= 2:
        assert rpc_client.last_event == {"event": "ready", "slot": 0}
    else:
        assert rpc_client.last_event is None

"""The context-manager driving API -- HidBridge -> connect() -> HidSession (+ the session() shorthand).

Written red-first: this pins the CALLER-facing surface before the implementation exists. The whole
lifecycle (open the board, list/pair devices, connect a bonded host on demand, drive it, disconnect)
runs against an in-memory FakeBoard -- no hardware, no serial, no bluetooth -- so the red/green loop
is pure logic.

Design under test:
  with HidBridge("/dev/ttyUSB3") as bridge:           # board channel; probes + self-identifies
      bridge.list_devices()                           # [Device(name=..., bonded=..., connected=...)]
      with bridge.connect("laptop3") as session:      # connect-on-demand; blocks until ready
          session.type("hi")                          # tell it WHAT; the board builds + sends
      # session exit -> disconnect, bond kept

  with hidbridge.session("/dev/ttyUSB3", "laptop3") as s:   # single-host shorthand: opens + connects
      s.type("hi")

The board type (esp32/pico) is NEVER passed in -- `bridge.info` is discovered from the probe. A
second concurrent connect() raises Unsupported (single-identity backend) or CapacityError (multi
backend, slots full); an unknown device raises NotBonded; a device that never attaches raises
ConnectTimeout. All inherit HidbridgeError.
"""

import pytest

import hidbridge
from hidbridge import (
    CapacityError,
    ConnectTimeout,
    HidBridge,
    HidbridgeError,
    HidSession,
    NotBonded,
    Unsupported,
)
class _Dev:
    def __init__(self, name, bonded=True, connected=False):
        self.name, self.bonded, self.connected = name, bonded, connected


class FakeBoard:
    """In-memory stand-in for a board channel (the object injected via backend= instead of a real
    serial/net/dongle driver). Implements exactly the protocol HidBridge calls -- including the
    HIGH-LEVEL drive verbs (type/key/move/click/scroll): the caller tells the board WHAT, the board
    builds + sends. Byte-parity with reports.py is a per-backend concern (asserted in the board /
    rpc-drift tests), NOT here."""

    def __init__(self, *, board="esp32", fw="dev", devices=("laptop3",),
                 max_sessions=1, will_connect=True, proto=None, caps=None, raise_on=None):
        self._board, self._fw = board, fw
        self._proto = proto                           # None = not a versioned MCU (like the dongle)
        self._devices = {n: _Dev(n) for n in devices}
        self.capabilities = {"max_sessions": max_sessions, "layouts": True}
        if caps is not None:                          # a caps-advertising board (an MCU via hello)
            self.capabilities["caps"] = list(caps)
        self._will_connect = will_connect
        # method names that raise the firmware "method not found" RpcError (simulates an older board
        # that lacks a method the wheel calls) -- the _guard should turn it into IncompatibleFirmware.
        self._raise_on = set(raise_on or ())
        # observability for assertions
        self.drove: list = []                         # the high-level drive calls, in order
        self.prepared = self.closed = False
        self.connects, self.disconnects = [], []

    # ---- lifecycle / probe ----
    def prepare(self):
        self.prepared = True

    def close(self):
        self.closed = True

    def board_info(self):
        return {"board": self._board, "fw": self._fw, "proto": self._proto}

    def _maybe_raise(self, method):
        if method in self._raise_on:
            from hidbridge.rpc import RpcError
            raise RpcError(f"{method}: method not found (code -32601)")

    # ---- device management ----
    def list_devices(self):
        self._maybe_raise("list_devices")
        return list(self._devices.values())

    def pair(self, name):
        self._devices[name] = _Dev(name)

    def unpair(self, name):
        self._devices.pop(name, None)

    # ---- connect on demand (per device; may hold several up to max_sessions) ----
    def connect(self, name, timeout):
        if not self._will_connect:
            return False
        self.connects.append(name)
        self._devices[name].connected = True
        return True

    def disconnect(self, name):
        self.disconnects.append(name)
        if name in self._devices:
            self._devices[name].connected = False

    # ---- drive (high-level; the board owns report building) ----
    def type(self, text):
        self._maybe_raise("type")
        self.drove.append(("type", text)); return {"ok": True}

    def key(self, name, mod=0):
        self.drove.append(("key", name, mod)); return {"ok": True}

    def move(self, dx, dy):
        self.drove.append(("move", dx, dy)); return {"ok": True}

    def click(self, button="left"):
        self.drove.append(("click", button)); return {"ok": True}

    def scroll(self, clicks):
        self.drove.append(("scroll", clicks)); return {"ok": True}


def test_bridge_enters_prepares_and_exposes_probe_info():
    fake = FakeBoard(board="esp32", fw="dev")
    with HidBridge("/dev/ttyUSB3", backend=fake) as bridge:
        assert fake.prepared
        assert bridge.info["board"] == "esp32"      # discovered, NOT passed in
        assert bridge.info["fw"] == "dev"
        assert bridge.capabilities.get("layouts") is True
    assert fake.closed                              # bridge exit closed the channel


def test_list_devices_and_pair_unpair():
    fake = FakeBoard(devices=("laptop3", "dev-trix"))
    with HidBridge("x", backend=fake) as b:
        assert [d.name for d in b.list_devices()] == ["laptop3", "dev-trix"]
        b.pair("laptop4")
        assert "laptop4" in [d.name for d in b.list_devices()]
        b.unpair("dev-trix")
        assert "dev-trix" not in [d.name for d in b.list_devices()]


def test_connect_returns_hidsession_and_delegates_drive_to_board():
    fake = FakeBoard(devices=("laptop3",))
    with HidBridge("x", backend=fake) as b:
        with b.connect("laptop3") as s:
            assert isinstance(s, HidSession)
            s.type("hi")
            s.key("enter", 8)
            s.move(3, -4)
            s.click("right")
            s.scroll(-2)
    # the session is a thin pass-through to the board's high-level verbs (the board builds the bytes)
    assert fake.drove == [("type", "hi"), ("key", "enter", 8), ("move", 3, -4),
                          ("click", "right"), ("scroll", -2)]
    assert fake.connects == ["laptop3"]


def test_session_surface_is_high_level_only():
    # exactly type/key/move/click/scroll -- NO unlock, NO frames, NO raw controller escape hatch
    fake = FakeBoard(devices=("laptop3",))
    with HidBridge("x", backend=fake) as b:
        with b.connect("laptop3") as s:
            for verb in ("type", "key", "move", "click", "scroll"):
                assert callable(getattr(s, verb))
            for gone in ("unlock", "frames", "controller"):
                assert not hasattr(s, gone), f"HidSession must not expose {gone!r}"


def test_session_exit_disconnects_and_keeps_bond():
    fake = FakeBoard(devices=("laptop3",))
    with HidBridge("x", backend=fake) as b:
        with b.connect("laptop3"):
            pass
        assert fake.disconnects == ["laptop3"]                   # disconnected on session exit
        assert "laptop3" in [d.name for d in b.list_devices()]   # bond kept


def test_session_shorthand_opens_connects_drives_and_tears_down():
    fake = FakeBoard(devices=("laptop3",))
    with hidbridge.session("/dev/ttyUSB3", "laptop3", backend=fake) as s:
        assert isinstance(s, HidSession)
        s.type("hi")
    assert fake.connects == ["laptop3"]
    assert fake.disconnects == ["laptop3"]          # session torn down
    assert fake.closed                              # ...and the bridge under it


def test_connect_unbonded_raises_not_bonded():
    fake = FakeBoard(devices=("laptop3",))
    with HidBridge("x", backend=fake) as b:
        with pytest.raises(NotBonded):
            b.connect("ghost")


def test_connect_timeout_raises():
    fake = FakeBoard(devices=("laptop3",), will_connect=False)
    with HidBridge("x", backend=fake) as b:
        with pytest.raises(ConnectTimeout):
            b.connect("laptop3", timeout=0.05)


def test_second_connect_on_single_identity_raises_unsupported():
    fake = FakeBoard(devices=("laptop3", "dev-trix"), max_sessions=1)
    with HidBridge("x", backend=fake) as b:
        with b.connect("laptop3"):
            with pytest.raises(Unsupported):
                b.connect("dev-trix")


def test_capacity_exhausted_on_multi_backend_raises():
    fake = FakeBoard(devices=("a", "b", "c"), max_sessions=2)
    with HidBridge("x", backend=fake) as b:
        with b.connect("a"), b.connect("b"):
            with pytest.raises(CapacityError):
                b.connect("c")


def test_all_errors_subclass_hidbridge_error():
    from hidbridge import IncompatibleFirmware
    for exc in (NotBonded, ConnectTimeout, Unsupported, CapacityError, IncompatibleFirmware):
        assert issubclass(exc, HidbridgeError)


# --------------------------------------------------------------------------- #
# version negotiation (Phase B): the host<->MCU contract is `hello`'s proto + caps
# --------------------------------------------------------------------------- #
def test_new_host_old_firmware_low_proto_raises_incompatible():
    # a board whose firmware protocol predates MIN_FIRMWARE_PROTO -> a clear typed refusal on __enter__,
    # NOT a silent proceed. This is the new-wheel / old-board direction of the decoupling.
    from hidbridge import IncompatibleFirmware
    from hidbridge.bridge import MIN_FIRMWARE_PROTO
    fake = FakeBoard(proto=MIN_FIRMWARE_PROTO - 1, caps=["keyboard", "mouse"])
    with pytest.raises(IncompatibleFirmware, match="protocol"):
        HidBridge("x", backend=fake).__enter__()
    assert fake.closed                                # the failed open still closed the channel


def test_supported_proto_enters_cleanly():
    from hidbridge.bridge import MIN_FIRMWARE_PROTO
    fake = FakeBoard(proto=MIN_FIRMWARE_PROTO, caps=["keyboard", "mouse", "connect"])
    with HidBridge("x", backend=fake) as b:
        assert b.info["proto"] == MIN_FIRMWARE_PROTO
        assert b.supports("connect") and not b.supports("nope")


def test_old_host_new_firmware_ignores_unknown_caps():
    # a NEWER board advertising caps this wheel does not know -> we simply use the shared ones; unknown
    # caps are inert (never crash on them). This is the old-wheel / new-board direction.
    fake = FakeBoard(proto=99, caps=["keyboard", "mouse", "connect", "some_future_thing"])
    with HidBridge("x", backend=fake) as b:
        with b.connect("laptop3") as s:
            s.type("hi")
    assert fake.drove == [("type", "hi")]


def test_require_capability_missing_raises_incompatible():
    from hidbridge import IncompatibleFirmware
    fake = FakeBoard(proto=1, caps=["keyboard", "mouse"])   # no 'layout'
    with HidBridge("x", backend=fake) as b:
        with pytest.raises(IncompatibleFirmware, match="lacks"):
            b.require_capability("layout", "per-slot keyboard layout")
        b.require_capability("keyboard")                    # a present cap does not raise


def test_require_capability_skips_boards_without_caps_list():
    # the dongle advertises no caps list -> require_capability must NOT gate it (its own Unsupported
    # errors cover it); gating here would wrongly reject the dongle.
    fake = FakeBoard()                                      # caps=None -> no 'caps' key
    with HidBridge("x", backend=fake) as b:
        b.require_capability("connect")                     # no raise


def test_firmware_method_not_found_maps_to_incompatible():
    from hidbridge import IncompatibleFirmware
    # a board that REPLIES 'method not found' (an older firmware missing a method) -> the guard turns
    # the raw RpcError into the typed IncompatibleFirmware, both on management and drive paths.
    fake = FakeBoard(proto=1, caps=["keyboard"], raise_on=("list_devices",))
    with HidBridge("x", backend=fake) as b:
        with pytest.raises(IncompatibleFirmware, match="upgrade the firmware"):
            b.list_devices()
    fake2 = FakeBoard(proto=1, caps=["keyboard"], raise_on=("type",))
    with HidBridge("x", backend=fake2) as b:
        # connect works; the drive verb hits the missing method -> IncompatibleFirmware
        sess = HidSession(b, fake2, "laptop3")
        with pytest.raises(IncompatibleFirmware):
            sess.type("hi")

"""Offline tests for the CLI backend wiring -- non-dongle backends now open a HidBridge.

Monkeypatches hidbridge.HidBridge so no hardware is touched: asserts _init_backend maps --backend/
--port/--host/--rpc to the right LOCATION + mode, routes report commands through the bridge's board
transport, and tears the bridge down. The dongle backend keeps the daemon-socket path (no bridge).
"""

import hidbridge
from hidbridge import cli


class _FakeBoard:
    def transport(self, name=None):
        return "BOARD_TRANSPORT"


class _FakeBridge:
    last = None

    def __init__(self, location, *, mode="raw", token=None, **opts):
        self.location = location
        self.mode = mode
        self._board = _FakeBoard()
        self.info = {"board": "esp32", "fw": "testfw", "nick": "espeth"}
        self.entered = self.exited = False
        _FakeBridge.last = self

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc):
        self.exited = True
        return False

    @property
    def capabilities(self):
        return {"max_sessions": 2, "caps": ["frames"]}


def _reset():
    cli._ALT_TRANSPORT = None
    cli._BRIDGE = None
    cli._BACKEND = "dongle"


def test_esp32_backend_opens_bridge_serial_location_raw(monkeypatch):
    _reset()
    monkeypatch.setattr(hidbridge, "HidBridge", _FakeBridge)
    args = cli.build_parser().parse_args(["--backend", "esp32", "--port", "/dev/ttyUSB3", "status"])
    cli._init_backend(args)
    b = _FakeBridge.last
    assert b.location == "/dev/ttyUSB3" and b.mode == "raw" and b.entered
    assert cli._ALT_TRANSPORT == "BOARD_TRANSPORT"
    cli._close_backend()
    assert b.exited and cli._BRIDGE is None
    _reset()


def test_net_backend_uses_host_location(monkeypatch):
    _reset()
    monkeypatch.setattr(hidbridge, "HidBridge", _FakeBridge)
    args = cli.build_parser().parse_args(["--backend", "net", "--host", "192.0.2.102", "status"])
    cli._init_backend(args)
    assert _FakeBridge.last.location == "192.0.2.102"
    cli._close_backend()
    _reset()


def test_rpc_flag_selects_rpc_mode(monkeypatch):
    _reset()
    monkeypatch.setattr(hidbridge, "HidBridge", _FakeBridge)
    args = cli.build_parser().parse_args(["--backend", "esp32", "--port", "/dev/ttyUSB3", "--rpc",
                                          "type", "hi"])
    cli._init_backend(args)
    assert _FakeBridge.last.mode == "rpc"
    cli._close_backend()
    _reset()


def test_dongle_backend_keeps_daemon_socket_path(monkeypatch):
    _reset()
    # if a bridge were wrongly opened this would blow up; assert it is NOT
    monkeypatch.setattr(hidbridge, "HidBridge",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no bridge for dongle")))
    args = cli.build_parser().parse_args(["--backend", "dongle", "status"])
    cli._init_backend(args)
    assert cli._ALT_TRANSPORT is None and cli._BRIDGE is None
    _reset()

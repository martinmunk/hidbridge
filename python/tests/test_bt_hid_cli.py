"""Offline tests for the CLI backend wiring -- non-dongle backends now open a HidBridge.

Monkeypatches hidbridge.HidBridge so no hardware is touched: asserts _init_backend maps --backend/
--port/--host to the right LOCATION, drives report commands through the board's HIGH-LEVEL verbs (the
BOARD builds the reports), and tears the bridge down. The dongle backend keeps the daemon-socket path
(no bridge).
"""

import hidbridge
from hidbridge import cli


class _FakeBoard:
    def __init__(self):
        self.drove = []

    def transport(self, name=None):
        return "BOARD_TRANSPORT"

    def type(self, text):
        self.drove.append(("type", text)); return {"ok": True}

    def key(self, name, mod=0):
        self.drove.append(("key", name, mod)); return {"ok": True}

    def move(self, dx, dy):
        self.drove.append(("move", dx, dy)); return {"ok": True}

    def click(self, button="left"):
        self.drove.append(("click", button)); return {"ok": True}

    def scroll(self, clicks):
        self.drove.append(("scroll", clicks)); return {"ok": True}


class _FakeBridge:
    last = None

    def __init__(self, location, *, token=None, **opts):
        self.location = location
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
    cli._BOARD = None
    cli._BRIDGE = None
    cli._BACKEND = "dongle"


def test_esp32_backend_opens_bridge_serial_location(monkeypatch):
    _reset()
    monkeypatch.setattr(hidbridge, "HidBridge", _FakeBridge)
    args = cli.build_parser().parse_args(["--backend", "esp32", "--port", "/dev/ttyUSB3", "status"])
    cli._init_backend(args)
    b = _FakeBridge.last
    assert b.location == "/dev/ttyUSB3" and b.entered
    assert cli._BOARD is b._board
    assert cli._ALT_TRANSPORT == "BOARD_TRANSPORT"     # the raw frame transport (radio ops)
    cli._close_backend()
    assert b.exited and cli._BRIDGE is None and cli._BOARD is None
    _reset()


def test_net_backend_uses_host_location(monkeypatch):
    _reset()
    monkeypatch.setattr(hidbridge, "HidBridge", _FakeBridge)
    args = cli.build_parser().parse_args(["--backend", "net", "--host", "192.0.2.102", "status"])
    cli._init_backend(args)
    assert _FakeBridge.last.location == "192.0.2.102"
    cli._close_backend()
    _reset()


def test_drive_commands_route_to_board_high_level_verbs(monkeypatch):
    # the CLI tells the BOARD what to do; the board builds the reports (no host-side frame building)
    _reset()
    monkeypatch.setattr(hidbridge, "HidBridge", _FakeBridge)
    args = cli.build_parser().parse_args(["--backend", "esp32", "--port", "/dev/ttyUSB3", "type", "hi"])
    cli._init_backend(args)
    board = cli._BOARD
    assert cli._alt_send({"cmd": "type", "text": "hi"}) == {"ok": True}
    cli._alt_send({"cmd": "key", "name": "enter", "modifiers": 8})
    cli._alt_send({"cmd": "move", "dx": 3, "dy": -4})
    cli._alt_send({"cmd": "click", "button": "back"})
    assert board.drove == [("type", "hi"), ("key", "enter", 8), ("move", 3, -4), ("click", "back")]
    cli._close_backend()
    _reset()


def test_unlock_types_passcode_then_enter_via_board(monkeypatch):
    # a passcode is just text: board builds it, then a separate Enter key -- no host-built frames
    _reset()
    monkeypatch.setattr(hidbridge, "HidBridge", _FakeBridge)
    args = cli.build_parser().parse_args(["--backend", "esp32", "--port", "/dev/ttyUSB3",
                                          "unlock", "1234"])
    cli._init_backend(args)
    board = cli._BOARD
    cli.cmd_unlock(args)
    assert board.drove == [("type", "1234"), ("key", "enter", 0)]   # never as pre-built frames
    cli._close_backend()
    _reset()


def test_dongle_backend_keeps_daemon_socket_path(monkeypatch):
    _reset()
    # if a bridge were wrongly opened this would blow up; assert it is NOT
    monkeypatch.setattr(hidbridge, "HidBridge",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no bridge for dongle")))
    args = cli.build_parser().parse_args(["--backend", "dongle", "status"])
    cli._init_backend(args)
    assert cli._ALT_TRANSPORT is None and cli._BRIDGE is None and cli._BOARD is None
    _reset()

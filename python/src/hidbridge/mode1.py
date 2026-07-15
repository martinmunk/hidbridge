"""Mode 1 client -- drive a hidbridge board by telling it WHAT to do (JSON-RPC 2.0 over JSON-lines).

Mode 1 is the "smart firmware" path: the board owns the HID report descriptor + report building, and
the host sends high-level INTENT -- `{"jsonrpc":"2.0","id":1,"method":"type","params":{"text":"hi"}}`
-- rather than pre-built report bytes. Contrast Mode 2 (`controller.py` + the frame transports), where
the host builds the exact report bytes and the board is "just a radio".

Both modes talk to the SAME firmware over the same channel (serial or TCP); the board routes a line to
Mode 1 iff it is a JSON object (starts with `{`), else to the Mode 2 single-char frame handler. So a
network client authenticates once with the Mode 2 `@<token>` line, then sends JSON-RPC.

Wire protocol (see firmware/esp32/src/main.cpp handleJsonRpc):
  request : {"jsonrpc":"2.0","id":<n>,"method":<m>,"params":{...}}
  response: {"jsonrpc":"2.0","id":<n>,"result":{...}} | {..,"error":{"code":..,"message":..}}
  a params `"dry":true` returns result.frames (the built report bytes as hex) WITHOUT notifying.

Stdlib only (json + the injected channel), so this imports and unit-tests fully offline.
"""

from __future__ import annotations

import json

DEFAULT_BAUD = 115200


class Mode1Error(RuntimeError):
    """A Mode 1 (JSON-RPC) call failed: transport error, or a JSON-RPC error object was returned."""


class Mode1Client:
    """Send JSON-RPC requests to a board over a duck-typed channel (write(bytes)/readline()->bytes).

    The channel is a `serial_link` port, a `socket_link.SocketLine`, or a fake in tests.
    """

    def __init__(self, channel, max_reads: int = 12):
        self._ch = channel
        self._id = 0
        self._max_reads = max_reads

    # -- JSON-RPC core --
    def call(self, method: str, params: dict | None = None, *, dry: bool = False) -> dict:
        """Send one request, return its `result` dict (raises Mode1Error on a JSON-RPC error)."""
        self._id += 1
        p = dict(params or {})
        if dry:
            p["dry"] = True
        req = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": p}
        self._ch.write((json.dumps(req, separators=(",", ":")) + "\n").encode("ascii"))
        want = self._id
        for _ in range(self._max_reads):
            raw = self._ch.readline()
            if not raw:
                raise Mode1Error(f"no response to {method!r} (timeout)")
            line = raw.decode("ascii", "replace").strip()
            if not line.startswith("{"):
                continue                      # a Mode-2 banner / boot line -- skip
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") is None:
                continue                      # a notification (async event), not a reply
            if msg.get("id") != want:
                continue                      # a different (pipelined) reply -- skip
            err = msg.get("error")
            if err:
                raise Mode1Error(f"{method}: {err.get('message')} (code {err.get('code')})")
            return msg.get("result", {}) or {}
        raise Mode1Error(f"no matching JSON-RPC response to {method!r}")

    # -- capabilities handshake --
    def hello(self) -> dict:
        """Discover the board: {proto, device, name, modes, caps}. Allowed unauthenticated."""
        return self.call("hello")

    def supports(self, cap: str) -> bool:
        try:
            return cap in (self.hello().get("caps") or [])
        except Mode1Error:
            return False

    # -- authentication (network only; serial is trusted). Uses the Mode 2 `@<token>` line. --
    def authenticate(self, token: str) -> None:
        self._ch.write(("@" + token + "\n").encode("ascii"))
        for _ in range(self._max_reads):
            raw = self._ch.readline()
            if not raw:
                raise Mode1Error("no response to authenticate (timeout)")
            line = raw.decode("ascii", "replace").strip()
            if line.startswith("OK auth"):
                return
            if line.startswith("ERR"):
                raise Mode1Error("network auth rejected (bad or unprovisioned token)")
        raise Mode1Error("no auth acknowledgement")

    # -- high-level actions ("tell it WHAT") --
    def type_text(self, text: str, *, dry: bool = False) -> dict:
        return self.call("type", {"text": text}, dry=dry)

    def key(self, name: str, mod: int = 0, *, dry: bool = False) -> dict:
        return self.call("key", {"name": name, "mod": int(mod)}, dry=dry)

    def wake(self, *, dry: bool = False) -> dict:
        return self.call("wake", {}, dry=dry)

    def move(self, dx: int, dy: int, *, dry: bool = False) -> dict:
        return self.call("move", {"dx": int(dx), "dy": int(dy)}, dry=dry)

    def click(self, button: str = "left", *, dry: bool = False) -> dict:
        return self.call("click", {"button": button}, dry=dry)

    def scroll(self, clicks: int, *, dry: bool = False) -> dict:
        return self.call("scroll", {"clicks": int(clicks)}, dry=dry)

    def move_abs(self, x: float, y: float, *, dry: bool = False) -> dict:
        return self.call("moveabs", {"x": float(x), "y": float(y)}, dry=dry)

    def tap(self, x: float, y: float, button: str = "left", *, dry: bool = False) -> dict:
        return self.call("tap", {"x": float(x), "y": float(y), "button": button}, dry=dry)

    def touch(self, x: float, y: float, *, dry: bool = False) -> dict:
        return self.call("touch", {"x": float(x), "y": float(y)}, dry=dry)

    def close(self) -> None:
        if hasattr(self._ch, "close"):
            self._ch.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _split_target(target: str | None) -> tuple[str, str | None]:
    """'esp32:/dev/ttyUSB3' -> ('esp32','/dev/ttyUSB3'); 'net:host:3333' -> ('net','host:3333');
    a bare device path or None -> ('serial', <path or None>)."""
    if not target:
        return "serial", None
    if ":" in target:
        backend, _, arg = target.partition(":")
        b = backend.lower()
        if b in ("net", "esp32", "pico", "serial", "dongle"):
            return b, (arg or None)
    return "serial", target


def open_client(target: str | None = None, *, token: str | None = None,
                timeout: float = 3.0) -> Mode1Client:
    """Open a Mode1Client to a board.

    target: 'esp32:/dev/ttyUSB3' / 'pico:/dev/ttyACM0' / a bare serial path / None (autodetect the
    ESP32); or 'net:<host>[:port]' for TCP. A network client authenticates with the token
    (arg/BT_HID_NET_TOKEN/~/.config/bt_hid/net.token) before returning.
    """
    backend, arg = _split_target(target)
    if backend == "net":
        from . import socket_link
        from . import transport_net as TN
        host, port = TN._split_host_port(arg)
        cli = Mode1Client(socket_link.open_socket(host, port, timeout))
        tok = TN.load_token(token)
        if tok:
            cli.authenticate(tok)
        return cli
    from . import serial_link
    from .transport_esp32 import detect_port
    dev = arg or detect_port()
    if not dev:
        raise Mode1Error("no serial device; pass esp32:/dev/ttyUSBx or set BT_HID_ESP32_PORT")
    return Mode1Client(serial_link.open_serial(dev, DEFAULT_BAUD, timeout, prefer_raw=True))

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
import warnings
from dataclasses import dataclass, field

DEFAULT_BAUD = 115200

# The protocol version THIS client knows. The firmware bakes in one version and reports it (+ a
# capability list) in `hello`; the host is the flexible party. We DON'T branch on the version number
# (a board may ship a subset of features) -- we feature-detect on `caps`. The version is used only to
# WARN when a board is newer than we understand, so we never crash on an unknown version -- we just
# use the capabilities we both share. (Robustness principle: be liberal in what you accept.)
KNOWN_PROTO = 1


class Mode1Error(RuntimeError):
    """A Mode 1 (JSON-RPC) call failed: transport error, or a JSON-RPC error object was returned."""


@dataclass
class ServerInfo:
    """The board's `hello` handshake, cached: version + capabilities the host adapts to."""

    proto: int
    caps: frozenset = field(default_factory=frozenset)
    device: str = ""
    name: str = ""
    nick: str = ""
    modes: tuple = ()
    raw: dict = field(default_factory=dict)


class Mode1Client:
    """Send JSON-RPC requests to a board over a duck-typed channel (write(bytes)/readline()->bytes).

    The channel is a `serial_link` port, a `socket_link.SocketLine`, or a fake in tests.
    """

    def __init__(self, channel, max_reads: int = 12):
        self._ch = channel
        self._id = 0
        self._max_reads = max_reads
        self._info: ServerInfo | None = None

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

    # -- capabilities handshake + version handling --
    def hello(self) -> dict:
        """Discover the board: {proto, device, name, nick, modes, caps}. Caches it as `.info`.

        If the board's protocol is NEWER than this client knows, warn (don't fail) -- we proceed on
        the capabilities we both share. Allowed unauthenticated."""
        r = self.call("hello")
        self._info = ServerInfo(
            proto=int(r.get("proto", 0)), caps=frozenset(r.get("caps") or []),
            device=r.get("device", ""), name=r.get("name", ""), nick=r.get("nick", ""),
            modes=tuple(r.get("modes") or ()), raw=r)
        if self._info.proto > KNOWN_PROTO:
            warnings.warn(
                f"board speaks protocol {self._info.proto}; this client knows {KNOWN_PROTO} -- using "
                f"the capabilities both share ({sorted(self._info.caps)})", stacklevel=2)
        return r

    def info(self) -> ServerInfo:
        """The cached `hello` handshake (fetched once on first use)."""
        if self._info is None:
            self.hello()
        return self._info

    def supports(self, cap: str) -> bool:
        try:
            return cap in self.info().caps
        except Mode1Error:
            return False

    def require_cap(self, cap: str) -> None:
        """Raise a clear error if the board lacks capability `cap`, rather than sending a command it
        would reject cryptically."""
        info = self.info()
        if cap not in info.caps:
            raise Mode1Error(
                f"board {info.device or '?'} (proto {info.proto}) lacks capability {cap!r}; "
                f"it advertises {sorted(info.caps)}")

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

    def frames(self, frames, *, dry: bool = False) -> dict:
        """Mode 2 semantics via JSON-RPC: relay PRE-BUILT report frames ('<ridHex><bodyHex>' strings,
        the shape a dry run returns) -- the board just notifies them."""
        self.require_cap("frames")
        return self.call("frames", {"frames": list(frames)}, dry=dry)

    # -- Mode 1 connection management BY CLIENT NAME (firmware-specific; see caps 'connect'/'nickname').
    #    Each capability-gates so an older/minimal board gives a clear error instead of a cryptic one. --
    def nickname(self, name: str | None = None) -> dict:
        """Get (no arg) or set the board's nickname. The advertised name is HIDBRIDGE-<nick>."""
        self.require_cap("nickname")
        return self.call("nickname", {"name": name} if name else {})

    def identities(self) -> dict:
        """List the board's identity slots: {slots:[{slot,client,addr,name,conn,sub}], adv, tgt}."""
        self.require_cap("connect")
        return self.call("identities")

    def pair(self, client: str) -> dict:
        """Assign a free identity slot to <client> + advertise it so that iPad can bond."""
        self.require_cap("connect")
        return self.call("pair", {"client": client})

    def connect(self, client: str) -> dict:
        """Bring <client>'s identity up (advertise it so the bonded iPad reconnects) + target it."""
        self.require_cap("connect")
        return self.call("connect", {"client": client})

    def disconnect(self, client: str | None = None) -> dict:
        """Disconnect <client> (or the current target) -- drop the central, keep the bond, park."""
        self.require_cap("connect")
        return self.call("disconnect", {"client": client} if client else {})

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
        tok = TN.load_token(token)
        if not tok:
            raise Mode1Error(
                "the network backend is TLS-encrypted and needs the board token (the TLS pre-shared "
                "key); provision it with `hidbridge net-provision`, then set BT_HID_NET_TOKEN or "
                "~/.config/bt_hid/net.token")
        # token = the TLS-PSK: the handshake authenticates. authenticate() re-sends @token over the
        # now-encrypted channel for the board's existing gate (redundant but harmless).
        cli = Mode1Client(socket_link.open_socket(host, port, timeout, token=tok))
        cli.authenticate(tok)
        return cli
    from . import serial_link
    from .transport_esp32 import detect_port
    dev = arg or detect_port()
    if not dev:
        raise Mode1Error("no serial device; pass esp32:/dev/ttyUSBx or set BT_HID_ESP32_PORT")
    return Mode1Client(serial_link.open_serial(dev, DEFAULT_BAUD, timeout, prefer_raw=True))

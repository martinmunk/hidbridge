"""RPC client -- drive a hidbridge board by telling it WHAT to do (JSON-RPC 2.0 over JSON-lines).

JSON-RPC is the ONLY report path on the MCU wire: the board owns the HID report descriptor + report
building, and the host sends high-level INTENT --
`{"jsonrpc":"2.0","id":1,"method":"type","params":{"text":"hi"}}` -- rather than pre-built report
bytes. (The old "raw" K/M frame relay, where the host built the exact bytes and the board was "just a
radio", was removed from the firmware; the dongle backend still host-builds via reports.py, but over
its own daemon socket, not this MCU channel.)

Everything on the MCU wire is JSON-RPC (proto>=2): requests, replies, JSON-RPC error responses, and
async link/readiness state as id-less `event` NOTIFICATIONS. A network client is authenticated by the
TLS-PSK handshake itself (the token IS the pre-shared key), so it sends JSON-RPC immediately -- there
is no app-layer auth line. (PROTO-1 BACK-COMPAT: an older board emits plain-text `EVT ...`/`ERR ...`
frames and expects the redundant `@<token>` line; the client tolerates both, gated on `hello.proto`.)

This module is also the board driver's device-management channel: `identities`/`pair`/`unpair`/
`connect`/`disconnect`/`nickname`/`layout` resolve a device by NAME (client label) to a firmware slot,
so `hidbridge.board.McuBoard` uses it regardless of the drive path.

Wire protocol (see firmware/shared/hidbridge_rpc.h handleJsonRpc):
  request : {"jsonrpc":"2.0","id":<n>,"method":<m>,"params":{...}}
  response: {"jsonrpc":"2.0","id":<n>,"result":{...}} | {..,"error":{"code":..,"message":..}}
  a params `"dry":true` returns result.frames (the built report bytes as hex) WITHOUT notifying.

Stdlib only (json + the injected channel), so this imports and unit-tests fully offline.
"""

from __future__ import annotations

import json
import logging
import warnings
from dataclasses import dataclass, field

DEFAULT_BAUD = 115200

# Structured logs the board streams as id-less JSON-RPC `log` notifications are routed to THIS logger
# (a child of the package logger), at the level named in the notification. The application configures
# handlers/levels on "hidbridge.mcu" (or a parent) as it wishes -- by default Python's last-resort
# handler shows WARNING+ only, so info/debug board logs stay quiet until the app opts in.
_MCU_LOG = logging.getLogger("hidbridge.mcu")
_LOG_LEVELS = {"error": logging.ERROR, "warn": logging.WARNING,
               "info": logging.INFO, "debug": logging.DEBUG}

# The protocol version THIS client knows. The firmware bakes in one version and reports it (+ a
# capability list) in `hello`; the host is the flexible party. We DON'T branch on the version number
# (a board may ship a subset of features) -- we feature-detect on `caps`. The version is used only to
# WARN when a board is newer than we understand, so we never crash on an unknown version -- we just
# use the capabilities we both share. (Robustness principle: be liberal in what you accept.)
KNOWN_PROTO = 2


class RpcError(RuntimeError):
    """An RPC (JSON-RPC) call failed: transport error, or a JSON-RPC error object was returned."""


class RpcTimeout(RpcError):
    """No bytes at all came back within the read budget -- the board is unreachable / asleep / not a
    board (as opposed to a board that answered but not with JSON-RPC)."""


class RpcProtocolError(RpcError):
    """The board ANSWERED, but never with a matching JSON-RPC reply -- e.g. legacy/pre-JSON-RPC
    firmware that replies `ERR unknowncmd` to a JSON request. Distinct from RpcTimeout (silence) so a
    caller can tell 'wrong/old firmware' from 'nothing there'."""


@dataclass
class ServerInfo:
    """The board's `hello` handshake, cached: version + capabilities the host adapts to."""

    proto: int
    caps: frozenset = field(default_factory=frozenset)
    device: str = ""
    name: str = ""
    nick: str = ""
    fw: str = ""
    modes: tuple = ()
    raw: dict = field(default_factory=dict)


class RpcClient:
    """Send JSON-RPC requests to a board over a duck-typed channel (write(bytes)/readline()->bytes).

    The channel is a `serial_link` port, a `socket_link.SocketLine`, or a fake in tests.
    """

    def __init__(self, channel, max_reads: int = 12):
        self._ch = channel
        self._id = 0
        self._max_reads = max_reads
        self._info: ServerInfo | None = None
        self.last_event: dict | None = None   # latest async `event` notification's params (proto>=2)
        self.last_log: dict | None = None     # latest async `log` notification's params (the "logs" cap)

    # -- JSON-RPC core --
    def call(self, method: str, params: dict | None = None, *, dry: bool = False) -> dict:
        """Send one request, return its `result` dict (raises RpcError on a JSON-RPC error)."""
        self._id += 1
        p = dict(params or {})
        if dry:
            p["dry"] = True
        req = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": p}
        self._ch.write((json.dumps(req, separators=(",", ":")) + "\n").encode("ascii"))
        want = self._id
        # Demux robustly. During the post-connect window the board emits a burst of async events
        # (id-less JSON-RPC `event` notifications on proto>=2 firmware; unsolicited plain-text
        # `EVT ...` lines on proto-1 firmware) and can briefly stall (an empty read) BEFORE the real
        # reply, so we must not let any of that starve it:
        #   * async notifications, mismatched-id (pipelined) replies, and noise lines are SKIPPED and
        #     do NOT consume the response budget the way a fixed loop count did;
        #   * a TRANSIENT stall (a bounded run of empty reads -- each is one full channel-timeout
        #     window with no complete line) is tolerated, not treated as the end, so a reply that
        #     lands just after the stall is still read;
        #   * total work is capped (an empty run AND an overall line count) so a stuck/flooding board
        #     can't spin forever.
        # The RpcTimeout(silence)/RpcProtocolError(answered-wrong) distinction is preserved: `noise`
        # is set only by a non-JSON-RPC line, so pure silence still raises RpcTimeout and a legacy
        # `ERR unknowncmd` still raises RpcProtocolError (board._probe/_probe_port rely on this).
        noise = None
        empty_run = 0
        # Consecutive empty reads tolerated: a POST-CONNECT stall is brief (one, maybe two,
        # channel-timeout windows), so a small run keeps latency for a genuinely silent board low
        # (silence still terminates in a handful of windows -> RpcTimeout fast).
        max_empty = 3
        max_total = max(self._max_reads * 8, 128)  # overall line cap: a notification flood can't loop forever
        for _ in range(max_total):
            raw = self._ch.readline()
            if not raw:
                empty_run += 1
                if empty_run > max_empty:
                    break                     # a real stall / silence: stop reading
                continue
            empty_run = 0                     # data flowed -- the stall (if any) is over
            line = raw.decode("ascii", "replace").strip()
            if not line.startswith("{"):
                # PROTO-1 BACK-COMPAT SHIM [remove when MIN_FIRMWARE_PROTO >= 2]: proto-1 firmware
                # broadcasts async events as plain-text `EVT ...` lines (ready/connected/disconnected/
                # unsubscribed/net ...). Skip them -- neither a reply NOR legacy noise (recording one as
                # noise wrongly turned a post-connect stall into RpcProtocolError). Any OTHER non-JSON
                # line (a boot banner, a legacy `ERR unknowncmd`) is still noise -> RpcProtocolError.
                if line and not line.startswith("EVT "):
                    noise = line
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                noise = line
                continue
            if msg.get("id") is None:
                # An id-less JSON-RPC notification (async event OR log), not a reply. On proto>=2 the
                # firmware sends {"method":"event","params":{...}} for link/readiness state and
                # {"method":"log","params":{"level":..,"msg":..,"slot":N?}} for diagnostics -- surface
                # both, then keep waiting for the pending reply. NOTE: notifications are drained only
                # while call() reads the channel (no background reader), so a board log surfaces on the
                # NEXT call -- fine for now.
                notif = msg.get("method")
                if notif == "event":
                    self.last_event = msg.get("params") or {}
                elif notif == "log":
                    self._route_log(msg.get("params") or {})
                continue
            if msg.get("id") != want:
                continue                      # a different (pipelined) reply -- skip
            err = msg.get("error")
            if err:
                raise RpcError(f"{method}: {err.get('message')} (code {err.get('code')})")
            return msg.get("result", {}) or {}
        if noise is not None:
            raise RpcProtocolError(
                f"no JSON-RPC response to {method!r}; board replied {noise!r} (legacy/incompatible "
                f"firmware?)")
        raise RpcTimeout(f"no response to {method!r} (timeout)")

    def _route_log(self, params: dict) -> None:
        """Route a firmware `log` notification to the `hidbridge.mcu` logger at its mapped level.

        error->ERROR, warn->WARNING, info->INFO, debug->DEBUG. Includes the slot when the board tagged
        one. Also cached as `last_log` for a caller that wants the raw record."""
        self.last_log = params
        level = _LOG_LEVELS.get(str(params.get("level", "info")).lower(), logging.INFO)
        msg = params.get("msg", "")
        slot = params.get("slot")
        if slot is not None:
            _MCU_LOG.log(level, "[slot %s] %s", slot, msg)
        else:
            _MCU_LOG.log(level, "%s", msg)

    # -- capabilities handshake + version handling --
    def hello(self) -> dict:
        """Discover the board: {proto, device, name, nick, fw, modes, caps}. Caches it as `.info`.

        If the board's protocol is NEWER than this client knows, warn (don't fail) -- we proceed on
        the capabilities we both share. Allowed unauthenticated."""
        r = self.call("hello")
        self._info = ServerInfo(
            proto=int(r.get("proto", 0)), caps=frozenset(r.get("caps") or []),
            device=r.get("device", ""), name=r.get("name", ""), nick=r.get("nick", ""),
            fw=r.get("fw", ""), modes=tuple(r.get("modes") or ()), raw=r)
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
        except RpcError:
            return False

    def require_cap(self, cap: str) -> None:
        """Raise a clear error if the board lacks capability `cap`, rather than sending a command it
        would reject cryptically."""
        info = self.info()
        if cap not in info.caps:
            raise RpcError(
                f"board {info.device or '?'} (proto {info.proto}) lacks capability {cap!r}; "
                f"it advertises {sorted(info.caps)}")

    # -- authentication (network only; serial is trusted) --
    def authenticate(self, token: str) -> None:
        """Network app-layer auth, PROTO-GATED on the board's `hello`.

        proto>=2: a no-op -- the TLS-PSK handshake (the token IS the pre-shared key) already
        authenticated the socket, so no @token line goes on the wire (TLS-PSK is the sole net auth).

        proto==1: PROTO-1 BACK-COMPAT SHIM [remove when MIN_FIRMWARE_PROTO >= 2] -- the old firmware's
        auth gate still expects the raw `@<token>` line and answers `OK auth`/`ERR auth`.
        """
        if self.info().proto >= 2:
            return
        # -- proto-1 shim --
        self._ch.write(("@" + token + "\n").encode("ascii"))
        for _ in range(self._max_reads):
            raw = self._ch.readline()
            if not raw:
                raise RpcError("no response to authenticate (timeout)")
            line = raw.decode("ascii", "replace").strip()
            if line.startswith("OK auth"):
                return
            if line.startswith("ERR"):
                raise RpcError("network auth rejected (bad or unprovisioned token)")
        raise RpcError("no auth acknowledgement")

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

    def press(self, button: str = "left", *, dry: bool = False) -> dict:
        """Hold a mouse button DOWN (for click-and-drag). Subsequent move/scroll reports carry it
        until release()."""
        return self.call("press", {"button": button}, dry=dry)

    def release(self, button: str = "left", *, dry: bool = False) -> dict:
        """Release a button held by press()."""
        return self.call("release", {"button": button}, dry=dry)

    def scroll(self, clicks: int, *, dry: bool = False) -> dict:
        return self.call("scroll", {"clicks": int(clicks)}, dry=dry)

    def frames(self, frames, *, dry: bool = False) -> dict:
        """Raw semantics via JSON-RPC: relay PRE-BUILT report frames ('<ridHex><bodyHex>' strings,
        the shape a dry run returns) -- the board just notifies them."""
        self.require_cap("frames")
        return self.call("frames", {"frames": list(frames)}, dry=dry)

    # -- connection management BY CLIENT NAME (firmware-specific; see caps 'connect'/'nickname').
    #    Each capability-gates so an older/minimal board gives a clear error instead of a cryptic one. --
    def nickname(self, name: str | None = None) -> dict:
        """Get (no arg) or set the board's nickname. The advertised name is HIDBRIDGE-<nick>."""
        self.require_cap("nickname")
        return self.call("nickname", {"name": name} if name else {})

    def identities(self) -> dict:
        """List the board's identity slots: {slots:[{slot,client,addr,name,conn,sub,layout}], adv, tgt}."""
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

    def unpair(self, client: str) -> dict:
        """Forget <client>: drop the link, DELETE the BLE bond, clear the slot label (persisted).

        Unlike disconnect (keeps the bond so the iPad silently reconnects), unpair frees the slot --
        the device must pair again."""
        self.require_cap("connect")
        return self.call("unpair", {"client": client})

    def target(self, slot: int) -> dict:
        """Claim slot `slot` as THIS session's send-target WITHOUT (re)advertising it (the old `T<n>`).

        Unlike connect (which brings the identity up + advertises), target assumes an already-live
        central and just routes this session's subsequent reports to it."""
        self.require_cap("target")
        return self.call("target", {"slot": int(slot)})

    def ping(self) -> dict:
        """Liveness + readiness of the CALLING session's target slot: {device, subscribed, conn}.

        The single-char `P` folded into JSON-RPC. `subscribed` is the iOS-enabled-the-reports
        readiness signal; `conn` is link-up. Allowed unauthenticated (a bare liveness probe)."""
        return self.call("ping")

    def advertise(self) -> dict:
        """(Re)start advertising the current identity slot (the old `A`)."""
        self.require_cap("advertise")
        return self.call("advertise")

    def park(self) -> dict:
        """Stop advertising but KEEP existing connections + bonds (the old `O`).

        Lets another board serve an iPad without this one grabbing it. Resume with connect()."""
        self.require_cap("park")
        return self.call("park")

    def net_status(self) -> dict:
        """The board's Ethernet/TCP state (the old `N`): {eth, ip, port, mdns, token, clients}.

        Answered on a serial-only build too (eth=false), so a host can always ask 'do you have a
        network path?'. `token` is whether a secret is PROVISIONED, never the secret itself."""
        self.require_cap("net_status")
        return self.call("net_status")

    def provision(self, token: str) -> dict:
        """Set the board's network auth token (the old `W<token>`) -- SERIAL ONLY.

        SECURITY: `token` is the shared secret + TLS pre-shared key. The firmware REFUSES this method
        over the TCP/TLS net channel (it is honored only on the physically-attached serial channel), so
        the secret can only be set over the cable. Never log the token or this call's args; the board
        never echoes it back."""
        self.require_cap("provision")
        return self.call("provision", {"token": token})

    def set_log_level(self, level: str) -> dict:
        """Set the board's runtime log filter ('error'|'warn'|'info'|'debug') -- returns {level:...}.

        Capability-gated on 'logs': error/warn/info emit by default, debug is suppressed until raised.
        Board `log` notifications route to the `hidbridge.mcu` logger (see _route_log)."""
        self.require_cap("logs")
        return self.call("set_log_level", {"level": level})

    def layout(self, name: str | None = None, *, slot: int | None = None,
               client: str | None = None) -> dict:
        """Get or set a device slot's keyboard layout (PER SLOT: each bonded device remembers its
        own). With no `name`, returns the current layout; with `name` ('us'/'da'), sets + persists it.

        Address the slot like connect/unpair: an explicit `slot` index or a `client` name; with
        neither, the CURRENT TARGET slot. The active layout for `type`/`key` is always the target
        slot's. Persisted in NVS, changeable on the fly (not only at pair)."""
        self.require_cap("layout")
        p: dict = {}
        if name is not None:
            p["name"] = name
        if slot is not None:
            p["slot"] = int(slot)
        if client is not None:
            p["client"] = client
        return self.call("layout", p)

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
                timeout: float = 3.0) -> RpcClient:
    """Open an RpcClient to a board.

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
            raise RpcError(
                "the network backend is TLS-encrypted and needs the board token (the TLS pre-shared "
                "key); provision it with `hidbridge net-provision`, then set BT_HID_NET_TOKEN or "
                "~/.config/bt_hid/net.token")
        # token = the TLS-PSK: the handshake authenticates the socket. authenticate() is a no-op on
        # proto>=2 boards (nothing on the wire) and the proto-1 @token shim on older ones.
        cli = RpcClient(socket_link.open_socket(host, port, timeout, token=tok))
        cli.authenticate(tok)
        return cli
    from . import serial_link
    from .transport_esp32 import detect_port
    dev = arg or detect_port()
    if not dev:
        raise RpcError("no serial device; pass esp32:/dev/ttyUSBx or set BT_HID_ESP32_PORT")
    return RpcClient(serial_link.open_serial(dev, DEFAULT_BAUD, timeout, prefer_raw=True))

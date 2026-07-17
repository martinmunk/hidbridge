"""bt_hid controller -- the API-first, importable HID INPUT GENERATOR.

Two roles:
  1. ops helper: wake / unlock / type / move / click / scroll an iPad-iPhone over the dongle.
  2. a scriptable, deterministic input generator to DEVELOP + TEST HID delivery -- e.g. a
     device-gated closed-loop e2e that injects known key/pointer events with THIS API and asserts
     they arrive decoded on the target. This is standard USB-HID.

Design: this layer builds deterministic report FRAMES from the pure `reports` module and hands them
to a pluggable `transport`. That keeps it dependency-free and unit-testable (a RecordingTransport
asserts exact frames with no device). The default `SocketTransport` talks to the running `ble_hid`
daemon over its Unix control socket; the CLI is a thin wrapper over this class.

A frame is a `(kind, body_bytes)` tuple, kind in {"keyboard", "mouse"}.

SECURITY: `unlock()` expands the passcode to frames and keeps NO copy; nothing here logs frames or
the passcode.
"""

from __future__ import annotations

import importlib
import json
import os
import socket
import time
from typing import Callable

from . import reports as R

CONTROL_SOCK = os.environ.get("BT_HID_SOCK") or os.path.join(
    os.environ.get("XDG_RUNTIME_DIR", "/run"), "bt_hid.sock")


def _frames(reports_list) -> list[tuple[str, bytes]]:
    return [(r.kind, r.data) for r in reports_list]


# --------------------------------------------------------------------------- #
# Transports
# --------------------------------------------------------------------------- #
class RecordingTransport:
    """Test transport: records every frame instead of sending it to a device."""

    def __init__(self):
        self.frames: list[tuple[str, bytes]] = []

    def send(self, frames: list[tuple[str, bytes]]) -> dict:
        self.frames.extend(frames)
        return {"ok": True}


class SocketTransport:
    """Sends frames to the running ble_hid daemon over its Unix control socket."""

    def __init__(self, path: str = CONTROL_SOCK, timeout: float = 5.0):
        self.path = path
        self.timeout = timeout

    def request(self, payload: dict) -> dict:
        """Send one JSON control request to the daemon and return its JSON reply."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self.path)
            s.sendall(json.dumps(payload).encode())
            data = s.recv(65536)
        finally:
            s.close()
        try:
            return json.loads(data.decode())
        except ValueError:
            return {"ok": False, "error": "bad daemon response"}

    def send(self, frames: list[tuple[str, bytes]]) -> dict:
        return self.request({"cmd": "reports",
                             "frames": [{"kind": k, "hex": b.hex()} for (k, b) in frames]})

    def status(self) -> dict:
        """Ask the daemon which HID report characteristics iOS has subscribed to (StartNotify).

        Reply shape: {"ok", "subscribed": <keyboard char notifying>, "any_subscribed",
        "chars": {kind: bool}}. Used by the dongle readiness gate (HidSession)."""
        return self.request({"cmd": "status"})


# --------------------------------------------------------------------------- #
# Backend selection (pluggable HID transports)
# --------------------------------------------------------------------------- #
# The default backend is the BlueZ USB dongle (the running ble_hid daemon over its Unix socket).
# Alternative backends live in `bt_hid.transport_<name>` modules, each exposing a
# `make_transport(**opts)` factory returning a transport with a `.send(frames)` method (e.g.
# transport_esp32 = an ESP32 BLE-HID board over USB serial). This loader is backend-AGNOSTIC: adding
# a backend is just dropping a new transport_<name>.py file -- no edit here -- so independent
# backends merge cleanly.
_BUILTIN_BACKENDS = {"dongle", "daemon", "ble", "socket"}


def make_transport(backend: str = "dongle", **opts):
    """Return a transport for `backend`.

    'dongle'/'daemon'/'ble'/'socket' -> the ble_hid daemon SocketTransport (accepts path=, timeout=).
    'recording'                       -> an in-memory RecordingTransport (tests).
    anything else                     -> bt_hid.transport_<backend>.make_transport(**opts).
    """
    name = (backend or "dongle").lower()
    if name in _BUILTIN_BACKENDS:
        kw = {k: opts[k] for k in ("path", "timeout") if k in opts}
        return SocketTransport(**kw)
    if name == "recording":
        return RecordingTransport()
    try:
        mod = importlib.import_module(f"{__package__}.transport_{name}")
    except ImportError as e:
        raise ValueError(f"unknown HID backend {backend!r} "
                         f"(no bt_hid.transport_{name} module)") from e
    return mod.make_transport(**opts)


# --------------------------------------------------------------------------- #
# Controller
# --------------------------------------------------------------------------- #
class HidController:
    """Scriptable keyboard+mouse injector. Pass a transport (default = the daemon socket)."""

    def __init__(self, transport=None):
        self.transport = transport if transport is not None else SocketTransport()

    # ---- frame builders (pure; handy for capture cross-checks / e2e assertions) ----
    def build_type_text(self, text: str):
        return R.type_text(text)

    def build_key(self, name: str, modifiers: int = 0):
        return R.key_reports(name, modifiers)

    # ---- actions ----
    def _send(self, reports_list) -> dict:
        return self.transport.send(_frames(reports_list))

    def type_text(self, text: str) -> dict:
        return self._send(R.type_text(text))

    def send_key(self, name: str, modifiers: int = 0) -> dict:
        return self._send(R.key_reports(name, modifiers))

    def wake(self) -> dict:
        # a benign keystroke that wakes the device without typing a lock-screen character
        return self._send(R.key_reports("space"))

    def unlock(self, passcode: str, press_enter: bool = True) -> dict:
        # expand + send; retain nothing
        return self._send(R.passcode_reports(passcode, press_enter=press_enter))

    def move(self, dx: int, dy: int) -> dict:
        return self._send(R.mouse_move_reports(dx, dy))

    def click(self, button: str = "left") -> dict:
        return self._send(R.mouse_click_reports(button))

    def scroll(self, clicks: int) -> dict:
        return self._send(R.mouse_scroll_reports(clicks))

    # ---- scripted timeline ----
    def play(self, events: list[dict], sleep: Callable[[float], None] = time.sleep) -> None:
        """Play a timeline of events. Each event: {"after": seconds, "action": <name>, **kwargs}.

        actions: type(text=), key(name=, modifiers=), move(dx=, dy=), click(button=),
                 scroll(clicks=), wake(), unlock(passcode=, press_enter=), delay (no-op).
        `sleep` is injectable so tests run instantly + assert timing.
        """
        for ev in events:
            delay = float(ev.get("after", 0.0))
            if delay:
                sleep(delay)
            elif "after" in ev:
                sleep(delay)  # honour an explicit after:0 (tests assert it)
            action = ev.get("action", "delay")
            if action == "delay":
                continue
            if action == "type":
                self.type_text(ev["text"])
            elif action == "key":
                self.send_key(ev["name"], int(ev.get("modifiers", 0)))
            elif action == "move":
                self.move(int(ev["dx"]), int(ev["dy"]))
            elif action == "click":
                self.click(ev.get("button", "left"))
            elif action == "scroll":
                self.scroll(int(ev["clicks"]))
            elif action == "wake":
                self.wake()
            elif action == "unlock":
                self.unlock(ev["passcode"], press_enter=ev.get("press_enter", True))
            else:
                raise ValueError(f"unknown timeline action {action!r}")

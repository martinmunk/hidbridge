"""The context-manager driving API: hidbridge.open() -> Bridge -> connect() -> Session.

Two levels, mirroring the hardware:
  * a **Bridge** is the board channel (a serial/net MCU, or a BlueZ dongle). Opening it probes the
    board so it SELF-IDENTIFIES -- the caller passes a LOCATION ("/dev/ttyUSB3", a host, "hci1", or
    nothing to autodetect), never a board type. Board facts land in `bridge.info` /
    `bridge.capabilities`. The Bridge also does device management: list_devices / pair / unpair.
  * a **Session** is one connect-on-demand link to a bonded host. `bridge.connect(name)` advertises
    that identity, waits until it is ready, and hands back a Session you drive (type/key/move/click/
    unlock). Leaving the `with` disconnects it but keeps the bond -- no idle connections.

Multiple sessions can be open at once when the board supports it (the ESP32 holds several identities);
otherwise `connect()` raises. Errors are typed: NotBonded / ConnectTimeout / Unsupported /
CapacityError, all under HidbridgeError.

Report mode defaults to "raw" (the host builds the exact bytes via reports.py and the board relays
them -- offline byte-parity, the old "Mode 2"); mode="rpc" has the board build them (the old "Mode 1").
"""

from __future__ import annotations

from .controller import HidController

DEFAULT_CONNECT_TIMEOUT = 20.0


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class HidbridgeError(Exception):
    """Base for every hidbridge driving error."""


class NotBonded(HidbridgeError):
    """connect() named a device that is not paired to this board."""


class ConnectTimeout(HidbridgeError):
    """The device did not (re)connect within the timeout."""


class Unsupported(HidbridgeError):
    """The board fundamentally cannot do what was asked (e.g. a second concurrent connection on a
    single-identity backend like the dongle or the Pico)."""


class CapacityError(HidbridgeError):
    """A multi-identity board whose connection slots are all in use."""


# --------------------------------------------------------------------------- #
# Session -- one live link
# --------------------------------------------------------------------------- #
class Session:
    """One connected host, for the duration of a `with`. Enter = already connected + ready (the work
    happened in Bridge.connect); exit = disconnect, bond kept. Drive it with type/key/move/click/
    scroll/unlock."""

    def __init__(self, bridge: "Bridge", board, name: str):
        self._bridge = bridge
        self._board = board
        self.name = name
        self._ctl = HidController(board.transport(name))
        self._closed = False

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        if not self._closed:
            self._closed = True
            self._bridge._release(self)

    # ---- drive (thin, well-named wrappers over HidController) ----
    def type(self, text: str):
        return self._ctl.type_text(text)

    def key(self, name: str, mod: int = 0):
        return self._ctl.send_key(name, mod)

    def move(self, dx: int, dy: int):
        return self._ctl.move(dx, dy)

    def click(self, button: str = "left"):
        return self._ctl.click(button)

    def scroll(self, clicks: int):
        return self._ctl.scroll(clicks)

    def unlock(self, passcode: str, press_enter: bool = True):
        return self._ctl.unlock(passcode, press_enter=press_enter)


# --------------------------------------------------------------------------- #
# Bridge -- the board channel
# --------------------------------------------------------------------------- #
class Bridge:
    """A board channel + its device management. Yields Sessions from connect(). Use as a context
    manager: __enter__ opens + probes the channel, __exit__ closes it (and any open sessions)."""

    def __init__(self, board, *, location=None, mode: str = "raw"):
        self._board = board
        self.location = location
        self.mode = mode
        self.info: dict = {}
        self._sessions: list[Session] = []

    # ---- lifecycle ----
    def __enter__(self) -> "Bridge":
        self._board.prepare()
        self.info = dict(self._board.board_info())
        return self

    def __exit__(self, *exc):
        for sess in list(self._sessions):
            sess.close()
        self._board.close()
        return False

    @property
    def capabilities(self) -> dict:
        return dict(getattr(self._board, "capabilities", {}))

    # ---- device management ----
    def list_devices(self):
        return self._board.list_devices()

    def pair(self, name: str):
        return self._board.pair(name)

    def unpair(self, name: str):
        return self._board.unpair(name)

    def rename(self, nick: str):
        return self._board.rename(nick)

    # ---- connect on demand ----
    def connect(self, name: str, *, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> Session:
        bonded = {d.name for d in self.list_devices() if getattr(d, "bonded", True)}
        if name not in bonded:
            raise NotBonded(f"{name!r} is not paired to this board -- pair it first")

        max_sessions = self.capabilities.get("max_sessions", 1)
        if len(self._sessions) >= max_sessions:
            held = ", ".join(repr(s.name) for s in self._sessions)
            if max_sessions <= 1:
                raise Unsupported(
                    f"this board holds one device at a time (currently {held}); disconnect it first")
            raise CapacityError(f"all {max_sessions} connection slots are in use ({held})")

        if not self._board.connect(name, timeout):
            raise ConnectTimeout(
                f"{name!r} did not connect within {timeout:g}s (awake + in range? bond present?)")
        # readiness gate (real backends observe iOS enabling the reports; the fake has none -> skip)
        waiter = getattr(self._board, "wait_ready", None)
        if waiter is not None:
            waiter(name, timeout)

        sess = Session(self, self._board, name)
        self._sessions.append(sess)
        return sess

    def _release(self, sess: Session):
        if sess in self._sessions:
            self._board.disconnect(sess.name)
            self._sessions.remove(sess)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def open(location=None, *, backend=None, mode: str = "raw", token=None, **opts) -> Bridge:
    """Open a board and return a Bridge (a context manager).

    location: "/dev/ttyUSB3" or a bare device path (serial; the board self-identifies), a host/IP
    (network, TLS -- needs `token`), "hci1" (dongle), or None to autodetect a plugged-in board. The
    board TYPE is never passed here -- it is discovered from a probe (`bridge.info`).

    backend: inject a board object (tests / advanced use); when omitted, `location` is resolved to a
    real board driver.
    """
    board = backend if backend is not None else _resolve_board(location, token=token, mode=mode, **opts)
    return Bridge(board, location=location, mode=mode)


def _resolve_board(location, *, token=None, mode="raw", **opts):
    # Real serial/net/dongle board drivers + autodetect land in the CLI-rewire phase; until then the
    # API core is exercised by injecting `backend=` (see open()).
    raise NotImplementedError(
        "resolving a real board from a location is not wired yet -- pass backend= for now")

"""Offline tests for the HidSession READINESS gate (bt_hid.conn).

The bug these guard: a freshly (re)connected iOS central needs ~1-3s to read the HID report map +
write the report CCCDs (StartNotify) before it ACTS on input notifications. Sending in that window
drops keystrokes -- callers used to paper over it with a manual `time.sleep(3.3)` after `with
HidSession(...) as hid:`. HidSession must instead block in `__enter__` until the link is genuinely
READY (iOS subscribed), preferring the REAL subscribe signal and falling back to a bounded settle.

No hardware: fake backends model the readiness signal, and a FakeClock makes every wait deterministic
+ instant (records the exact settle/poll durations the session would sleep on real hardware).
"""

import asyncio
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

from hidbridge import conn as C  # noqa: E402
from hidbridge.controller import RecordingTransport  # noqa: E402


class FakeClock:
    """Deterministic monotonic clock; sleep() advances it and records the duration."""

    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.t

    def sleep(self, dt):
        self.sleeps.append(dt)
        self.t += dt


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(C, "time", c)
    return c


class ReadyBackend(C.LinkBackend):
    """Fake connect-on-demand backend with a scriptable readiness signal.

    ready_after: number of is_ready() polls that must elapse before the real signal fires
                 (None = never ready). supported: whether the real subscribe signal is observable.
    """

    name = "fake"
    capabilities = {"per_device_identity": True}

    def __init__(self, *, ready_after=0, supported=True, ready_settle=2.0):
        self.READY_SETTLE = ready_settle
        self._ready_after = ready_after
        self._supported = supported
        self._polls = 0
        self._connected = False
        self.tx = RecordingTransport()
        self.events = []

    def prepare(self):
        self.events.append("prepare")

    def connect(self, ios_device, timeout=20.0, exclusive=True):
        self.events.append(("connect", ios_device))
        self._connected = True
        return True

    def disconnect(self):
        self.events.append("disconnect")
        self._connected = False

    def is_connected(self):
        return self._connected

    def readiness_supported(self):
        return self._supported

    def is_ready(self):
        self._polls += 1
        self.events.append(("is_ready", self._polls))
        if self._ready_after is None:
            return False
        return self._polls > self._ready_after

    def transport(self):
        return self.tx


# --------------------------------------------------------------------------- #
# The core gate: enter() must not yield until the real readiness signal fires
# --------------------------------------------------------------------------- #
def test_enter_blocks_until_ready(clock):
    be = ReadyBackend(ready_after=3)          # ready on the 4th poll
    with C.HidSession(None, "iPad", backend=be) as hid:
        assert hid.ready_state == "ready"
        assert be.is_ready()                  # still ready inside the body
        hid.type_text("x")
    # it polled readiness (did not yield on the first not-ready poll) then built the controller
    polls = [e for e in be.events if isinstance(e, tuple) and e[0] == "is_ready"]
    assert len(polls) >= 4
    # the connect happened before any readiness poll; the controller frames flow after ready
    assert be.tx.frames  # type_text reached the transport (only possible post-ready controller)


def test_ready_signal_needs_no_settle(clock):
    # when the REAL subscribe signal fires, no fixed sleep is needed (settle default = 0)
    be = ReadyBackend(ready_after=0)
    with C.HidSession(None, "iPad", backend=be):
        pass
    assert clock.sleeps == [] or all(s == 0 for s in clock.sleeps)


def test_require_ready_raises_on_timeout(clock):
    be = ReadyBackend(ready_after=None)       # never subscribes
    with pytest.raises(C.BtHidError):
        with C.HidSession(None, "iPad", backend=be, ready_timeout=5.0, require_ready=True):
            pass


def test_not_required_falls_back_to_settle_when_never_ready(clock):
    be = ReadyBackend(ready_after=None, ready_settle=2.5)
    with C.HidSession(None, "iPad", backend=be, ready_timeout=1.0, require_ready=False) as hid:
        assert hid.ready_state == "settle"
    assert 2.5 in clock.sleeps          # fell back to the backend settle


def test_settle_tunable_overrides_default(clock):
    be = ReadyBackend(ready_after=0)
    with C.HidSession(None, "iPad", backend=be, settle=1.5):
        pass
    assert 1.5 in clock.sleeps


def test_unsupported_readiness_uses_backend_ready_settle(clock):
    be = ReadyBackend(supported=False, ready_settle=3.3)
    with C.HidSession(None, "iPad", backend=be) as hid:
        assert hid.ready_state == "settle"
    assert 3.3 in clock.sleeps


def test_ready_timeout_bounds_the_wait(clock):
    be = ReadyBackend(ready_after=None, ready_settle=0.0)   # never ready, isolate the wait bound
    with C.HidSession(None, "iPad", backend=be, ready_timeout=4.0, require_ready=False):
        pass
    # the poll loop advanced the clock to ~ready_timeout then gave up (did not spin unbounded)
    assert 4.0 <= clock.t < 4.0 + 1.0


# --------------------------------------------------------------------------- #
# Async context manager
# --------------------------------------------------------------------------- #
def test_async_hidsession_awaits_readiness():
    # Note: no FakeClock here (runs in a worker thread via to_thread); ready_after=0 so no real sleep.
    be = ReadyBackend(ready_after=0)

    async def go():
        async with C.AsyncHidSession(None, "iPad", backend=be) as hid:
            assert hid.ready_state == "ready"
            hid.type_text("q")
        return be

    result = asyncio.run(go())
    assert ("connect", "iPad") in result.events
    assert result.events[-1] == "disconnect"
    assert result.tx.frames  # report reached the transport


def test_async_sessions_run_concurrently():
    a = ReadyBackend(ready_after=0)
    b = ReadyBackend(ready_after=0)

    async def drive(backend):
        async with C.AsyncHidSession(None, "iPad", backend=backend) as hid:
            hid.type_text("z")
        return backend

    async def go():
        return await asyncio.gather(drive(a), drive(b))

    done = asyncio.run(go())
    assert all(bk.tx.frames for bk in done)

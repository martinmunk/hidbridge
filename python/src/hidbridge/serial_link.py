"""Reset-free USB-serial link for the MCU HID backends (ESP32 CP2102 / FTDI, Pico USB-CDC).

THE PROBLEM (why talking to the board kept dropping its BLE link)
----------------------------------------------------------------
On an ESP32 dev board the USB-serial bridge's modem-control lines drive the auto-reset circuit:
RTS -> EN (chip enable/reset), DTR -> GPIO0 (boot select). The Linux tty layer ASSERTS DTR+RTS
when it activates a port on open -- kernel `tty_port_raise_dtr_rts()`, which runs during the
`open(2)` syscall itself and is gated on `C_BAUD(tty) != 0`. That RTS assert pulses EN low and
REBOOTS the ESP32, dropping its bonded BLE connection to the iPad/iPhone. So every naive port
open can kill the link, which is exactly the "iPad stayed conn=0 after our port-opens" symptom.

pyserial's documented `dtr=False; rts=False` BEFORE `open()` does NOT prevent this: the kernel
raise happens INSIDE `os.open()`, before pyserial gets a chance to clear the lines. (Verified live
on the wsb CP2102 at /dev/ttyUSB1, 2026-07-10: the ESP32 ROM boot log + "HIDBRIDGE-esp32 ready"
banner reprints on every such open.)

THE FIX (verified: 3 consecutive opens, no boot banner, iPad conn stayed 1)
--------------------------------------------------------------------------
Leave the tty at baud **B0** on close. The next open then sees `C_BAUD(tty) == 0`, so the kernel
SKIPS `tty_port_raise_dtr_rts()` -- DTR/RTS are never asserted, RTS never pulses EN, and the board
stays up. We open at the raw fd level (`os.open`, so pyserial's own baud reconfigure cannot
re-trigger the raise), read the still-B0 port, then raise the REAL baud AFTER the tty is already
active (raising baud post-activation does not re-raise the modem lines). On close we set B0 again
+ clear HUPCL (so close does not drop the lines and force a re-raise next time).

CAVEAT: a TRULY cold enumeration (board just physically plugged in) still resets ONCE, because the
default termios baud is nonzero and nothing has left B0 yet. Our lifecycle opens once and HOLDS the
port for the whole session, always leaving B0 on close, so within a session and across our own
sessions there are ZERO further resets -- only a cold plug-in costs the one unavoidable reset.

TWO BOARDS, ONE IMPLEMENTATION (verified live 2026-07-10 on the CP2102 DevKit /dev/ttyUSB1 AND the
FT2232H ESP32-Ethernet-Kit /dev/ttyUSB3): the two bridges reset for OPPOSITE reasons, but this one
open path is safe for both:
  * CP2102 DevKit  -- resets on the DTR/RTS RAISE at open (the B0 trick above suppresses it).
  * FT2232H Kit    -- default open (lines left asserted) does NOT reset; it resets on a DTR/RTS
                      MISMATCH transient. pyserial's `dtr=False; rts=False` applies the two lines in
                      SEPARATE ioctls, so one drops before the other -> a momentary mismatch pulses
                      EN -> reset. We instead clear DTR+RTS ATOMICALLY in a single TIOCMBIC ioctl,
                      so the lines never mismatch and the Ethernet-Kit does not reset either.
So: always clear the two modem lines together (never one-at-a-time), and leave B0 on close.

This module is Linux-only for the no-reset path (it uses termios + the TIOCM ioctls). On other
platforms, or if the raw path is unavailable, callers fall back to pyserial (which resets, but the
persistent-session lifecycle + reconnect wait still self-heal the link).
"""

from __future__ import annotations

import fcntl
import os
import select
import struct
import sys
import termios
import time

# Modem-control ioctls (Linux). TIOCMBIC = bit-clear the given modem lines.
_TIOCMBIC = 0x5417
_TIOCM_DTR = 0x002
_TIOCM_RTS = 0x004
# CBAUD mask (baud bits in c_cflag); B0 == 0 clears them -> "hang up" speed -> kernel skips the
# DTR/RTS raise on the next open. 0o010017 on all mainstream Linux archs.
_CBAUD = 0o010017

# Baud-rate name -> termios speed constant (the ones we actually use).
_BAUD_CONST = {
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
    230400: termios.B230400,
}

IS_LINUX = sys.platform.startswith("linux")


class SerialLinkError(RuntimeError):
    pass


def _clear_dtr_rts(fd: int) -> None:
    fcntl.ioctl(fd, _TIOCMBIC, struct.pack("I", _TIOCM_DTR | _TIOCM_RTS))


def _leave_b0(fd: int) -> None:
    """Set the tty to baud B0 + clear HUPCL + drop DTR/RTS, so the NEXT open does not reset."""
    a = termios.tcgetattr(fd)
    a[2] = (a[2] & ~_CBAUD) & ~termios.HUPCL      # c_cflag: B0, no HUPCL
    a[4] = termios.B0                              # ispeed
    a[5] = termios.B0                              # ospeed
    termios.tcsetattr(fd, termios.TCSANOW, a)
    _clear_dtr_rts(fd)


def _configure_raw(fd: int, baud: int) -> None:
    """Put the (already-active) tty into raw 8N1 at `baud`, no HUPCL, non-blocking reads."""
    speed = _BAUD_CONST.get(baud)
    if speed is None:
        raise SerialLinkError(f"unsupported baud {baud}")
    a = termios.tcgetattr(fd)
    a[0] = 0                                        # iflag: no translation/flow
    a[1] = 0                                        # oflag: raw output
    a[3] = 0                                        # lflag: non-canonical, no echo
    a[2] = (a[2] & ~_CBAUD & ~termios.HUPCL & ~termios.PARENB & ~termios.CSTOPB & ~termios.CSIZE)
    a[2] |= termios.CS8 | termios.CREAD | termios.CLOCAL | speed
    a[4] = speed
    a[5] = speed
    a[6][termios.VMIN] = 0
    a[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, a)


class RawSerialLink:
    """Raw-fd serial handle that opens WITHOUT resetting an ESP32 (see module docstring).

    Duck-typed to match the subset of the pyserial API the MCU transports use:
    ``write(bytes)``, ``readline() -> bytes``, ``reset_input_buffer()``, ``flush()``,
    ``close()``, plus ``.port`` and ``.fileno()``. Opening relies on the PREVIOUS close having
    left the tty at B0; the very first open of a cold-plugged board still resets once.
    """

    def __init__(self, port: str, baud: int = 115200, timeout: float = 1.0, open_now: bool = True):
        if not IS_LINUX:
            raise SerialLinkError("RawSerialLink is Linux-only; use the pyserial fallback")
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._fd: int | None = None
        self._buf = b""
        if open_now:
            self.open()

    # -- lifecycle ---------------------------------------------------------- #
    def open(self) -> None:
        if self._fd is not None:
            return
        fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            # The tty is still at whatever the last close left (B0 if we closed it, so NO reset).
            _configure_raw(fd, self.baud)          # raise real baud AFTER activation
            _clear_dtr_rts(fd)                     # keep the reset line quiescent
        except Exception:
            os.close(fd)
            raise
        self._fd = fd

    def close(self) -> None:
        if self._fd is None:
            return
        try:
            _leave_b0(self._fd)                    # so the NEXT open is reset-free
        except OSError:
            pass
        try:
            os.close(self._fd)
        finally:
            self._fd = None
            self._buf = b""

    def fileno(self) -> int:
        if self._fd is None:
            raise SerialLinkError("port not open")
        return self._fd

    # -- io ----------------------------------------------------------------- #
    def write(self, data: bytes) -> int:
        if self._fd is None:
            raise SerialLinkError("port not open")
        return os.write(self._fd, data)

    def flush(self) -> None:
        if self._fd is not None:
            try:
                termios.tcdrain(self._fd)
            except OSError:
                pass

    def reset_input_buffer(self) -> None:
        self._buf = b""
        if self._fd is not None:
            try:
                termios.tcflush(self._fd, termios.TCIFLUSH)
            except OSError:
                pass

    def readline(self) -> bytes:
        r"""Read one '\n'-terminated line (bounded by self.timeout). Returns b'' on timeout."""
        end = time.monotonic() + self.timeout
        while b"\n" not in self._buf:
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            r, _, _ = select.select([self._fd], [], [], remaining)
            if not r:
                break
            try:
                chunk = os.read(self._fd, 256)
            except BlockingIOError:
                continue
            except OSError as e:
                raise SerialLinkError(f"read failed: {e}") from e
            if not chunk:
                continue
            self._buf += chunk
        if b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            return line + b"\n"
        # timed out: return whatever partial we have (mirrors pyserial's timeout semantics)
        line, self._buf = self._buf, b""
        return line

    # -- context manager ---------------------------------------------------- #
    def __enter__(self) -> RawSerialLink:
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def prime_b0(port: str) -> bool:
    """Best-effort: leave `port` at B0 so a LATER open is reset-free. Returns True on success.

    Useful to run once at session start (or from a udev hook) so even the first transport open in a
    session avoids the reset -- provided the board was not just cold-plugged. Safe no-op off Linux.
    """
    if not IS_LINUX:
        return False
    try:
        fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        _leave_b0(fd)
        return True
    finally:
        os.close(fd)


def open_serial(port: str, baud: int = 115200, timeout: float = 1.0, prefer_raw: bool = True):
    """Open `port` reset-free if possible (RawSerialLink on Linux), else fall back to pyserial.

    Returns a serial-like object (RawSerialLink or serial.Serial). The pyserial fallback sets
    dtr/rts False (best effort; still resets on this hardware) so the persistent-session lifecycle
    + reconnect wait carry the robustness on non-Linux hosts.
    """
    if prefer_raw and IS_LINUX:
        try:
            return RawSerialLink(port, baud=baud, timeout=timeout)
        except SerialLinkError:
            pass
    import serial  # lazy
    s = serial.Serial()
    s.port = port
    s.baudrate = baud
    s.timeout = timeout
    s.dtr = False
    s.rts = False
    s.open()
    return s

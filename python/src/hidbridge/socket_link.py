"""TCP line channel for the NETWORK HID backend -- the wire under `transport_net`.

The serial backends talk to an MCU over a tty (`serial_link.py`); an Ethernet-equipped ESP32 serves
the SAME newline-terminated ASCII protocol on TCP 3333. This module is the socket counterpart: a
duck-typed `write(bytes)` / `readline() -> bytes` / `close()` channel, which is exactly the interface
`Esp32Transport` already accepts for an injected port -- so the whole command layer (report frames,
ping/readiness, slots, park) is reused verbatim over the network instead of reimplemented.

WHY A HAND-ROLLED readline(): the firmware answers one line per command and ALSO emits unsolicited
`EVT ...` lines at any moment, so the reader must be line-buffered across recv() boundaries (a single
recv can return half a line, or a reply plus a trailing EVT). We keep the partial tail in `_buf` and
hand back exactly one line at a time.

TIMEOUT SEMANTICS mirror pyserial deliberately: on timeout `readline()` returns b"" rather than
raising, because the callers (`Esp32Transport._readline`, `wait_ready`, `wait_connected`) already
treat an empty read as "nothing yet, poll again". A CLOSED peer also reads as b"", which those
callers' bounded loops handle; `send()` notices the dead socket on the next write and reconnects.

No dependency on pyserial (or anything outside the stdlib), so this imports and unit-tests offline.
"""

from __future__ import annotations

import socket

DEFAULT_PORT = 3333
DEFAULT_TIMEOUT = 5.0


class SocketLinkError(RuntimeError):
    """The TCP channel could not be opened (host unresolvable / refused / unreachable)."""


class SocketLine:
    """A newline-framed TCP channel to the board's line server.

    Exposes the same small surface as an open pyserial port (`write`/`readline`/`flush`/`close`),
    so it drops straight into the existing transport.
    """

    def __init__(self, host: str, port: int = DEFAULT_PORT, timeout: float = DEFAULT_TIMEOUT):
        self.host = host
        self.tcp_port = int(port)
        self.timeout = timeout
        self._buf = b""
        try:
            self._sock = socket.create_connection((host, self.tcp_port), timeout=timeout)
        except OSError as e:
            raise SocketLinkError(
                f"cannot reach the hidbridge board at {host}:{self.tcp_port} ({e}). Check the "
                f"Ethernet link + that the firmware was flashed with the network build "
                f"(pio run -e esp32-ethernet-kit); if the mDNS name does not resolve, pass the "
                f"board's IP instead (--host 192.168.x.y)") from e
        # HID frames are tiny and latency-sensitive (a keystroke is 2 frames of ~20 bytes, each
        # awaiting an OK). Nagle would coalesce them into ~40ms round trips; turn it off.
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock.settimeout(timeout)

    # Display name; `Esp32Transport` reports `self.port` in its status dicts.
    @property
    def port(self) -> str:
        return f"{self.host}:{self.tcp_port}"

    def write(self, data: bytes) -> int:
        self._sock.sendall(data)
        return len(data)

    def flush(self) -> None:
        pass          # sendall() already flushed; present so the transport's flush probe is a no-op

    def readline(self) -> bytes:
        """Return ONE line (with its trailing newline), or b"" on timeout / closed peer."""
        while b"\n" not in self._buf:
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                return b""            # nothing yet -- callers poll again within their own deadline
            except OSError:
                return b""            # link died; the next write() surfaces it as a real error
            if not chunk:
                return b""            # peer closed
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return line + b"\n"

    def close(self) -> None:
        try:
            self._sock.close()
        finally:
            self._buf = b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def open_socket(host: str, port: int = DEFAULT_PORT, timeout: float = DEFAULT_TIMEOUT) -> SocketLine:
    """Open a line channel to the board (factory mirroring serial_link.open_serial)."""
    return SocketLine(host, port, timeout)

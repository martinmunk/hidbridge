"""TLS-PSK line channel for the NETWORK HID backend -- the wire under `transport_net`.

The serial backends talk to an MCU over a tty (`serial_link.py`); an Ethernet/WiFi-equipped ESP32
serves the SAME newline-terminated ASCII protocol on TCP 3333. This module is the socket counterpart:
a duck-typed `write(bytes)` / `readline() -> bytes` / `close()` channel, which is exactly the
interface `Esp32Transport` already accepts for an injected port -- so the whole command layer (report
frames, ping/readiness, slots, park) is reused verbatim over the network instead of reimplemented.

ENCRYPTION (TLS-PSK): port 3333 is TLS-ONLY -- there is no plaintext path. The pre-shared key IS the
board's provisioned token (the same secret the web `X-Auth-Token` header and the OTA password reuse).
Because the PSK never crosses the wire, completing the handshake both encrypts the link AND proves we
hold the token -- so a wrong/absent token fails the TLS handshake outright, and the session is
authenticated the moment the handshake lands (there is no app-layer auth line). We pin TLS 1.2 + a PSK
cipher to match the board's mbedTLS server (`firmware/esp32/src/main.cpp` netTlsSetup).
`set_psk_client_callback` needs Python 3.13+; an older interpreter raises a clear error here.

WHY A HAND-ROLLED readline(): the firmware answers one line per command and ALSO emits unsolicited
async `event` notification lines at any moment, so the reader must be line-buffered across recv()
boundaries (a single recv can return half a line, or a reply plus a trailing notification). We keep
the partial tail in `_buf` and hand back exactly one line at a time.

TIMEOUT SEMANTICS mirror pyserial deliberately: on timeout `readline()` returns b"" rather than
raising, because the callers (`Esp32Transport._readline`, `wait_ready`, `wait_connected`) already
treat an empty read as "nothing yet, poll again". A CLOSED peer also reads as b"", which those
callers' bounded loops handle; `send()` notices the dead socket on the next write and reconnects.

Stdlib only (socket + ssl), so this imports and unit-tests offline.
"""

from __future__ import annotations

import socket
import ssl

DEFAULT_PORT = 3333
DEFAULT_TIMEOUT = 5.0

# Identity string the client presents in the PSK handshake. The board's mbedTLS server configures a
# single PSK and ignores the identity, so this is cosmetic -- but it must be non-empty.
_PSK_IDENTITY = "hidbridge"
# The one cipher both ends enable: TLS_PSK_WITH_AES_128_GCM_SHA256 (ESP32 mbedTLS has PSK + AES + GCM).
_PSK_CIPHER = "PSK-AES128-GCM-SHA256"


class SocketLinkError(RuntimeError):
    """The TLS channel could not be opened (host unreachable, no token, or handshake/auth rejected)."""


def _psk_context(token: str) -> ssl.SSLContext:
    """A TLS 1.2 client context that authenticates with `token` as the pre-shared key (no certs)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False                  # PSK auth carries no certificate to match a name to
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2   # board speaks TLS 1.2 PSK (1.3 external PSK differs)
    try:
        ctx.set_ciphers(_PSK_CIPHER)
    except ssl.SSLError as e:                    # PSK cipher disabled in this OpenSSL build
        raise SocketLinkError(f"this Python's OpenSSL lacks the {_PSK_CIPHER} PSK cipher ({e})") from e
    if not hasattr(ctx, "set_psk_client_callback"):
        raise SocketLinkError(
            "TLS-PSK needs Python 3.13+ (ssl.set_psk_client_callback); this interpreter is older")
    psk = token.encode("utf-8")
    ctx.set_psk_client_callback(lambda hint: (_PSK_IDENTITY, psk))
    return ctx


class SocketLine:
    """A newline-framed TLS-PSK channel to the board's line server.

    Exposes the same small surface as an open pyserial port (`write`/`readline`/`flush`/`close`),
    so it drops straight into the existing transport.
    """

    def __init__(self, host: str, port: int = DEFAULT_PORT, timeout: float = DEFAULT_TIMEOUT,
                 token: str | None = None):
        self.host = host
        self.tcp_port = int(port)
        self.timeout = timeout
        self._buf = b""
        if not token:
            raise SocketLinkError(
                "the network backend is TLS-encrypted and needs the board token (the TLS pre-shared "
                "key); provision one with `hidbridge net-provision` and set BT_HID_NET_TOKEN or "
                "~/.config/bt_hid/net.token")
        ctx = _psk_context(token)
        try:
            raw = socket.create_connection((host, self.tcp_port), timeout=timeout)
        except OSError as e:
            raise SocketLinkError(
                f"cannot reach the hidbridge board at {host}:{self.tcp_port} ({e}). Check the "
                f"network link + that the firmware was flashed with the network build "
                f"(pio run -e esp32-ethernet-kit); if the mDNS name does not resolve, pass the "
                f"board's IP instead (--host 192.168.x.y)") from e
        # HID frames are tiny and latency-sensitive (a keystroke is 2 frames of ~20 bytes, each
        # awaiting an OK). Nagle would coalesce them into ~40ms round trips; turn it off.
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            self._sock = ctx.wrap_socket(raw, server_hostname=None)
        except (ssl.SSLError, OSError) as e:
            raw.close()
            raise SocketLinkError(
                f"TLS-PSK handshake with {host}:{self.tcp_port} failed ({e}); the token likely does "
                f"not match the board's provisioned token (they must be identical -- the token is "
                f"the pre-shared key)") from e
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


def open_socket(host: str, port: int = DEFAULT_PORT, timeout: float = DEFAULT_TIMEOUT,
                token: str | None = None) -> SocketLine:
    """Open a TLS-PSK line channel to the board (factory mirroring serial_link.open_serial).

    `token` is REQUIRED -- it is the TLS pre-shared key (the board's provisioned token). Without it
    the encrypted port cannot be reached; SocketLine raises a clear error.
    """
    return SocketLine(host, port, timeout, token=token)

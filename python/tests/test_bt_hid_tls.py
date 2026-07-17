"""Offline TLS-PSK proof for the network channel (socket_link) -- no board, no hardware.

Port 3333 is TLS-ONLY, with the board's token as the pre-shared key (firmware/esp32/src/main.cpp
netTlsSetup). These tests stand a LOCAL Python TLS-PSK server in a thread and drive the real
`socket_link.SocketLine` against it, proving the client half end to end: the handshake succeeds only
when the tokens match (the PSK is the auth), a wrong token fails the handshake outright, and a missing
token never even opens a socket. This is the same handshake the ESP32 mbedTLS server performs, so a
green run here means the client speaks a wire the board understands (TLS 1.2, PSK-AES128-GCM-SHA256).

set_psk_client_callback / set_psk_server_callback need Python 3.13+; the whole module skips below that.
"""

from __future__ import annotations

import socket
import ssl
import threading

import pytest

from hidbridge import socket_link

pytestmark = pytest.mark.skipif(
    not hasattr(ssl.SSLContext, "set_psk_server_callback"),
    reason="TLS-PSK needs Python 3.13+ (ssl PSK callbacks)")

TOKEN = "correcthorsebattery"


class PskEchoServer:
    """A one-shot local TLS-PSK server: accept one connection, echo each received line back.

    Mirrors the ESP32 server's crypto (TLS 1.2, single PSK for identity 'hidbridge', the
    PSK-AES128-GCM-SHA256 cipher) so the client is exercised against a faithful peer."""

    def __init__(self, token: str = TOKEN):
        self._psk = token.encode("utf-8")
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self._srv.settimeout(5.0)
        self.port = self._srv.getsockname()[1]
        self.error: Exception | None = None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _context(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        ctx.set_ciphers("PSK-AES128-GCM-SHA256")
        # Return the one PSK regardless of the client identity (the board configures a single PSK too).
        ctx.set_psk_server_callback(lambda identity: self._psk, identity_hint="hidbridge")
        return ctx

    def _serve(self):
        try:
            raw, _ = self._srv.accept()
            with self._context().wrap_socket(raw, server_side=True) as tls:
                buf = b""
                while True:
                    chunk = tls.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, _, buf = buf.partition(b"\n")
                        tls.sendall(line + b"\n")     # echo the line straight back
        except Exception as e:  # noqa: BLE001 -- surfaced to the test thread via .error
            self.error = e
        finally:
            self._srv.close()

    def join(self):
        self._thread.join(timeout=5.0)


def test_matching_token_handshakes_and_round_trips():
    srv = PskEchoServer(TOKEN)
    ch = socket_link.open_socket("127.0.0.1", srv.port, timeout=5.0, token=TOKEN)
    try:
        ch.write(b"hello over tls\n")
        assert ch.readline() == b"hello over tls\n"       # encrypted round trip succeeded
    finally:
        ch.close()
        srv.join()
    assert srv.error is None


def test_wrong_token_fails_the_handshake():
    srv = PskEchoServer(TOKEN)
    with pytest.raises(socket_link.SocketLinkError) as ei:
        socket_link.open_socket("127.0.0.1", srv.port, timeout=5.0, token="wrong-token-entirely")
    # the error points at the token mismatch, and never contains either token value
    assert "handshake" in str(ei.value).lower()
    assert TOKEN not in str(ei.value) and "wrong-token-entirely" not in str(ei.value)
    srv.join()


def test_no_token_never_opens_a_socket():
    # TLS-PSK cannot proceed without the key; SocketLine refuses before any connect attempt.
    with pytest.raises(socket_link.SocketLinkError) as ei:
        socket_link.open_socket("127.0.0.1", 1, timeout=0.5, token=None)
    assert "token" in str(ei.value).lower()


def test_context_pins_tls12_and_psk_cipher():
    # the client context is locked to the board's crypto: TLS 1.2 only, PSK-AES128-GCM-SHA256.
    ctx = socket_link._psk_context(TOKEN)
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
    assert ctx.maximum_version == ssl.TLSVersion.TLSv1_2
    assert any(c["name"] == "PSK-AES128-GCM-SHA256" for c in ctx.get_ciphers())

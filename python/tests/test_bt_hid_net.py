"""Offline tests for the NETWORK HID backend (transport_net + socket_link + link_net).

No sockets are opened and no board is needed: a fake line channel (duck-typing write/readline)
stands in for the TCP socket, exactly as the serial tests use a fake serial. These assert the parts
that are pure logic -- host:port parsing, the auth handshake, that report frames over the network are
byte-identical to the serial path, and that a bad token neither authenticates nor leaks the secret.
"""

import socket
import threading
import time

import pytest

from hidbridge import conn, controller
from hidbridge import reports as R
from hidbridge import socket_link
from hidbridge import transport_net as TN


class FakeChannel:
    """A duck-typed SocketLine: hands back queued replies, records what was written."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.written = []
        self.port = "board.local:3333"

    def write(self, b):
        self.written.append(b)
        return len(b)

    def flush(self):
        pass

    def readline(self):
        return self.replies.pop(0) if self.replies else b""

    def close(self):
        pass


def _authed_transport(replies):
    t = TN.NetTransport(host=None, open_now=False, token="supersecret")
    t._ser = FakeChannel([*replies])
    t.host, t.port = "board.local", "board.local:3333"
    t._authenticate()
    return t


# --------------------------------------------------------------------------- #
# host:port parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("target,expect", [
    ("hidbridge-esp32.local", ("hidbridge-esp32.local", 3333)),
    ("hidbridge-esp32.local:4000", ("hidbridge-esp32.local", 4000)),
    ("192.0.2.42", ("192.0.2.42", 3333)),
    ("192.0.2.42:9999", ("192.0.2.42", 9999)),
    ("[fe80::1]:3333", ("fe80::1", 3333)),
    ("[fe80::1]", ("fe80::1", 3333)),
])
def test_split_host_port(target, expect):
    assert TN._split_host_port(target) == expect


def test_target_grammar_routes_to_net_backend():
    assert conn._parse_target("net:board.local") == ("net", "board.local")
    assert conn._parse_target("net:board.local:4000") == ("net", "board.local:4000")


# --------------------------------------------------------------------------- #
# auth: the TLS-PSK handshake IS the authentication -- no @token app-line on the wire
# --------------------------------------------------------------------------- #
def test_authenticate_sets_authed_without_any_app_line():
    # The token doubles as the TLS pre-shared key, so a completed handshake already proves we hold
    # it; the transport sends NO @token line (dropped with proto 2 -- TLS-PSK is the sole net auth).
    t = _authed_transport([])
    assert t._authed is True
    assert t._ser.written == []


def test_no_token_stays_unauthed_and_sends_nothing():
    t = TN.NetTransport(host=None, open_now=False, token=None)
    t._ser = FakeChannel([])
    t.host, t.port = "board.local", "board.local:3333"
    t._authenticate()
    assert t._authed is False
    assert t._ser.written == []


# --------------------------------------------------------------------------- #
# report frames over the network == the serial/report-layer bytes
# --------------------------------------------------------------------------- #
def test_network_report_frames_match_reports_layer():
    t = _authed_transport([b"OK\n", b"OK\n"])
    res = controller.HidController(t).type_text("A")
    assert res["ok"] and res["backend"] == "net" and res["sent"] == 2
    expected = [b"K" + r.data.hex().encode() + b"\n" for r in R.type_text("A")]
    assert t._ser.written == expected     # no @auth line precedes the frames now


# NOTE: the net-status (`N`) reply parsing + token-provisioning (`W<token>`) framing tests were
# REMOVED here: those single-char device commands are gone from the firmware (net status +
# provisioning are now JSON-RPC methods on board.McuBoard / rpc.py), so the transport no longer
# carries parse_net_status / net_status / encode_set_token / set_net_token.


# --------------------------------------------------------------------------- #
# token resolution precedence (explicit > env > file), never opens a socket
# --------------------------------------------------------------------------- #
def test_load_token_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv("BT_HID_NET_TOKEN", raising=False)
    tf = tmp_path / "net.token"
    tf.write_text("filetoken123\n")
    monkeypatch.setattr(TN, "TOKEN_PATH", str(tf))
    assert TN.load_token("explicit-wins") == "explicit-wins"
    monkeypatch.setenv("BT_HID_NET_TOKEN", "envtoken456")
    assert TN.load_token(None) == "envtoken456"
    monkeypatch.delenv("BT_HID_NET_TOKEN")
    assert TN.load_token(None) == "filetoken123"


def test_load_token_absent_is_none(tmp_path, monkeypatch):
    monkeypatch.delenv("BT_HID_NET_TOKEN", raising=False)
    monkeypatch.setattr(TN, "TOKEN_PATH", str(tmp_path / "nope.token"))
    assert TN.load_token(None) is None


# --------------------------------------------------------------------------- #
# link_net plugs into make_link_backend as an MCU-family backend
# --------------------------------------------------------------------------- #
def test_link_net_backend_resolves():
    be = conn.make_link_backend("net:board.local")
    assert be.name == "net"
    assert be.capabilities["medium"] == "tcp"
    assert be.capabilities["per_device_identity"] is True     # inherited from the esp32 backend
    assert "board.local" in be.describe()


# --------------------------------------------------------------------------- #
# the OPEN (resolve + connect + handshake) must be BOUNDED by `timeout` and raise SocketLinkError --
# never hang. A stuck/incompatible board (accepts TCP, never completes TLS) or a stalling .local
# lookup must surface a clear error in ~the timeout, not a multi-minute hang.
# --------------------------------------------------------------------------- #
def _accept_only_listener():
    """A throwaway TCP listener that ACCEPTS then goes silent (never speaks TLS). Returns (host, port);
    the accept thread is a daemon, so the test never blocks on teardown."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    host, port = srv.getsockname()

    def loop():
        try:
            conn_sock, _ = srv.accept()
            time.sleep(30)                # hold the connection open, send nothing (half-open peer)
            conn_sock.close()
        except OSError:
            pass
    threading.Thread(target=loop, daemon=True).start()
    return host, port


def test_open_socket_bounds_a_stuck_tls_handshake():
    host, port = _accept_only_listener()
    t0 = time.monotonic()
    with pytest.raises(socket_link.SocketLinkError) as ei:
        socket_link.open_socket(host, port, timeout=2, token="x")
    elapsed = time.monotonic() - t0
    assert elapsed < 5, f"open hung {elapsed:.1f}s -- the TLS handshake was not bounded by timeout"
    assert "handshake" in str(ei.value).lower()


def test_open_socket_bounds_a_stalling_name_resolution(monkeypatch):
    # getaddrinfo (mDNS/.local) is NOT bounded by create_connection's timeout arg -- a stalled lookup
    # would hang for the OS default. open_socket must bound name resolution too.
    real = socket.getaddrinfo

    def slow(*a, **k):
        time.sleep(30)
        return real(*a, **k)
    monkeypatch.setattr(socket, "getaddrinfo", slow)
    t0 = time.monotonic()
    with pytest.raises(socket_link.SocketLinkError) as ei:
        socket_link.open_socket("stuck.local", 3333, timeout=2, token="x")
    elapsed = time.monotonic() - t0
    assert elapsed < 5, f"open hung {elapsed:.1f}s -- name resolution was not bounded by timeout"
    assert "resolve" in str(ei.value).lower() or "reach" in str(ei.value).lower()

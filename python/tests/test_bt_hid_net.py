"""Offline tests for the NETWORK HID backend (transport_net + socket_link + link_net).

No sockets are opened and no board is needed: a fake line channel (duck-typing write/readline)
stands in for the TCP socket, exactly as the serial tests use a fake serial. These assert the parts
that are pure logic -- host:port parsing, the auth handshake, that report frames over the network are
byte-identical to the serial path, and that a bad token neither authenticates nor leaks the secret.
"""

import pytest

from hidbridge import conn, controller
from hidbridge import reports as R
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

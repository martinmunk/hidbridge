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
    t._ser = FakeChannel([b"OK auth\n", *replies])
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
# auth handshake
# --------------------------------------------------------------------------- #
def test_authenticate_sends_token_and_sets_authed():
    t = _authed_transport([])
    assert t._authed is True
    assert t._ser.written[0] == b"@supersecret\n"


def test_no_token_stays_unauthed_and_sends_nothing():
    t = TN.NetTransport(host=None, open_now=False, token=None)
    t._ser = FakeChannel([])
    t.host, t.port = "board.local", "board.local:3333"
    t._authenticate()
    assert t._authed is False
    assert t._ser.written == []


def test_bad_token_raises_and_does_not_leak_token():
    t = TN.NetTransport(host=None, open_now=False, token="hunter2secret")
    t._ser = FakeChannel([b"ERR auth\n"])
    t.host, t.port = "board.local", "board.local:3333"
    with pytest.raises(TN.NetError) as ei:
        t._authenticate()
    assert "hunter2secret" not in str(ei.value)


# --------------------------------------------------------------------------- #
# report frames over the network == the serial/report-layer bytes
# --------------------------------------------------------------------------- #
def test_network_report_frames_match_reports_layer():
    t = _authed_transport([b"OK\n", b"OK\n"])
    res = controller.HidController(t).type_text("A")
    assert res["ok"] and res["backend"] == "net" and res["sent"] == 2
    expected = [b"K" + r.data.hex().encode() + b"\n" for r in R.type_text("A")]
    assert t._ser.written[1:] == expected     # [0] is the @auth line


# --------------------------------------------------------------------------- #
# N (net status) reply parsing
# --------------------------------------------------------------------------- #
def test_parse_net_status():
    st = TN.NetTransport(host=None, open_now=False, token=None)
    st._ser = FakeChannel(
        [b"OK net eth=1 ip=192.0.2.42 port=3333 mdns=hidbridge-esp32.local token=1 clients=2\n"])
    got = st.net_status()
    assert got["ok"] and got["eth"] is True and got["ip"] == "192.0.2.42"
    assert got["port"] == 3333 and got["mdns"] == "hidbridge-esp32.local"
    assert got["token"] is True and got["clients"] == 2


def test_net_status_on_serial_only_firmware():
    from hidbridge.transport_esp32 import parse_net_status
    got = parse_net_status("OK net eth=0 ip=0.0.0.0 port=0 mdns= token=0 clients=0")
    assert got["ok"] and got["eth"] is False and got["token"] is False


# --------------------------------------------------------------------------- #
# token provisioning framing (W<token>) -- serial only, min length, no newline
# --------------------------------------------------------------------------- #
def test_encode_set_token_ok():
    from hidbridge.transport_esp32 import encode_set_token
    assert encode_set_token("abcdefgh") == b"Wabcdefgh\n"


@pytest.mark.parametrize("bad", ["short", "", "has\nnewline12"])
def test_encode_set_token_rejects_bad(bad):
    from hidbridge.transport_esp32 import Esp32Error, encode_set_token
    with pytest.raises(Esp32Error):
        encode_set_token(bad)


# The TLS firmware answers `W<token>` with an ack that varies by whether the TLS port could be brought
# up right then: "OK token set (TLS enabled)" on a fresh board provisioned live, "(reboot to apply as
# the new TLS key)" when a key is already baked in, "(reboot to enable TLS)" if bring-up failed, or a
# bare "OK token set" from older firmware. set_net_token must treat EVERY "OK..." as success (so
# `net-provision` does not falsely report failure) and must NEVER surface the token in the ack.
@pytest.mark.parametrize("ack", [
    b"OK token set (TLS enabled)\n",
    b"OK token set (reboot to apply as the new TLS key)\n",
    b"OK token set (reboot to enable TLS)\n",
    b"OK token set\n",
])
def test_set_net_token_accepts_all_tls_acks_without_leaking(ack):
    from hidbridge.transport_esp32 import Esp32Transport
    t = Esp32Transport(port=None, open_now=False)
    t._ser = FakeChannel([ack])
    res = t.set_net_token("supersecrettoken")
    assert res["ok"] is True                       # every OK-ack is provisioning success
    assert "supersecrettoken" not in res["resp"]   # the board never echoes the token
    assert t._ser.written[0] == b"Wsupersecrettoken\n"   # W<token> framing went out


def test_set_net_token_board_rejection_is_not_ok():
    # a board that rejects the token (e.g. too short for its own guard) must read as failure so
    # `net-provision` exits non-zero rather than claiming success.
    from hidbridge.transport_esp32 import Esp32Transport
    t = Esp32Transport(port=None, open_now=False)
    t._ser = FakeChannel([b"ERR shorttoken\n"])
    res = t.set_net_token("abcdefgh")
    assert res["ok"] is False


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

"""DEVICE-GATED live acceptance: ONE board driven over SERIAL and NET (TCP) AT THE SAME TIME.

Acceptance for the ref-counted, PER-SESSION multi-driver targeting (esp32 main.cpp: g_serialDrivenSlot,
NetSession.drivenSlot, slotClaimCount, g_activeTarget). The USB-serial channel and a TCP session are
INDEPENDENT sessions: each owns its OWN claimed slot, ROUTING is per session (g_activeTarget is set to
the CALLING session's claimed slot before every line), and a slot is parked only when its claim count
hits 0 -- so one driver dropping never yanks a slot another driver still holds.

Two acceptances:
  1. REF-COUNT (needs 1 bonded host on the target slot): serial + net both CLAIM the same connected
     slot; releasing the serial claim keeps the slot up (net still claims it -> refcount 1), releasing
     the net claim too parks it (refcount 0).
  2. NON-CLOBBER (needs 2 bonded/named slots): serial claims slot A, net claims slot B; each session's
     `ping` reports ITS OWN target slot's `device` name (not one shared global target), and interleaved
     `type` from each channel keeps landing on its own slot. A global drive target would collapse both
     pings to the same name and let one channel steal the other's reports.

This drives `rpc.RpcClient` directly (the MCU wire is JSON-RPC; the board builds every report), one
client per transport, BOTH pointed at the SAME physical board.

PRECONDITIONS (physical; skipped unless BT_HID_LIVE_SERIAL_NET=1):
  A board reachable over BOTH serial and net simultaneously -- this repo's `esp32eth`
  (serial BT_HID_ESP32_PORT, net BT_HID_NET_HOST=192.0.2.102, token in BT_HID_NET_TOKEN or
  ~/.config/bt_hid/net.token). Firmware with the multi-driver targeting (>= fw 1.1.0). At least one
  bonded, awake host in range for #1; two distinct bonded slots (BT_HID_SN_SLOT_A/_B) for #2.

Run:
  BT_HID_LIVE_SERIAL_NET=1 BT_HID_ESP32_PORT=/dev/ttyUSB3 BT_HID_NET_HOST=192.0.2.102 \
      uv run --with pyserial --with pytest pytest tests/test_bt_hid_serial_net_live.py -v -s
"""

import os
import pathlib
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    os.environ.get("BT_HID_LIVE_SERIAL_NET") != "1",
    reason="live serial+net concurrency test; set BT_HID_LIVE_SERIAL_NET=1 "
           "(+ a board reachable over serial AND net, at least one bonded host)")

from hidbridge.rpc import open_client  # noqa: E402

SLOT_A = int(os.environ.get("BT_HID_SN_SLOT_A", "0"))
SLOT_B = int(os.environ.get("BT_HID_SN_SLOT_B", "1"))


def _slot(rpc, slot) -> dict:
    for s in rpc.identities().get("slots", []):
        if s.get("slot") == slot:
            return s
    return {}


def _wait_conn(rpc, slot, want=True, timeout=40.0, poll=1.0) -> bool:
    # A readiness POLL: a board mid-BLE-(re)connect can transiently not answer an `identities` poll
    # (a RpcTimeout inside the post-connect window) -- that just means "not confirmed this cycle",
    # so keep polling rather than propagating. A genuinely stuck board still fails via the deadline.
    from hidbridge.rpc import RpcError
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            last = bool(_slot(rpc, slot).get("conn"))
        except RpcError:
            last = None
        if last == want:
            return True
        time.sleep(poll)
    return last == want


@pytest.fixture()
def serial_cli():
    port = os.environ.get("BT_HID_ESP32_PORT")
    if not port:
        from hidbridge import transport_esp32 as TE
        port = TE.detect_port()
    if not port:
        pytest.skip("no ESP32 serial port (set BT_HID_ESP32_PORT)")
    cli = open_client(f"esp32:{port}")
    yield cli
    cli.close()


@pytest.fixture()
def net_cli():
    host = os.environ.get("BT_HID_NET_HOST")
    if not host:
        pytest.skip("no net host (set BT_HID_NET_HOST, e.g. 192.0.2.102)")
    # token from BT_HID_NET_TOKEN / ~/.config/bt_hid/net.token (TLS-PSK pre-shared key)
    cli = open_client(f"net:{host}")
    yield cli
    cli.close()


@pytest.fixture()
def same_board(serial_cli, net_cli):
    """Guard against a misconfig where the two channels point at DIFFERENT boards: both must report the
    same slot0 identity address. Skip (not fail) if they diverge -- it means the env vars are wrong."""
    a = _slot(serial_cli, 0).get("addr")
    b = _slot(net_cli, 0).get("addr")
    if not a or not b or a != b:
        pytest.skip(f"serial and net do not resolve to the SAME board (slot0 addr {a!r} vs {b!r}) -- "
                    "check BT_HID_ESP32_PORT / BT_HID_NET_HOST")
    return serial_cli, net_cli


def test_refcount_shared_slot_survives_one_driver_leaving(same_board):
    serial_cli, net_cli = same_board
    # Both channels claim + connect the SAME slot. `connect` brings the identity up; the second claim
    # from the other channel just ADDS a claimer (does not drop the first).
    serial_cli.call("connect", {"slot": SLOT_A})
    if not _wait_conn(serial_cli, SLOT_A):
        pytest.skip(f"no bonded host connected on slot {SLOT_A} (needed for the ref-count test)")
    net_cli.call("connect", {"slot": SLOT_A})
    assert _slot(net_cli, SLOT_A).get("conn"), "net view: slot not connected after the net claim"
    # Both can drive it (central is up): each build+notify reports sent>0.
    serial_cli.target(SLOT_A); assert serial_cli.type_text("s").get("sent"), "serial drive failed"
    net_cli.target(SLOT_A);    assert net_cli.type_text("n").get("sent"), "net drive failed"
    # Release the SERIAL claim -> slot must STAY connected (net still claims it: refcount 1, not 0).
    serial_cli.call("disconnect", {"slot": SLOT_A})
    time.sleep(2)
    assert _slot(net_cli, SLOT_A).get("conn"), \
        "slot dropped when serial released though net still claimed it -- refcount broken"
    assert net_cli.type_text("n2").get("sent"), "net drive failed after serial released its claim"
    # Release the NET claim too -> refcount 0 -> the slot parks (disconnects). The board may close
    # this now-idle TLS session as it processes the release, so the net call may not return cleanly --
    # tolerate that and observe the final park over the PERSISTENT serial channel (which never closes).
    try:
        net_cli.call("disconnect", {"slot": SLOT_A})
    except Exception:
        pass
    assert _wait_conn(serial_cli, SLOT_A, want=False), "slot did not park after the LAST claim released"


def test_per_session_targets_do_not_clobber(same_board):
    serial_cli, net_cli = same_board
    if SLOT_A == SLOT_B:
        pytest.skip("set BT_HID_SN_SLOT_A/_B to two distinct bonded slots for the non-clobber test")
    serial_cli.call("connect", {"slot": SLOT_A})
    net_cli.call("connect", {"slot": SLOT_B})
    nameA = _slot(serial_cli, SLOT_A).get("name")
    nameB = _slot(net_cli, SLOT_B).get("name")
    if not nameA or not nameB or nameA == nameB:
        pytest.skip("need two distinctly-named bonded slots (A and B) for the non-clobber test")
    # Each session claims its own slot; targeting is PER SESSION.
    serial_cli.target(SLOT_A)
    net_cli.target(SLOT_B)
    # ping reports the CALLING session's target slot -> each must see ITS OWN slot, not one shared one.
    sdev = serial_cli.ping().get("device")
    ndev = net_cli.ping().get("device")
    assert sdev == nameA, f"serial session's target clobbered: ping.device={sdev!r}, expected {nameA!r}"
    assert ndev == nameB, f"net session's target clobbered: ping.device={ndev!r}, expected {nameB!r}"
    assert sdev != ndev, "the two sessions collapsed to ONE global drive target (clobbered)"
    # Interleave drives: neither steals the other's target. (Only asserts sent>0 for slots that are
    # actually connected; the ping.device check above is the routing proof regardless of connection.)
    for _ in range(3):
        if _slot(serial_cli, SLOT_A).get("conn"):
            assert serial_cli.type_text("a").get("sent"), "serial->A drive lost after net targeted B"
        if _slot(net_cli, SLOT_B).get("conn"):
            assert net_cli.type_text("b").get("sent"), "net->B drive lost after serial targeted A"
        # targets stay independent across the interleave
        assert serial_cli.ping().get("device") == nameA and net_cli.ping().get("device") == nameB, \
            "a session's target drifted during interleaved driving"

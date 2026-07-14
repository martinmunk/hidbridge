"""DEVICE-GATED live test: a per-adapter ble_hid daemon isolates + cleans up.

Proves on REAL hardware what the offline tests can only mock: a daemon pinned to a spare dongle
gets its OWN per-adapter control socket + per-adapter advertised name (wsb-hid-<hciN>), the CLI
resolves that socket, and a SIGTERM (systemctl stop / timeout / pkill) shuts it down CLEANLY with
NO leftover socket. This is the regression guard for the two bugs found live 2026-07-10: the shared
socket/name (multi-daemon collision) and the SIGTERM socket leak.

Gated: set BT_HID_LIVE_ADAPTER=hciN to a SPARE dongle you may bring up/down (NEVER the built-in,
NEVER a dongle with a live iPad session). Requires root (BlueZ adapter config). Skips otherwise, so
`pytest tests/bt_hid/` stays green on any host.

    sudo BT_HID_LIVE_ADAPTER=hci2 python3 -m pytest tests/bt_hid/test_bt_hid_multi_daemon_live.py -v
"""

import os
import pathlib
import subprocess
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

ADAPTER = os.environ.get("BT_HID_LIVE_ADAPTER")
BT_HID = os.environ.get("HIDBRIDGE_BIN", "hidbridge")

pytestmark = [
    pytest.mark.skipif(not ADAPTER, reason="set BT_HID_LIVE_ADAPTER=hciN (a spare dongle) to run"),
    pytest.mark.skipif(os.geteuid() != 0, reason="needs root for BlueZ adapter config"),
]


def _slug_sock():
    from hidbridge import naming as N
    return N.control_sock(ADAPTER)


@pytest.fixture
def daemon():
    """Bring the adapter up, launch the daemon, yield its Popen, then SIGTERM + restore DOWN."""
    sock = _slug_sock()
    try:
        os.remove(sock)
    except OSError:
        pass
    subprocess.run(["hciconfig", ADAPTER, "up"], check=False)
    proc = subprocess.Popen(["/usr/bin/python3", BT_HID, "--adapter", ADAPTER, "pair-mode"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    # wait up to 8s for the socket to appear (daemon ready)
    for _ in range(32):
        if os.path.exists(sock):
            break
        if proc.poll() is not None:
            pytest.fail(f"daemon exited early: {proc.stdout.read()}")
        time.sleep(0.25)
    yield proc, sock
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    subprocess.run(["hciconfig", ADAPTER, "down"], check=False)
    try:
        os.remove(sock)
    except OSError:
        pass


def test_per_adapter_socket_and_name(daemon):
    proc, sock = daemon
    assert os.path.exists(sock), "daemon did not create its per-adapter control socket"
    # CLI status must resolve THIS adapter's socket and report the per-adapter name.
    out = subprocess.run(["/usr/bin/python3", BT_HID, "--adapter", ADAPTER, "status"],
                         capture_output=True, text=True).stdout
    assert f"daemon UP on {ADAPTER}" in out
    assert f"wsb-hid-{ADAPTER}" in out


def test_sigterm_cleans_up_socket(daemon):
    proc, sock = daemon
    assert os.path.exists(sock)
    proc.terminate()  # SIGTERM
    proc.wait(timeout=6)
    assert not os.path.exists(sock), "SIGTERM leaked the control socket (should be unlinked on stop)"

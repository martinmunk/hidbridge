"""Device-gated: the BLE ADVERTISED name tracks the board's naming state (idle / paired / renamed).

The HW-in-loop check for the NAMING surface -- the gap that let the board report a name it could not
actually advertise. It drives the board through states with the Mode1 client and, at each state, scans
with a USB dongle as central (hidbridge.name_probe) and asserts the OVER-THE-AIR Complete Local Name
equals what the board REPORTS it advertises. It also asserts an over-budget label is rejected (the
31-byte advertising limit), and -- best effort, since LE-connect stalls on this host -- reads the GAP
Device Name characteristic (0x2A00) and checks it agrees.

Why this exists: test_bt_hid_central_selftest.py verifies the report-map + notification BYTES over BLE
but never the NAME; the name was guarded only by source-parsing. This closes that loop. Scanning works
on this host even where LE auto-connect stalls (docs/bt-hid-central-selftest-status.md), so the
advertised-name assertions actually run; the 0x2A00 read is best-effort.

Gated -- SKIPPED unless ALL of:
  * BT_HID_NAMING=1
  * a reachable board: BT_HID_NET_HOST=<host>  OR  BT_HID_ESP32_PORT=/dev/ttyUSBx
  * a scanning adapter: BT_HID_CENTRAL_ADAPTER=hciN (a USB dongle; the built-in also scans fine)
  * BlueZ scanning works (system python w/ dbus; usually root). BT_HID_SYS_PY overrides the interpreter.
Run:
  BT_HID_NAMING=1 BT_HID_NET_HOST=192.0.2.102 BT_HID_CENTRAL_ADAPTER=hci4 \
    sudo -E python -m pytest tests/test_bt_hid_naming_live.py
"""

import json
import os
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BT_HID_NAMING") != "1",
    reason="hardware-gated: set BT_HID_NAMING=1 (+ a board + BT_HID_CENTRAL_ADAPTER + a BLE dongle)")

SYS_PY = os.environ.get("BT_HID_SYS_PY", "/usr/bin/python3")
ADAPTER = os.environ.get("BT_HID_CENTRAL_ADAPTER")
SCAN_SECONDS = float(os.environ.get("BT_HID_NAMING_SCAN_SECONDS", "12"))


def _open_client():
    from hidbridge import rpc
    host = os.environ.get("BT_HID_NET_HOST")
    if host:
        return rpc.open_client("net:" + host)
    port = os.environ.get("BT_HID_ESP32_PORT")
    if port:
        return rpc.open_client("esp32:" + port)
    pytest.skip("no board: set BT_HID_NET_HOST or BT_HID_ESP32_PORT")


def _hex_to_addr(h: str) -> str:
    """'aabbccddee03' -> 'AA:BB:CC:DD:EE:03' (the BlueZ address form)."""
    return ":".join(h[i:i + 2] for i in range(0, 12, 2)).upper()


def _adv_slot(client):
    """The identity slot currently being advertised, as its identities dict entry."""
    ids = client.identities()
    adv = ids.get("adv", 0)
    for s in ids["slots"]:
        if s["slot"] == adv:
            return s
    return ids["slots"][0]


def _scan_name(address: str, read_gap: bool = False) -> dict:
    """Shell out to the BLE name probe (system python) and return its verdict dict."""
    cmd = [SYS_PY, "-m", "hidbridge.name_probe", "--central", ADAPTER,
           "--address", address, "--seconds", str(SCAN_SECONDS), "--json"]
    if read_gap:
        cmd.append("--read-gap")
    env = dict(os.environ)
    # make the package importable by the system interpreter (mirrors central.py's shell-out)
    env["PYTHONPATH"] = os.pathsep.join(
        [os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/src",
         env.get("PYTHONPATH", "")])
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=SCAN_SECONDS + 30)
    out = proc.stdout.strip().splitlines()
    if not out:
        pytest.fail(f"name_probe produced no output: stderr={proc.stderr!r}")
    return json.loads(out[-1])


def _assert_advertised(client, expected_reported_name: str):
    """Scan the currently-advertised identity and assert the OTA name == what the board reports."""
    slot = _adv_slot(client)
    assert slot["name"] == expected_reported_name, (
        f"board reports slot name {slot['name']!r}, expected {expected_reported_name!r}")
    v = _scan_name(_hex_to_addr(slot["addr"]))
    if v.get("status") != "ok":
        pytest.skip(f"board not seen advertising {slot['addr']} ({v.get('status')}: held by a device, "
                    f"parked, or the adapter cannot scan) -- verdict {v}")
    assert v["adv_name"] == slot["name"], (
        f"OTA advertised name {v['adv_name']!r} != the name the board reports {slot['name']!r} "
        f"-- the board is broadcasting a name that disagrees with its state")
    return slot


@pytest.mark.skipif(not ADAPTER, reason="set BT_HID_CENTRAL_ADAPTER=hciN (a scanning adapter)")
def test_advertised_name_tracks_naming_state():
    c = _open_client()
    try:
        if not c.supports("connect"):
            pytest.skip("board lacks the 'connect'/slots capability")
        # need a FREE slot (no label, no bond) to drive without disturbing a real device
        ids = c.identities()
        free = next((s for s in ids["slots"] if not s["client"] and not s["bonded"]), None)
        if free is None:
            pytest.skip("no free identity slot to test with (all bonded/labelled)")
        slot_n = free["slot"]

        # 1) IDLE -- advertise the base name and assert the OTA name matches (connect BY SLOT)
        c.call("connect", {"slot": slot_n})
        base = _assert_advertised(c, _adv_slot(c)["name"])
        assert "-" in base["name"] and base["name"].startswith("HIDBRIDGE-")

        # 2) PAIRED with a short label -- OTA name must gain the label
        label = "pytnm"
        res = c.pair(label)
        assert res["name"].endswith("-" + label)
        _assert_advertised(c, res["name"])

        # 3) an OVER-BUDGET label is rejected (would exceed the 31-byte advert), not silently mis-advertised
        from hidbridge import rpc
        c.call("unpair", {"slot": res["slot"]})
        with pytest.raises(rpc.RpcError) as ei:
            c.pair("thislabeliswaytoolongforble")
        assert "too long" in str(ei.value).lower()

        # 4) restore the base advertisement and re-verify
        c.call("connect", {"slot": slot_n})
        _assert_advertised(c, _adv_slot(c)["name"])
    finally:
        # best-effort cleanup: clear the test label + re-advertise slot 0
        try:
            c.call("unpair", {"slot": 0})
            c.call("connect", {"slot": 0})
        except Exception:  # noqa: BLE001
            pass
        c.close()


@pytest.mark.skipif(not ADAPTER, reason="set BT_HID_CENTRAL_ADAPTER=hciN (a scanning adapter)")
def test_gap_name_matches_advertised_name_best_effort():
    """After connecting, the GAP Device Name (0x2A00) should equal the advertised name (the fix for
    the two-names bug). Best-effort: on a host where LE-connect stalls, this SKIPS rather than fails."""
    c = _open_client()
    try:
        slot = _adv_slot(c)
        v = _scan_name(_hex_to_addr(slot["addr"]), read_gap=True)
        if v.get("status") != "ok":
            pytest.skip(f"board not advertising ({v.get('status')})")
        gap = v.get("gap") or {}
        if gap.get("status") != "ok":
            pytest.skip(f"could not read GAP 0x2A00 (LE-connect stall on this host): {gap}")
        assert gap["gap_name"] == v["adv_name"] == slot["name"], (
            f"GAP name {gap['gap_name']!r} != advertised {v['adv_name']!r} != reported {slot['name']!r}")
    finally:
        c.close()

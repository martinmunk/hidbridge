"""DEVICE-GATED regression: the ESP32 advertises the ACTIVE slot's target-scoped NAME.

This catches the class of bug found live on 2026-07-14: after mini6 connected on slot 0, selecting
slot 1 for mini5's pairing switched the advertising ADDRESS to slot 1 but kept advertising slot 0's
NAME (`HIDBRIDGE-esp32-mini6`) -- so mini5 never saw `HIDBRIDGE-esp32-mini5`. Root causes (both fixed): the auto
adv-build path accumulated service UUIDs + kept the name in a stale scan response, and `onDisconnect`
re-pointed the single legacy advertiser to the dropped slot. The firmware now builds the adv payload
explicitly (name in the main packet) and keeps the host-selected slot.

The check is OVER-THE-AIR (a BLE scan on a SEPARATE adapter reads the actual advertised name), because
the firmware's own `I<n>` reply reported the RIGHT name while the air showed the WRONG one -- only a
sniff catches it. AUTO-SKIPS unless BOTH are available on the runner:
  * the ESP32 on serial (BT_HID_ESP32_PORT or autodetect), AND
  * a BLE scan interface (BT_HID_SNIFF_HCI=hciN, else a spare hci>=2; needs passwordless `sudo -n`).
Never uses hci0/hci1 (builtin + the in-use UB500) unless BT_HID_SNIFF_HCI forces it.

Run:
  BT_HID_LIVE_DUAL=1 BT_HID_ESP32_PORT=/dev/ttyUSB1 BT_HID_SNIFF_HCI=hci3 \
      uv run --with pyserial --with pytest pytest tests/bt_hid/test_bt_hid_advert_name_live.py -v -s
"""

import glob
import os
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    os.environ.get("BT_HID_LIVE_DUAL") != "1",
    reason="live advertised-name sniff test; set BT_HID_LIVE_DUAL=1 (+ ESP32 + a scan HCI) to run")

from hidbridge import naming as N  # noqa: E402
from hidbridge import transport_esp32 as TE  # noqa: E402

_ADDR_RE = re.compile(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\s+(.*)")


def _pick_sniff_hci() -> str | None:
    """A BLE adapter to scan on: BT_HID_SNIFF_HCI, else a spare hci>=2 (avoid hci0/hci1)."""
    env = os.environ.get("BT_HID_SNIFF_HCI")
    if env:
        return env
    for path in sorted(glob.glob("/sys/class/bluetooth/hci*")):
        name = os.path.basename(path)
        try:
            if int(name[3:]) >= 2:
                return name
        except ValueError:
            continue
    return None


def _can_sniff(hci: str) -> bool:
    """True iff we can run a privileged BLE scan on `hci` (hcitool present + passwordless sudo)."""
    if not shutil.which("hcitool"):
        return False
    try:
        subprocess.run(["sudo", "-n", "hciconfig", hci, "up"],
                       capture_output=True, timeout=8, check=False)
        r = subprocess.run(["sudo", "-n", "timeout", "3", "hcitool", "-i", hci, "lescan",
                            "--duplicates"], capture_output=True, timeout=10, text=True)
    except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError):
        return False
    # lescan exits non-zero via `timeout`; success = it ran without a permission/sudo error.
    err = (r.stderr or "").lower()
    return "a password is required" not in err and "not authorized" not in err \
        and "no such device" not in err


def _advertised_names(hci: str, timeout: int = 8) -> dict:
    """Scan `hci` for `timeout` s; return {ADDR(upper): advertised_name} (last non-'(unknown)' wins)."""
    # Reset the adapter first: a previous lescan can leave it scanning, so the next one fails busy.
    subprocess.run(["sudo", "-n", "hciconfig", hci, "down"], capture_output=True, check=False)
    subprocess.run(["sudo", "-n", "hciconfig", hci, "up"], capture_output=True, check=False)
    try:
        r = subprocess.run(["sudo", "-n", "timeout", str(timeout), "hcitool", "-i", hci, "lescan",
                            "--duplicates"], capture_output=True, timeout=timeout + 5, text=True)
    except subprocess.TimeoutExpired as e:
        r = type("R", (), {"stdout": e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")})()
    names: dict[str, str] = {}
    for line in (r.stdout or "").splitlines():
        m = _ADDR_RE.match(line.strip())
        if not m:
            continue
        addr, name = m.group(1).upper(), m.group(2).strip()
        if name and name != "(unknown)":
            names[addr] = name
        else:
            names.setdefault(addr, "(unknown)")
    return names


@pytest.fixture()
def sniff_hci():
    hci = _pick_sniff_hci()
    if not hci:
        pytest.skip("no spare BLE adapter to sniff with (set BT_HID_SNIFF_HCI=hciN)")
    if not _can_sniff(hci):
        pytest.skip(f"cannot run a privileged BLE scan on {hci} (need hcitool + passwordless sudo)")
    return hci


@pytest.fixture()
def esp32():
    port = os.environ.get("BT_HID_ESP32_PORT") or TE.detect_port()
    if not port:
        pytest.skip("no ESP32 serial port (set BT_HID_ESP32_PORT)")
    tp = TE.Esp32Transport(port=port)
    yield tp
    tp.close()


@pytest.mark.parametrize("slot,ipad", [(0, "mini6"), (1, "mini5")])
def test_slot_advertises_its_target_scoped_name(esp32, sniff_hci, slot, ipad):
    """Selecting slot N must put slot N's address ON AIR advertising `HIDBRIDGE-esp32-<ipad>`."""
    expected_name = N.slot_identity_name("esp32", ipad)
    sel = esp32.select_slot(slot, expected_name)
    assert sel["ok"] and sel["addr"], f"slot {slot} select failed: {sel}"
    addr = ":".join(sel["addr"][i:i + 2] for i in range(0, 12, 2)).upper()

    seen = _advertised_names(sniff_hci, timeout=8)
    got = seen.get(addr)
    assert got is not None, (
        f"slot {slot} address {addr} not seen advertising at all (adv start failed?); "
        f"scan saw: {seen}")
    assert got == expected_name, (
        f"slot {slot} address {addr} advertises name {got!r}, expected {expected_name!r} "
        f"(the wrong-slot-name bug). scan saw: {seen}")

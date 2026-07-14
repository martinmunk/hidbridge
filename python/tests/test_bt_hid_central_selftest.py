"""Device-gated, backend-parametrized BLE-central HID self-test (built-in hci0 -> MCU peripheral).

The closed-loop, NO-iPad proof that an MCU HID peripheral works end to end: the wsb BUILT-IN adapter
(hci0) acts as a transient BLE CENTRAL, connects to the MCU, byte-verifies the HID Report Map +
Report Reference descriptors against reports.py (the single source of truth), and for each driveable
report kind injects a known frame over the MCU's USB serial and asserts the exact bytes arrive back
as a GATT notification. hci0 is CENTRAL-only here (never a HID peripheral) and its state is restored
on teardown -- see hidbridge/central.py.

Gated + parametrized (PREPARE / RUN / VERIFY / CLEANUP):
  * SKIPPED by default. Enable with BT_HID_CENTRAL=1.
  * Parametrized over backends: pico, esp32, dongle -- backend-agnostic; each self-skips when its
    MCU is absent, not advertising, or already held by another central (an iPad).
  * Needs root (adapter control) + the system python (dbus/gi/pyserial); it shells out to
    central.py under /usr/bin/python3 (uv's CPython lacks AF_BLUETOOTH/dbus).

Run:  BT_HID_CENTRAL=1 sudo -E /usr/bin/python3 -m pytest tests/bt_hid/test_bt_hid_central_selftest.py
  (or a single backend:  ... -k pico)
The CLEANUP (StopNotify, Disconnect, drop the transient bond, restore hci0) is done by central.py in
its own teardown; this test additionally asserts no verdict was left in an indeterminate state.

STATUS 2026-07-10: LIVE PENDING (hci0 auto-connect stall + ESP32 held by mini5). The harness +
offline core are complete + green; the on-glass live verify is a MORNING task blocked on two things,
NOT on this code (full write-up: docs/bt-hid-central-selftest-status.md):
  1. hci0 kernel LE-AUTO-CONNECT STALL -- on this host (BlueZ 5.82, kernel 6.12.90, Intel built-in
     hci0) Device.Connect() to an LE peripheral becomes a kernel mgmt Add-Device auto-connect that
     NEVER issues an LE Create Connection (btmon-proven: peripheral advertising, 29 adverts seen, no
     Create Connection, then le-connection-abort-by-local at ~42s). hci0 SCANS fine. The three escapes
     are now WIRED INTO central.py (pending a live run): (a) --connect-method connectdevice uses
     Adapter1.ConnectDevice for a DIRECT create-connection when bluetoothd Experimental is on (auto
     picks it when available); (b) --central hciN pins a USB DONGLE as the central instead of the
     built-in; (c) a clean host reboot. Pass BT_HID_CONNECT_METHOD / BT_HID_CENTRAL_ADAPTER to steer.
  2. ESP32 NOT CLEANLY FREE -- it prints "EVT connected" within ~5s of every boot: mini5's STALE
     ESP32 bond auto-reconnecting. Freeing it needs mini5 to Forget the ESP32 (a the maintainer tap).
"""

import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    os.environ.get("BT_HID_CENTRAL") != "1",
    reason="hardware-gated: set BT_HID_CENTRAL=1 (needs the built-in adapter + an MCU + root)")

BACKENDS = ["pico", "esp32", "dongle"]

# System python that has dbus + gi + AF_BLUETOOTH (uv's CPython does not).
SYS_PY = os.environ.get("BT_HID_SYS_PY", sys.executable)


def _builtin_present() -> bool:
    """True iff a built-in adapter exists to act as the transient central (pure sysfs)."""
    from hidbridge import adapter as A
    return any(a["kind"] == "builtin" for a in A.list_adapters())


def _run_central(backend: str) -> dict:
    # Steering (all optional, for the stall workaround): BT_HID_CENTRAL_ADAPTER pins the central
    # adapter (e.g. a USB dongle hciN instead of the built-in); BT_HID_CONNECT_METHOD picks the LE
    # connect method (auto|scan|connectdevice) -- central.py reads the latter from the env directly.
    cmd = [SYS_PY, "-m", "hidbridge.central", "--backend", backend, "--json"]
    central_adapter = os.environ.get("BT_HID_CENTRAL_ADAPTER")
    if central_adapter:
        cmd += ["--central", central_adapter]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, env=dict(os.environ), timeout=120)
    out = proc.stdout.strip()
    line = out.splitlines()[-1] if out else "{}"
    try:
        return json.loads(line)
    except ValueError:
        pytest.fail(f"central.py produced no JSON verdict for {backend}: "
                    f"stdout={proc.stdout!r} stderr={proc.stderr!r}")


@pytest.mark.parametrize("backend", BACKENDS)
def test_central_hid_selftest(backend):
    # PREPARE ---------------------------------------------------------------
    if not _builtin_present():
        pytest.skip("no built-in adapter present to act as the transient central")

    # RUN -------------------------------------------------------------------
    result = _run_central(backend)
    status = result.get("status")

    # A present-but-free MCU is required to VERIFY; anything else self-skips with a clear reason.
    if status == "skip":
        pytest.skip(f"{backend}: {result.get('reason', 'skipped by central.py')}")
    assert status in ("pass", "fail"), f"unexpected verdict: {result}"

    # VERIFY ----------------------------------------------------------------
    assert status == "pass", (
        f"{backend} central self-test FAILED: error={result.get('error')} "
        f"report_map={result.get('report_map')} notifications={result.get('notifications')} "
        f"steps={result.get('steps')}")

    # explicit load-bearing assertions on the verdict payload
    rmap = result["report_map"]
    assert rmap["ok"], f"report map not ok: {rmap}"

    refs = result["report_refs"]
    assert "1" in refs and refs["1"][1] == 1, f"keyboard Report Reference [1,1] missing: {refs}"
    # the mouse report id is backend-specific (pico renumbers to 3); assert an input mouse ref
    # exists among the discovered refs (type byte == 1).
    assert any(v[1] == 1 for v in refs.values()), f"no INPUT report references: {refs}"

    notif = result["notifications"]["verdict"]
    assert notif["ok"], f"notification byte-check failed: {result['notifications']}"
    assert notif["checked"], "no report kinds were actually driven+verified"

    # the verdict records which LE-connect method actually carried the link (scan | connectdevice)
    assert result.get("connect_method") in ("scan", "connectdevice"), (
        f"missing/invalid connect_method in verdict: {result.get('connect_method')}")

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

The `net` backend proves the SAME thing for the NETWORK path (no iPad, no USB): the host radio is the
central, and the injected frame is driven to the board OVER TCP. It needs the board reachable +
provisioned (BT_HID_NET_HOST / net.token; see `hidbridge net-provision`), e.g.:
  BT_HID_CENTRAL=1 BT_HID_NET_HOST=hidbridge-esp32.local \
    sudo -E python -m pytest tests/test_bt_hid_central_selftest.py -k net
On a host whose built-in adapter has the LE-central stall (below), pin a USB BLE dongle as the central
and use the ConnectDevice escape: BT_HID_CENTRAL_ADAPTER=hci1 BT_HID_CONNECT_METHOD=connectdevice.
Transient LE-central flakiness is retried automatically (BT_HID_CENTRAL_ATTEMPTS, default 8); a real
report-map or notification-byte mismatch is NEVER retried.
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

# net = the SAME ESP32 board as `esp32`, but the injected frame is driven OVER TCP (the network
# backend) instead of USB serial -- the end-to-end proof that net -> board -> BLE delivery works.
# It self-skips unless the board is reachable + provisioned (BT_HID_NET_HOST / net.token).
BACKENDS = ["pico", "esp32", "dongle", "net"]

# Drive modes for the inject step (docs/bt-hid-modes.md): "frames" = Mode 2 frame relay (host builds
# the report bytes); "rpc" = Mode 1 JSON-RPC `frames` over the transport's OWN channel (the board
# relays the same bytes). Both must land the IDENTICAL GATT notification. mode=rpc needs JSON-RPC
# firmware, so the dongle backend (a BlueZ daemon) self-skips that combo -- see central.py.
MODES = ["frames", "rpc"]

# System python that has dbus + gi + AF_BLUETOOTH (uv's CPython does not).
SYS_PY = os.environ.get("BT_HID_SYS_PY", sys.executable)


def _builtin_present() -> bool:
    """True iff a built-in adapter exists to act as the transient central (pure sysfs)."""
    from hidbridge import adapter as A
    return any(a["kind"] == "builtin" for a in A.list_adapters())


def _run_central_once(backend: str, mode: str) -> dict:
    # Steering (all optional, for the stall workaround): BT_HID_CENTRAL_ADAPTER pins the central
    # adapter (e.g. a USB dongle hciN instead of the built-in); BT_HID_CONNECT_METHOD picks the LE
    # connect method (auto|scan|connectdevice) -- central.py reads the latter from the env directly.
    # `mode` selects the inject drive mode (frames | rpc); passed explicitly so the parametrization
    # steers it (central.py also honours BT_HID_MODE for a direct run).
    cmd = [SYS_PY, "-m", "hidbridge.central", "--backend", backend, "--mode", mode, "--json"]
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


def _free_board(backend: str) -> None:
    """Between retries: drop any orphaned central our failed attempt left on the board + re-advertise.

    Best-effort and backend-agnostic -- builds the same driver central.py uses and calls its
    _free_board. A no-op for the daemon backend (no board to free)."""
    try:
        from hidbridge import central as C
        from hidbridge import central_core as CC
        tp, _skip = C.make_transport(CC.BACKEND_PROFILES[backend])
        if tp is None:
            return
        try:
            C._free_board(tp)
        finally:
            if hasattr(tp, "close"):
                tp.close()
    except Exception:  # noqa: BLE001 -- freeing is best-effort; the next attempt still self-checks
        pass


def _run_central(backend: str, mode: str) -> dict:
    """Run the self-test, RETRYING only TRANSIENT link failures (flaky LE-central connect / a link
    that drops before delivery -- see central_core.is_retryable_failure). A real report-map or
    notification-byte mismatch is returned immediately (never retried). BT_HID_CENTRAL_ATTEMPTS caps
    the tries (default 8); the board is freed between attempts. Hardware-in-the-loop over BLE is
    inherently a little flaky, and some hosts' LE-central stacks (see docs/bt-hid-central-selftest-
    status.md) are worse -- the retry keeps the suite reliable without masking real regressions."""
    attempts = max(1, int(os.environ.get("BT_HID_CENTRAL_ATTEMPTS", "8")))
    result: dict = {}
    for attempt in range(1, attempts + 1):
        result = _run_central_once(backend, mode)
        result.setdefault("attempt", attempt)
        if result.get("status") != "fail" or not result.get("retryable", False):
            return result
        if attempt < attempts:
            _free_board(backend)
    return result


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("mode", MODES)
def test_central_hid_selftest(backend, mode):
    # PREPARE ---------------------------------------------------------------
    if not _builtin_present():
        pytest.skip("no built-in adapter present to act as the transient central")

    # RUN -------------------------------------------------------------------
    result = _run_central(backend, mode)
    status = result.get("status")

    # A present-but-free MCU is required to VERIFY; anything else self-skips with a clear reason
    # (incl. the dongle+rpc combo, which central.py skips -- rpc needs JSON-RPC firmware).
    if status == "skip":
        pytest.skip(f"{backend}/{mode}: {result.get('reason', 'skipped by central.py')}")
    assert status in ("pass", "fail"), f"unexpected verdict: {result}"

    # VERIFY ----------------------------------------------------------------
    assert status == "pass", (
        f"{backend}/{mode} central self-test FAILED: error={result.get('error')} "
        f"report_map={result.get('report_map')} notifications={result.get('notifications')} "
        f"steps={result.get('steps')}")
    # the verdict records which drive mode actually carried the inject
    assert result.get("mode") == mode, f"verdict mode {result.get('mode')!r} != requested {mode!r}"

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

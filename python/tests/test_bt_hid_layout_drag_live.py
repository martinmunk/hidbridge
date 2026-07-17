"""Hardware-in-the-loop BLE verification of two behaviours the offline suite can only check structurally.

A transient BLE CENTRAL (a USB dongle or the built-in adapter) connects to the MCU peripheral,
subscribes to its HID input reports, and byte-verifies the GATT notifications that arrive OVER THE AIR
while the board is driven over its OWN channel (serial or net) with Mode 1 JSON-RPC. This reuses the
central self-test machinery in hidbridge/central.py (connect-as-central + bond + subscribe + capture),
extended to capture an ORDERED SEQUENCE of notifications and to drive an arbitrary Mode 1 command
(see central.SCENARIOS + central_core.sequence_verdict).

What each test PROVES (that reports.py alone cannot, offline):
  * test_danish_ae_over_ble -- the Danish keyboard layout actually delivers the right SCANCODE on the
    wire. The board's target slot is set to layout 'da', then `type "ae"` (the real char U+00E6) is
    driven; the captured keyboard (report id 1) notifications must be the Danish 'ae' key:
    down = 00 00 33 00 00 00 00 00 (mod 0, usage 0x33), then up = all zero. A US-layout regression
    would put a different usage on the wire and fail loudly.
  * test_hold_and_drag_over_ble -- a HELD mouse button survives motion (click-and-drag). press-left,
    move(20,10), release-left are driven; the captured mouse (report id 2) notifications must be
    [01 00 00 00][01 14 0a 00][00 00 00 00]. The MIDDLE frame (buttons byte still 0x01 during the
    move) is the load-bearing assertion: a regression where move clears the buttons would give
    00 14 0a 00 and must fail.

The expected bytes are built HERE from reports.py (the single source of truth) and additionally
hard-asserted (usage 0x33 / mod 0; the held-button move frame == [0x01,20,10,0]) so neither a firmware
drift nor a reports.py change can slip through. central_core pins the same expectations as pure data
(danish_ae_expected / hold_drag_expected), unit-tested offline in test_bt_hid_central_core.py.

PRECONDITION: the board must be flashed with the CURRENT firmware -- per-slot `layout` (Mode 1) and
the press/release drag commands. If the board rejects a command (older firmware), central.py returns a
SKIP, not a failure.

Gating (all must hold or the test SKIPS with a clear reason -- never a false failure):
  * BT_HID_CENTRAL=1              enable the hardware-in-the-loop central (needs an adapter + root)
  * a reachable + FREE board:     net via BT_HID_NET_HOST (+ net.token / BT_HID_NET_TOKEN), or serial
                                  via BT_HID_ESP32_PORT (else autodetected)
  * a central adapter:            BT_HID_CENTRAL_ADAPTER=hciN pins a USB dongle as the central (the
                                  built-in hci0 LE auto-connect stall workaround); default = built-in
A board that is busy/held by another central, or an LE connect that stalls, is a retryable SKIP; a
real notification-byte MISMATCH is never retried -- it FAILS.

Run (system python -- dbus + gi; a USB dongle central sidesteps the hci0 stall):
  BT_HID_CENTRAL=1 BT_HID_CENTRAL_ADAPTER=hci1 BT_HID_CONNECT_METHOD=connectdevice \
    BT_HID_NET_HOST=hidbridge-esp32.local \
    sudo -E /usr/bin/python3 -m pytest tests/test_bt_hid_layout_drag_live.py
  (or serial:  BT_HID_CENTRAL=1 BT_HID_CENTRAL_ADAPTER=hci1 BT_HID_ESP32_PORT=/dev/ttyUSB1 \
                 sudo -E /usr/bin/python3 -m pytest tests/test_bt_hid_layout_drag_live.py)
"""

import json
import os
import subprocess
import sys

import pytest

from hidbridge import central_core as CC  # noqa: E402
from hidbridge import reports as R  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("BT_HID_CENTRAL") != "1",
    reason="hardware-gated: set BT_HID_CENTRAL=1 (needs a BLE adapter as central + a free MCU + root)")

# System python that has dbus + gi (uv's CPython does not); central.py runs there.
SYS_PY = os.environ.get("BT_HID_SYS_PY", sys.executable)

AE_CHAR = "æ"   # U+00E6 LATIN SMALL LETTER AE -- the real Danish 'ae' character


def _pick_backend() -> str | None:
    """Choose a board driver: net if a host is configured, else the ESP32 over serial, else None.

    Pure/offline-safe: BT_HID_NET_HOST / BT_HID_ESP32_PORT are env, and detect_port only walks sysfs
    (no hardware import). Returns None if no board is configured -> the caller SKIPS.
    """
    if os.environ.get("BT_HID_NET_HOST"):
        return "net"
    from hidbridge.transport_esp32 import detect_port
    if os.environ.get("BT_HID_ESP32_PORT") or detect_port():
        return "esp32"
    return None


def _run_scenario_once(backend: str, scenario: str) -> dict:
    cmd = [SYS_PY, "-m", "hidbridge.central", "--backend", backend,
           "--mode", "rpc", "--scenario", scenario, "--json"]
    central_adapter = os.environ.get("BT_HID_CENTRAL_ADAPTER")
    if central_adapter:
        cmd += ["--central", central_adapter]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ), timeout=150)
    out = proc.stdout.strip()
    line = out.splitlines()[-1] if out else "{}"
    try:
        return json.loads(line)
    except ValueError:
        pytest.fail(f"central.py produced no JSON verdict for {backend}/{scenario}: "
                    f"stdout={proc.stdout!r} stderr={proc.stderr!r}")


def _free_board(backend: str) -> None:
    """Between retries: drop any orphaned central our failed attempt left + re-advertise (best-effort)."""
    try:
        from hidbridge import central as C
        tp, _skip = C.make_transport(CC.BACKEND_PROFILES[backend])
        if tp is None:
            return
        try:
            C._free_board(tp)
        finally:
            if hasattr(tp, "close"):
                tp.close()
    except Exception:  # noqa: BLE001 -- freeing is best-effort; the next attempt self-checks anyway
        pass


def _run_scenario(backend: str, scenario: str) -> dict:
    """Run the scenario, RETRYING only TRANSIENT link failures (see central_core.is_retryable_failure).

    A real notification-byte mismatch (a frame ARRIVED with wrong bytes) is returned immediately and is
    NEVER retried -- retrying would mask a regression. BT_HID_CENTRAL_ATTEMPTS caps the tries.
    """
    attempts = max(1, int(os.environ.get("BT_HID_CENTRAL_ATTEMPTS", "8")))
    result: dict = {}
    for attempt in range(1, attempts + 1):
        result = _run_scenario_once(backend, scenario)
        result.setdefault("attempt", attempt)
        if result.get("status") != "fail" or not result.get("retryable", False):
            return result
        if attempt < attempts:
            _free_board(backend)
    return result


def _drive_scenario_or_skip(scenario: str) -> dict:
    backend = _pick_backend()
    if backend is None:
        pytest.skip("no board configured: set BT_HID_NET_HOST (net) or BT_HID_ESP32_PORT (serial)")
    result = _run_scenario(backend, scenario)
    status = result.get("status")
    # A busy/held board, a stalled LE connect, an old firmware -> all self-skip with a clear reason.
    if status == "skip":
        pytest.skip(f"{backend}/{scenario}: {result.get('reason', 'skipped by central.py')}")
    assert status in ("pass", "fail"), f"unexpected verdict: {result}"
    return result


def _received_bodies(result: dict) -> list[bytes]:
    """The captured notification bodies (ORDERED) from central.py's JSON verdict, as bytes."""
    notif = result.get("notifications", {})
    return [bytes.fromhex(h) for h in notif.get("received", [])]


# --------------------------------------------------------------------------- #
# Test 1 -- Danish 'ae' over BLE (keyboard layout end-to-end)
# --------------------------------------------------------------------------- #
def test_danish_ae_over_ble():
    # Expected, from reports.py (single source of truth): typing the real 'ae' char on a 'da' layout.
    expected = [rpt.data for rpt in R.type_text(AE_CHAR, layout="da")]
    down, up = expected
    # Hard assertions so a US-layout regression fails loudly, independent of the byte compare below.
    assert down == bytes([0x00, 0x00, 0x33, 0, 0, 0, 0, 0]), f"unexpected 'ae' down report: {down.hex()}"
    assert down[2] == 0x33, f"'ae' usage must be 0x33 (Danish 'ae' key), got {down[2]:#04x}"
    assert down[0] == 0x00, f"'ae' modifier must be 0, got {down[0]:#04x}"
    assert up == bytes(8), f"'ae' up report must be all-zero, got {up.hex()}"
    # cross-check against the pure central_core expectation (also unit-tested offline)
    assert expected == CC.danish_ae_expected()

    # RUN over the air -----------------------------------------------------
    result = _drive_scenario_or_skip("danish-ae")

    # VERIFY the captured notification sequence, independently of central.py's own verdict.
    received = _received_bodies(result)
    v = CC.sequence_verdict(received, expected)
    assert v["ok"], (f"Danish 'ae' notification byte-check FAILED: {v['mismatches']} "
                     f"(received={[b.hex() for b in received]} expected={[b.hex() for b in expected]})")
    assert result["status"] == "pass", f"central.py verdict not pass: {result}"
    assert result["notifications"]["verdict"]["ok"], f"central.py notification verdict: {result}"


# --------------------------------------------------------------------------- #
# Test 2 -- hold-and-drag over BLE (a held button survives motion)
# --------------------------------------------------------------------------- #
def test_hold_and_drag_over_ble():
    # Expected, from reports.py: press left, move (20,10) WITH the button held, release.
    press = R.mouse_report(buttons=R.BTN_LEFT).data
    drag = R.mouse_report(buttons=R.BTN_LEFT, dx=20, dy=10).data
    release = R.mouse_report(buttons=0).data
    expected = [press, drag, release]
    # Load-bearing hard assertions: the move frame keeps the button DOWN (byte 0 == 0x01).
    assert press == bytes([0x01, 0, 0, 0]), f"unexpected press report: {press.hex()}"
    assert drag == bytes([0x01, 20, 10, 0]), f"drag move must keep the button held: {drag.hex()}"
    assert drag[0] == R.BTN_LEFT, f"drag move buttons byte must be 0x01 (held), got {drag[0]:#04x}"
    assert release == bytes([0x00, 0, 0, 0]), f"unexpected release report: {release.hex()}"
    # cross-check against the pure central_core expectation (also unit-tested offline)
    assert expected == CC.hold_drag_expected()

    # RUN over the air -----------------------------------------------------
    result = _drive_scenario_or_skip("hold-drag")

    # VERIFY: allow a firmware that pads the mouse body with trailing zero(s) (e.g. the Pico's pan
    # byte); esp32/net are exact. A cleared-buttons regression on the middle frame fails the compare.
    received = _received_bodies(result)
    v = CC.sequence_verdict(received, expected, allow_trailing_zero=True)
    assert v["ok"], (f"hold-and-drag notification byte-check FAILED: {v['mismatches']} "
                     f"(received={[b.hex() for b in received]} expected={[b.hex() for b in expected]})")
    assert result["status"] == "pass", f"central.py verdict not pass: {result}"
    assert result["notifications"]["verdict"]["ok"], f"central.py notification verdict: {result}"

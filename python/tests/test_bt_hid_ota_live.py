"""HARDWARE-GATED live proof: over-the-air (OTA) firmware update works BOTH directions.

The ESP32 net firmware exposes ArduinoOTA on UDP 3232, gated behind the net token (the token doubles
as the OTA password -- fail-closed, same secret as the TLS-PSK link). This test proves the whole OTA
loop end to end on the bench board, which is the ONLY way to know the board can be reflashed over the
LAN without a USB cable AND -- critically -- that a freshly OTA-flashed image can ITSELF be OTA-flashed
again (a firmware that broke its own OTA path would leave you stranded, reachable only over USB).

How the swap is verified: main.cpp carries a compile-time HB_FW_TAG (default "dev"), surfaced in the
`hello` handshake as `fw`. We build two candidates with DISTINCT tags, OTA-flash each, and read
`hello.fw` back over the net -- an image that did not actually replace the running firmware would still
report the old tag, so the assertion is a true swap check, not just "the flash tool exited 0".

Flow (each build is a real `pio run -e esp32-ethernet-kit`, ~1-2 min):
  0. baseline: read hello.fw (whatever is currently flashed).
  1. build candidate A (HB_FW_TAG="otaA..."), OTA-push it, wait for reboot+reconnect,
     assert hello.fw == "otaA..."   -> proves UPGRADE TO a candidate over the air.
  2. build candidate B (HB_FW_TAG="otaB..."), OTA-push it (this push runs against candidate A, so it
     exercises candidate A's OWN OTA path), wait+reconnect, assert hello.fw == "otaB..."
     -> proves OTA UPGRADE FROM the candidate (never stranded).

GATING (skips otherwise so the offline suite stays clean):
  BT_HID_OTA=1                    opt in to touching real hardware + reflashing the board
  BT_HID_ESP32_OTA_HOST=<ip/host>  the Ethernet board (falls back to BT_HID_NET_HOST)
  the net token via the usual loader (transport_net.load_token: BT_HID_NET_TOKEN or
  ~/.config/bt_hid/net.token) -- it is both the TLS-PSK and the OTA password.

WARNING: this test REFLASHES the board (leaving it on a test tag). The caller is expected to restore a
known-good build over USB afterward (`pio run -e esp32-ethernet-kit -t upload`). It does not touch any
bonded iPad; it only swaps firmware. Coordinate board use before running.
"""

import os
import pathlib
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
ESP32_DIR = ROOT / "firmware" / "esp32"
FW_BIN = ESP32_DIR / ".pio" / "build" / "esp32-ethernet-kit" / "firmware.bin"
BUILD_ENV = "esp32-ethernet-kit"

_OTA = os.environ.get("BT_HID_OTA") == "1"
_HOST = os.environ.get("BT_HID_ESP32_OTA_HOST") or os.environ.get("BT_HID_NET_HOST")

# Distinct, deterministic tags -- must differ from the default "dev" and from each other. Determinism
# keeps them safe to pass through a build flag (Date.now()/random would complicate the -D quoting) and
# makes a re-run idempotent.
TAG_A = "otaAlpha"
TAG_B = "otaBravo"

OTA_PORT = 3232
REBOOT_TIMEOUT = 45.0        # seconds to wait for the board to reboot + rejoin after an OTA push
POLL_INTERVAL = 2.0


def _espota_path():
    """Locate the espota.py bundled with the pioarduino Arduino-ESP32 framework."""
    base = pathlib.Path.home() / ".platformio" / "packages"
    for name in ("framework-arduinoespressif32",):
        cand = base / name / "tools" / "espota.py"
        if cand.exists():
            return cand
    # fall back to a glob in case the package dir is versioned
    hits = sorted(base.glob("framework-arduinoespressif32*/tools/espota.py"))
    if hits:
        return hits[0]
    return None


def _read_fw(token, timeout=6.0):
    """Read hello.fw over the net (None if the board is not answering yet)."""
    from hidbridge import mode1
    try:
        cli = mode1.open_client(f"net:{_HOST}", token=token, timeout=timeout)
    except Exception:
        return None
    try:
        return cli.hello().get("fw")
    except Exception:
        return None
    finally:
        try:
            cli.close()
        except Exception:
            pass


def _wait_for_fw(token, expected, timeout=REBOOT_TIMEOUT):
    """Poll hello.fw until it equals `expected` or timeout. Returns the last-seen fw value."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = _read_fw(token)
        if last == expected:
            return last
        time.sleep(POLL_INTERVAL)
    return last


def _build(tag):
    """`pio run -e esp32-ethernet-kit` with HB_FW_TAG=tag; returns the built firmware.bin path."""
    env = dict(os.environ)
    # Append our tag define to whatever build flags the env already carries. The nested quotes make it
    # a C string literal after the shell/scons strip one layer.
    extra = f'-DHB_FW_TAG=\'"{tag}"\''
    prev = env.get("PLATFORMIO_BUILD_FLAGS", "")
    env["PLATFORMIO_BUILD_FLAGS"] = (prev + " " + extra).strip()
    r = subprocess.run(["pio", "run", "-e", BUILD_ENV], cwd=str(ESP32_DIR),
                       env=env, capture_output=True, text=True, timeout=900)
    assert r.returncode == 0, f"pio build for tag {tag!r} failed:\n{r.stdout[-3000:]}\n{r.stderr[-2000:]}"
    assert FW_BIN.exists(), f"expected {FW_BIN} after build"
    return FW_BIN


def _ota_push(host, token, binpath):
    """OTA-flash binpath onto host via espota.py (auth = token). Raises on non-zero exit."""
    espota = _espota_path()
    assert espota is not None, "espota.py not found under ~/.platformio/packages/framework-arduinoespressif32"
    # espota.py is stdlib-only; run it under the SAME interpreter as this test (a bare `python` may
    # not be on PATH -- the venv exposes only `python3`/`sys.executable`).
    cmd = [sys.executable, str(espota), "-i", host, "-p", str(OTA_PORT),
           "-a", token, "-f", str(binpath), "-r", "-t", "30"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, f"espota push failed (exit {r.returncode}):\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}"
    return r.stdout


@pytest.mark.skipif(not (_OTA and _HOST),
                    reason="set BT_HID_OTA=1 + BT_HID_ESP32_OTA_HOST=<ip> (net token via "
                           "~/.config/bt_hid/net.token) to run the live OTA loop")
def test_ota_both_directions():
    from hidbridge import transport_net

    token = transport_net.load_token()
    assert token, ("no net token (OTA password); provision it with `hidbridge net-provision` -- OTA is "
                   "fail-closed and cannot proceed without it")

    # 0. baseline -- the board must be reachable before we start swapping firmware.
    baseline = _read_fw(token)
    assert baseline is not None, f"board at {_HOST} is not answering `hello` over the net; aborting OTA test"
    print(f"\n[ota-live] baseline hello.fw = {baseline!r} (host {_HOST})")
    assert TAG_A != baseline and TAG_B != baseline and TAG_A != TAG_B, "test tags must be distinct"

    # 1. UPGRADE TO candidate A over the air.
    _build(TAG_A)
    _ota_push(_HOST, token, FW_BIN)
    got = _wait_for_fw(token, TAG_A)
    assert got == TAG_A, (f"after OTA-flashing candidate A the board reports fw={got!r}, expected "
                          f"{TAG_A!r} -- the image did not swap (or the board did not rejoin)")
    print(f"[ota-live] after OTA #1: hello.fw = {got!r}  (upgraded TO candidate over the air)")

    # 2. OTA UPGRADE FROM candidate A -> candidate B. This push is served by candidate A's OWN OTA
    #    stack, so a candidate that broke OTA would fail HERE (proving you are never stranded).
    _build(TAG_B)
    _ota_push(_HOST, token, FW_BIN)
    got = _wait_for_fw(token, TAG_B)
    assert got == TAG_B, (f"after OTA-flashing candidate B the board reports fw={got!r}, expected "
                          f"{TAG_B!r} -- candidate A's own OTA path failed, or the board did not rejoin")
    print(f"[ota-live] after OTA #2: hello.fw = {got!r}  (OTA upgrade FROM the candidate works)")
    print("[ota-live] BOTH OTA directions verified. Restore a known-good build over USB afterward.")

#!/usr/bin/env python3
"""Live-verify the Pico 2 W BLE HID backend against a connected iPad, webcam-verified.

Sequence (proves keyboard + pointer end to end through the Pico over BLE HID):
  1. ping the Pico  -> require a BLE connection (PONG ... conn=1)
  2. Cmd+Space      -> open Spotlight   (GUI modifier registers)
  3. type <marker>  -> the marker appears in the Spotlight field  (alphanumerics register)
  4. move the pointer a few steps       (mouse path registers)
  5. grab a cropped webcam still of the iPad so the marker can be read on the glass.

The iPad under test is mini5 = the LEFT panel in the overhead C920 (that is where the Pico is
paired). Mirrors hidbridge/esp32_live_verify.py for the Pico backend. Uses the shared
HidController + reports layer, so the exact report BYTES match every other backend.

    uv run --with pyserial python hidbridge/pico_live_verify.py [--marker PICOHIDOK]
"""
from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = pathlib.Path(os.environ.get("HIDBRIDGE_CAPTURE_ROOT", str(HERE.parent.parent)))

from hidbridge.controller import HidController  # noqa: E402
from hidbridge.transport_pico import PicoSerialTransport, autodetect_port  # noqa: E402

# Overhead C920 crop for the LEFT iPad (mini5); see tools/webcam_ipad.sh header.
MINI5_CROP = "277:556:362:3"


def _webcam(out_path: pathlib.Path) -> bool:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "WEBCAM_CROP": MINI5_CROP}
    try:
        p = subprocess.run([str(REPO / "tools" / "webcam_ipad.sh"), "snap", str(out_path)],
                           env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=60)
        print(p.stdout.strip())
        return out_path.exists() and out_path.stat().st_size > 0
    except Exception as e:  # noqa: BLE001
        print(f"webcam grab failed: {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=os.environ.get("BT_HID_PICO_PORT"))
    ap.add_argument("--marker", default="PICOHIDOK")
    ap.add_argument("--outdir", default=None)
    args = ap.parse_args()

    port = args.port or autodetect_port()
    if not port:
        print("NO_PICO_PORT (flashed + plugged in, not in BOOTSEL?)")
        return 1
    tp = PicoSerialTransport(port=port)

    png = tp.ping()
    reply = png.get("reply") or ""
    print(f"PING: {reply!r}")
    connected = "conn=1" in reply
    if not connected:
        print("WARN: Pico reports NO BLE connection (conn!=1) -- the iPad may not be paired/connected. "
              "Continuing anyway so the failure is visible.")

    ctl = HidController(tp)

    print("Cmd+Space (open Spotlight):", ctl.send_key("space", 0x08))
    time.sleep(1.3)
    print(f"type {args.marker!r}:", ctl.type_text(args.marker))
    time.sleep(0.6)
    print("pointer move:", end=" ")
    for _ in range(6):
        ctl.move(25, 18)
        time.sleep(0.05)
    print("done")
    time.sleep(0.5)

    outdir = pathlib.Path(args.outdir) if args.outdir else (
        REPO / "captures" / f"pico_verify_{time.strftime('%Y%m%d_%H%M%S')}")
    frame = outdir / "pico_mini5_typed.jpg"
    ok = _webcam(frame)
    print(f"FRAME: {frame}  ({'ok' if ok else 'MISSING'})")
    print("VERDICT: read the frame -- the Spotlight field should show "
          f"'{args.marker}' on mini5 (left panel).")
    return 0 if (connected and ok) else 2


if __name__ == "__main__":
    raise SystemExit(main())

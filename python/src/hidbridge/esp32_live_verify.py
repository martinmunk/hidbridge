#!/usr/bin/env python3
"""Live ESP32-HID-backend validation on a paired iPad (keyboard + pointer, webcam-verified).

Holds ONE serial session open for the whole run: opening the port auto-resets the ESP32 (CP2102
DTR/RTS -> EN), which briefly drops the BLE link, so we open ONCE, wait for the bonded iPad to
auto-reconnect (serial "EVT connected"), then send the whole sequence over that single held link.
Every report frame the firmware notifies to a connected central replies "OK" (vs "OK noconn").

Sequence: wiggle+click the pointer -> Cmd+Space (open Spotlight = a focused text field) -> type a
distinctive string -> Cmd+H (home) -> absolute-pointer move/click (reportID 3) -> digitizer tap
(reportID 4). C920 webcam stills are grabbed at each step as visual ground truth (the on-glass image
is the only proof the input actually registered, not just decoded). The abs/digitizer steps are the
GATED ones (does iOS honour absolute HID); pass --skip-abs to run only the proven keyboard+rel-mouse.

Usage:
    hidbridge/esp32_live_verify.py [--port /dev/ttyUSB1] [--cam /dev/video5]
                                      [--text ESP32HIDOK] [--out captures/esp32_verify_<ts>]
Prereq: the iPad is already PAIRED to wsb-hid-esp32 (one-time physical tap). Needs pyserial + ffmpeg.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

from hidbridge import reports as R  # noqa: E402
from hidbridge import transport_esp32 as TE  # noqa: E402


def _grab(cam: str, out: str, tag: str) -> str:
    path = os.path.join(out, f"esp32_verify_{tag}.jpg")
    subprocess.run(["ffmpeg", "-y", "-f", "v4l2", "-video_size", "1280x720", "-i", cam,
                    "-ss", "0.4", "-frames:v", "1", path],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"  [webcam] {path}")
    return path


def _drain(ser, secs: float, want: str | None, log: list[str]) -> bool:
    end = time.time() + secs
    while time.time() < end:
        raw = ser.readline()
        if not raw:
            continue
        line = raw.decode("ascii", "replace").rstrip()
        if not line:
            continue
        log.append(line)
        print(f"  [serial] {line}")
        if want and want in line:
            return True
    return False


def _send(ser, reports_list, log: list[str], gap: float = 0.05):
    for r in reports_list:
        # encode via the transport's framer so every kind (keyboard/mouse/abs_mouse/digitizer)
        # gets the correct serial prefix from the single source of truth (no hardcoded K/M here).
        ser.write(TE.encode_frame(r.kind, r.data))
        ser.flush()
        time.sleep(gap)
        for _ in range(6):
            raw = ser.readline()
            if not raw:
                break
            line = raw.decode("ascii", "replace").rstrip()
            if not line:
                continue
            log.append(line)
            if line.startswith("EVT"):
                print(f"  [serial] {line}")
                continue
            break


def _kbd(mod: int, usages: list[int]):
    return [R.keyboard_report(mod, usages), R.keyboard_release()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", default=os.environ.get("BT_HID_ESP32_PORT", "/dev/ttyUSB1"))
    ap.add_argument("--cam", default=os.environ.get("CAM", "/dev/video5"))
    ap.add_argument("--text", default="ESP32HIDOK")
    ap.add_argument("--wait", type=float, default=45.0, help="seconds to await iPad auto-reconnect")
    ap.add_argument("--skip-abs", action="store_true",
                    help="skip the absolute-pointer (ID 3) + digitizer (ID 4) test steps")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    out = args.out or time.strftime("captures/esp32_verify_%Y%m%d_%H%M%S")
    os.makedirs(out, exist_ok=True)
    try:
        import serial
    except ImportError:
        print("pyserial required (run under system python or `uv run --with pyserial`)",
              file=sys.stderr)
        return 2

    log: list[str] = []
    ser = serial.Serial()
    ser.port = args.port
    ser.baudrate = 115200
    ser.timeout = 1.0
    ser.dtr = False
    ser.rts = False
    ser.open()
    try:
        print(f"opened {args.port}; waiting for boot banner ...")
        _drain(ser, 4, "wsb-hid-esp32", log)
        print(f"waiting up to {args.wait:.0f}s for the iPad to auto-reconnect (EVT connected) ...")
        connected = _drain(ser, args.wait, "EVT connected", log)
        ser.write(b"P\n"); ser.flush(); time.sleep(0.3); _drain(ser, 2, None, log)
        print(f"connected(EVT)={connected}")
        _grab(args.cam, out, "01_connected")

        print("pointer: wiggle to reveal cursor, then click")
        _send(ser, R.mouse_move_reports(60, 0) + R.mouse_move_reports(-40, 40)
              + R.mouse_move_reports(30, -20), log)
        _grab(args.cam, out, "02_pointer_moved")
        _send(ser, R.mouse_click_reports("left"), log)
        time.sleep(0.5)
        _grab(args.cam, out, "03_after_click")

        print("keyboard: Cmd+Space (Spotlight)")
        _send(ser, _kbd(R.MOD_LGUI, [0x2C]), log)   # Cmd+Space
        time.sleep(1.2)
        _grab(args.cam, out, "04_spotlight")

        print(f"keyboard: type {args.text!r}")
        _send(ser, R.type_text(args.text), log, gap=0.06)
        time.sleep(0.8)
        _grab(args.cam, out, "05_typed")

        print("keyboard: Cmd+H (home)")
        _send(ser, _kbd(R.MOD_LGUI, [0x0B]), log)   # Cmd+H (h usage = 0x0B)
        time.sleep(1.0)
        _grab(args.cam, out, "06_after_cmdH")

        # --- absolute-pointer test (reportID 3) + digitizer touch (reportID 4) ---
        # These are the GATED behaviours: whether iOS honours an ABSOLUTE mouse axis / a digitizer
        # touch. Move the abs pointer to screen-center then the corners, then a touchscreen tap at
        # center. If iOS honours them the cursor/touch jumps to the exact fraction (webcam-verified).
        if not args.skip_abs:
            print("abs pointer: center -> corners (reportID 3)")
            _send(ser, R.abs_move_reports(0.5, 0.5), log); time.sleep(0.4)
            _grab(args.cam, out, "07_abs_center")
            for xf, yf in ((0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)):
                _send(ser, R.abs_move_reports(xf, yf), log); time.sleep(0.3)
            _grab(args.cam, out, "08_abs_corners")
            print("abs pointer: click at center (reportID 3)")
            _send(ser, R.abs_click_reports(0.5, 0.5), log); time.sleep(0.5)
            _grab(args.cam, out, "09_abs_click")
            print("digitizer: single-finger tap at center (reportID 4)")
            _send(ser, R.digitizer_tap_reports(0.5, 0.5), log); time.sleep(0.5)
            _grab(args.cam, out, "10_touch_tap")

        ser.write(b"P\n"); ser.flush(); time.sleep(0.3); _drain(ser, 2, None, log)
    finally:
        ser.close()

    oks = sum(1 for line in log if line.startswith("OK") and "noconn" not in line)
    noconn = sum(1 for line in log if "noconn" in line)
    print(f"\n=== summary: frames OK(notified)={oks} noconn={noconn} "
          f"connected={'EVT connected' in log} out={out} ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

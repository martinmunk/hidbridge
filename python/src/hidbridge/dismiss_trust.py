"""dismiss_trust -- detect + dismiss the iPadOS "Trust This Computer?" overlay over BT HID.

Task #15 ("detect + dismiss the Linux trust overlay"). When Linux (this host) re-enumerates on an
iPad's USB (e.g. a KVM move, an NCM bring-up, a `usbmuxd` pairing attempt), the iPad throws the modal
**"Trust This Computer?"** alert. It blocks the whole screen until answered. This module drives a
paired BLE-HID backend (dongle / ESP32 / Pico -- see `bt_hid.targets`) to answer it hands-free.

WHAT ACTUALLY WORKS -- arrow keys DO answer "Trust" (the maintainer, live 2026-07-14)
-----------------------------------------------------------------------------
The prior "HID-hardened / arrow keys can neither reach nor activate Trust" conclusion is **REFUTED**.
the maintainer LIVE-VERIFIED on mini5 (after a KVM switch to dev-usb threw a fresh Trust prompt) that a
bonded BT keyboard CAN move the highlight onto "Trust" with ARROW keys and then activate it.

  * Keyboard: the FKA focus DOES move under arrow keys and the focused button DOES activate. The
    default focus rests on "Don't Trust" (left); an arrow toward "Trust" (Right) moves the highlight
    onto "Trust" (right); Space/Return then presses it. => `build_trust_arrow_plan` (`method="trust"`).
    After activation a passcode-confirm sheet may follow -- reuse the PIN path (never logged).

    WHY THE EARLIER AGENT WAS WRONG -- THE SUBTLE HIGHLIGHT CUE. The focus indicator on this alert
    is NOT a bold focus ring. It is a SUBTLE relative brightness shift between the two buttons:
      - "Don't Trust" (left): bright blue when focused -> LESS blue when it loses focus.
      - "Trust" (right): plain grey when unfocused -> SLIGHTLY BRIGHTER grey when it gains focus.
    The earlier agent read the C920 webcam looking for a ring, saw no ring jump to Trust, and wrongly
    concluded focus never left "Don't Trust". Focus WAS moving the whole time. To read it on the
    webcam, COMPARE THE RELATIVE BRIGHTNESS OF THE TWO BUTTONS (does the left dim + the right
    brighten?), do NOT hunt for a ring. (mini6 English "Trust" / mini5 Danish "Godkend" -- FKA
    behaviour identical, pure localization.)
    (Ordinary dialogs like the Sidecar "Mistet forbindelse" one answer Tab-then-Space -- see
    `build_dialog_dismiss_plan`; that path is unchanged.)
  * Relative pointer: still not aimable closed-loop (the pointer is invisible in the webcam -- dim,
    reflective glass, content shimmer; absolute coords were ignored by the host, memory
    `ipad-absolute-hid-ignored`). The ARROW-KEY path above is the one that actually works.

=> On the current rig the Trust alert CAN be answered over BT HID via arrow-key focus + activate.
   This tool DETECTS the overlay (grabs a webcam frame for the caller to confirm), drives the
   arrow+activate Trust path (`method="trust"`) or the ordinary-dialog path (`method="dialog"`),
   and returns a structured, passcode-free result.

Everything that builds a HID step list here is PURE (no hardware), so the plans unit-test offline
against a RecordingTransport; the live parts are gated behind a real HidSession.

SECURITY: the device passcode (for the post-Trust confirm) is read from a 0600 file and expanded to
HID frames LOCALLY; it is never logged, echoed, stored, or placed on argv.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from hidbridge import targets as TG  # noqa: E402

# Default location of the device unlock/confirm PIN (0600, never committed). Mirrors bt_hid.unlock.
DEFAULT_PIN_FILE = os.path.join(os.path.expanduser("~"), ".config", "bt_hid", "idevice.pin")


# --------------------------------------------------------------------------- #
# passcode (secure)
# --------------------------------------------------------------------------- #
def read_pin(path: str = DEFAULT_PIN_FILE) -> str:
    """Read the device PIN from a 0600 file. NEVER log/echo the return value.

    Raises FileNotFoundError with a helpful message if absent.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"no PIN file at {path}; create it 0600 with the device passcode (never committed)")
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip()


# --------------------------------------------------------------------------- #
# pure step plans (no hardware -- unit-testable)
# --------------------------------------------------------------------------- #
# A "step" is a dict the runner maps 1:1 onto HidController calls:
#   {"op": "key",      "name": <keyname>, "mod": <int>}
#   {"op": "click",    "button": "left"}
#   {"op": "move",     "dx": int, "dy": int}
#   {"op": "passcode"}                              # types the PIN + Enter (secure, filled at run)
#   {"op": "sleep",    "seconds": float}
#   {"op": "wake"}


def build_dialog_dismiss_plan(activate_key: str = "space", focus_tabs: int = 1,
                              settle: float = 0.4) -> list[dict]:
    """Steps that answer an ORDINARY iOS dialog: reveal focus with Tab(s), then activate.

    This is the proven "Tab then Space" recipe for non-security dialogs (e.g. the Sidecar
    "Mistet forbindelse" alert). It does NOT dismiss the hardened "Trust This Computer?" alert
    (see the module docstring) -- use `build_trust_arrow_plan` for the working Trust path.
    """
    plan: list[dict] = [{"op": "wake"}, {"op": "sleep", "seconds": settle}]
    for _ in range(max(0, focus_tabs)):
        plan += [{"op": "key", "name": "tab", "mod": 0}, {"op": "sleep", "seconds": settle}]
    plan += [{"op": "key", "name": activate_key, "mod": 0}, {"op": "sleep", "seconds": settle}]
    return plan


def build_trust_arrow_plan(*, direction: str = "right", presses: int = 2,
                           activate_key: str = "space", with_passcode: bool = True,
                           settle: float = 0.6) -> list[dict]:
    """Answer the "Trust This Computer?" alert with ARROW-key focus move + activate (the maintainer 2026-07-14).

    Default focus is on "Don't Trust" (left); "Trust" is the right button. Sends `presses` arrow
    keys toward "Trust" (default Right), then activates the focused button (Space), then -- if the
    passcode-confirm sheet follows -- types the PIN + Enter (secure, filled at run; never logged).

    `presses` defaults to 2 for robustness: iOS FKA sometimes consumes the FIRST arrow just to REVEAL
    the current focus without moving it, and arrow focus CLAMPS at the row edges (it does not wrap),
    so a second Right lands/stays on the rightmost button ("Trust") either way. The highlight cue is
    SUBTLE (left button dims blue / right button brightens grey -- no ring); read it by comparing the
    two buttons' relative brightness (see the module docstring), not by looking for a focus ring.
    """
    key = {"right": "right", "left": "left", "up": "up", "down": "down"}.get(direction, direction)
    plan: list[dict] = [{"op": "wake"}, {"op": "sleep", "seconds": 0.3}]
    for _ in range(max(1, presses)):
        plan += [{"op": "key", "name": key, "mod": 0}, {"op": "sleep", "seconds": settle}]
    plan += [{"op": "key", "name": activate_key, "mod": 0}, {"op": "sleep", "seconds": settle}]
    if with_passcode:
        plan += [{"op": "passcode"}, {"op": "sleep", "seconds": settle}]
    return plan


def plan_summary(plan: list[dict]) -> str:
    """A one-line, passcode-free summary of a plan (safe to log)."""
    parts = []
    for s in plan:
        op = s.get("op")
        if op == "key":
            parts.append(f"key:{s['name']}")
        elif op == "passcode":
            parts.append("passcode:<redacted>")
        elif op == "click":
            parts.append(f"click:{s.get('button', 'left')}")
        elif op == "move":
            parts.append(f"move:{s['dx']},{s['dy']}")
        elif op in ("wake", "sleep"):
            continue
        else:
            parts.append(str(op))
    return " -> ".join(parts)


# --------------------------------------------------------------------------- #
# runner (drives a HidController; passcode filled here, never in the plan)
# --------------------------------------------------------------------------- #
def run_plan(controller, plan: list[dict], *, pin: str | None = None,
             sleep=time.sleep) -> list[str]:
    """Execute a step plan against a HidController. Returns the ops performed (passcode redacted).

    `pin` is only consumed by a {"op":"passcode"} step and is expanded to frames by reports.py
    (never logged). `sleep` is injectable so tests run instantly.
    """
    done: list[str] = []
    for s in plan:
        op = s.get("op")
        if op == "wake":
            controller.wake(); done.append("wake")
        elif op == "sleep":
            sleep(float(s.get("seconds", 0.0)))
        elif op == "key":
            controller.send_key(s["name"], int(s.get("mod", 0))); done.append(f"key:{s['name']}")
        elif op == "click":
            controller.click(s.get("button", "left")); done.append("click")
        elif op == "move":
            controller.move(int(s["dx"]), int(s["dy"])); done.append("move")
        elif op == "passcode":
            if not pin:
                raise ValueError("plan has a passcode step but no PIN was provided")
            controller.unlock(pin, press_enter=True); done.append("passcode")
        else:
            raise ValueError(f"unknown plan op {op!r}")
    return done


# --------------------------------------------------------------------------- #
# target resolution
# --------------------------------------------------------------------------- #
def resolve(device: str | None, target: str | None, bond: str | None,
            registry: TG.TargetRegistry | None = None) -> tuple[str, str]:
    """Resolve (target, bond) from either a registry `device` name or explicit target+bond."""
    if target and bond:
        return target, bond
    if device:
        reg = registry or TG.TargetRegistry.load()
        r = reg.resolve(device)
        return r.target, r.bond
    raise ValueError("specify --device <name> (registry) OR both --target and --bond")


# --------------------------------------------------------------------------- #
# webcam detect/verify (best-effort, optional -- import cv2/ffmpeg lazily)
# --------------------------------------------------------------------------- #
def grab_webcam(path: str, device: str = "/dev/video5", size: str = "1920x1080") -> str | None:
    """Grab one frame from the C920 for the caller to confirm the overlay. Returns path or None.

    Best-effort: the wsb webcam node is dynamic (see docs/hardware.md); override `device`. Never
    raises -- returns None if ffmpeg/the device is unavailable.
    """
    import subprocess
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "v4l2", "-video_size", size, "-i", device,
             "-frames:v", "1", path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        return path if r.returncode == 0 and os.path.exists(path) else None
    except Exception:  # noqa: BLE001  (best-effort snapshot; missing cam/ffmpeg is not fatal)
        return None


# --------------------------------------------------------------------------- #
# live dismissal
# --------------------------------------------------------------------------- #
def dismiss(target: str, bond: str, *, method: str = "dialog",
            activate_key: str = "space", focus_tabs: int = 1,
            arrow_direction: str = "right", arrow_presses: int = 2,
            pin: str | None = None, ready_timeout: float = 8.0,
            connect_timeout: float = 20.0) -> dict:
    """Connect the HID backend and run a dismissal plan against the device. Returns a result dict.

    method:
      "dialog"        -> Tab(s) + activate (answers ORDINARY dialogs; inert on the Trust alert).
      "trust"         -> ARROW toward "Trust" + activate (+ passcode) -- the working path (2026-07-14).
      "decline"       -> Tab + activate (the Tab-focused button is "Don't Trust" on the Trust alert).
    """
    from hidbridge.conn import HidSession

    if method in ("dialog", "decline"):
        plan = build_dialog_dismiss_plan(activate_key=activate_key, focus_tabs=focus_tabs)
    elif method == "trust":
        plan = build_trust_arrow_plan(direction=arrow_direction, presses=arrow_presses,
                                      activate_key=activate_key, with_passcode=bool(pin))
    else:
        raise ValueError(f"unknown method {method!r} (dialog|trust|decline)")

    result = {"target": target, "bond": bond, "method": method,
              "plan": plan_summary(plan), "ran": None, "connected": False}
    with HidSession(target, bond, ready_timeout=ready_timeout,
                    connect_timeout=connect_timeout) as hid:
        result["connected"] = True
        result["ready"] = hid.ready_state
        result["ran"] = run_plan(hid.controller, plan, pin=pin)
    return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dismiss_trust",
        description="Detect + dismiss the iPadOS 'Trust This Computer?' overlay over BT HID.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--device", help="registry device name (e.g. mini6, mini5)")
    p.add_argument("--target", help="explicit HidSession target (esp32:/dev/ttyUSB1, hci1, ...)")
    p.add_argument("--bond", help="explicit iOS-device bond identity")
    p.add_argument("--method", default="dialog",
                   choices=["dialog", "trust", "decline"],
                   help="dialog=Tab+activate (ordinary dialogs); trust=ARROW+activate (Trust alert, "
                        "the working path); decline=Tab+activate (Trust alert's 'Don't Trust' button)")
    p.add_argument("--activate-key", default="space", help="key that activates the focused button")
    p.add_argument("--focus-tabs", type=int, default=1, help="number of Tabs to reveal focus")
    p.add_argument("--arrow-direction", default="right",
                   help="arrow toward Trust for method=trust (Trust is the RIGHT button)")
    p.add_argument("--arrow-presses", type=int, default=2,
                   help="arrow presses toward Trust for method=trust (2 = reveal+move, clamps at edge)")
    p.add_argument("--pin-file", default=DEFAULT_PIN_FILE,
                   help="0600 file with the device passcode (never logged)")
    p.add_argument("--no-passcode", action="store_true",
                   help="do not type the passcode (skip the post-Trust confirm)")
    p.add_argument("--webcam", metavar="PATH",
                   help="also grab a webcam frame to PATH for visual confirmation")
    p.add_argument("--webcam-device", default="/dev/video5")
    p.add_argument("--dry-run", action="store_true",
                   help="print the plan and exit without touching hardware")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        target, bond = resolve(args.device, args.target, args.bond)
    except (ValueError, KeyError) as e:
        print(f"target error: {e}", file=sys.stderr)
        return 2

    if args.dry_run:
        if args.method == "trust":
            plan = build_trust_arrow_plan(direction=args.arrow_direction, presses=args.arrow_presses,
                                          activate_key=args.activate_key,
                                          with_passcode=not args.no_passcode)
        else:
            plan = build_dialog_dismiss_plan(activate_key=args.activate_key,
                                             focus_tabs=args.focus_tabs)
        print(f"target={target} bond={bond} method={args.method}")
        print("plan:", plan_summary(plan))
        return 0

    pin = None
    if args.method == "trust" and not args.no_passcode:
        try:
            pin = read_pin(args.pin_file)   # never logged
        except FileNotFoundError as e:
            print(f"passcode error: {e}", file=sys.stderr)
            return 2

    if args.webcam:
        before = grab_webcam(args.webcam, device=args.webcam_device)
        print(f"webcam(before): {before or 'unavailable'}")

    try:
        res = dismiss(target, bond, method=args.method, activate_key=args.activate_key,
                      focus_tabs=args.focus_tabs, arrow_direction=args.arrow_direction,
                      arrow_presses=args.arrow_presses, pin=pin)
    except Exception as e:  # noqa: BLE001  (surface any live-HID failure as a clean CLI error)
        print(f"dismiss failed: {e}", file=sys.stderr)
        return 1

    print(f"connected={res['connected']} ready={res.get('ready')} "
          f"method={res['method']} plan={res['plan']}")
    print(f"ran: {' '.join(res['ran'] or [])}")
    if args.webcam:
        after = grab_webcam(args.webcam + ".after.jpg", device=args.webcam_device)
        print(f"webcam(after): {after or 'unavailable'} -- eyeball to confirm the overlay is gone")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

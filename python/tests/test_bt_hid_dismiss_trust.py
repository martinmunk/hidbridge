"""Offline + device-gated tests for bt_hid.dismiss_trust (task #15: dismiss the trust overlay).

The plan builders + runner are PURE (no hardware): a plan is a list of step dicts that the runner
maps onto a HidController, so we drive it against a RecordingTransport and assert the exact frames.
Asserts:
  * build_dialog_dismiss_plan is the proven Tab(s)+activate recipe (wake -> tab*n -> activate);
  * build_trust_arrow_plan is arrow(toward Trust) -> activate -> passcode (the user, live 2026-07-14);
  * plan_summary NEVER leaks the passcode (redacted) -- security invariant;
  * run_plan emits the right HID frames for keys/click/aim, and REQUIRES a pin for a passcode step;
  * resolve() accepts explicit target+bond OR a registry device, and errors when given neither;
  * read_pin reads a file and raises a clear error when the PIN file is absent.
The LIVE test (needs the paired BT-HID backend + the iPad) is gated behind BT_HID_DISMISS_LIVE.
"""

import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

from hidbridge import dismiss_trust as D  # noqa: E402
from hidbridge import targets as TG  # noqa: E402
from hidbridge.controller import HidController, RecordingTransport  # noqa: E402


# --------------------------------------------------------------------------- #
# pure plan builders
# --------------------------------------------------------------------------- #
def test_dialog_plan_is_tab_then_activate():
    plan = D.build_dialog_dismiss_plan(activate_key="space", focus_tabs=1)
    ops = [s["op"] for s in plan if s["op"] != "sleep"]
    assert ops == ["wake", "key", "key"]
    keys = [s["name"] for s in plan if s["op"] == "key"]
    assert keys == ["tab", "space"]


def test_dialog_plan_multi_tab():
    plan = D.build_dialog_dismiss_plan(focus_tabs=3)
    keys = [s["name"] for s in plan if s["op"] == "key"]
    assert keys == ["tab", "tab", "tab", "space"]


def test_trust_arrow_plan_is_arrow_then_activate():
    # The user live-verified 2026-07-14: arrow toward "Trust" (right button) + activate DOES answer it.
    plan = D.build_trust_arrow_plan(direction="right", presses=2, activate_key="space",
                                    with_passcode=True)
    ops = [s["op"] for s in plan if s["op"] != "sleep"]
    assert ops == ["wake", "key", "key", "key", "passcode"]
    keys = [s["name"] for s in plan if s["op"] == "key"]
    # two arrows toward Trust, then the activation key -- never a Tab/Left-toward-DontTrust
    assert keys == ["right", "right", "space"]


def test_trust_arrow_plan_minimal_single_press_and_return():
    plan = D.build_trust_arrow_plan(presses=1, activate_key="return", with_passcode=False)
    keys = [s["name"] for s in plan if s["op"] == "key"]
    assert keys == ["right", "return"]
    assert not any(s["op"] == "passcode" for s in plan)


def test_trust_arrow_plan_emits_arrow_then_activate_frames():
    # runner -> exact HID frames: 2 arrows + activate = 3 keys => 6 keyboard frames (press+release)
    ran, frames = _frames_for(D.build_trust_arrow_plan(presses=2, with_passcode=False))
    assert ran == ["wake", "key:right", "key:right", "key:space"]
    assert len(frames) == 8  # wake + 2 right + space, each press+release
    assert all(kind == "keyboard" for kind, _ in frames)


def test_plan_summary_redacts_passcode():
    plan = [{"op": "wake"}, {"op": "click", "button": "left"}, {"op": "passcode"}]
    s = D.plan_summary(plan)
    assert "redacted" in s
    assert "click" in s


# --------------------------------------------------------------------------- #
# runner -> exact HID frames (RecordingTransport)
# --------------------------------------------------------------------------- #
def _frames_for(plan, pin=None):
    t = RecordingTransport()
    c = HidController(t)
    ran = D.run_plan(c, plan, pin=pin, sleep=lambda *_: None)
    return ran, t.frames


def test_run_dialog_plan_emits_wake_tab_space():
    ran, frames = _frames_for(D.build_dialog_dismiss_plan())
    assert ran == ["wake", "key:tab", "key:space"]
    # each key is a press + release => wake(2) + tab(2) + space(2) = 6 keyboard frames
    assert len(frames) == 6
    assert all(kind == "keyboard" for kind, _ in frames)


def test_run_move_and_click():
    plan = [{"op": "move", "dx": 40, "dy": -20}, {"op": "click", "button": "left"}]
    ran, frames = _frames_for(plan)
    assert ran == ["move", "click"]
    kinds = [k for k, _ in frames]
    assert "mouse" in kinds  # the relative move + the click both ride mouse reports


def test_passcode_step_requires_pin():
    plan = [{"op": "passcode"}]
    with pytest.raises(ValueError):
        _frames_for(plan, pin=None)


def test_passcode_step_types_pin_and_enter():
    ran, frames = _frames_for([{"op": "passcode"}], pin="1234")
    assert ran == ["passcode"]
    # 4 digits (press+release each) + Enter (press+release) = 10 keyboard frames
    assert len(frames) == 10
    assert all(kind == "keyboard" for kind, _ in frames)


def test_unknown_op_raises():
    with pytest.raises(ValueError):
        _frames_for([{"op": "teleport"}])


# --------------------------------------------------------------------------- #
# target resolution + PIN file
# --------------------------------------------------------------------------- #
def test_resolve_explicit_target_bond():
    assert D.resolve(None, "esp32:/dev/ttyUSB1", "mini6") == ("esp32:/dev/ttyUSB1", "mini6")


def test_resolve_from_registry_device():
    reg = TG.TargetRegistry(entries={
        "mini6": TG.TargetEntry("mini6", target="esp32:/dev/ttyUSB1", bond="mini6")})
    assert D.resolve("mini6", None, None, registry=reg) == ("esp32:/dev/ttyUSB1", "mini6")


def test_resolve_needs_something():
    with pytest.raises(ValueError):
        D.resolve(None, None, None)


def test_default_registry_maps_mini6_to_esp32_and_mini5_to_pico():
    # the reconciled live-rig defaults (2026-07-13): MCU backends, no shared-MAC warning
    reg = TG.TargetRegistry.load(str(pathlib.Path("/nonexistent/targets.json")))
    assert reg.resolve("mini6").target == "esp32:/dev/ttyUSB1"
    assert reg.resolve("mini5").target == "pico:/dev/ttyACM0"
    assert reg.resolve("mini6").shared_mac_warning is None


def test_read_pin(tmp_path):
    p = tmp_path / "idevice.pin"
    p.write_text("135790\n")
    assert D.read_pin(str(p)) == "135790"


def test_read_pin_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        D.read_pin(str(tmp_path / "nope.pin"))


# --------------------------------------------------------------------------- #
# device-gated live test (skips without the BT-HID backend + iPad)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not os.environ.get("BT_HID_DISMISS_LIVE"),
                    reason="live BT-HID dismiss test; set BT_HID_DISMISS_LIVE=<device> to run")
def test_live_dialog_dismiss_connects_and_runs():
    """PREPARE+RUN+VERIFY: resolve the device, connect its HID backend, run the dialog plan.

    Gated: needs the paired backend (ESP32/Pico/dongle) + the iPad awake. Asserts the session
    connects and the plan runs end-to-end (delivery). To actually answer a live "Trust" alert use
    method="trust" (arrow + activate, the user, live 2026-07-14) and webcam-confirm the highlight moves
    onto "Trust" by comparing the two buttons' relative brightness -- see the module docstring.
    """
    device = os.environ["BT_HID_DISMISS_LIVE"]
    target, bond = D.resolve(device, None, None)
    res = D.dismiss(target, bond, method="dialog")
    assert res["connected"] is True
    assert res["ran"] and res["ran"][0] == "wake"

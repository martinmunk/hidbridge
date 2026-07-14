"""Offline tests for the importable bt_hid controller API (hidbridge/controller.py).

The controller is the API-first entry point: a scriptable HID INPUT GENERATOR usable both as an ops
helper AND as the injection fixture for a future Sidecar-HID relay e2e (inject key/pointer on the
iPad -> assert it arrives decoded over the Sidecar HIDRelay stream). It builds deterministic report
frames from the pure `reports` layer and hands them to a pluggable transport, so tests assert the
exact emitted frames without any bluetooth device.

Frames are (kind, body_bytes) tuples: kind in {"keyboard","mouse"}.
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]

from hidbridge import reports as R  # noqa: E402
from hidbridge.controller import HidController, RecordingTransport  # noqa: E402


def _ctl():
    t = RecordingTransport()
    return HidController(t), t


def test_type_text_emits_exact_frames():
    ctl, t = _ctl()
    ctl.type_text("1")
    assert t.frames == [
        ("keyboard", bytes([0x00, 0x00, 0x1E, 0, 0, 0, 0, 0])),
        ("keyboard", bytes(8)),
    ]


def test_send_key_with_modifier():
    ctl, t = _ctl()
    ctl.send_key("space", modifiers=R.MOD_LGUI)
    assert t.frames[0] == ("keyboard", bytes([R.MOD_LGUI, 0x00, 0x2C, 0, 0, 0, 0, 0]))
    assert t.frames[1] == ("keyboard", bytes(8))


def test_move_and_click():
    ctl, t = _ctl()
    ctl.move(5, -3)
    ctl.click("right")
    assert t.frames[0] == ("mouse", bytes([0x00, 5, (-3) & 0xFF, 0x00]))
    assert t.frames[1][1][0] == R.BTN_RIGHT   # button-down report
    assert t.frames[2] == ("mouse", bytes([0x00, 0, 0, 0]))  # release


def test_scroll_emits_wheel_reports():
    ctl, t = _ctl()
    ctl.scroll(2)
    assert t.frames == [("mouse", bytes([0x00, 0, 0, 2]))]


def _string_attrs(obj, _seen=None):
    """Yield every str/bytes VALUE reachable through obj's attribute graph.

    Walks __dict__ recursively but only surfaces string-like leaves -- it deliberately does NOT
    render object reprs. A default `<Cls object at 0x...>` repr embeds a memory address whose hex
    can coincidentally contain the passcode digits, which made the old `repr(ctl.__dict__)` check an
    allocation-order-dependent flake: green run alone, red in the full random-ordered suite (the
    RecordingTransport happened to land at e.g. 0x7f00de651310 -> contains "00"). Issue #45.
    """
    _seen = set() if _seen is None else _seen
    if id(obj) in _seen:
        return
    _seen.add(id(obj))
    d = getattr(obj, "__dict__", None)
    if not d:
        return
    for v in d.values():
        if isinstance(v, str):
            yield v
        elif isinstance(v, (bytes, bytearray)):
            yield bytes(v).decode("latin1")
        else:
            yield from _string_attrs(v, _seen)


def test_unlock_expands_passcode_and_never_stores_it():
    ctl, t = _ctl()
    passcode = "4271"   # distinctive digits (placeholder only)
    ctl.unlock(passcode)
    # N digits * (down/up) + Enter down/up frames
    assert len(t.frames) == 2 * len(passcode) + 2
    assert t.frames[-2] == ("keyboard", bytes([0x00, 0x00, 0x28, 0, 0, 0, 0, 0]))  # Enter down
    assert t.frames[-1] == ("keyboard", bytes(8))
    # The controller must not retain the passcode as a string/bytes attribute on the instance.
    # Assert against attribute VALUES, never repr(__dict__) -- the object-repr memory address is a
    # flaky false positive (see _string_attrs).
    assert all(passcode not in s for s in _string_attrs(ctl))


def test_wake_sends_a_keystroke():
    ctl, t = _ctl()
    ctl.wake()
    assert t.frames and t.frames[0][0] == "keyboard"


def test_play_timeline_runs_actions_in_order_with_injected_sleep():
    ctl, t = _ctl()
    slept = []
    ctl.play([
        {"after": 0.5, "action": "type", "text": "a"},
        {"after": 0.1, "action": "move", "dx": 10, "dy": 0},
        {"after": 0.0, "action": "click"},
    ], sleep=slept.append)
    # sleeps happened in order
    assert slept == [0.5, 0.1, 0.0]
    # 'a' down/up, one move, click down/up = 5 frames
    kinds = [f[0] for f in t.frames]
    assert kinds == ["keyboard", "keyboard", "mouse", "mouse", "mouse"]


def test_wire_frames_helper_matches_bthid_encoding():
    # The controller can also yield reportID-prefixed / 0xA1-framed bytes for a capture cross-check.
    ctl, _ = _ctl()
    reps = ctl.build_type_text("A")
    assert reps[0].bthid_bytes() == bytes([0xA1, 1, R.MOD_LSHIFT, 0x00, 0x04, 0, 0, 0, 0, 0])


# --------------------------------------------------------------------------- #
# Absolute pointer + digitizer routing: the controller tags each frame with its
# own `kind` so the daemon routes it to the right report characteristic.
# --------------------------------------------------------------------------- #
def test_move_abs_emits_abs_mouse_frame_at_scaled_coords():
    ctl, t = _ctl()
    ctl.move_abs(0.5, 0.25)
    assert len(t.frames) == 1
    kind, body = t.frames[0]
    assert kind == "abs_mouse"
    x = body[1] | (body[2] << 8)
    y = body[3] | (body[4] << 8)
    assert x == R.frac_to_abs(0.5)
    assert y == R.frac_to_abs(0.25)


def test_tap_abs_emits_move_press_release():
    ctl, t = _ctl()
    ctl.tap_abs(0.1, 0.9)
    assert [f[0] for f in t.frames] == ["abs_mouse", "abs_mouse", "abs_mouse"]
    assert [f[1][0] for f in t.frames] == [0, R.BTN_LEFT, 0]


def test_touch_tap_emits_digitizer_down_then_up():
    ctl, t = _ctl()
    ctl.touch_tap(0.5, 0.5)
    assert [f[0] for f in t.frames] == ["digitizer", "digitizer"]
    assert t.frames[0][1][0] & R.TIP_SWITCH      # finger down
    assert t.frames[1][1] == bytes(R.DIGITIZER_BODY_LEN)  # lift


def test_play_timeline_supports_abs_and_touch_actions():
    ctl, t = _ctl()
    ctl.play([
        {"action": "move_abs", "x": 0.5, "y": 0.5},
        {"action": "tap_abs", "x": 0.2, "y": 0.3},
        {"action": "touch", "x": 0.8, "y": 0.1},
    ], sleep=lambda _s: None)
    kinds = [f[0] for f in t.frames]
    # 1 move_abs + 3 tap_abs (move/down/up) + 2 touch (down/up) = 6 frames
    assert kinds == ["abs_mouse", "abs_mouse", "abs_mouse", "abs_mouse",
                     "digitizer", "digitizer"]

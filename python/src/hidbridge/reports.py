"""Bluetooth-HID peripheral report layer -- PURE, importable, no bluetooth deps.

We present a STANDARD USB boot-protocol HID device (keyboard reportID 1 + mouse reportID 2) to an
iPad/iPhone over a USB Bluetooth dongle, so we can programmatically wake / unlock / type / click
without a physical poke. iPads speak standard HID to a Bluetooth peripheral, so the byte layouts
here are the STANDARD USB-HID boot layouts -- NOT Apple's private UniversalHID; this is a distinct,
standards-based device.

This module is intentionally dependency-free (only stdlib) so it imports + unit-tests fully offline
under uv, and can equally be imported by the emulator running under the system python (which is the
interpreter that has AF_BLUETOOTH). Keep it that way: no socket / dbus / gi imports here.

SECURITY: this layer builds the reports that type a device passcode. It must NEVER log, print, or
put a real passcode into a repr/str. `passcode_reports()` takes the digits, emits reports, and keeps
no copy; callers must not log the argument.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# HID keyboard modifier byte bits (STANDARD, usage page 0x07, keys 0xE0-0xE7)
# --------------------------------------------------------------------------- #
MOD_LCTRL = 0x01
MOD_LSHIFT = 0x02
MOD_LALT = 0x04
MOD_LGUI = 0x08   # left Command / Super
MOD_RCTRL = 0x10
MOD_RSHIFT = 0x20
MOD_RALT = 0x40
MOD_RGUI = 0x80

# --------------------------------------------------------------------------- #
# Mouse button bits (reportID 2, byte 0)
# --------------------------------------------------------------------------- #
BTN_LEFT = 0x01
BTN_RIGHT = 0x02
BTN_MIDDLE = 0x04
BTN_BACK = 0x08     # button 4 -- "Back" (an "additional" button iOS AssistiveTouch can remap)
BTN_FORWARD = 0x10  # button 5 -- "Forward"

# --------------------------------------------------------------------------- #
# reportIDs (match the emulator's HID report descriptor)
# --------------------------------------------------------------------------- #
REPORT_ID_KEYBOARD = 1
REPORT_ID_MOUSE = 2
REPORT_ID_ABS_MOUSE = 3        # absolute-position pointer (Generic Desktop Mouse, X/Y absolute)
REPORT_ID_DIGITIZER = 4        # single-finger touchscreen digitizer (usage page 0x0D)

KEYBOARD_BODY_LEN = 8          # mod, reserved, k1..k6
MOUSE_BODY_LEN = 4             # buttons, dx, dy, wheel
ABS_MOUSE_BODY_LEN = 5         # buttons, X(u16 LE), Y(u16 LE)
DIGITIZER_BODY_LEN = 6         # flags(tip|inrange), contactID, X(u16 LE), Y(u16 LE)
_KBD_MAX_KEYS = 6
_MOUSE_STEP = 127              # max signed-int8 relative delta per report

# Absolute coordinate space: both the absolute mouse and the digitizer report X/Y as a u16 in a
# fixed 0..32767 logical range (the HID convention for a normalized absolute axis). Callers pass a
# fraction 0.0..1.0 of the screen and we scale it to this range; the device (iOS) maps 0..32767
# across the physical panel. 32767 = 0x7FFF is the max positive value that fits Logical Maximum
# without needing a 32-bit (negative-looking) item, so it is the standard absolute-axis full scale.
ABS_AXIS_MAX = 32767

# Digitizer flag bits (report byte 0)
TIP_SWITCH = 0x01   # finger is touching the surface (a "tap"/"press")
IN_RANGE = 0x02     # finger is detected in range (hovering or touching)

# BT-HID transport header for a DATA/Input transaction on the interrupt channel (type 0xA, param 1).
BTHID_DATA_INPUT = 0xA1

# --------------------------------------------------------------------------- #
# USB-HID Report Descriptor -- the SINGLE source of truth for the report layout.
# Combined boot-style keyboard (reportID 1, 8-byte body) + mouse (reportID 2, 4-byte body).
# The BLE HID-over-GATT service serves these exact bytes as its Report Map (0x2A4B); the byte
# layouts produced by keyboard_report()/mouse_report() above MUST match this descriptor.
# --------------------------------------------------------------------------- #
REPORT_DESCRIPTOR = bytes([
    # ---- Keyboard, Report ID 1 ----
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x06,        # Usage (Keyboard)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0x01,        #   Report ID (1)
    0x05, 0x07,        #   Usage Page (Key Codes)
    0x19, 0xE0,        #   Usage Minimum (224)
    0x29, 0xE7,        #   Usage Maximum (231)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x08,        #   Report Count (8)
    0x81, 0x02,        #   Input (Data,Var,Abs)   -- modifier byte
    0x95, 0x01,        #   Report Count (1)
    0x75, 0x08,        #   Report Size (8)
    0x81, 0x03,        #   Input (Const)          -- reserved byte
    0x95, 0x06,        #   Report Count (6)
    0x75, 0x08,        #   Report Size (8)
    0x15, 0x00,        #   Logical Minimum (0)
    0x26, 0xFF, 0x00,  #   Logical Maximum (255)
    0x05, 0x07,        #   Usage Page (Key Codes)
    0x19, 0x00,        #   Usage Minimum (0)
    0x29, 0xFF,        #   Usage Maximum (255)
    0x81, 0x00,        #   Input (Data,Array)     -- 6 key slots
    0xC0,              # End Collection
    # ---- Mouse, Report ID 2 ----
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x02,        # Usage (Mouse)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0x02,        #   Report ID (2)
    0x09, 0x01,        #   Usage (Pointer)
    0xA1, 0x00,        #   Collection (Physical)
    0x05, 0x09,        #     Usage Page (Buttons)
    0x19, 0x01,        #     Usage Minimum (1)
    0x29, 0x05,        #     Usage Maximum (5)  -- L/R/M + Back(4) + Forward(5)
    0x15, 0x00,        #     Logical Minimum (0)
    0x25, 0x01,        #     Logical Maximum (1)
    0x95, 0x05,        #     Report Count (5)
    0x75, 0x01,        #     Report Size (1)
    0x81, 0x02,        #     Input (Data,Var,Abs)  -- 5 buttons
    0x95, 0x01,        #     Report Count (1)
    0x75, 0x03,        #     Report Size (3)
    0x81, 0x03,        #     Input (Const)         -- 3-bit padding (buttons byte stays 1 byte)
    0x05, 0x01,        #     Usage Page (Generic Desktop)
    0x09, 0x30,        #     Usage (X)
    0x09, 0x31,        #     Usage (Y)
    0x09, 0x38,        #     Usage (Wheel)
    0x15, 0x81,        #     Logical Minimum (-127)
    0x25, 0x7F,        #     Logical Maximum (127)
    0x75, 0x08,        #     Report Size (8)
    0x95, 0x03,        #     Report Count (3)
    0x81, 0x06,        #     Input (Data,Var,Rel)  -- X, Y, Wheel
    0xC0,              #   End Collection
    0xC0,              # End Collection
    # ---- Absolute Mouse, Report ID 3 ----
    # Same Generic Desktop Mouse collection as reportID 2, but X/Y are ABSOLUTE (no Relative flag)
    # and 16-bit over Logical 0..32767. iOS pointer support (iPadOS 13.4+) is RELATIVE for the
    # AssistiveTouch cursor; whether it honours an ABSOLUTE mouse axis is what this report TESTS.
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x02,        # Usage (Mouse)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0x03,        #   Report ID (3)
    0x09, 0x01,        #   Usage (Pointer)
    0xA1, 0x00,        #   Collection (Physical)
    0x05, 0x09,        #     Usage Page (Buttons)
    0x19, 0x01,        #     Usage Minimum (1)
    0x29, 0x03,        #     Usage Maximum (3)
    0x15, 0x00,        #     Logical Minimum (0)
    0x25, 0x01,        #     Logical Maximum (1)
    0x95, 0x03,        #     Report Count (3)
    0x75, 0x01,        #     Report Size (1)
    0x81, 0x02,        #     Input (Data,Var,Abs)   -- 3 buttons
    0x95, 0x01,        #     Report Count (1)
    0x75, 0x05,        #     Report Size (5)
    0x81, 0x03,        #     Input (Const)          -- 5-bit padding
    0x05, 0x01,        #     Usage Page (Generic Desktop)
    0x09, 0x30,        #     Usage (X)
    0x09, 0x31,        #     Usage (Y)
    0x15, 0x00,        #     Logical Minimum (0)
    0x26, 0xFF, 0x7F,  #     Logical Maximum (32767)
    0x75, 0x10,        #     Report Size (16)
    0x95, 0x02,        #     Report Count (2)
    0x81, 0x02,        #     Input (Data,Var,Abs)   -- X, Y ABSOLUTE (no Rel bit)
    0xC0,              #   End Collection
    0xC0,              # End Collection
    # ---- Digitizer / Touch Screen, Report ID 4 ----
    # A single-finger touchscreen digitizer. If iOS turns these into real touch events we get true
    # taps at exact coordinates (the "true touch" path). Tip Switch = finger down; Contact ID = 0;
    # X/Y absolute 0..32767 on the Generic Desktop page (the digitizer convention).
    0x05, 0x0D,        # Usage Page (Digitizers)
    0x09, 0x04,        # Usage (Touch Screen)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0x04,        #   Report ID (4)
    0x09, 0x22,        #   Usage (Finger)
    0xA1, 0x02,        #   Collection (Logical)
    0x09, 0x42,        #     Usage (Tip Switch)
    0x15, 0x00,        #     Logical Minimum (0)
    0x25, 0x01,        #     Logical Maximum (1)
    0x75, 0x01,        #     Report Size (1)
    0x95, 0x01,        #     Report Count (1)
    0x81, 0x02,        #     Input (Data,Var,Abs)   -- Tip Switch (bit 0)
    0x09, 0x32,        #     Usage (In Range)
    0x81, 0x02,        #     Input (Data,Var,Abs)   -- In Range (bit 1)
    0x95, 0x06,        #     Report Count (6)
    0x81, 0x03,        #     Input (Const)          -- 6-bit padding to fill byte 0
    0x75, 0x08,        #     Report Size (8)
    0x95, 0x01,        #     Report Count (1)
    0x09, 0x51,        #     Usage (Contact Identifier)
    0x81, 0x02,        #     Input (Data,Var,Abs)   -- Contact ID (byte 1)
    0x05, 0x01,        #     Usage Page (Generic Desktop)
    0x15, 0x00,        #     Logical Minimum (0)
    0x26, 0xFF, 0x7F,  #     Logical Maximum (32767)
    0x75, 0x10,        #     Report Size (16)
    0x95, 0x01,        #     Report Count (1)
    0x09, 0x30,        #     Usage (X)
    0x81, 0x02,        #     Input (Data,Var,Abs)   -- X ABSOLUTE
    0x09, 0x31,        #     Usage (Y)
    0x81, 0x02,        #     Input (Data,Var,Abs)   -- Y ABSOLUTE
    0xC0,              #   End Collection
    0xC0,              # End Collection
])


# --------------------------------------------------------------------------- #
# Scancode map (HID usage page 0x07)
# --------------------------------------------------------------------------- #
# unshifted printable ASCII -> usage.  Shifted variants are derived (letters -> shift, and the
# _SHIFTED_SYMBOLS table for punctuation).
_BASE_USAGE: dict[str, int] = {}
# letters
for _i, _c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    _BASE_USAGE[_c] = 0x04 + _i
# digits 1-9 then 0
for _i, _c in enumerate("123456789"):
    _BASE_USAGE[_c] = 0x1E + _i
_BASE_USAGE["0"] = 0x27
# common punctuation (unshifted)
_BASE_USAGE.update({
    "\n": 0x28,   # Enter
    "\t": 0x2B,   # Tab
    " ": 0x2C,    # Space
    "-": 0x2D, "=": 0x2E, "[": 0x2F, "]": 0x30, "\\": 0x31,
    ";": 0x33, "'": 0x34, "`": 0x35, ",": 0x36, ".": 0x37, "/": 0x38,
})

# Named keys (for `key`/`key_reports`).
KEY_NAME_TO_USAGE: dict[str, int] = {
    "enter": 0x28, "return": 0x28, "esc": 0x29, "escape": 0x29,
    "backspace": 0x2A, "delete": 0x2A, "tab": 0x2B, "space": 0x2C,
    "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52,
    "home": 0x4A, "end": 0x4D, "pageup": 0x4B, "pagedown": 0x4E,
    "f1": 0x3A, "f2": 0x3B, "f3": 0x3C, "f4": 0x3D, "f5": 0x3E, "f6": 0x3F,
    "f7": 0x40, "f8": 0x41, "f9": 0x42, "f10": 0x43, "f11": 0x44, "f12": 0x45,
}

# Shifted punctuation -> the unshifted char whose usage it shares (with shift held).
_SHIFTED_SYMBOLS: dict[str, str] = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5",
    "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
    "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\",
    ":": ";", '"': "'", "~": "`", "<": ",", ">": ".", "?": "/",
}


def char_to_usage(ch: str) -> tuple[int, bool]:
    """Map a single character to (HID usage, shift_required).

    Raises ValueError for a character with no boot-keyboard mapping.
    """
    if ch in _BASE_USAGE:
        return _BASE_USAGE[ch], False
    if ch.isalpha() and ch.lower() in _BASE_USAGE:
        return _BASE_USAGE[ch.lower()], True      # uppercase letter -> shift
    if ch in _SHIFTED_SYMBOLS:
        return _BASE_USAGE[_SHIFTED_SYMBOLS[ch]], True
    raise ValueError(f"no HID scancode for character {ch!r}")


def _clamp(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else hi if v > hi else v


@dataclass
class HidReport:
    """One HID input report: reportID + report body bytes."""

    report_id: int
    data: bytes
    kind: str = ""  # "keyboard" | "mouse" (informational)

    def wire_bytes(self) -> bytes:
        """reportID-prefixed report (standard numbered-report framing)."""
        return bytes([self.report_id & 0xFF]) + self.data

    def bthid_bytes(self) -> bytes:
        """Interrupt-channel frame: 0xA1 (DATA/Input) + reportID + body."""
        return bytes([BTHID_DATA_INPUT, self.report_id & 0xFF]) + self.data


# --------------------------------------------------------------------------- #
# Keyboard (reportID 1) -- 8-byte boot layout: mod, reserved, k1..k6
# --------------------------------------------------------------------------- #
def keyboard_report(modifier_byte: int, key_usages) -> HidReport:
    keys = [u for u in dict.fromkeys(key_usages) if not (0xE0 <= u <= 0xE7)]
    if len(keys) > _KBD_MAX_KEYS:
        body = bytes([modifier_byte & 0xFF, 0x00]) + bytes([0x01] * _KBD_MAX_KEYS)
        return HidReport(REPORT_ID_KEYBOARD, body, "keyboard")
    body = bytearray([modifier_byte & 0xFF, 0x00])
    body += bytes(keys)
    body += bytes(_KBD_MAX_KEYS - len(keys))
    return HidReport(REPORT_ID_KEYBOARD, bytes(body), "keyboard")


def keyboard_release() -> HidReport:
    """All-keys-up report."""
    return HidReport(REPORT_ID_KEYBOARD, bytes(KEYBOARD_BODY_LEN), "keyboard")


def _char_reports(ch: str) -> list[HidReport]:
    usage, shift = char_to_usage(ch)
    mod = MOD_LSHIFT if shift else 0
    return [keyboard_report(mod, [usage]), keyboard_release()]


def type_text(text: str) -> list[HidReport]:
    """Expand a string into the down/up keyboard report sequence that types it."""
    seq: list[HidReport] = []
    for ch in text:
        seq += _char_reports(ch)
    return seq


def key_reports(name: str, modifiers: int = 0) -> list[HidReport]:
    """Press + release a single key (optionally with modifiers held).

    `name` is a named key ("enter", "tab", "f5", ...) OR a single printable char ("c", "/") so that
    modifier shortcuts work -- key_reports("c", MOD_LGUI) is Cmd+C. For the single-char form the
    passed `modifiers` are authoritative and the char's own shift is ignored (mirrors the firmware
    hidbridge_mode1.h `key` handler)."""
    key = name.lower()
    if key in KEY_NAME_TO_USAGE:
        usage = KEY_NAME_TO_USAGE[key]
    elif len(name) == 1:
        usage, _shift = char_to_usage(name)
    else:
        raise ValueError(f"unknown key name {name!r}")
    return [keyboard_report(modifiers, [usage]), keyboard_release()]


def passcode_reports(passcode: str, press_enter: bool = True) -> list[HidReport]:
    """Reports that type a device passcode at the lock screen, then optionally Enter.

    SECURITY: does NOT retain or log the passcode. Callers MUST NOT log the argument.
    """
    seq: list[HidReport] = []
    for ch in passcode:
        # digits only for a numeric passcode, but accept any typeable char (alphanumeric passcodes)
        seq += _char_reports(ch)
    if press_enter:
        seq += [keyboard_report(0, [KEY_NAME_TO_USAGE["enter"]]), keyboard_release()]
    return seq


# --------------------------------------------------------------------------- #
# Mouse (reportID 2) -- buttons, dx, dy, wheel (each signed int8 except buttons)
# --------------------------------------------------------------------------- #
def mouse_report(buttons: int = 0, dx: int = 0, dy: int = 0, wheel: int = 0) -> HidReport:
    body = bytes([
        buttons & 0xFF,
        _clamp(dx, -128, 127) & 0xFF,
        _clamp(dy, -128, 127) & 0xFF,
        _clamp(wheel, -128, 127) & 0xFF,
    ])
    return HidReport(REPORT_ID_MOUSE, body, "mouse")


def mouse_release() -> HidReport:
    return HidReport(REPORT_ID_MOUSE, bytes(MOUSE_BODY_LEN), "mouse")


_BUTTON_NAME = {
    "left": BTN_LEFT, "right": BTN_RIGHT, "middle": BTN_MIDDLE,
    "back": BTN_BACK, "forward": BTN_FORWARD,
    # numeric aliases (1-based button index) for the CLI
    "1": BTN_LEFT, "2": BTN_RIGHT, "3": BTN_MIDDLE, "4": BTN_BACK, "5": BTN_FORWARD,
}


def mouse_click_reports(button: str = "left") -> list[HidReport]:
    b = _BUTTON_NAME.get(button.lower())
    if b is None:
        raise ValueError(f"unknown mouse button {button!r}")
    return [mouse_report(buttons=b), mouse_release()]


def _split_delta(d: int) -> list[int]:
    """Break a large relative delta into <=127-per-report signed steps."""
    steps: list[int] = []
    remaining = d
    while remaining != 0:
        step = _clamp(remaining, -_MOUSE_STEP, _MOUSE_STEP)
        steps.append(step)
        remaining -= step
    return steps


def mouse_move_reports(dx: int, dy: int) -> list[HidReport]:
    """A relative move of (dx, dy), split into bounded int8 steps per report."""
    xs = _split_delta(dx)
    ys = _split_delta(dy)
    n = max(len(xs), len(ys), 1)
    xs += [0] * (n - len(xs))
    ys += [0] * (n - len(ys))
    return [mouse_report(dx=xs[i], dy=ys[i]) for i in range(n)]


def mouse_scroll_reports(clicks: int) -> list[HidReport]:
    """Vertical wheel scroll of `clicks` detents (signed), split into bounded int8 steps."""
    steps = _split_delta(clicks)
    return [mouse_report(wheel=s) for s in steps] or [mouse_report(wheel=0)]


# --------------------------------------------------------------------------- #
# Absolute pointer helpers (fraction 0..1 of the screen -> 0..ABS_AXIS_MAX)
# --------------------------------------------------------------------------- #
def frac_to_abs(frac: float) -> int:
    """Map a screen fraction (0.0 = left/top edge .. 1.0 = right/bottom edge) to a u16 axis value.

    Clamps out-of-range fractions into [0, ABS_AXIS_MAX]. This is the single scaling rule shared by
    the absolute mouse and the digitizer, so calibration (which fraction hits which pixel) lives in
    one place.
    """
    return _clamp(int(round(frac * ABS_AXIS_MAX)), 0, ABS_AXIS_MAX)


# --------------------------------------------------------------------------- #
# Absolute Mouse (reportID 3) -- buttons, X(u16 LE), Y(u16 LE) ABSOLUTE
# --------------------------------------------------------------------------- #
def abs_mouse_report(buttons: int = 0, x: int = 0, y: int = 0) -> HidReport:
    """One absolute-pointer report. x/y are u16 in [0, ABS_AXIS_MAX] (little-endian on the wire)."""
    body = bytes([buttons & 0xFF]) + struct.pack(
        "<HH", _clamp(x, 0, ABS_AXIS_MAX), _clamp(y, 0, ABS_AXIS_MAX))
    return HidReport(REPORT_ID_ABS_MOUSE, body, "abs_mouse")


def abs_mouse_release(x: int = 0, y: int = 0) -> HidReport:
    """Buttons-up report, keeping the pointer at (x, y)."""
    return abs_mouse_report(0, x, y)


def abs_move_reports(x_frac: float, y_frac: float) -> list[HidReport]:
    """Move the absolute pointer to (x_frac, y_frac) of the screen (no button)."""
    x, y = frac_to_abs(x_frac), frac_to_abs(y_frac)
    return [abs_mouse_report(0, x, y)]


def abs_click_reports(x_frac: float, y_frac: float, button: str = "left") -> list[HidReport]:
    """Move to (x_frac, y_frac) then press+release a button THERE -- an absolute-position click.

    Emits: move-to (button up) -> button down at (x,y) -> button up at (x,y). Holding the position
    across all three keeps the click anchored to the target for hosts that only act on the
    button-down transition.
    """
    b = _BUTTON_NAME.get(button.lower())
    if b is None:
        raise ValueError(f"unknown mouse button {button!r}")
    x, y = frac_to_abs(x_frac), frac_to_abs(y_frac)
    return [abs_mouse_report(0, x, y), abs_mouse_report(b, x, y), abs_mouse_report(0, x, y)]


# --------------------------------------------------------------------------- #
# Digitizer / Touch Screen (reportID 4) -- flags, contactID, X(u16 LE), Y(u16 LE)
# --------------------------------------------------------------------------- #
def digitizer_report(tip: bool = False, contact_id: int = 0,
                     x: int = 0, y: int = 0, in_range: bool | None = None) -> HidReport:
    """One touch report. tip = finger touching; in_range defaults to tip (a touch is in range)."""
    if in_range is None:
        in_range = tip
    flags = (TIP_SWITCH if tip else 0) | (IN_RANGE if in_range else 0)
    body = bytes([flags, contact_id & 0xFF]) + struct.pack(
        "<HH", _clamp(x, 0, ABS_AXIS_MAX), _clamp(y, 0, ABS_AXIS_MAX))
    return HidReport(REPORT_ID_DIGITIZER, body, "digitizer")


def digitizer_lift() -> HidReport:
    """Finger-up, out-of-range report (all zero)."""
    return HidReport(REPORT_ID_DIGITIZER, bytes(DIGITIZER_BODY_LEN), "digitizer")


def digitizer_tap_reports(x_frac: float, y_frac: float,
                          contact_id: int = 0) -> list[HidReport]:
    """A single-finger tap at (x_frac, y_frac): finger-down at (x,y) then finger-up."""
    x, y = frac_to_abs(x_frac), frac_to_abs(y_frac)
    return [digitizer_report(tip=True, contact_id=contact_id, x=x, y=y), digitizer_lift()]

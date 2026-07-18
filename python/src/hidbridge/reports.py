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

KEYBOARD_BODY_LEN = 8          # mod, reserved, k1..k6
MOUSE_BODY_LEN = 4             # buttons, dx, dy, wheel
_KBD_MAX_KEYS = 6
_MOUSE_STEP = 127              # max signed-int8 relative delta per report

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


# --------------------------------------------------------------------------- #
# Keyboard LAYOUTS -- char -> (HID usage, modifier byte)
# --------------------------------------------------------------------------- #
# A BLE HID keyboard sends physical-key USAGES (scancodes); the HOST turns those into characters using
# ITS active keyboard layout. So to type the RIGHT character on a non-US host we must send the usage +
# modifier(s) that, under THAT host's layout, land on the character we want. The modifier byte carries
# Shift (MOD_LSHIFT 0x02) and/or AltGr (MOD_RALT 0x40 -- "Right Alt", the layer that reaches @ [ ] { }
# \ | etc. on the Danish and other European layouts).
#
# `us` is the original US-QWERTY table and is the DEFAULT: with layout="us" every function here emits
# BYTE-FOR-BYTE what it always did (a shift bool is just modifier 0x00 / 0x02).
MOD_ALTGR = MOD_RALT     # AltGr = the Right-Alt modifier, the third (European) shift level


def _us_char_to_usage(ch: str) -> tuple[int, int]:
    if ch in _BASE_USAGE:
        return _BASE_USAGE[ch], 0
    if ch.isalpha() and ch.lower() in _BASE_USAGE:
        return _BASE_USAGE[ch.lower()], MOD_LSHIFT     # uppercase letter -> shift
    if ch in _SHIFTED_SYMBOLS:
        return _BASE_USAGE[_SHIFTED_SYMBOLS[ch]], MOD_LSHIFT
    raise ValueError(f"no US HID scancode for character {ch!r}")


# Danish (ISO) layout. Letters + digits share the US usages (Danish is QWERTY, z/y NOT swapped), so
# they are handled programmatically; this table is the punctuation + Danish-letter + AltGr layer.
# Verified against the on-disk X11 keymap (/usr/share/X11/xkb/symbols/dk), the Windows KBDDA reference
# (kbdlayout.info/KBDDA), and the USB HID usage tables -- see docs/bt-hid-keyboard-layouts.md.
# DEAD KEYS OMITTED: on a Danish host `` ` ``, ^, ~ (and the accent glyphs) are dead keys -- one
# keypress arms an accent for the NEXT letter, so they cannot be sent as a single self-contained
# keystroke. They are intentionally absent here; typing one raises ValueError (documented limitation).
_DA_SYMBOLS: dict[str, tuple[int, int]] = {
    " ": (0x2C, 0), "\n": (0x28, 0), "\t": (0x2B, 0),
    # Danish letters (positional: ae=US ';', oe=US "'", aa=US '[')
    "æ": (0x33, 0), "Æ": (0x33, MOD_LSHIFT),   # ae / AE
    "ø": (0x34, 0), "Ø": (0x34, MOD_LSHIFT),   # oe / OE
    "å": (0x2F, 0), "Å": (0x2F, MOD_LSHIFT),   # aa / AA
    # number-row Shift layer
    "!": (0x1E, MOD_LSHIFT), '"': (0x1F, MOD_LSHIFT), "#": (0x20, MOD_LSHIFT),
    "%": (0x22, MOD_LSHIFT), "&": (0x23, MOD_LSHIFT), "/": (0x24, MOD_LSHIFT),
    "(": (0x25, MOD_LSHIFT), ")": (0x26, MOD_LSHIFT), "=": (0x27, MOD_LSHIFT),
    # number-row AltGr layer
    "@": (0x1F, MOD_ALTGR), "£": (0x20, MOD_ALTGR), "$": (0x21, MOD_ALTGR),
    "€": (0x22, MOD_ALTGR),                          # euro
    "{": (0x24, MOD_ALTGR), "[": (0x25, MOD_ALTGR), "]": (0x26, MOD_ALTGR), "}": (0x27, MOD_ALTGR),
    "|": (0x2E, MOD_ALTGR), "\\": (0x64, MOD_ALTGR),
    # '+ ?' key (US '-' position)
    "+": (0x2D, 0), "?": (0x2D, MOD_LSHIFT),
    # "' *" key (US backslash position) -- base apostrophe is a REAL char (not dead)
    "'": (0x31, 0), "*": (0x31, MOD_LSHIFT),
    # bottom-row punctuation
    ",": (0x36, 0), ";": (0x36, MOD_LSHIFT),
    ".": (0x37, 0), ":": (0x37, MOD_LSHIFT),
    "-": (0x38, 0), "_": (0x38, MOD_LSHIFT),
    # ISO key left of Z (usage 0x64)
    "<": (0x64, 0), ">": (0x64, MOD_LSHIFT),
}


def _da_char_to_usage(ch: str) -> tuple[int, int]:
    if "a" <= ch <= "z":
        return 0x04 + (ord(ch) - ord("a")), 0
    if "A" <= ch <= "Z":
        return 0x04 + (ord(ch) - ord("A")), MOD_LSHIFT
    if "1" <= ch <= "9":
        return 0x1E + (ord(ch) - ord("1")), 0
    if ch == "0":
        return 0x27, 0
    if ch in _DA_SYMBOLS:
        return _DA_SYMBOLS[ch]
    raise ValueError(f"no Danish HID scancode for character {ch!r} "
                     f"(note: ` ^ ~ are dead keys on the Danish layout and are not supported)")


# British (ISO) layout. Letters + digits share the US usages (UK is QWERTY, z/y NOT swapped), so they
# are handled programmatically; this table is the punctuation layer. UK differs from US only at a few
# keys: " (Shift+2), GBP (Shift+3), @ (Shift+'), # ~ on the ISO key (usage 0x32), \ | on the ISO key
# left of Z (usage 0x64), and not-sign on Shift+grave. UNLIKE Danish there are NO dead keys -- ` is a
# real backtick -- so every printable ASCII char is a single self-contained keystroke. Verified against
# the on-disk X11 keymap (/usr/share/X11/xkb/symbols/gb), the Windows KBDUK reference
# (kbdlayout.info/KBDUK), and the USB HID usage tables -- see docs/bt-hid-keyboard-layouts.md.
_UK_SYMBOLS: dict[str, tuple[int, int]] = {
    " ": (0x2C, 0), "\n": (0x28, 0), "\t": (0x2B, 0),
    # number-row Shift layer (differs from US only at '2' -> " and '3' -> GBP)
    "!": (0x1E, MOD_LSHIFT), '"': (0x1F, MOD_LSHIFT), "£": (0x20, MOD_LSHIFT),
    "$": (0x21, MOD_LSHIFT), "%": (0x22, MOD_LSHIFT), "^": (0x23, MOD_LSHIFT),
    "&": (0x24, MOD_LSHIFT), "*": (0x25, MOD_LSHIFT), "(": (0x26, MOD_LSHIFT),
    ")": (0x27, MOD_LSHIFT),
    # number-row AltGr layer (euro on the '4' key)
    "€": (0x21, MOD_ALTGR),
    # right-of-alpha punctuation (same usages as US)
    "-": (0x2D, 0), "_": (0x2D, MOD_LSHIFT),
    "=": (0x2E, 0), "+": (0x2E, MOD_LSHIFT),
    "[": (0x2F, 0), "{": (0x2F, MOD_LSHIFT),
    "]": (0x30, 0), "}": (0x30, MOD_LSHIFT),
    # ISO '# ~' key (usage 0x32) -- UK-specific position for # and ~
    "#": (0x32, 0), "~": (0x32, MOD_LSHIFT),
    ";": (0x33, 0), ":": (0x33, MOD_LSHIFT),
    # "' @" key (US "'" position) -- UK: apostrophe base, @ shifted (US has " here)
    "'": (0x34, 0), "@": (0x34, MOD_LSHIFT),
    # grave key -- ` is a REAL char (not dead), not-sign shifted
    "`": (0x35, 0), "¬": (0x35, MOD_LSHIFT),
    # ISO '\ |' key left of Z (usage 0x64)
    "\\": (0x64, 0), "|": (0x64, MOD_LSHIFT),
    # bottom-row punctuation (same usages as US)
    ",": (0x36, 0), "<": (0x36, MOD_LSHIFT),
    ".": (0x37, 0), ">": (0x37, MOD_LSHIFT),
    "/": (0x38, 0), "?": (0x38, MOD_LSHIFT),
}


def _uk_char_to_usage(ch: str) -> tuple[int, int]:
    if "a" <= ch <= "z":
        return 0x04 + (ord(ch) - ord("a")), 0
    if "A" <= ch <= "Z":
        return 0x04 + (ord(ch) - ord("A")), MOD_LSHIFT
    if "1" <= ch <= "9":
        return 0x1E + (ord(ch) - ord("1")), 0
    if ch == "0":
        return 0x27, 0
    if ch in _UK_SYMBOLS:
        return _UK_SYMBOLS[ch]
    raise ValueError(f"no British HID scancode for character {ch!r}")


# The layout registry. Add a layout by dropping a `char -> (usage, mod)` resolver here; the whole
# type/key path threads a `layout=` name through to it. (A future per-target layout just passes the
# target's layout name per call.)
LAYOUTS: dict[str, "callable"] = {
    "us": _us_char_to_usage, "da": _da_char_to_usage, "uk": _uk_char_to_usage,
}
DEFAULT_LAYOUT = "us"


def char_to_usage(ch: str, layout: str = DEFAULT_LAYOUT) -> tuple[int, int]:
    """Map a single character to (HID usage, modifier_byte) under `layout`.

    The modifier byte carries Shift (MOD_LSHIFT) and/or AltGr (MOD_RALT). layout="us" (the default)
    reproduces the original US-QWERTY mapping byte-for-byte. Raises ValueError for an unmapped
    character or an unknown layout name.
    """
    resolve = LAYOUTS.get(layout)
    if resolve is None:
        raise ValueError(f"unknown keyboard layout {layout!r} (known: {', '.join(sorted(LAYOUTS))})")
    return resolve(ch)


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


def _char_reports(ch: str, layout: str = DEFAULT_LAYOUT) -> list[HidReport]:
    usage, mod = char_to_usage(ch, layout)
    return [keyboard_report(mod, [usage]), keyboard_release()]


def type_text(text: str, layout: str = DEFAULT_LAYOUT) -> list[HidReport]:
    """Expand a string into the down/up keyboard report sequence that types it.

    `layout` selects the target host's keyboard layout ("us" default, "da" Danish, "uk" British); it
    decides which usage + modifier(s) each character maps to. With the default it is byte-identical to
    before.
    """
    seq: list[HidReport] = []
    for ch in text:
        seq += _char_reports(ch, layout)
    return seq


def key_reports(name: str, modifiers: int = 0, layout: str = DEFAULT_LAYOUT) -> list[HidReport]:
    """Press + release a single key (optionally with modifiers held).

    `name` is a named key ("enter", "tab", "f5", ...) OR a single printable char ("c", "/") so that
    modifier shortcuts work -- key_reports("c", MOD_LGUI) is Cmd+C. For the single-char form the
    passed `modifiers` are authoritative and the char's own shift is ignored (mirrors the firmware
    hidbridge_mode1.h `key` handler)."""
    key = name.lower()
    if key in KEY_NAME_TO_USAGE:
        usage = KEY_NAME_TO_USAGE[key]
    elif len(name) == 1:
        usage, _mod = char_to_usage(name, layout)
    else:
        raise ValueError(f"unknown key name {name!r}")
    return [keyboard_report(modifiers, [usage]), keyboard_release()]


def passcode_reports(passcode: str, press_enter: bool = True,
                     layout: str = DEFAULT_LAYOUT) -> list[HidReport]:
    """Reports that type a device passcode at the lock screen, then optionally Enter.

    SECURITY: does NOT retain or log the passcode. Callers MUST NOT log the argument.
    """
    seq: list[HidReport] = []
    for ch in passcode:
        # digits only for a numeric passcode, but accept any typeable char (alphanumeric passcodes)
        seq += _char_reports(ch, layout)
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

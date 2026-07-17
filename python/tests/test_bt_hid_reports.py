"""Offline tests for the Bluetooth-HID peripheral report layer (hidbridge/reports.py).

This is a STANDARD USB boot-protocol HID emulator (keyboard reportID 1 + mouse reportID 2) that we
present to an iPad/iPhone over a USB Bluetooth dongle, so we can wake/unlock/type/click without a
physical poke. The report layer is PURE (no bluetooth deps) so it unit-tests fully offline; the
byte layouts here are the standard USB-HID boot layouts (NOT Apple's UniversalHID), because the
iPad talks standard HID to a Bluetooth peripheral.

SECURITY: these tests use the placeholder passcode "0000" ONLY. Never put a real passcode here.
"""

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

from hidbridge import reports as R  # noqa: E402


# --------------------------------------------------------------------------- #
# Scancode map (HID usage page 0x07)
# --------------------------------------------------------------------------- #
def test_scancode_digits_and_letters_and_enter():
    assert R.char_to_usage("1") == (0x1E, False)
    assert R.char_to_usage("0") == (0x27, False)
    assert R.char_to_usage("a") == (0x04, False)
    assert R.char_to_usage("z") == (0x1D, False)
    assert R.char_to_usage("A") == (0x04, True)   # shifted
    assert R.char_to_usage(" ") == (0x2C, False)
    # named keys
    assert R.KEY_NAME_TO_USAGE["enter"] == 0x28
    assert R.KEY_NAME_TO_USAGE["esc"] == 0x29
    assert R.KEY_NAME_TO_USAGE["backspace"] == 0x2A


def test_scancode_shifted_symbol():
    # '!' is shift+'1'
    assert R.char_to_usage("!") == (0x1E, True)
    # '@' is shift+'2'
    assert R.char_to_usage("@") == (0x1E + 1, True)


# --------------------------------------------------------------------------- #
# Keyboard report bytes (8-byte boot layout: mod, reserved, k1..k6)
# --------------------------------------------------------------------------- #
def test_keyboard_report_layout():
    rep = R.keyboard_report(R.MOD_LSHIFT, [0x04])  # shift+a
    assert rep.report_id == 1
    assert rep.data == bytes([R.MOD_LSHIFT, 0x00, 0x04, 0, 0, 0, 0, 0])
    # wire_bytes prepends the numbered-report reportID
    assert rep.wire_bytes() == bytes([1, R.MOD_LSHIFT, 0x00, 0x04, 0, 0, 0, 0, 0])
    # bthid interrupt-channel framing prepends 0xA1 (DATA/Input) + reportID
    assert rep.bthid_bytes() == bytes([0xA1, 1, R.MOD_LSHIFT, 0x00, 0x04, 0, 0, 0, 0, 0])


def test_keyboard_release_is_all_zero():
    rep = R.keyboard_release()
    assert rep.report_id == 1
    assert rep.data == bytes(8)


# --------------------------------------------------------------------------- #
# type_text -> exact down/up report sequence
# --------------------------------------------------------------------------- #
def test_type_text_digits_exact_sequence():
    seq = R.type_text("12")
    # each char = key-down report then all-zero release
    assert [r.data for r in seq] == [
        bytes([0x00, 0x00, 0x1E, 0, 0, 0, 0, 0]),  # '1' down
        bytes(8),                                   # release
        bytes([0x00, 0x00, 0x1F, 0, 0, 0, 0, 0]),  # '2' down
        bytes(8),                                   # release
    ]


def test_type_text_uppercase_sets_shift_modifier():
    seq = R.type_text("A")
    assert seq[0].data == bytes([R.MOD_LSHIFT, 0x00, 0x04, 0, 0, 0, 0, 0])
    assert seq[1].data == bytes(8)


# --------------------------------------------------------------------------- #
# Passcode: digit reports + Enter, never echoing the secret in repr
# --------------------------------------------------------------------------- #
def test_passcode_reports_digits_then_enter():
    seq = R.passcode_reports("0000")
    # 4 digits * 2 (down/up) + Enter down + up = 10 reports
    assert len(seq) == 10
    # first digit '0' usage 0x27 down
    assert seq[0].data == bytes([0x00, 0x00, 0x27, 0, 0, 0, 0, 0])
    # last two are Enter down then release
    assert seq[-2].data == bytes([0x00, 0x00, 0x28, 0, 0, 0, 0, 0])
    assert seq[-1].data == bytes(8)


def test_passcode_reports_no_enter_when_disabled():
    seq = R.passcode_reports("00", press_enter=False)
    assert len(seq) == 4  # 2 digits * (down/up), no enter


# --------------------------------------------------------------------------- #
# key_by_name
# --------------------------------------------------------------------------- #
def test_key_report_by_name_with_modifier():
    # Cmd+Space (spotlight-style); GUI = left-cmd
    seq = R.key_reports("space", modifiers=R.MOD_LGUI)
    assert seq[0].data == bytes([R.MOD_LGUI, 0x00, 0x2C, 0, 0, 0, 0, 0])
    assert seq[1].data == bytes(8)


def test_key_report_single_char_with_modifier():
    # Cmd+C -- a single printable char as the key name, with an explicit modifier (browser passthrough
    # uses this shape). 'c' is usage 0x06; the char's own shift is ignored, only MOD_LGUI is applied.
    seq = R.key_reports("c", modifiers=R.MOD_LGUI)
    assert seq[0].data == bytes([R.MOD_LGUI, 0x00, 0x06, 0, 0, 0, 0, 0])
    assert seq[1].data == bytes(8)
    # an uppercase name resolves to the SAME base usage (no auto-shift added)
    assert R.key_reports("C", modifiers=R.MOD_LGUI)[0].data == seq[0].data


def test_key_report_unknown_multichar_name_raises():
    with pytest.raises(ValueError, match="unknown key name"):
        R.key_reports("notakey")


# --------------------------------------------------------------------------- #
# Mouse report bytes (reportID 2: buttons, dx, dy, wheel)
# --------------------------------------------------------------------------- #
def test_mouse_move_report_layout():
    rep = R.mouse_report(buttons=0, dx=5, dy=-3, wheel=0)
    assert rep.report_id == 2
    assert rep.data == bytes([0x00, 5, (-3) & 0xFF, 0x00])
    assert rep.bthid_bytes() == bytes([0xA1, 2, 0x00, 5, (-3) & 0xFF, 0x00])


def test_mouse_move_clamps_to_int8():
    rep = R.mouse_report(dx=500, dy=-500)
    assert rep.data[1] == 127        # +dx clamps to +127
    assert rep.data[2] == (-128) & 0xFF  # -dy clamps to -128


def test_mouse_click_down_up_sequence():
    seq = R.mouse_click_reports("left")
    assert seq[0].data == bytes([R.BTN_LEFT, 0, 0, 0])   # button down, no motion
    assert seq[1].data == bytes([0x00, 0, 0, 0])          # release
    seq_r = R.mouse_click_reports("right")
    assert seq_r[0].data[0] == R.BTN_RIGHT


def test_report_descriptor_declares_both_report_ids():
    d = R.REPORT_DESCRIPTOR
    # Report ID items: 0x85 0x01 (keyboard) and 0x85 0x02 (mouse)
    assert bytes([0x85, 0x01]) in d
    assert bytes([0x85, 0x02]) in d
    # starts with Usage Page (Generic Desktop), Usage (Keyboard)
    assert d[:4] == bytes([0x05, 0x01, 0x09, 0x06])
    # two application collections closed -> ends with two End-Collection (0xC0) items
    assert d[-1] == 0xC0


def test_mouse_move_split_into_bounded_steps():
    # a large move is split into <=127-per-axis relative steps
    seq = R.mouse_move_reports(300, 0)
    assert all(-128 <= r.data[1] - (256 if r.data[1] > 127 else 0) <= 127 for r in seq)
    total = sum((r.data[1] if r.data[1] <= 127 else r.data[1] - 256) for r in seq)
    assert total == 300


# --------------------------------------------------------------------------- #
# HID report-descriptor parser (walks the Report Map, so tests assert usages/flags,
# not just raw byte substrings). Short-item format per the USB HID 1.11 spec:
#   prefix = (bTag<<4)|(bType<<2)|bSize ; bSize 0/1/2/3 -> 0/1/2/4 data bytes.
# --------------------------------------------------------------------------- #
_SIZE = {0: 0, 1: 1, 2: 2, 3: 4}

# Input main-item flag bits (USB HID 6.2.2.5)
IN_CONSTANT = 0x01
IN_VARIABLE = 0x02
IN_RELATIVE = 0x04


def _u(data):  # little-endian unsigned int from item data bytes
    v = 0
    for i, b in enumerate(data):
        v |= b << (8 * i)
    return v


def parse_report_map(desc):
    """Parse a HID report descriptor into a list of Input items.

    Each Input item -> dict(report_id, usage_page, usages, size, count, flags). `usages` is the
    list of Local Usage tags that applied to that Input (expanded across Usage Min/Max ranges).
    """
    items = []
    i = 0
    g = {"usage_page": None, "report_id": None, "report_size": None, "report_count": None}
    local_usages = []
    usage_min = usage_max = None
    while i < len(desc):
        prefix = desc[i]
        if prefix == 0xFE:  # long item (unused here) -- skip by its length byte
            ln = desc[i + 1]
            i += 3 + ln
            continue
        size = _SIZE[prefix & 0x03]
        tag_type = prefix & 0xFC
        data = desc[i + 1:i + 1 + size]
        val = _u(data)
        if tag_type == 0x04:            # Usage Page (global)
            g["usage_page"] = val
        elif tag_type == 0x84:          # Report ID (global)
            g["report_id"] = val
        elif tag_type == 0x74:          # Report Size (global)
            g["report_size"] = val
        elif tag_type == 0x94:          # Report Count (global)
            g["report_count"] = val
        elif tag_type == 0x08:          # Usage (local)
            local_usages.append(val)
        elif tag_type == 0x18:          # Usage Minimum (local)
            usage_min = val
        elif tag_type == 0x28:          # Usage Maximum (local)
            usage_max = val
        elif tag_type == 0x80:          # Input (main)
            usages = list(local_usages)
            if usage_min is not None and usage_max is not None:
                usages += list(range(usage_min, usage_max + 1))
            items.append({
                "report_id": g["report_id"],
                "usage_page": g["usage_page"],
                "usages": usages,
                "size": g["report_size"],
                "count": g["report_count"],
                "flags": val,
            })
            local_usages = []
            usage_min = usage_max = None
        elif tag_type in (0xA0, 0xC0):  # Collection / End Collection -> clear locals
            local_usages = []
            usage_min = usage_max = None
        i += 1 + size
    return items


def _inputs_for(report_id):
    return [it for it in parse_report_map(R.REPORT_DESCRIPTOR) if it["report_id"] == report_id]


# --------------------------------------------------------------------------- #
# Report map is well-formed AND additive (all 4 report IDs present, originals intact)
# --------------------------------------------------------------------------- #
def test_report_map_declares_all_four_report_ids():
    d = R.REPORT_DESCRIPTOR
    for rid in (1, 2, 3, 4):
        assert bytes([0x85, rid]) in d, f"missing Report ID {rid}"
    # balanced collections: every Collection (0xA1) closes with an End Collection (0xC0)
    assert d.count(0xA1) == d.count(0xC0)


def test_relative_mouse_unchanged_still_relative():
    # reportID 2 X/Y must still carry the RELATIVE flag (we did not disturb the working mouse)
    xy = [it for it in _inputs_for(R.REPORT_ID_MOUSE)
          if it["usage_page"] == 0x01 and 0x30 in it["usages"]]
    assert xy, "relative mouse X/Y input missing"
    assert all(it["flags"] & IN_RELATIVE for it in xy)


# --------------------------------------------------------------------------- #
# Absolute mouse (reportID 3): Generic Desktop X/Y, ABSOLUTE (no Relative flag), 0..32767
# --------------------------------------------------------------------------- #
def test_abs_mouse_descriptor_xy_absolute_16bit():
    inputs = _inputs_for(R.REPORT_ID_ABS_MOUSE)
    xy = [it for it in inputs if it["usage_page"] == 0x01
          and 0x30 in it["usages"] and 0x31 in it["usages"]]
    assert xy, "absolute mouse X(0x30)/Y(0x31) input missing"
    it = xy[0]
    assert it["flags"] & IN_VARIABLE          # Data,Var
    assert not (it["flags"] & IN_RELATIVE)    # ABSOLUTE -- the whole point
    assert it["size"] == 16                    # 16-bit axis
    # button byte present on the Buttons page (0x09) as variable
    btn = [b for b in inputs if b["usage_page"] == 0x09]
    assert btn and (btn[0]["flags"] & IN_VARIABLE)


def test_abs_mouse_descriptor_logical_max_is_32767():
    # the 3-byte Logical Maximum item 0x26 0xFF 0x7F (=32767) must appear
    assert bytes([0x26, 0xFF, 0x7F]) in R.REPORT_DESCRIPTOR
    assert R.ABS_AXIS_MAX == 32767


def test_abs_mouse_report_layout():
    rep = R.abs_mouse_report(buttons=R.BTN_LEFT, x=0x1234, y=0x7FFF)
    assert rep.report_id == 3
    assert rep.kind == "abs_mouse"
    # buttons, X LE, Y LE
    assert rep.data == bytes([R.BTN_LEFT, 0x34, 0x12, 0xFF, 0x7F])
    assert len(rep.data) == R.ABS_MOUSE_BODY_LEN


def test_frac_to_abs_endpoints_and_clamp():
    assert R.frac_to_abs(0.0) == 0
    assert R.frac_to_abs(1.0) == 32767
    assert R.frac_to_abs(0.5) == round(0.5 * 32767)
    assert R.frac_to_abs(-1.0) == 0          # clamp low
    assert R.frac_to_abs(2.0) == 32767       # clamp high


def test_abs_move_reports_scales_fraction():
    seq = R.abs_move_reports(0.25, 0.75)
    assert len(seq) == 1
    x = seq[0].data[1] | (seq[0].data[2] << 8)
    y = seq[0].data[3] | (seq[0].data[4] << 8)
    assert x == R.frac_to_abs(0.25)
    assert y == R.frac_to_abs(0.75)
    assert seq[0].data[0] == 0               # no buttons on a plain move


def test_abs_click_reports_move_press_release_all_anchored():
    seq = R.abs_click_reports(0.5, 0.5, "left")
    assert [r.data[0] for r in seq] == [0, R.BTN_LEFT, 0]  # up, down, up
    # X/Y identical across all three reports (click stays anchored to target)
    xys = {(r.data[1], r.data[2], r.data[3], r.data[4]) for r in seq}
    assert len(xys) == 1


# --------------------------------------------------------------------------- #
# Digitizer / touch screen (reportID 4): usage page 0x0D, Tip Switch, X/Y absolute 0..32767
# --------------------------------------------------------------------------- #
def test_digitizer_descriptor_usage_page_and_tip_switch():
    d = R.REPORT_DESCRIPTOR
    # Digitizers usage page (0x0D) + Touch Screen (0x04) + Finger (0x22) present
    assert bytes([0x05, 0x0D]) in d
    assert bytes([0x09, 0x04]) in d     # Touch Screen
    assert bytes([0x09, 0x22]) in d     # Finger
    inputs = _inputs_for(R.REPORT_ID_DIGITIZER)
    # Tip Switch (0x42) on the digitizer page
    tip = [it for it in inputs if it["usage_page"] == 0x0D and 0x42 in it["usages"]]
    assert tip, "digitizer Tip Switch (0x42) input missing"


def test_digitizer_descriptor_xy_absolute_16bit():
    inputs = _inputs_for(R.REPORT_ID_DIGITIZER)
    xs = [it for it in inputs if it["usage_page"] == 0x01 and 0x30 in it["usages"]]
    ys = [it for it in inputs if it["usage_page"] == 0x01 and 0x31 in it["usages"]]
    assert xs and ys, "digitizer X/Y on Generic Desktop missing"
    for it in xs + ys:
        assert it["flags"] & IN_VARIABLE
        assert not (it["flags"] & IN_RELATIVE)   # absolute touch coordinates
        assert it["size"] == 16


def test_digitizer_report_layout_tip_and_coords():
    rep = R.digitizer_report(tip=True, contact_id=0, x=0x0100, y=0x7FFF)
    assert rep.report_id == 4
    assert rep.kind == "digitizer"
    # flags(tip|in_range), contactID, X LE, Y LE
    assert rep.data == bytes([R.TIP_SWITCH | R.IN_RANGE, 0x00, 0x00, 0x01, 0xFF, 0x7F])
    assert len(rep.data) == R.DIGITIZER_BODY_LEN


def test_digitizer_tap_is_down_then_up():
    seq = R.digitizer_tap_reports(0.5, 0.5)
    assert len(seq) == 2
    # down: tip + in-range set
    assert seq[0].data[0] & R.TIP_SWITCH
    assert seq[0].data[0] & R.IN_RANGE
    # up: all zero (finger lifted, out of range)
    assert seq[1].data == bytes(R.DIGITIZER_BODY_LEN)


# --------------------------------------------------------------------------- #
# 5-button relative mouse (reportID 2): add button 4 (Back, 0x08) + button 5
# (Forward, 0x10) so iPadOS AssistiveTouch "Customize Additional Buttons" can map
# one of them. Buttons 1-3 (left/right/middle) MUST stay byte-identical.
# --------------------------------------------------------------------------- #
def test_mouse_descriptor_declares_five_buttons():
    # reportID 2 buttons live on the Buttons usage page (0x09) as a variable input.
    btn = [it for it in _inputs_for(R.REPORT_ID_MOUSE) if it["usage_page"] == 0x09]
    assert btn, "mouse buttons input missing"
    it = btn[0]
    assert it["flags"] & IN_VARIABLE
    # Usage Minimum 1 .. Usage Maximum 5 -> buttons 1..5 declared
    assert set(it["usages"]) >= {1, 2, 3, 4, 5}, f"expected buttons 1..5, got {it['usages']}"
    assert it["count"] == 5 and it["size"] == 1
    # the constant padding after the 5 buttons must be 3 bits so the buttons byte stays 1 byte
    pad = [it for it in _inputs_for(R.REPORT_ID_MOUSE)
           if (it["flags"] & IN_CONSTANT) and it["size"] == 3]
    assert pad and pad[0]["count"] == 1, "expected 3-bit constant padding to fill the buttons byte"


def test_mouse_back_forward_button_bits():
    assert R.BTN_BACK == 0x08
    assert R.BTN_FORWARD == 0x10
    back = R.mouse_click_reports("back")
    assert back[0].data == bytes([0x08, 0, 0, 0])   # button 4 down
    assert back[1].data == bytes([0x00, 0, 0, 0])   # release
    fwd = R.mouse_click_reports("forward")
    assert fwd[0].data[0] == 0x10                    # button 5 down
    # numeric aliases work too
    assert R.mouse_click_reports("4")[0].data[0] == 0x08
    assert R.mouse_click_reports("5")[0].data[0] == 0x10


def test_existing_three_buttons_byte_unchanged():
    # regression: left/right/middle must be byte-identical to the original 3-button device.
    assert R.BTN_LEFT == 0x01 and R.BTN_RIGHT == 0x02 and R.BTN_MIDDLE == 0x04
    assert R.mouse_click_reports("left")[0].data == bytes([0x01, 0, 0, 0])
    assert R.mouse_click_reports("right")[0].data == bytes([0x02, 0, 0, 0])
    assert R.mouse_click_reports("middle")[0].data == bytes([0x04, 0, 0, 0])
    # body length + release unchanged
    assert R.MOUSE_BODY_LEN == 4
    assert R.mouse_release().data == bytes(4)

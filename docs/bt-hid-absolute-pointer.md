# BLE HID absolute pointer + digitizer (exact on-screen taps)

Goal: tap an EXACT on-screen (x, y) on an iPad over BLE HID, instead of the unreliable
relative-cursor drift (`move`/`click` on the AssistiveTouch cursor). This lets tools/tests land a
tap on a known Settings row / Spotlight result / dialog button deterministically.

Status: OFFLINE implementation + tests DONE (branch `bt-hid-abs-pointer`). Hardware verdict PENDING
(needs the dongle + an iPad forget/re-pair -- see "Empirical test", below). This file records the
design and will hold the webcam-verified verdict + calibration.

## What was added (ADDITIVE -- relative mouse + keyboard untouched)

Two NEW HID report IDs were appended to the single-source-of-truth report map in
`python/src/hidbridge/reports.py` (`REPORT_DESCRIPTOR`). The existing keyboard (reportID 1) and RELATIVE
mouse (reportID 2) descriptors + report builders are byte-for-byte unchanged.

### (a) Absolute mouse -- reportID 3 (`REPORT_ID_ABS_MOUSE`)
Generic Desktop (0x01) / Mouse (0x02) / Pointer (0x01), same shape as the relative mouse but:
- X (0x30) + Y (0x31) marked ABSOLUTE -- Input flag `Data,Var,Abs` (0x02), the Relative bit (0x04)
  is OMITTED (the relative mouse uses `Data,Var,Rel` = 0x06).
- Logical Minimum 0 / Logical Maximum 32767 (item `0x26 0xFF 0x7F`), Report Size 16, Count 2.
- Button byte first (buttons 1-3, Buttons page 0x09), then X (u16 LE), then Y (u16 LE).
- Body = 5 bytes: `[buttons, Xlo, Xhi, Ylo, Yhi]`.

USB HID usage-table citations: Generic Desktop page 0x01; Mouse 0x02, Pointer 0x01, X 0x30, Y 0x31,
Wheel 0x38 (HID Usage Tables 1.4, section 4 "Generic Desktop Page"). Button page 0x09. The
Rel/Abs distinction is the Input main-item bit 2 (USB HID 1.11, section 6.2.2.5).

### (b) Digitizer / touch screen -- reportID 4 (`REPORT_ID_DIGITIZER`)
Digitizers page (0x0D) / Touch Screen (0x04) / Finger (0x22), a single-contact touchscreen:
- Tip Switch (0x42) bit 0 -- finger touching (the "press").
- In Range (0x32) bit 1 -- finger detected.
- 6-bit padding to fill byte 0.
- Contact Identifier (0x51) -- byte 1 (single contact, id 0).
- X (0x30) + Y (0x31) on the Generic Desktop page, ABSOLUTE, Logical 0..32767, Report Size 16.
- Body = 6 bytes: `[flags, contactID, Xlo, Xhi, Ylo, Yhi]`.

USB HID citations: Digitizers page 0x0D; Touch Screen 0x04, Finger 0x22, Tip Switch 0x42,
In Range 0x32, Contact Identifier 0x51 (HID Usage Tables 1.4, section 16 "Digitizers Page"). The
X/Y axes reuse Generic Desktop 0x30/0x31 per the digitizer convention.

## Coordinate space
Both reports carry X/Y as a u16 in `[0, 32767]` (`R.ABS_AXIS_MAX`). Callers pass a screen FRACTION
0.0..1.0 and `R.frac_to_abs()` scales it (0.0 = left/top edge, 1.0 = right/bottom edge). 32767 =
0x7FFF is the largest value that fits Logical Maximum without a 32-bit (sign-ambiguous) item, so it
is the standard normalized-absolute full scale. Actual pixel mapping (orientation/origin/scaling)
must be CALIBRATED against the physical panel once the hardware verdict is in (see below).

## API
`python/src/hidbridge/reports.py` (pure, offline-tested):
- `abs_mouse_report(buttons, x, y)` / `abs_move_reports(x_frac, y_frac)` /
  `abs_click_reports(x_frac, y_frac, button)` -- move-to, press, release, all anchored at (x,y).
- `digitizer_report(tip, contact_id, x, y, in_range)` / `digitizer_tap_reports(x_frac, y_frac)`.
- `frac_to_abs(frac)` -- the shared 0..1 -> 0..32767 scaler.

`python/src/hidbridge/controller.py` (`HidController`):
- `move_abs(x_frac, y_frac)`, `tap_abs(x_frac, y_frac, button)` -- absolute mouse.
- `touch_tap(x_frac, y_frac)` -- digitizer.
- `play()` timeline actions: `move_abs`, `tap_abs`, `touch`.

`python/src/hidbridge/cli.py`:
- `bt_hid move-abs <x> <y>` / `bt_hid tap <x> <y> [button]` -- absolute mouse.
- `bt_hid touch <x> <y>` -- digitizer.

`python/src/hidbridge/ble_hid.py` (the BlueZ GATT daemon): two NEW HID Report characteristics (Report
Reference [3,1] and [4,1]) are served in the HID service; `_emit` routes a frame's `kind`
(`abs_mouse` / `digitizer`) to the matching characteristic. Daemon control cmds: `move_abs`,
`tap_abs`, `touch`.

## Offline tests
`python/tests/test_bt_hid_reports.py` -- a real HID report-descriptor PARSER (`parse_report_map`,
walks the short-item format) asserts: all 4 report IDs present; the relative mouse is STILL relative
(regression guard); the absolute mouse X/Y are Var+Abs, 16-bit, NOT relative; the digitizer has the
Tip Switch on page 0x0D and Var+Abs 16-bit X/Y; exact report byte layouts; frac->abs scaling+clamp.
`python/tests/test_bt_hid_controller.py` -- the controller tags frames `abs_mouse`/`digitizer` and
emits move/press/release + down/up correctly.

## Empirical test (the OPEN question -- do NOT assume)
iPadOS pointer support (13.4+) is documented as RELATIVE (the circular AssistiveTouch cursor).
Whether iOS honours an ABSOLUTE mouse axis and/or a HID digitizer is unknown and must be tested on
real glass, webcam-verified.

Procedure (dongle = hci1 only; coordinate device access first):
1. iOS caches the GATT report map PER BOND -> the maintainer must FORGET the bt_hid device on the iPad and
   RE-PAIR so it reads the new report map (reportIDs 3 + 4).
2. Run the daemon (`sudo python/src/hidbridge/bt_hid pair-mode`) on the dongle; confirm the iPad connects.
3. Command a KNOWN target and webcam-verify (`tools/webcam_ipad.sh`) where the tap LANDS:
   - `bt_hid move-abs 0.5 0.5` then `0.9 0.1`, `0.1 0.9` -- does the cursor JUMP to those absolute
     spots (absolute honoured) or DRIFT relatively (ignored)?
   - `bt_hid tap 0.5 0.9` at a known Settings row / Spotlight result.
   - `bt_hid touch 0.5 0.9` -- does a digitizer tap register as a real touch?
4. If honoured: verify with `btmon`/daemon logs that the reports were DELIVERED, then map several
   fractions to observed pixels to derive the orientation/origin/scaling calibration.
5. If ignored: prove the negative decisively -- `btmon` showing the reports WERE delivered but had
   NO effect -- and keep the relative + on-screen-verify fallback.

## VERDICT (2026-07-10, mini6 / iPad, webcam-verified)

NEGATIVE, decisively and with delivery proof: iPadOS on mini6 does NOT honour either the
absolute-mouse position (reportID 3) or the HID digitizer (reportID 4). The reports are ACCEPTED at
the HID layer (iOS parsed the new report map after a fresh forget/re-pair and subscribed -- daemon
logged StartNotify on char6=abs-mouse and char7=digitizer) and DELIVERED over the air, but iOS does
not act on the absolute coordinate or the touch.

Evidence (capture bundle `captures/bt_hid_abs_20260710_031031/`, kept on disk, jpgs gitignored):
- Delivery PROVEN: `btmon.btsnoop` shows 12 ATT Handle Value Notifications -- 8 of len 7 (2-byte
  handle + 5-byte abs-mouse body) and 4 of len 8 (2-byte handle + 6-byte digitizer body). The daemon
  log matches: 8x `notify 2a4d len=5` + 4x `notify 2a4d len=6`. Every report reached the iPad.
- Absolute MOUSE ignored: the 5 abs-move commands to the 4 corners + centre
  (`absmouse_tl/tr/br/bl/mid.jpg`) are pixel-identical (mean-abs-diff ~1.8 = webcam sensor noise);
  the cursor did NOT snap to any commanded coordinate. Confirmed by a follow-up A/B that needed no
  re-pair (`ab1_rel_topleft` -> `ab2_absmove_BR`): parked the RELATIVE cursor top-left, then commanded
  an ABSOLUTE move to bottom-right (0.95, 0.95) -- the on-screen pointer/hover-highlight did not move
  (mean-abs-diff 1.61 = noise). A pure-absolute report carries no relative dx/dy, so with the abs
  axis ignored iOS sees "no movement" and the cursor stays put.
- Digitizer ignored: the 2 taps (`digitizer_mid` at 0.5,0.5 and `digitizer_low` at 0.5,0.85) each
  changed nothing (mean-abs-diff 1.63, ~12-58 px of noise). No touch registered.
- One thing that DID work: the absolute-CLICK (`absmouse_tap_result`, move-to 0.5,0.9 + button
  press/release) coincided with a large content change (the Settings sidebar scrolled -- diff spread
  across all the text, `diff_mid_to_tapclick.png`; global brightness flat ~23, so not a dim). Read:
  the BUTTON press is honoured (it is position-independent in the report) and fired at the pointer's
  EXISTING location, NOT the commanded (0.5, 0.9). So a click acts, but you cannot aim it by
  coordinate with these descriptors.

Conclusion: exact on-screen (x, y) tapping via a STANDARD absolute-mouse or a combined-descriptor
digitizer is NOT available on iPadOS (matches the documented "iPadOS pointer is relative" behaviour;
the AssistiveTouch cursor is driven by relative deltas only). The reports are harmless + additive
(they do not disturb the working keyboard/relative-mouse), so they stay in the tree behind their own
report IDs, but tools/tests must NOT rely on them landing a coordinate.

### Fallback (what to use instead)
Keep driving with the RELATIVE mouse (reportID 2) + keyboard, and land targets by CLOSED-LOOP
on-screen verification: move a relative step, snap the webcam (`tools/webcam_ipad.sh`), correct, click
-- rather than open-loop absolute coordinates. For fixed dialogs (e.g. "Trust"), a
relative-nudge-from-a-known-anchor + webcam-confirm is the reliable path.

### One untested hypothesis (would need another forget/re-pair -- the maintainer's physical tap)
The digitizer here was the 4th report in a COMBINED report map (kbd+mouse+abs-mouse+digitizer under
one HID service). Some iOS versions only accept a touchscreen digitizer as a DEDICATED HID device
(its own report map / GATT HID instance, GAP appearance = digitizer, no mouse alongside). Whether a
standalone-digitizer device flips this negative is unproven. It is a real but lower-odds avenue that
costs one more the maintainer re-pair; deferred unless prioritised. (The absolute MOUSE is not worth retrying
-- iPadOS pointer is fundamentally relative.)

## VERDICT 2 -- STANDALONE dedicated devices (2026-07-28, iPad, on-glass observation)

The one open hypothesis above is now TESTED and also NEGATIVE. Built two throwaway Pico research
firmwares (firmware/pico-abspointer/, off main), each a DEDICATED single-report HID device -- no
keyboard, no relative mouse alongside:
- absmouse: only the absolute-mouse report, GAP appearance 0x03C2 (Mouse).
- digitizer: only the touchscreen report, GAP appearance 0x03C5 (Digitizer Tablet).
Each auto-sweeps to the 4 corners + centre on subscribe. Flashed to the spare Pico (5BEDFC28), fresh
forget/re-pair per variant.

Result:
- Standalone ABS-MOUSE: pairs, but iPadOS shows NO CURSOR at all -- it does not enable a pointer for a
  pure-absolute mouse (the pointer is relative-driven, so a device with no relative axis gives it
  nothing to show). No coordinate control.
- Standalone DIGITIZER (dedicated, digitizer appearance): pairs, but NO taps register on glass.
- On the Pico serial (the firmware sweeps/taps only after the report CCCD is subscribed) NEITHER
  variant emitted a sweep/tap log, i.e. iOS never subscribed to either report -- the device is
  accepted at pairing but not ENGAGED.

Caveat on rigor: this round had NO independent over-the-air delivery proof -- the Pico's USB-CDC serial
is DTR-gated + flaky, and none of the host BLE controllers would power on for a btmon capture. So the
"nothing" rests on the on-glass observation (+ the silent-serial subscription signal), not a btsnoop.
But it matches VERDICT 1, which DID have btmon delivery proof, so the conclusion holds.

CONCLUSION (final): a DEDICATED/standalone device does NOT flip the negative. iPadOS honours neither an
absolute mouse nor a HID digitizer for exact-coordinate control -- combined map or standalone. The
absolute-pointer-on-iPad question is CLOSED: there is no absolute on-screen (x,y) control of an iPad
over BLE HID with these descriptors. Keep the relative + closed-loop webcam approach. The
firmware/pico-abspointer/ project is throwaway (kept off main); the spare Pico was reflashed back to
the shipping 2.0.0 firmware afterward.

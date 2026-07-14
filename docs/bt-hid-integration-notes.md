# bt_hid integration branch -- reconciliation notes (review draft)

Branch: `bt-hid-integration` (based on `origin/main`). This is a REVIEW DRAFT -- NOT merged to
`main`. It converges the six overnight BT-HID branches into one coherent, fully-green integration
branch. Offline only (no hardware was touched). Verified: full `uv run --extra test pytest tests/`
green (860 passed, 46 skipped, 1 xfailed) + `uv run --with ruff ruff check .` clean, ASCII-only.

## Merge order

Merged onto `origin/main` in this order (real merge commits, never rebase):

1. `bt-hid-mouse-buttons` -- 5-button relative mouse (reportID 2). Fast-forward, no conflict.
2. `bt-hid-abs-pointer` -- negative-hardware-verdict doc for abs pointer/digitizer. 1 README conflict.
3. `bt-hid-esp32` -- ESP32 NimBLE backend + firmware. Clean merge (files new to this line).
4. `bt-hid-abs-esp32` -- abs-mouse(3)/digitizer(4) in the ESP32 firmware+transport. add/add conflicts.
5. `bt-hid-pico` -- Pico BTstack backend + firmware. cli.py + README conflicts.
6. `bt-hid-abs-pico` -- abs-mouse(3)/digitizer(4) in the Pico custom report map. add/add conflicts.
7. `bt-hid-selftest-central` -- BLE-central self-test + the CH340 (`1a86`) autodetect safety fix.
   add/add + content conflicts (this was merged LAST so its safety fix lands on top).

Plus two convergence commits I made by hand (see conflicts below):
- `701d9cc9` -- propagate the 5-button relative-mouse change into the ESP32 firmware REPORT_MAP.
- the Pico firmware 5-button propagation was folded into the abs-pico merge commit (`3d2ccd5d`).

## Conflicts and how each was resolved

The single source of truth is `python/src/hidbridge/reports.py::REPORT_DESCRIPTOR`; every firmware descriptor
must equal it byte-for-byte (enforced by the guard tests). All resolutions honor that.

### abs-pointer merge
- `README.md` (abs-pointer intro line): kept the 5-button paragraph from HEAD AND took abs-pointer's
  more-accurate heading ("iOS does NOT honour these (verdict 2026-07-10)") over HEAD's older
  "EXPERIMENTAL -- pending a hardware verdict" wording.

### abs-esp32 merge (add/add on 4 esp32 files)
- Merge base predated the esp32 files, so both sides "added" them -> whole-file add/add conflicts.
  Verified HEAD's copies were byte-identical to the esp32-branch tip, so abs-esp32 = esp32 + the abs
  commit is a strict superset. Took `--theirs` (abs-esp32) for `firmware/esp32/src/main.cpp`,
  `transport_esp32.py`, `esp32_live_verify.py`, `test_bt_hid_esp32.py`.
- Then `701d9cc9`: the guard test went RED (firmware REPORT_MAP index 65 = `0x03` vs reports' `0x05`)
  because the 5-button change (mouse-buttons) only lived in reports.py. Propagated the 3-byte delta
  into the ESP32 firmware's Report ID 2 buttons block ONLY (Usage Max 3->5, Report Count 3->5,
  padding Report Size 5->3). Abs-mouse (ID 3) buttons stay at 3. Guard GREEN.

### pico merge
- `cli.py`: the two branches had DIFFERENT backend architectures. HEAD (esp32) has a
  backend-agnostic router: `_init_backend()` builds `_ALT_TRANSPORT` via
  `controller.make_transport(<backend>)` and every report command already routes through it.
  The pico branch shipped a dedicated `_pico_dispatch()` + `--pico-port`. Resolved by KEEPING the
  generic architecture (Pico rides `make_transport("pico")` -> `transport_pico.make_transport()`)
  and DROPPING `_pico_dispatch`. Kept ONE generic `--backend` (no restrictive `choices=` so esp32/
  pico/future backends all work), kept both `--port` and `--pico-port` (the latter as a pico alias),
  and made `_init_backend` read `args.port or args.pico_port`. No test depended on `_pico_dispatch`
  (the pico tests are transport-level).
- `README.md`: dropped the pico branch's duplicate "## Backends" section and instead folded Pico
  into the existing "HID backends" table (dongle/esp32/pico rows) + the module list. Kept HEAD's
  newer "Status (2026-07-10)" heading.
- FOLLOW-UP BUG (`2c39c6c1`): dropping `_pico_dispatch` meant Pico now routes through
  `controller.make_transport("pico") -> transport_pico.make_transport()`, but `transport_pico` had
  no `make_transport` factory (esp32 did) -> `--backend pico` would `AttributeError` at runtime.
  Caught by inspection, reproduced with a RED test, fixed by adding the factory (mirrors esp32).

### abs-pico merge (add/add on 4 pico files)
- Same pattern as abs-esp32: verified HEAD == pico-branch tip, took `--theirs` (abs-pico superset)
  for `firmware/pico/src/main.cpp`, `transport_pico.py`, `test_bt_hid_transport_pico.py`,
  `firmware/pico/README.md`. abs-pico is the commit that replaces arduino-pico's auto-map (which put
  the relative mouse at ID 3) with a CUSTOM `HID_REPORT_MAP` byte-identical to reports.py
  (keyboard 1, relative mouse 2, abs mouse 3, digitizer 4) and adds the firmware-map guard test.
- Folded into the merge commit: the same 3-byte 5-button delta into the Pico firmware's Report ID 2
  buttons block (guard was RED at index 65 for the identical reason). Guard GREEN.

### selftest-central merge (the safety fix lands last)
- `transport_esp32.py` -- the important COMBINE. HEAD had abs (`_KIND_PREFIX` with abs_mouse/
  digitizer) but the OLD name-hint autodetect; selftest-central had the CH340 safety fix
  (vendor-gated `detect_port` + `_tty_usb_vendor`, `_BANNED_VENDORS=("1a86",)`,
  `_ESP32_PREFERRED_VENDORS=("10c4",)`) but no abs. Merged BOTH: kept HEAD's abs `_KIND_PREFIX` +
  abs docstring, took the safety-fixed `detect_port`/`_tty_usb_vendor`/vendor constants, and DELETED
  the now-unused `_usb_hint()` + `_ESP32_USB_HINTS` (the old hint list contained "ch34", which is
  exactly what would have mis-selected the KVM CH340 -- it is gone).
- `firmware/esp32/src/main.cpp`, `firmware/pico/src/main.cpp` -- took `--ours` (HEAD = the fully
  converged abs + 5-button descriptors); selftest-central carried the older no-abs/3-button copies.
- `transport_pico.py`, `firmware/pico/README.md` -- git rerere re-applied the abs (HEAD) resolution;
  verified byte-identical to HEAD.

## Final report map (consistent EVERYWHERE: reports.py + both firmware descriptors)

| Report ID | Device                | Body (bytes)                         | Notes |
|-----------|-----------------------|--------------------------------------|-------|
| 1         | Keyboard              | 8  (mod, resv, k1..k6)               | boot-style |
| 2         | Relative mouse        | 4  (buttons, dx, dy, wheel)          | **5 buttons** (L/R/M + Back=0x08 + Forward=0x10) |
| 3         | Absolute mouse        | 5  (buttons, Xlo, Xhi, Ylo, Yhi)     | 3 buttons; **iOS ignores it** (proven negative) |
| 4         | Digitizer/touchscreen | 6  (flags, contactID, Xlo,Xhi,Ylo,Yhi) | **iOS ignores it** (proven negative) |

The abs (3) + digitizer (4) reports are ADDITIVE and harmless behind their own report IDs; iOS
does not act on them (memory `ipad-absolute-hid-ignored`). They are NOT wired as a working
exact-tap path. Correctness anchor: `test_bt_hid_esp32.py` and `test_bt_hid_transport_pico.py` each
assert the firmware REPORT_MAP == `reports.REPORT_DESCRIPTOR` byte-for-byte -- both green.

## CH340 safety fix -- CONFIRMED PRESENT

`python/src/hidbridge/transport_esp32.py` hard-excludes USB vendor `1a86` (the KVM CH340 on /dev/ttyUSB0)
via `_BANNED_VENDORS=("1a86",)` and prefers the CP2102 `10c4`; the old name-hint path that could
select "ch34" was removed. Regression-pinned by `python/tests/test_bt_hid_esp32_portsafety.py`
(5 tests, incl. "excludes KVM even as sole candidate" -> returns None). The Pico transport is
independently vendor-gated to Raspberry Pi `2e8a`, ttyACM-only
(`transport_pico.py::_PICO_USB_VENDOR`), so neither serial backend can ever drive the KVM.

## Residual concerns for review

- The Pico-over-generic-`make_transport` routing is now covered by transport-level tests
  (`test_controller_make_transport_selects_pico`), but the top-level `bt_hid` CLI `main()` /
  argparse dispatch runs under system python (needs `dbus` for the dongle path) and is NOT exercised
  by the offline pytest suite. Worth a quick `bt_hid --backend pico status` smoke on a host with a
  Pico before relying on the CLI end-to-end.
- `bt-hid-selftest-central`'s own status doc (`docs/bt-hid-central-selftest-status.md`) records the
  central self-test as LIVE-PENDING (an on-hardware bring-up step remains); that is unchanged here.
- The two hand-applied firmware 5-button deltas were byte-verified only via the guard tests (no
  re-flash). Any real device must be FORGOTTEN + RE-PAIRED once to pick up the new report map (iOS
  caches the GATT report map per bond) -- documented in the READMEs.

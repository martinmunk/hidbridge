# bt_hid integration v2 -- reconciliation notes (review draft)

Branch: `bt-hid-integration-v2` (== `origin/bt-hid-hidsession-ready`, HEAD `bfe635e9`). REVIEW
DRAFT -- NOT merged to `main`. Offline only; no hardware was touched during this reconcile (the
live device tests were deliberately NOT run). Supersedes `bt-hid-integration` (the earlier
draft) and its notes in `docs/bt-hid-integration-notes.md`.

Verified on this branch:
- `uv run --extra test pytest tests/ -q` -> **926 passed, 51 skipped, 1 xfailed** (bt_hid subset:
  176 passed, 10 skipped).
- `uv run --with ruff ruff check .` -> **All checks passed!**
- ASCII-only (reports.py / transport_esp32.py / conn.py / firmware verified non-ASCII clean).

## Key finding: the reconcile was ALREADY DONE

I mapped the DAG before assuming anything (`git merge-base --is-ancestor` for every branch pair).
`bt-hid-hidsession-ready` is a strict SUPERSET of every other bt-hid branch -- each one's HEAD is
already an ancestor of it:

| branch                    | HEAD      | contained in hidsession-ready? |
|---------------------------|-----------|--------------------------------|
| bt-hid-multi-device       | 49a74c81  | YES                            |
| bt-hid-mcu-robust         | b01baf93  | YES                            |
| bt-hid-mouse-buttons      | b976c865  | YES                            |
| bt-hid-abs-pointer        | dba6ceb6  | YES                            |
| bt-hid-abs-esp32          | 300d9a89  | YES                            |
| bt-hid-abs-pico           | 489aa795  | YES                            |
| bt-hid-esp32              | cdb975d7  | YES                            |
| bt-hid-pico               | d0895511  | YES                            |
| bt-hid-controller         | (anc.)    | YES                            |
| bt-hid-selftest-central   | a6f40f7c  | YES                            |
| bt-hid-integration        | 9bba54e5  | YES                            |

Therefore `bt-hid-integration-v2` is created with `git checkout -B bt-hid-integration-v2
origin/bt-hid-hidsession-ready` and there are **ZERO new merges and ZERO new conflicts to
resolve** -- doing otherwise would only create empty/duplicate merge commits. Everything the task
asked to converge is already converged. This document records the verification.

## The DAG

`bt-hid-hidsession-ready` is the deepest chain. Reading from `main` outward it is a single linear
progression whose early section is `bt-hid-integration` (which itself carries the six overnight
branch merges), then multi-device, then mcu-robust, then the readiness gate on top:

```
origin/main (5b1484f0)
  |
  |  == section that IS bt-hid-integration (9bba54e5) ==
  +- 57106728 esp32 backend + firmware
  +- 532a8dfc / 2ca1778f / d0895511  pico backend + live-verify
  +- 489aa795 pico abs-mouse(3) + digitizer(4)
  +- dba6ceb6 abs-pointer NEGATIVE hardware verdict doc
  +- 300d9a89 esp32 abs-mouse(3) + digitizer(4)
  +- e546526a / e54d8403 / 22f4d29d  BLE-central self-test + CH340(1a86) safety gate + async connect
  +- b976c865 5-button relative mouse (Back=btn4/Forward=btn5)   [== bt-hid-mouse-buttons HEAD]
  +- a6f40f7c central self-test LIVE-PENDING doc + CH340 propagation
  +- b4eed0f2 MERGE bt-hid-abs-pointer
  +- c2bef5db MERGE bt-hid-esp32
  +- d35921ee MERGE bt-hid-abs-esp32
  +- 701d9cc9 converge esp32 firmware REPORT_MAP -> 5-button relative mouse
  +- c6d34687 MERGE bt-hid-pico
  +- 3d2ccd5d MERGE bt-hid-abs-pico
  +- 8f011ab1 MERGE bt-hid-selftest-central   (CH340 fix lands on top here)
  +- 3c471021 docs: bt-hid-integration reconciliation notes
  +- 2c39c6c1 pico make_transport() factory
  +- 9bba54e5 docs: pico factory follow-up          <-- bt-hid-integration HEAD
  |
  +- 49a74c81 multi-dongle / multi-iOS + HidSession connect-on-demand   <-- bt-hid-multi-device HEAD
  +- b01baf93 MCU no-reset serial + connect-on-demand + FT2232H         <-- bt-hid-mcu-robust HEAD
  +- bfe635e9 HidSession readiness gate (enter() waits for iOS enable)  <-- bt-hid-hidsession-ready HEAD == v2
```

The six branch merges (b4eed0f2, c2bef5db, d35921ee, c6d34687, 3d2ccd5d, 8f011ab1) were made in
the ORIGINAL `bt-hid-integration` work; the conflicts they resolved are documented in
`docs/bt-hid-integration-notes.md` (README add/add conflicts, cli.py, and the two firmware
5-button REPORT_MAP propagations -- 701d9cc9 for ESP32, folded into 3d2ccd5d for Pico). v2 inherits
those resolutions unchanged; nothing was re-resolved.

## Merge order for v2

None required. v2 = hidsession-ready. The "merge the branches it does not already contain" step
found the contained-set to be complete. No `git merge` was run; no `--no-ff` empty merges were
manufactured.

## Convergence target -- verified on the v2 tree

### Report map (single source of truth: `python/src/hidbridge/reports.py::REPORT_DESCRIPTOR`)
- keyboard  = report ID 1 (8-byte boot layout: mod, reserved, k1..k6)
- mouse     = report ID 2, RELATIVE, **5 buttons** (L/R/M + Back=0x08/Forward=0x10), body buttons,dx,dy,wheel
- abs mouse = report ID 3 (Generic Desktop Mouse, X/Y absolute u16 0..32767)
- digitizer = report ID 4 (single-finger touchscreen, usage page 0x0D, X/Y absolute u16)

`REPORT_ID_KEYBOARD=1`, `REPORT_ID_MOUSE=2`, `REPORT_ID_ABS_MOUSE=3`, `REPORT_ID_DIGITIZER=4`;
`BTN_BACK=0x08`, `BTN_FORWARD=0x10`.

Both firmware descriptors match this map:
- `firmware/esp32/src/main.cpp` -- Report IDs 1/2(5-button)/3/4.
- `firmware/pico/src/main.cpp`  -- Report IDs 1/2(5-button L/R/M+Back(4)+Forward(5))/3/4.

The firmware-map == reports.py equality is enforced by the guard tests (the parser in
`python/tests/test_bt_hid_central_core.py` reads REPORT_DESCRIPTOR; `test_bt_hid_esp32.py` asserts
the exact serial frames incl. `abs_mouse` -> `A01ff7f0040`; `test_bt_hid_central_selftest.py`
checks each firmware's Report Reference descriptors against reports.py). All green.

The abs-mouse(3)/digitizer(4) reports are PROVEN-NEGATIVE on iPadOS (iOS ignores them -- see
`docs/bt-hid-absolute-pointer.md` / commit dba6ceb6). They are kept as additive, harmless report
IDs; they are NOT wired as an exact-tap path. Relative mouse (ID 2) remains the driveable pointer.

### CH340 (`1a86`) hard-exclusion -- INTACT
`python/src/hidbridge/transport_esp32.py`: `_BANNED_VENDORS = ("1a86",)`; `detect_port()` is vendor-gated
and `continue`s past any `1a86` tty ("NEVER the KVM CH340"). Prefers `10c4`/`0403`, and for an
FT2232H (`0403`) prefers the higher interface (channel B). Guarded by
`python/tests/test_bt_hid_esp32_portsafety.py` (green). The fix landed via e54d8403 and was
re-asserted on top by the selftest-central merge (8f011ab1) -- it survives at HEAD.

### API surface -- the single shared abstraction
`python/src/hidbridge/conn.py`:
- `class LinkBackend` -- base: `capabilities` dict (incl. `"per_device_identity"`), readiness hooks
  (`readiness_supported()`, `is_ready()`, `wait_ready(timeout, poll)`), `READY_SETTLE` fallback.
- `class HidSession` -- the shared session/gate: `enter()` -> `_await_ready()` blocks for the REAL
  iOS-subscribed readiness signal (default `ready_timeout=8.0`), with a settle fallback when
  readiness is unobservable and an optional `require_ready` strict mode.
- Backends: BlueZ dongle (`per_device_identity=False`), MCU serial base
  (`link_esp32.Esp32LinkBackend`, `link_pico.PicoLinkBackend`, both `per_device_identity=True`).
  `make_link_backend("esp32:/dev/ttyUSB1")` / `("pico:/dev/ttyACM0")` factory resolves them.

Readiness gate details: MCU backends surface a firmware `subscribed=<0|1>` ping field (old firmware
omits it -> parses None -> readiness unsupported -> settle fallback; backward compatible). The
BlueZ backend reports readiness when iOS issues StartNotify on the report chars. Verified by
`test_bt_hid_readiness.py`, `test_bt_hid_ready_signal.py`, `test_bt_hid_link_mcu.py`,
`test_bt_hid_hidsession.py` (all green).

### MCU robustness (mcu-robust / multi-device) -- present
No-reset serial open + connect-on-demand + FT2232H channel-B handling (b01baf93); multi-dongle /
multi-iOS support with per-adapter isolation and same-MAC warning (49a74c81). Covered by
`test_bt_hid_esp32_noreset.py`, `test_bt_hid_multi_adapter.py`, `test_bt_hid_adapter.py`,
`test_bt_hid_naming.py` (green).

## Ready to merge to main? -- recommendation

**Yes, mergeable as a review draft** -- with these notes for the reviewer:

1. This branch is byte-identical to `bt-hid-hidsession-ready`; if that branch was already blessed,
   v2 adds nothing but the branch name + this doc. Reviewing either is equivalent.
2. Full offline suite + ruff are green; all convergence invariants (report map, CH340 ban,
   readiness gate, per_device_identity capability, firmware==reports guards) are verified above.
3. What a reviewer should still confirm before main (NOT doable offline here):
   - Re-run the LIVE device tests (esp32/pico live_verify, multi-daemon, readiness_live) on real
     hardware -- they are skipped/gated offline. Today's mini6 bring-up already validated the
     three backends live; a final confirmation run is the only gap.
   - Decide whether the proven-negative abs-mouse(3)/digitizer(4) reports stay in the shipped
     descriptor (they are harmless + additive, but expand the report map for no iOS benefit).
4. MERGE (never rebase) into main, per project directive. The older `bt-hid-integration` branch and
   its `docs/bt-hid-integration-notes.md` are superseded by this branch; they can be deleted after
   the merge.

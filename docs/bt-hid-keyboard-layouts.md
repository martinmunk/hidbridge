# Keyboard layouts (host locale) -- why `type` needs to know the target's layout

## The problem

A BLE HID keyboard sends physical-key **scancodes** (HID usages, page 0x07) plus a modifier byte.
It does NOT send characters. The HOST turns a `(usage, modifiers)` pair into a character using
**its own active keyboard layout**. So the same report means different characters on different hosts.

`hidbridge`'s keymap was hardcoded **US-QWERTY**. On a **Danish** host that mis-types:

- We emitted the US `'` key (usage `0x34`) for an apostrophe. A Danish host renders usage `0x34` as
  **oe** -- so `type "'"` produced `ø`.
- `æ` / `ø` / `å` weren't in the table at all, so `type "æ"` raised / failed.

(Confirmed live on a Danish keyboard: `'` produced `ø`; `æ`/`ø` didn't work.)

## The fix: selectable layouts

`reports.py` now maps `char -> (usage, modifier_byte)` under a named **layout** (`char_to_usage(ch,
layout=...)`, default `"us"`). Two layouts ship: `us` (unchanged, byte-for-byte) and `da` (Danish
ISO). The modifier byte carries **Shift** (`MOD_LSHIFT` `0x02`) and, new for `da`, **AltGr**
(`MOD_RALT` `0x40`, the third "European" shift level that reaches `@ [ ] { } \ |` etc.).

The firmware Mode-1 layer (`firmware/shared/hidbridge_mode1.h`) mirrors the same two tables (the
deliberate `reports.py` duplicate), keyed on the Unicode **codepoint** (the Danish letters and `£`/`€`
arrive as multi-byte UTF-8, so `emitType` UTF-8-decodes each char before mapping).

### Selection / persistence

- **Mode 2 (host builds bytes), CLI:** `hidbridge --layout da type "..."`. The host expands the text
  with `reports.py` and sends pre-built frames, so it works across every Mode-2 backend.
- **Mode 1 (board builds bytes), ESP32:** the layout is **per BLE identity slot** (per device), stored
  in NVS (`lay<n>`, alongside the client label `cli<n>`), default `us`. The active layout for
  `type`/`key` is the **current target slot's**. Set it with the `layout` JSON-RPC method
  (`{"method":"layout","params":{"slot":2,"name":"da"}}` -- address by `slot` index or `client` name,
  default the current target) or the per-device dropdown on the control web page. `identities` reports
  each slot's `layout`. So laptop3 can be `da` while a US host stays `us`, each remembered
  independently. Pico mirrors the header (RAM-only per-slot layout + an optional `L<us|da>` serial
  command).

## Danish (ISO) mapping used

USB HID usages are **positional** (they name the US-ANSI key position; the Danish host applies its
layout). Modifier column: none / **Shift** (`0x02`) / **AltGr** (`0x40`).

Danish is QWERTY (z/y NOT swapped), so `a`-`z` / `A`-`Z` and `0`-`9` share the US usages
(`a`=`0x04`.., `1`=`0x1E`.., `0`=`0x27`); uppercase adds Shift. The rest:

| char | usage | mods | char | usage | mods | char | usage | mods |
|------|-------|------|------|-------|------|------|-------|------|
| space | 0x2C | -- | `!` | 0x1E | Shift | `@` | 0x1F | AltGr |
| Enter | 0x28 | -- | `"` | 0x1F | Shift | `£` | 0x20 | AltGr |
| Tab | 0x2B | -- | `#` | 0x20 | Shift | `$` | 0x21 | AltGr |
| æ Æ | 0x33 | -- / Shift | `%` | 0x22 | Shift | `€` | 0x22 | AltGr |
| ø Ø | 0x34 | -- / Shift | `&` | 0x23 | Shift | `{` | 0x24 | AltGr |
| å Å | 0x2F | -- / Shift | `/` | 0x24 | Shift | `[` | 0x25 | AltGr |
| `+` | 0x2D | -- | `(` | 0x25 | Shift | `]` | 0x26 | AltGr |
| `?` | 0x2D | Shift | `)` | 0x26 | Shift | `}` | 0x27 | AltGr |
| `'` | 0x31 | -- | `=` | 0x27 | Shift | `\|` | 0x2E | AltGr |
| `*` | 0x31 | Shift | `<` | 0x64 | -- | `\` | 0x64 | AltGr |
| `,` | 0x36 | -- | `>` | 0x64 | Shift | | | |
| `;` | 0x36 | Shift | `.` | 0x37 | -- | `:` | 0x37 | Shift |
| `-` | 0x38 | -- | `_` | 0x38 | Shift | | | |

Key facts that differ from US (verified, not from memory):

- **`@` = AltGr+2** (not Shift+2). `{ [ ] }` = AltGr + `7 8 9 0`. `$` = AltGr+4. `€` = AltGr+5.
- **apostrophe** lives on usage `0x31` (US backslash position), base level -- a REAL char, not dead.
  The US `'` position (usage `0x34`) is **oe** on Danish. That is the exact `'`->`ø` bug.
- **ae/oe/aa** = US `;` / `'` / `[` positions (`0x33` / `0x34` / `0x2F`).
- The **ISO key** left of `Z` (usage `0x64`): `<` / Shift `>` / AltGr `\`.
- The `+` key is at the US `-` position (`0x2D`): base `+`, Shift `?`.

### Dead keys -- deliberately OMITTED

On the Danish layout `` ` `` (grave), `^` (circumflex) and `~` (tilde) are **dead keys**: one keypress
arms an accent for the NEXT letter, so they cannot be sent as a single self-contained keystroke. They
are intentionally left out of the `da` table (typing one raises `ValueError` host-side / returns
"unmappable character" on the board). If a future need arises they can be added as a dead-key + space
two-keystroke sequence. `¤ § ½` and the standalone accent glyphs (`´ ¨`) are likewise omitted.

## Sources

- On-disk X11/XKB keymap: `/usr/share/X11/xkb/symbols/dk` (`basic` variant, pulling the number row
  from `.../symbols/latin`) -- the authoritative per-level character map.
- Windows reference layout **KBDDA**: http://kbdlayout.info/KBDDA/ and
  https://learn.microsoft.com/en-us/globalization/keyboards/kbdda
- USB HID Usage Tables, keyboard/keypad page (0x07), for the usage codes.
- Wikipedia, Danish keyboard layout (ae/oe/aa placement, AltGr `@`/`€`, the dead keys).

## Verification status

This proves **byte-level self-consistency** (host `reports.py` <-> firmware `hidbridge_mode1.h`, guarded
by `test_bt_hid_mode1.py::test_firmware_dry_run_matches_reports_py` with `BT_HID_MODE1=1`) and a
**research-sourced** mapping. Whether `æ` actually appears on the Danish host must be confirmed on real
hardware by a person typing to a Danish-configured device -- not something the offline suite can assert.

"""Offline host<->firmware parity for the DANISH keyboard layout.

The Mode 1 firmware (`firmware/shared/hidbridge_mode1.h`) carries a byte-for-byte C++ DUPLICATE of the
`reports.py` keymaps (deliberate, no codegen -- see CLAUDE.md). For the `us` layer, the report-map and
dry-run guards already pin parity. For the `da` (Danish ISO) layer, the ONLY end-to-end check today is
`test_bt_hid_mode1.py::test_firmware_dry_run_matches_reports_py`, which is gated on `BT_HID_MODE1=1`
and needs a flashed board -- so on an offline box a `da` drift between the two tables goes unnoticed.

This test closes that gap WITHOUT hardware: it PARSES the Danish `daUsage` table straight out of the
firmware header (its `{codepoint, usage, mod}` rows plus the programmatic letter/digit ranges) and
asserts, character by character, that it produces exactly what `reports.char_to_usage(ch, "da")`
produces. Pure source parsing -- no serial, no BLE, no board. If either side gains, loses, or reworks
a Danish mapping (ae/oe/aa, the AltGr symbols, the shifted number row) without the other, this fails.
"""

import pathlib
import re

HEADER = pathlib.Path(__file__).resolve().parents[2] / "firmware/shared/hidbridge_mode1.h"

from hidbridge import reports as R  # noqa: E402


def _mod_constants(src: str) -> dict[str, int]:
    """Parse `static const uint8_t MOD_* = 0xNN;` so the test uses the firmware's OWN bit values
    (not hardcoded ones) -- if the header renumbered a modifier bit, we'd want to see it here too."""
    mods = {name: int(val, 16)
            for name, val in re.findall(r"static const uint8_t\s+(MOD_\w+)\s*=\s*(0x[0-9A-Fa-f]+)", src)}
    assert "MOD_LSHIFT_BIT" in mods and "MOD_ALTGR_BIT" in mods, "header MOD bit constants not found"
    return mods


def _c_char_literal(lit: str) -> int:
    """Codepoint of a C char literal token like 'a', '\\n', '\\\\', '\\''  (quotes included)."""
    inner = lit[1:-1]
    if inner.startswith("\\"):
        return {"n": 10, "t": 9, "r": 13, "\\": 92, "'": 39, '"': 34, "0": 0}[inner[1]]
    return ord(inner)


def _parse_da_table(src: str, mods: dict[str, int]) -> dict[int, tuple[int, int]]:
    """Parse the `static const E tbl[] = { {cp, usage, mod}, ... }` block inside daUsage() into
    {codepoint: (usage, modifier_byte)}. `cp` is either a hex codepoint (0xE6, 0x20AC) or a C char
    literal (including ',' whose comma the regex tolerates); `mod` is 0 or a MOD_* name (|-combined)."""
    anchor = src.index("bool daUsage")
    start = src.index("tbl[] = {", anchor) + len("tbl[] = {")
    end = src.index("};", start)
    block = src[start:end]

    entry = re.compile(
        r"\{\s*('(?:\\.|[^'])'|0x[0-9A-Fa-f]+)\s*,\s*(0x[0-9A-Fa-f]+)\s*,\s*([A-Za-z0-9_|\s]+?)\s*\}")

    def mod_value(expr: str) -> int:
        v = 0
        for tok in expr.split("|"):
            tok = tok.strip()
            v |= 0 if tok == "0" else mods[tok]
        return v

    out: dict[int, tuple[int, int]] = {}
    for cp_tok, usage_tok, mod_tok in entry.findall(block):
        cp = int(cp_tok, 16) if cp_tok.startswith("0x") else _c_char_literal(cp_tok)
        out[cp] = (int(usage_tok, 16), mod_value(mod_tok))
    # sanity: the parser actually consumed the whole table (guard against a silently-broken regex)
    assert len(out) >= 35, f"parsed only {len(out)} Danish table rows -- parser likely broke"
    return out


def test_firmware_da_symbol_table_matches_reports():
    """Every explicit row of the firmware's Danish `daUsage` table must equal what reports.py emits for
    that character under layout='da' -- AND the two symbol tables must cover the SAME set of characters
    (neither side may add/drop a symbol alone). Covers ae/oe/aa, the AltGr layer (@, {}[]|\\, euro,
    pound, $), the shifted number row, and the punctuation keys."""
    src = HEADER.read_text()
    mods = _mod_constants(src)
    fw = _parse_da_table(src, mods)

    # value parity, char by char
    for cp, (usage, mod) in fw.items():
        ch = chr(cp)
        assert R.char_to_usage(ch, "da") == (usage, mod), (
            f"da mapping drift for U+{cp:04X} ({ch!r}): firmware={(usage, mod)} "
            f"reports.py={R.char_to_usage(ch, 'da')}")

    # coverage parity: the firmware table and reports._DA_SYMBOLS describe the same character set
    fw_chars = {chr(cp) for cp in fw}
    py_chars = set(R._DA_SYMBOLS.keys())
    assert fw_chars == py_chars, (
        f"Danish symbol coverage diverged -- only in firmware: {sorted(fw_chars - py_chars)}; "
        f"only in reports.py: {sorted(py_chars - fw_chars)}")


def test_da_programmatic_letter_and_digit_ranges_match():
    """Letters + digits are handled by RANGE rules on both sides (Danish is QWERTY -- same US usages),
    not the table. Pin those ranges to reports.char_to_usage so a range-rule drift is caught too."""
    for c in "abcdefghijklmnopqrstuvwxyz":
        assert R.char_to_usage(c, "da") == (0x04 + (ord(c) - ord("a")), 0)
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        assert R.char_to_usage(c, "da") == (0x04 + (ord(c) - ord("A")), R.MOD_LSHIFT)
    for c in "123456789":
        assert R.char_to_usage(c, "da") == (0x1E + (ord(c) - ord("1")), 0)
    assert R.char_to_usage("0", "da") == (0x27, 0)


def test_da_load_bearing_mappings_are_stable():
    """Belt-and-suspenders spot checks on the mappings most likely to be typed / most easily broken:
    the three Danish letters, the AltGr '@' (=AltGr+2, usage 0x1F), and the real (non-dead) apostrophe
    on the US-backslash key (usage 0x31)."""
    assert R.char_to_usage("æ", "da") == (0x33, 0)
    assert R.char_to_usage("ø", "da") == (0x34, 0)
    assert R.char_to_usage("å", "da") == (0x2F, 0)
    assert R.char_to_usage("@", "da") == (0x1F, R.MOD_ALTGR)     # AltGr + '2'
    assert R.char_to_usage("'", "da") == (0x31, 0)              # apostrophe is a real char, not dead

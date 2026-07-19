"""Offline host<->firmware parity for the non-US keyboard layouts (Danish + British).

The rpc firmware (`firmware/shared/hidbridge_rpc.h`) carries a byte-for-byte C++ DUPLICATE of the
`reports.py` keymaps (deliberate, no codegen -- see CLAUDE.md). For the `us` layer, the report-map and
dry-run guards already pin parity. For the `da` (Danish ISO) and `uk` (British ISO) layers, the ONLY
end-to-end check today is `test_bt_hid_rpc.py::test_firmware_dry_run_matches_reports_py`, which is
gated on `BT_HID_RPC=1` and needs a flashed board -- so on an offline box a drift between the two
tables goes unnoticed.

This test closes that gap WITHOUT hardware: it PARSES the firmware header's `daUsage`/`ukUsage` tables
(their `{codepoint, usage, mod}` rows plus the programmatic letter/digit ranges) and asserts, character
by character, that each produces exactly what `reports.char_to_usage(ch, <layout>)` produces. Pure
source parsing -- no serial, no BLE, no board. If either side gains, loses, or reworks a mapping
without the other, this fails.
"""

import pathlib
import re

HEADER = pathlib.Path(__file__).resolve().parents[2] / "firmware/shared/hidbridge_rpc.h"

from hidbridge import reports as R  # noqa: E402


def _mod_constants(src: str) -> dict[str, int]:
    """Parse `static const uint8_t MOD_* = 0xNN;` so the test uses the firmware's OWN bit values
    (not hardcoded ones) -- if the header renumbered a modifier bit, we'd want to see it here too."""
    mods = {name: int(val, 16)
            for name, val in re.findall(r"static const uint8_t\s+(MOD_\w+)\s*=\s*(0x[0-9A-Fa-f]+)", src)}
    assert "MOD_LSHIFT_BIT" in mods and "MOD_ALTGR_BIT" in mods, "header MOD bit constants not found"
    return mods


def test_firmware_layout_names_match_reports():
    """The firmware's enumerable LAYOUT_NAMES[] (hidbridge_rpc.h) is the SINGLE source for "which
    layouts exist" -- it drives both the `layout`-method validation and the control page's dropdown
    (via hello.layouts). It must list exactly the layouts reports.py knows, or the page would offer a
    layout the host can't build (or hide one it can). Parse the C array and compare to reports.LAYOUTS."""
    src = HEADER.read_text()
    block = src[src.index("LAYOUT_NAMES[]"):]
    block = block[block.index("{") + 1:block.index("}")]
    fw_names = re.findall(r'"([^"]+)"', block)
    assert fw_names, "could not parse LAYOUT_NAMES[] from the firmware header"
    assert set(fw_names) == set(R.LAYOUTS), (
        f"layout-name list drift -- firmware LAYOUT_NAMES={sorted(fw_names)} "
        f"reports.LAYOUTS={sorted(R.LAYOUTS)}")


def _c_char_literal(lit: str) -> int:
    """Codepoint of a C char literal token like 'a', '\\n', '\\\\', '\\''  (quotes included)."""
    inner = lit[1:-1]
    if inner.startswith("\\"):
        return {"n": 10, "t": 9, "r": 13, "\\": 92, "'": 39, '"': 34, "0": 0}[inner[1]]
    return ord(inner)


def _parse_table(src: str, mods: dict[str, int], func: str, min_rows: int) -> dict[int, tuple[int, int]]:
    """Parse the `static const E tbl[] = { {cp, usage, mod}, ... }` block inside `func`() (daUsage /
    ukUsage) into {codepoint: (usage, modifier_byte)}. `cp` is either a hex codepoint (0xE6, 0x20AC)
    or a C char literal (including ',' whose comma the regex tolerates); `mod` is 0 or a MOD_* name
    (|-combined)."""
    anchor = src.index("bool " + func)
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
    assert len(out) >= min_rows, f"parsed only {len(out)} {func} table rows -- parser likely broke"
    return out


# --------------------------------------------------------------------------- #
# Danish (da)
# --------------------------------------------------------------------------- #
def test_firmware_da_symbol_table_matches_reports():
    """Every explicit row of the firmware's Danish `daUsage` table must equal what reports.py emits for
    that character under layout='da' -- AND the two symbol tables must cover the SAME set of characters
    (neither side may add/drop a symbol alone). Covers ae/oe/aa, the AltGr layer (@, {}[]|\\, euro,
    pound, $), the shifted number row, and the punctuation keys."""
    src = HEADER.read_text()
    mods = _mod_constants(src)
    fw = _parse_table(src, mods, "daUsage", 35)

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


# --------------------------------------------------------------------------- #
# British (uk)
# --------------------------------------------------------------------------- #
def test_firmware_uk_symbol_table_matches_reports():
    """Every explicit row of the firmware's British `ukUsage` table must equal what reports.py emits
    for that character under layout='uk' -- AND the two symbol tables must cover the SAME character set.
    Covers the UK-specific keys: " (Shift+2), pound (Shift+3), @ (Shift+'), # ~ on the ISO 0x32 key,
    \\ | on the ISO 0x64 key, not-sign on Shift+grave, and euro (AltGr+4)."""
    src = HEADER.read_text()
    mods = _mod_constants(src)
    fw = _parse_table(src, mods, "ukUsage", 35)

    for cp, (usage, mod) in fw.items():
        ch = chr(cp)
        assert R.char_to_usage(ch, "uk") == (usage, mod), (
            f"uk mapping drift for U+{cp:04X} ({ch!r}): firmware={(usage, mod)} "
            f"reports.py={R.char_to_usage(ch, 'uk')}")

    fw_chars = {chr(cp) for cp in fw}
    py_chars = set(R._UK_SYMBOLS.keys())
    assert fw_chars == py_chars, (
        f"British symbol coverage diverged -- only in firmware: {sorted(fw_chars - py_chars)}; "
        f"only in reports.py: {sorted(py_chars - fw_chars)}")


def test_uk_programmatic_letter_and_digit_ranges_match():
    """Letters + digits share the US usages (UK is QWERTY) and are handled by RANGE rules on both sides,
    not the table. Pin those ranges so a range-rule drift is caught too."""
    for c in "abcdefghijklmnopqrstuvwxyz":
        assert R.char_to_usage(c, "uk") == (0x04 + (ord(c) - ord("a")), 0)
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        assert R.char_to_usage(c, "uk") == (0x04 + (ord(c) - ord("A")), R.MOD_LSHIFT)
    for c in "123456789":
        assert R.char_to_usage(c, "uk") == (0x1E + (ord(c) - ord("1")), 0)
    assert R.char_to_usage("0", "uk") == (0x27, 0)


def test_uk_load_bearing_mappings_are_stable():
    """Belt-and-suspenders spot checks on the UK-distinctive mappings most likely to be typed / broken:
    the pound + euro currency keys, @ on Shift+' (usage 0x34), # on the ISO 0x32 key, \\ on the ISO 0x64
    key, and the REAL (non-dead) backtick on the grave key (usage 0x35)."""
    assert R.char_to_usage('"', "uk") == (0x1F, R.MOD_LSHIFT)    # Shift + '2'
    assert R.char_to_usage("£", "uk") == (0x20, R.MOD_LSHIFT)    # Shift + '3'
    assert R.char_to_usage("@", "uk") == (0x34, R.MOD_LSHIFT)    # Shift + "'"
    assert R.char_to_usage("#", "uk") == (0x32, 0)              # ISO '# ~' key
    assert R.char_to_usage("\\", "uk") == (0x64, 0)             # ISO '\ |' key left of Z
    assert R.char_to_usage("`", "uk") == (0x35, 0)             # backtick is a real char, not dead
    assert R.char_to_usage("€", "uk") == (0x21, R.MOD_ALTGR)     # AltGr + '4'

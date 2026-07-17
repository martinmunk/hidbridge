"""Offline unit tests for the BLE-central HID self-test pure core (hidbridge/central_core.py).

These cover EVERY piece of the central self-test that does not need hardware:
  * the USB-HID report-descriptor parser, against reports.py's REPORT_DESCRIPTOR (the single source
    of truth) -- it must recover reports 1/2/3/4 with their load-bearing fields;
  * the report-map comparator: byte-exact, subset (arduino-pico-style renumbered/extended mouse),
    and a partial firmware map (ESP32 without the abs reports);
  * the GATT-tree helper find_report_characteristics, with a fake ObjectManager tree + a fake
    descriptor reader (no dbus);
  * the notification verdict, including the Pico's trailing-zero (pan) mouse padding.

No dbus / bluetooth import here -- runs under uv CPython.
"""

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

from hidbridge import central_core as C  # noqa: E402
from hidbridge import reports as R  # noqa: E402


# --------------------------------------------------------------------------- #
# UUID helper
# --------------------------------------------------------------------------- #
def test_uuid16_expands_to_full_128bit():
    assert C.uuid16(0x2A4B) == "00002a4b-0000-1000-8000-00805f9b34fb"
    assert C.REPORT_MAP_UUID == "00002a4b-0000-1000-8000-00805f9b34fb"
    assert C.REPORT_UUID == "00002a4d-0000-1000-8000-00805f9b34fb"
    assert C.REPORT_REF_UUID == "00002908-0000-1000-8000-00805f9b34fb"
    assert C.HID_SERVICE_UUID == "00001812-0000-1000-8000-00805f9b34fb"


# --------------------------------------------------------------------------- #
# Parser vs the SINGLE SOURCE OF TRUTH
# --------------------------------------------------------------------------- #
def test_parser_recovers_all_four_reports_from_reports_descriptor():
    reps = C.parse_report_descriptor(R.REPORT_DESCRIPTOR)
    by_id = {r.report_id: r for r in reps}
    assert sorted(by_id) == [1, 2, 3, 4]
    # application usages
    assert (by_id[1].app_usage_page, by_id[1].app_usage) == (0x01, 0x06)   # Keyboard
    assert (by_id[2].app_usage_page, by_id[2].app_usage) == (0x01, 0x02)   # Mouse
    assert (by_id[3].app_usage_page, by_id[3].app_usage) == (0x01, 0x02)   # Abs Mouse
    assert (by_id[4].app_usage_page, by_id[4].app_usage) == (0x0D, 0x04)   # Touch Screen


def test_parser_keyboard_fields_load_bearing():
    kbd = {r.report_id: r for r in C.parse_report_descriptor(R.REPORT_DESCRIPTOR)}[1]
    data = kbd.data_fields()
    # modifier byte: Data,Var,Abs (0x02), logical 0..1, Key Codes page
    assert data[0].usage_page == 0x07 and data[0].flags == 0x02
    assert (data[0].logical_min, data[0].logical_max) == (0, 1)
    # 6 key slots: Data,Array (0x00), logical 0..255
    assert data[1].flags == 0x00 and (data[1].logical_min, data[1].logical_max) == (0, 255)


def test_parser_mouse_relative_xy_signed_range():
    mouse = {r.report_id: r for r in C.parse_report_descriptor(R.REPORT_DESCRIPTOR)}[2]
    rel = [f for f in mouse.data_fields() if f.is_relative]
    assert rel, "mouse must have a relative X/Y/Wheel field"
    f = rel[0]
    assert f.flags & C.INPUT_RELATIVE and f.flags & C.INPUT_VARIABLE
    assert (f.logical_min, f.logical_max) == (-127, 127)   # signed range parsed correctly


def test_parser_abs_mouse_is_absolute_16bit():
    absm = {r.report_id: r for r in C.parse_report_descriptor(R.REPORT_DESCRIPTOR)}[3]
    xy = [f for f in absm.data_fields()
          if f.usage_page == 0x01 and f.is_variable and not f.is_relative]
    assert xy, "abs mouse must have absolute X/Y"
    assert (xy[0].logical_min, xy[0].logical_max) == (0, 32767)


def test_parser_digitizer_tip_and_abs_xy():
    digi = {r.report_id: r for r in C.parse_report_descriptor(R.REPORT_DESCRIPTOR)}[4]
    assert (digi.app_usage_page, digi.app_usage) == (0x0D, 0x04)
    # X/Y absolute 0..32767 on the Generic Desktop page
    xy = [f for f in digi.data_fields() if f.usage_page == 0x01]
    assert xy and (xy[0].logical_min, xy[0].logical_max) == (0, 32767)


# --------------------------------------------------------------------------- #
# Comparator
# --------------------------------------------------------------------------- #
def test_compare_byte_exact_self():
    diff = C.compare_report_maps(R.REPORT_DESCRIPTOR, R.REPORT_DESCRIPTOR)
    assert diff.byte_exact and diff.ok and not diff.mismatches


def _prefix_two_reports() -> bytes:
    """reports.py descriptor truncated to just the keyboard(1)+mouse(2) blocks (the ESP32 map)."""
    # find the start of the Abs-Mouse block (Report ID 3 = 0x85,0x03 after a Mouse collection).
    # Easiest: rebuild from the known esp32 firmware prefix -> the first two reports.
    reps = C.parse_report_descriptor(R.REPORT_DESCRIPTOR)
    assert [r.report_id for r in reps] == [1, 2, 3, 4]
    # Reconstruct by slicing the raw bytes at the marker for Report ID 3.
    marker = bytes([0x85, 0x03])   # Report ID (3)
    idx = R.REPORT_DESCRIPTOR.find(marker)
    # back up to the Usage Page (Generic Desktop) + Usage (Mouse) + Collection that precede it
    head = R.REPORT_DESCRIPTOR[:idx]
    coll = head.rfind(bytes([0x05, 0x01, 0x09, 0x02, 0xA1, 0x01]))
    return R.REPORT_DESCRIPTOR[:coll]


def test_compare_partial_firmware_map_matches_present_reports():
    # An ESP32 without abs firmware serves only reports 1 + 2. That is a valid map: the two present
    # reports must match reports.py, and 3/4 simply aren't there (harness asserts the min set).
    partial = _prefix_two_reports()
    diff = C.compare_report_maps(R.REPORT_DESCRIPTOR, partial)
    assert not diff.byte_exact
    assert diff.load_bearing_ok and diff.ok
    assert sorted(diff.matched_reports) == [1, 2]


def _pico_like_map() -> bytes:
    """A hand-built arduino-pico-style map: keyboard id 1 (same block) + mouse id 3 with an EXTRA
    pan axis (so it renumbers AND extends -- the two ways the Pico legitimately differs)."""
    kbd_end = R.REPORT_DESCRIPTOR.find(bytes([0x85, 0x03]))
    coll = R.REPORT_DESCRIPTOR.rfind(bytes([0x05, 0x01, 0x09, 0x02, 0xA1, 0x01]), 0, kbd_end)
    keyboard_block = R.REPORT_DESCRIPTOR[:R.REPORT_DESCRIPTOR.find(
        bytes([0x05, 0x01, 0x09, 0x02, 0xA1, 0x01]))]   # up to end of keyboard collection
    # mouse with report id 3, buttons + X/Y/Wheel/Pan (count 4) relative
    mouse = bytes([
        0x05, 0x01, 0x09, 0x02, 0xA1, 0x01,  # Usage Page GD, Usage Mouse, Collection App
        0x85, 0x03,                          # Report ID 3
        0x09, 0x01, 0xA1, 0x00,              # Usage Pointer, Collection Physical
        0x05, 0x09, 0x19, 0x01, 0x29, 0x03,  # Buttons 1..3
        0x15, 0x00, 0x25, 0x01,
        0x95, 0x03, 0x75, 0x01, 0x81, 0x02,  # 3 buttons Var,Abs
        0x95, 0x01, 0x75, 0x05, 0x81, 0x03,  # 5-bit padding
        0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x09, 0x38, 0x09, 0x00,  # X,Y,Wheel,Pan(0x00)
        0x15, 0x81, 0x25, 0x7F,              # logical -127..127
        0x75, 0x08, 0x95, 0x04, 0x81, 0x06,  # 4 fields Var,Rel
        0xC0, 0xC0,
    ])
    return keyboard_block + mouse


def test_compare_pico_style_needs_remap_and_subset():
    pico = _pico_like_map()
    # strict compare WITHOUT remap/subset fails: mouse is id 3 not 2, and has an extra pan field.
    strict = C.compare_report_maps(R.REPORT_DESCRIPTOR, pico)
    assert not strict.byte_exact
    # with the Pico profile's remap (2->3) + subset fields it passes on the load-bearing fields.
    prof = C.BACKEND_PROFILES["pico"]
    lenient = C.compare_report_maps(R.REPORT_DESCRIPTOR, pico,
                                    report_id_remap=prof.report_id_remap,
                                    subset_fields=prof.map_subset)
    assert lenient.load_bearing_ok and lenient.ok, lenient.mismatches
    assert 1 in lenient.matched_reports and 3 in lenient.matched_reports


def test_compare_flags_semantic_mismatch():
    # Take the real map but flip the mouse X/Y from Relative to Absolute -> must be flagged.
    data = bytearray(R.REPORT_DESCRIPTOR)
    # the mouse relative Input item is 0x81, 0x06 (Data,Var,Rel); change the FIRST occurrence to Abs
    idx = data.find(bytes([0x81, 0x06]))
    assert idx != -1
    data[idx + 1] = 0x02   # Var,Abs (drop the Rel bit)
    diff = C.compare_report_maps(R.REPORT_DESCRIPTOR, bytes(data))
    assert not diff.byte_exact
    assert not diff.load_bearing_ok
    assert any("data-field" in m for m in diff.mismatches)


# --------------------------------------------------------------------------- #
# GATT-tree helper (fake ObjectManager + fake descriptor reader)
# --------------------------------------------------------------------------- #
CHRC = "org.bluez.GattCharacteristic1"
DESC = "org.bluez.GattDescriptor1"


def _fake_tree(dev="/org/bluez/hci0/dev_AA"):
    """A minimal ObjectManager tree: two Report chars (kbd + mouse) each with a 0x2908 descriptor,
    plus a Report Map char that must NOT be picked up as an input report."""
    return {
        f"{dev}/service0010": {"org.bluez.GattService1": {"UUID": C.HID_SERVICE_UUID}},
        f"{dev}/service0010/char0011": {CHRC: {"UUID": C.REPORT_MAP_UUID}},   # report map, not input
        f"{dev}/service0010/char0020": {CHRC: {"UUID": C.REPORT_UUID}},       # keyboard input
        f"{dev}/service0010/char0020/desc0021": {DESC: {"UUID": C.REPORT_REF_UUID}},
        f"{dev}/service0010/char0030": {CHRC: {"UUID": C.REPORT_UUID}},       # mouse input
        f"{dev}/service0010/char0030/desc0031": {DESC: {"UUID": C.REPORT_REF_UUID}},
        "/org/bluez/hci0/dev_BB/service0099/char0099": {CHRC: {"UUID": C.REPORT_UUID}},  # other dev
    }


def test_find_report_characteristics_maps_by_report_id():
    dev = "/org/bluez/hci0/dev_AA"
    objs = _fake_tree(dev)
    refs = {
        f"{dev}/service0010/char0020/desc0021": bytes([1, 1]),   # keyboard [id=1, input]
        f"{dev}/service0010/char0030/desc0031": bytes([2, 1]),   # mouse    [id=2, input]
    }
    found = C.find_report_characteristics(objs, dev, lambda p: refs[p])
    assert set(found) == {1, 2}
    assert found[1]["char_path"].endswith("char0020")
    assert found[1]["report_type"] == C.REPORT_TYPE_INPUT
    assert found[2]["char_path"].endswith("char0030")
    # the other device's char is NOT included (path prefix guard)
    assert all(v["char_path"].startswith(dev) for v in found.values())


def test_find_report_characteristics_skips_unreadable_descriptor():
    dev = "/org/bluez/hci0/dev_AA"
    objs = _fake_tree(dev)

    def reader(path):
        if path.endswith("desc0031"):
            raise RuntimeError("read failed")   # mouse ref unreadable
        return bytes([1, 1])

    found = C.find_report_characteristics(objs, dev, reader)
    assert set(found) == {1}   # only the keyboard resolved


# --------------------------------------------------------------------------- #
# Notification verdict
# --------------------------------------------------------------------------- #
def test_notification_matches_exact():
    want = R.keyboard_report(0, [0x04]).data       # 'a' down
    assert C.notification_matches(want, want)
    assert not C.notification_matches(bytes(8), want)


def test_notification_matches_trailing_zero_pico_pan():
    want = R.mouse_report(dx=3).data               # 4 bytes: 00 03 00 00
    padded = want + bytes([0x00])                  # Pico appends pan=0 -> 5 bytes
    assert C.notification_matches(padded, want, allow_trailing_zero=True)
    # a NON-zero trailing byte is a real mismatch, not padding
    assert not C.notification_matches(want + bytes([0x01]), want, allow_trailing_zero=True)
    # without the allowance, the length difference is a mismatch
    assert not C.notification_matches(padded, want, allow_trailing_zero=False)


def test_notification_verdict_reports_missing_and_mismatch():
    expected = {
        1: R.keyboard_report(0, [0x04]).data,
        2: R.mouse_report(dx=3).data,
    }
    received = {
        1: R.keyboard_report(0, [0x04]).data,       # correct
        2: R.mouse_report(dx=9).data,               # wrong
    }
    v = C.notification_verdict(received, expected)
    assert not v["ok"]
    assert v["checked"] == [1, 2]
    assert any("report 2" in m for m in v["mismatches"])

    # a fully correct set (with Pico pan padding on the mouse) passes
    received_ok = {
        1: R.keyboard_report(0, [0x04]).data,
        2: R.mouse_report(dx=3).data + bytes([0]),
    }
    v2 = C.notification_verdict(received_ok, expected, allow_trailing_zero={2: True})
    assert v2["ok"], v2


# --------------------------------------------------------------------------- #
# Drive-mode encoding: one KNOWN report as the Mode 1 `frames` hex string
# --------------------------------------------------------------------------- #
def test_frames_method_hex_encodes_report_id_and_body():
    # the SAME KNOWN drives the central self-test injects (keyboard 'a' down, mouse dx=3), now shaped
    # as the Mode 1 `frames` string the mode=rpc inject relays: 2-hex report id + body hex.
    kbd_body = R.keyboard_report(0, [0x04]).data      # 00 00 04 00 00 00 00 00
    assert C.frames_method_hex(1, kbd_body) == "01" + kbd_body.hex() == "010000040000000000"
    mouse_body = R.mouse_report(dx=3).data            # 00 03 00 00
    assert C.frames_method_hex(2, mouse_body) == "0200030000"
    # a renumbered mouse id (arduino-pico uses 3) is still a zero-padded 2-hex prefix
    assert C.frames_method_hex(3, mouse_body) == "0300030000"


def test_frames_method_hex_matches_reports_py_hex_shape():
    # the frames string must equal reports.py's own hex for the same KNOWN drive -- so mode=frames
    # (host builds bytes) and mode=rpc (board relays these bytes) inject byte-identical reports.
    for rid, body in ((1, R.keyboard_report(0, [0x04]).data), (2, R.mouse_report(dx=3).data)):
        assert C.frames_method_hex(rid, body) == "%02x%s" % (rid, body.hex())


# --------------------------------------------------------------------------- #
# LE-connect method resolution (the hci0 auto-connect-stall workaround)
# --------------------------------------------------------------------------- #
def test_resolve_connect_method_scan_always_scan():
    assert C.resolve_connect_method("scan", experimental_enabled=False) == "scan"
    assert C.resolve_connect_method("scan", experimental_enabled=True) == "scan"


def test_resolve_connect_method_auto_prefers_connectdevice_when_experimental():
    assert C.resolve_connect_method("auto", experimental_enabled=True) == "connectdevice"
    assert C.resolve_connect_method("auto", experimental_enabled=False) == "scan"


def test_resolve_connect_method_connectdevice_requires_experimental():
    assert C.resolve_connect_method("connectdevice", experimental_enabled=True) == "connectdevice"
    # explicit connectdevice with Experimental OFF must fail loudly (not silently stall on hci0)
    with pytest.raises(C.ConnectMethodError):
        C.resolve_connect_method("connectdevice", experimental_enabled=False)


def test_resolve_connect_method_rejects_unknown():
    with pytest.raises(C.ConnectMethodError):
        C.resolve_connect_method("teleport", experimental_enabled=True)


def test_connectdevice_properties_shape_and_default_type():
    assert C.connectdevice_properties("AA:BB:CC:DD:EE:FF", "random") == {
        "Address": "AA:BB:CC:DD:EE:FF", "AddressType": "random"}
    # a missing AddressType defaults to public (BlueZ Device1 default)
    assert C.connectdevice_properties("AA:BB:CC:DD:EE:FF", None) == {
        "Address": "AA:BB:CC:DD:EE:FF", "AddressType": "public"}


# --------------------------------------------------------------------------- #
# Backend-parametrized END-TO-END verdict pipeline (offline)
# --------------------------------------------------------------------------- #
# For EACH backend profile, drive the SAME three verification stages the live central harness runs --
# report-map compare, report-reference resolution, per-report notification byte-check -- through that
# profile's own parameters (remap / subset / mouse id / pan padding / byte-exact), using synthetic
# GATT data. This proves the offline core validates every backend's report map + report references +
# notification bytes with no hardware, backend-agnostic + parametrized (issue #38).
CHRC_IF = "org.bluez.GattCharacteristic1"
DESC_IF = "org.bluez.GattDescriptor1"


def _keyboard_block() -> bytes:
    """reports.py descriptor sliced to just the keyboard(1) application collection."""
    return R.REPORT_DESCRIPTOR[:R.REPORT_DESCRIPTOR.find(bytes([0x05, 0x01, 0x09, 0x02, 0xA1, 0x01]))]


def _synthetic_map_for(profile) -> bytes:
    """Build a Report Map representative of `profile`'s real firmware.

    dongle: reports.py descriptor BYTE-FOR-BYTE. esp32: keyboard(1)+mouse(2) prefix (base firmware,
    no abs). pico: arduino-pico-style keyboard(1)+mouse(3)+pan (renumbered + extended)."""
    if profile.name == "dongle":
        return R.REPORT_DESCRIPTOR
    if profile.name in ("esp32", "net"):
        # net drives the SAME ESP32 firmware over TCP -> same keyboard(1)+mouse(2) report map.
        return _prefix_two_reports()
    if profile.name == "pico":
        return _pico_like_map()
    raise AssertionError(f"no synthetic map for backend {profile.name}")


@pytest.mark.parametrize("backend", sorted(C.BACKEND_PROFILES))
def test_backend_full_verdict_pipeline_offline(backend):
    profile = C.BACKEND_PROFILES[backend]
    mouse_id = profile.mouse_report_id

    # STAGE 1: report map compare through the profile's remap/subset/byte-exact policy.
    report_map = _synthetic_map_for(profile)
    diff = C.compare_report_maps(
        R.REPORT_DESCRIPTOR, report_map,
        report_id_remap=profile.report_id_remap, subset_fields=profile.map_subset)
    assert diff.ok, f"{backend} report map should validate: {diff.mismatches}"
    if profile.byte_exact_map:
        assert diff.byte_exact, f"{backend} declares byte_exact_map but map differs"
    assert 1 in diff.matched_reports and mouse_id in diff.matched_reports

    # STAGE 2: report-reference resolution over a synthetic GATT tree (keyboard 1 + mouse mouse_id).
    dev = "/org/bluez/hci0/dev_ZZ"
    objs = {
        f"{dev}/service0010": {"org.bluez.GattService1": {"UUID": C.HID_SERVICE_UUID}},
        f"{dev}/service0010/char0011": {CHRC_IF: {"UUID": C.REPORT_MAP_UUID}},
        f"{dev}/service0010/char0020": {CHRC_IF: {"UUID": C.REPORT_UUID}},
        f"{dev}/service0010/char0020/desc0021": {DESC_IF: {"UUID": C.REPORT_REF_UUID}},
        f"{dev}/service0010/char0030": {CHRC_IF: {"UUID": C.REPORT_UUID}},
        f"{dev}/service0010/char0030/desc0031": {DESC_IF: {"UUID": C.REPORT_REF_UUID}},
    }
    refs = {
        f"{dev}/service0010/char0020/desc0021": bytes([1, C.REPORT_TYPE_INPUT]),
        f"{dev}/service0010/char0030/desc0031": bytes([mouse_id, C.REPORT_TYPE_INPUT]),
    }
    found = C.find_report_characteristics(objs, dev, lambda p: refs[p])
    assert set(found) == {1, mouse_id}
    assert found[1]["report_type"] == C.REPORT_TYPE_INPUT
    assert found[mouse_id]["report_type"] == C.REPORT_TYPE_INPUT

    # STAGE 3: per-report notification byte-check, honouring the profile's mouse pan padding.
    kbd_body = R.keyboard_report(0, [0x04]).data       # 'a' down
    mouse_body = R.mouse_report(dx=3).data             # move dx=3
    expected = {1: kbd_body, mouse_id: mouse_body}
    received = {1: kbd_body,
                mouse_id: mouse_body + (bytes([0]) if profile.mouse_pad_zero else b"")}
    pad = {mouse_id: profile.mouse_pad_zero}
    verdict = C.notification_verdict(received, expected, allow_trailing_zero=pad)
    assert verdict["ok"], f"{backend} notification verdict failed: {verdict}"
    assert sorted(verdict["checked"]) == sorted([1, mouse_id])

    # a corrupted mouse byte must be caught for every backend (guards the real end signal).
    bad = dict(received)
    bad[mouse_id] = R.mouse_report(dx=99).data + (bytes([0]) if profile.mouse_pad_zero else b"")
    assert not C.notification_verdict(bad, expected, allow_trailing_zero=pad)["ok"]


# --------------------------------------------------------------------------- #
# is_retryable_failure: retry TRANSIENT link flakiness, never a real mismatch
# --------------------------------------------------------------------------- #
def test_retryable_only_for_failed_status():
    # a pass or skip is never "retryable" -- there is nothing to retry.
    assert not C.is_retryable_failure({"status": "pass"})
    assert not C.is_retryable_failure({"status": "skip", "reason": "no board"})


def test_retryable_connect_and_delivery_failures():
    # failed at connect (no report_map / notifications yet) = transient
    assert C.is_retryable_failure(
        {"status": "fail", "error": "connect failed: org.bluez.Error.InProgress"})
    # reached inject but the notification never ARRIVED (received empty) = the link died = transient
    assert C.is_retryable_failure({
        "status": "fail",
        "report_map": {"ok": True},
        "notifications": {"expected": {"1": "0000040000000000"}, "received": {"1": ""}},
    })


def test_not_retryable_real_mismatches():
    # a wrong report map = real firmware/host drift, must NOT be retried
    assert not C.is_retryable_failure(
        {"status": "fail", "report_map": {"ok": False}})
    # a notification that ARRIVED with the WRONG bytes = real bug, must NOT be retried
    assert not C.is_retryable_failure({
        "status": "fail",
        "report_map": {"ok": True},
        "notifications": {"expected": {"1": "0000040000000000"},
                          "received": {"1": "0000ff0000000000"}},
    })


# --------------------------------------------------------------------------- #
# advertised-name matching -- PREFIX, not exact (boards append -<nick>[-<client>])
# --------------------------------------------------------------------------- #
def test_advert_name_matches_prefix_family():
    # a real board's name is the profile prefix + -<nick>[-<client>]
    assert C.advert_name_matches("HIDBRIDGE-esp32-ee03", "HIDBRIDGE-esp32")
    assert C.advert_name_matches("HIDBRIDGE-esp32-ee03-desk", "HIDBRIDGE-esp32")
    assert C.advert_name_matches("HIDBRIDGE-esp32", "HIDBRIDGE-esp32")          # bare (no nick yet)
    assert C.advert_name_matches("HIDBRIDGE-pico-a1b2", "HIDBRIDGE-pico")


def test_advert_name_does_not_cross_family_or_match_empty():
    assert not C.advert_name_matches("HIDBRIDGE-pico-a1b2", "HIDBRIDGE-esp32")
    assert not C.advert_name_matches("SOMETHINGELSE", "HIDBRIDGE-esp32")
    assert not C.advert_name_matches("", "HIDBRIDGE-esp32")


def test_advert_name_matches_real_profiles():
    # against the shipped profiles: each family's nick-suffixed name matches its own profile only
    esp = C.BACKEND_PROFILES["esp32"].advert_name
    pico = C.BACKEND_PROFILES["pico"].advert_name
    assert C.advert_name_matches(esp + "-9216", esp)
    assert not C.advert_name_matches(esp + "-9216", pico)

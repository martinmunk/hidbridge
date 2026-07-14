"""Pure core for the on-host BLE-central HID self-test -- NO bluetooth / dbus deps.

This module holds every piece of the "built-in adapter (hci0) as a BLE CENTRAL validates an MCU HID
peripheral" self-test that can be reasoned about WITHOUT touching hardware, so it imports + unit-tests
fully offline under uv:

  1. A real USB-HID Report Descriptor PARSER (`parse_report_descriptor`) that turns the raw
     0x2A4B Report Map bytes into a structured list of reports + input fields.
  2. A COMPARATOR (`compare_report_maps`) that, given the firmware's Report Map and reports.py's
     REPORT_DESCRIPTOR (the SINGLE SOURCE OF TRUTH), reports byte-exactness AND -- when the firmware
     legitimately differs in incidental ways (e.g. arduino-pico builds its own descriptor and shifts
     the mouse to report ID 3) -- whether the LOAD-BEARING fields still match: each report's ID,
     application usage page/usage, and each data field's Input flags (Rel vs Var+Abs) + Logical
     Min/Max.
  3. The GATT-tree helpers (`find_report_characteristics`) that locate the input Report chars
     (0x2A4D) under a connected device + pair each with its Report Reference descriptor (0x2908),
     written with the dbus reads INJECTED so they are testable with plain dicts.
  4. The notification VERDICT (`notification_verdict`) that compares the bytes received over a GATT
     notification against reports.py's builder output, byte-for-byte (with an explicit,
     per-report `allow_trailing_zero` for a firmware that pads the body with known-zero bytes, e.g.
     the Pico's 5-byte TinyUSB relative-mouse report = reports.py's 4 bytes + pan=0).
  5. Per-backend PROFILES (`BACKEND_PROFILES`) that pin, for each MCU, the advertised name, the
     expected report IDs, whether the Report Map is byte-exact, and any body padding -- so the live
     harness + the offline tests share ONE description of each backend.

Keep this module dependency-free (stdlib only): it is imported both by the offline pytest (uv
CPython) and by `central.py` running under the SYSTEM python (which has dbus + gi + AF_BLUETOOTH).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# LE-connect method resolution (the hci0 auto-connect-stall workaround)
# --------------------------------------------------------------------------- #
# On the wsb Intel built-in hci0 (BlueZ 5.82, kernel 6.12.90) a Device.Connect() to an LE
# peripheral is turned into a kernel mgmt Add-Device AUTO-CONNECT (accept-list + passive scan) that
# never issues an LE Create Connection -> a ~42s stall -> le-connection-abort-by-local. The two live
# escapes (docs/bt-hid-central-selftest-status.md) are: (a) bluetoothd Experimental ON +
# Adapter1.ConnectDevice() for a DIRECT create-connection that bypasses the accept-list; and (b) a
# USB dongle as the central instead of the built-in. This resolver picks the connect METHOD; it is
# pure so the offline suite pins its policy without touching dbus.
CONNECT_METHODS = ("auto", "scan", "connectdevice")


class ConnectMethodError(ValueError):
    """A connect method was requested that the running bluetoothd cannot provide."""


def resolve_connect_method(requested: str, experimental_enabled: bool) -> str:
    """Resolve the effective LE-connect method for the central self-test.

    requested:
      "scan"          -- discover the peripheral, then a DIRECT Device.Connect() issued WHILE the
                         active scan runs (BlueZ then does a real LE Create Connection). The default;
                         works on adapters without the accept-list auto-connect stall.
      "connectdevice" -- Adapter1.ConnectDevice({Address, AddressType}): a DIRECT create-connection
                         that bypasses the stalling accept-list path. Needs bluetoothd Experimental.
      "auto"          -- "connectdevice" when Experimental is available, else "scan".

    Raises ConnectMethodError if "connectdevice" is explicitly requested but Experimental is off
    (so the caller fails loudly rather than silently stalling on the built-in adapter).
    """
    req = (requested or "auto").lower()
    if req not in CONNECT_METHODS:
        raise ConnectMethodError(f"unknown connect method {requested!r} (choose {CONNECT_METHODS})")
    if req == "scan":
        return "scan"
    if req == "connectdevice":
        if not experimental_enabled:
            raise ConnectMethodError(
                "connect method 'connectdevice' needs bluetoothd Experimental (Adapter1."
                "ConnectDevice); enable it (ExecStart bluetoothd -E) or use --connect-method scan")
        return "connectdevice"
    # auto
    return "connectdevice" if experimental_enabled else "scan"


def connectdevice_properties(address: str, address_type: str | None) -> dict:
    """Build the Adapter1.ConnectDevice() properties dict for a scanned peripheral.

    address_type is the BlueZ Device1.AddressType ("public" | "random"); default "public" if the
    advertisement did not carry it. Kept pure (returns plain str values) so central.py wraps them in
    dbus.String at the call site and tests can assert the shape.
    """
    props = {"Address": address}
    props["AddressType"] = (address_type or "public")
    return props


# --------------------------------------------------------------------------- #
# GATT UUIDs (HID-over-GATT / HOG)
# --------------------------------------------------------------------------- #
BLUETOOTH_BASE_SUFFIX = "-0000-1000-8000-00805f9b34fb"


def uuid16(short: int) -> str:
    """Expand a 16-bit Bluetooth SIG UUID to its full 128-bit string form (lowercase)."""
    return f"0000{short:04x}{BLUETOOTH_BASE_SUFFIX}"


HID_SERVICE_UUID = uuid16(0x1812)      # Human Interface Device service
REPORT_MAP_UUID = uuid16(0x2A4B)       # HID Report Map characteristic
REPORT_UUID = uuid16(0x2A4D)           # HID Report characteristic (input reports)
REPORT_REF_UUID = uuid16(0x2908)       # Report Reference descriptor -> [reportID, reportType]
HID_INFO_UUID = uuid16(0x2A4A)         # HID Information characteristic
REPORT_TYPE_INPUT = 0x01               # Report Reference type byte for an INPUT report


# --------------------------------------------------------------------------- #
# HID Report Descriptor parser
# --------------------------------------------------------------------------- #
# Item-prefix decoding (USB HID 1.11 6.2.2). A short item's prefix byte is
# (tag << 4) | (type << 2) | size, where size is a CODE: 0->0, 1->1, 2->2, 3->4 data bytes.
_ITEM_TYPE_MAIN = 0
_ITEM_TYPE_GLOBAL = 1
_ITEM_TYPE_LOCAL = 2

# Main item tags
_MAIN_INPUT = 0x8
_MAIN_OUTPUT = 0x9
_MAIN_FEATURE = 0xB
_MAIN_COLLECTION = 0xA
_MAIN_END_COLLECTION = 0xC

# Global item tags
_GLOBAL_USAGE_PAGE = 0x0
_GLOBAL_LOGICAL_MIN = 0x1
_GLOBAL_LOGICAL_MAX = 0x2
_GLOBAL_REPORT_SIZE = 0x7
_GLOBAL_REPORT_ID = 0x8
_GLOBAL_REPORT_COUNT = 0x9

# Local item tags
_LOCAL_USAGE = 0x0

# Input item flag bits (USB HID 6.2.2.5)
INPUT_CONSTANT = 0x01   # 0 = Data, 1 = Constant (padding)
INPUT_VARIABLE = 0x02   # 0 = Array, 1 = Variable
INPUT_RELATIVE = 0x04   # 0 = Absolute, 1 = Relative

_SIZE_CODE_TO_LEN = {0: 0, 1: 1, 2: 2, 3: 4}


@dataclass
class InputField:
    """One HID Input main item (a run of `count` fields of `size` bits)."""

    usage_page: int
    usages: tuple[int, ...]
    flags: int
    report_size: int
    report_count: int
    logical_min: int
    logical_max: int

    @property
    def is_constant(self) -> bool:
        return bool(self.flags & INPUT_CONSTANT)

    @property
    def is_relative(self) -> bool:
        return bool(self.flags & INPUT_RELATIVE)

    @property
    def is_variable(self) -> bool:
        return bool(self.flags & INPUT_VARIABLE)

    def signature(self) -> tuple:
        """Load-bearing identity of a DATA field (usage page, flags, logical range).

        Deliberately excludes report_size/count + the exact usage LIST so an incidental descriptor
        difference (extra usage aliases, a padded field width) does not fail the compare, while the
        semantics an OS acts on (Rel vs Abs/Var, the logical range) still must match.
        """
        return (self.usage_page, self.flags, self.logical_min, self.logical_max)


@dataclass
class ReportInfo:
    """All input fields carried by one report ID inside one application collection."""

    report_id: int
    app_usage_page: int
    app_usage: int
    fields: list[InputField] = field(default_factory=list)

    def data_fields(self) -> list[InputField]:
        return [f for f in self.fields if not f.is_constant]

    def signature(self) -> tuple:
        """Load-bearing identity of the whole report (id, app usage, ordered data-field sigs)."""
        return (self.report_id, self.app_usage_page, self.app_usage,
                tuple(f.signature() for f in self.data_fields()))


def _sign_extend(value: int, nbytes: int) -> int:
    """Interpret `nbytes` little-endian bytes (already assembled into `value`) as signed."""
    if nbytes and value >= (1 << (nbytes * 8 - 1)):
        value -= 1 << (nbytes * 8)
    return value


def parse_report_descriptor(data: bytes) -> list[ReportInfo]:
    """Parse a raw USB-HID report descriptor into a list of ReportInfo (one per report ID seen).

    Handles short items only (long items, tag 0xFE, are vanishingly rare in HOG descriptors and are
    skipped by length). Tracks the global item state (usage page, logical min/max, report size/count,
    report ID) + the enclosing application collection's usage, and emits an InputField for every
    Input main item. Reports are returned in first-seen order.
    """
    usage_page = 0
    logical_min = 0
    logical_max = 0
    report_size = 0
    report_count = 0
    report_id = 0
    pending_usages: list[int] = []
    # collection stack of (usage_page, usage) for Application collections we are inside
    app_stack: list[tuple[int, int]] = []
    reports: dict[int, ReportInfo] = {}
    order: list[int] = []

    i = 0
    n = len(data)
    while i < n:
        prefix = data[i]
        i += 1
        if prefix == 0xFE:  # long item: [0xFE][dataSize][tag][data...] -- skip
            if i < n:
                data_size = data[i]
                i += 2 + data_size
            continue
        size_code = prefix & 0x03
        item_type = (prefix >> 2) & 0x03
        tag = (prefix >> 4) & 0x0F
        dlen = _SIZE_CODE_TO_LEN[size_code]
        raw = data[i:i + dlen]
        i += dlen
        uval = int.from_bytes(raw, "little") if raw else 0

        if item_type == _ITEM_TYPE_GLOBAL:
            if tag == _GLOBAL_USAGE_PAGE:
                usage_page = uval
            elif tag == _GLOBAL_LOGICAL_MIN:
                logical_min = _sign_extend(uval, dlen)
            elif tag == _GLOBAL_LOGICAL_MAX:
                logical_max = _sign_extend(uval, dlen)
            elif tag == _GLOBAL_REPORT_SIZE:
                report_size = uval
            elif tag == _GLOBAL_REPORT_COUNT:
                report_count = uval
            elif tag == _GLOBAL_REPORT_ID:
                report_id = uval
        elif item_type == _ITEM_TYPE_LOCAL:
            if tag == _LOCAL_USAGE:
                pending_usages.append(uval)
        elif item_type == _ITEM_TYPE_MAIN:
            if tag == _MAIN_COLLECTION:
                # collection type 0x01 = Application; remember its usage (last pending usage)
                usage = pending_usages[-1] if pending_usages else 0
                if uval == 0x01:
                    app_stack.append((usage_page, usage))
                pending_usages = []
            elif tag == _MAIN_END_COLLECTION:
                if app_stack:
                    # only pop for Application collections we pushed; Physical/Logical weren't pushed
                    # (we push ONLY Application), so a bare EndCollection for a nested Physical must
                    # not pop the Application. We disambiguate by not pushing non-Application above.
                    pass
                pending_usages = []
            elif tag == _MAIN_INPUT:
                app_page, app_usage = app_stack[-1] if app_stack else (usage_page, 0)
                rep = reports.get(report_id)
                if rep is None:
                    rep = ReportInfo(report_id, app_page, app_usage)
                    reports[report_id] = rep
                    order.append(report_id)
                rep.fields.append(InputField(
                    usage_page=usage_page,
                    usages=tuple(pending_usages),
                    flags=uval,
                    report_size=report_size,
                    report_count=report_count,
                    logical_min=logical_min,
                    logical_max=logical_max,
                ))
                pending_usages = []
            else:  # Output / Feature main items also clear locals
                pending_usages = []

    return [reports[r] for r in order]


# --------------------------------------------------------------------------- #
# EndCollection handling note
# --------------------------------------------------------------------------- #
# The parser above pushes ONLY Application collections onto app_stack and never pops for a nested
# Physical/Logical EndCollection -- but it also never pops the Application on ITS EndCollection,
# which is fine for our purpose: report_id already scopes fields, and the app usage is read at the
# moment an Input item is emitted (from the top of stack). Since every report here lives directly
# under exactly one Application collection whose usage is set before its Input items, top-of-stack is
# always the right application context. A malformed descriptor with re-used report IDs across two
# Application collections would attribute the app usage of the FIRST -- acceptable + explicit.


# --------------------------------------------------------------------------- #
# Report-map comparison
# --------------------------------------------------------------------------- #
@dataclass
class ReportMapDiff:
    byte_exact: bool
    load_bearing_ok: bool
    expected_reports: list[int]
    actual_reports: list[int]
    matched_reports: list[int] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """A report map passes if it is byte-exact OR load-bearing-equivalent."""
        return self.byte_exact or self.load_bearing_ok


def compare_report_maps(expected: bytes, actual: bytes,
                        report_id_remap: dict[int, int] | None = None,
                        subset_fields: bool = False) -> ReportMapDiff:
    """Compare a firmware Report Map (`actual`) against reports.py's descriptor (`expected`).

    Prefers a BYTE-EXACT match. Otherwise parses both and, for every report the FIRMWARE declares
    that has a reports.py counterpart, checks the load-bearing fields: the report's application usage
    page/usage + each data field's (usage page, Input flags, Logical Min/Max).

    Iterating over the FIRMWARE's reports (not reports.py's) is deliberate: a firmware may serve a
    SUBSET of reports.py -- e.g. the ESP32 without the abs firmware declares only keyboard(1)+mouse(2)
    -- and that is a valid map, not a failure. The harness separately asserts the REQUIRED minimum
    set (keyboard + mouse) is present; this function verifies whatever IS present is semantically
    correct.

    `report_id_remap` maps an EXPECTED reports.py id -> the firmware's id for a backend that renumbers
    a report (arduino-pico's mouse: expected 2 -> actual 3). `subset_fields` (for a firmware that
    builds its own descriptor, e.g. arduino-pico) requires only that reports.py's data-field
    signatures are a SUBSET of the firmware's (the firmware may add fields, e.g. a pan axis), rather
    than an exact ordered match.
    """
    byte_exact = expected == actual
    exp = {r.report_id: r for r in parse_report_descriptor(expected)}
    act = {r.report_id: r for r in parse_report_descriptor(actual)}
    remap = report_id_remap or {}
    inverse = {v: k for k, v in remap.items()}   # firmware id -> expected id
    mismatches: list[str] = []
    matched: list[int] = []

    for act_id, act_rep in act.items():
        exp_id = inverse.get(act_id, act_id)
        exp_rep = exp.get(exp_id)
        if exp_rep is None:
            # a firmware-only report (battery / vendor / an unmapped id) -- not our concern
            continue
        matched.append(act_id)
        if (exp_rep.app_usage_page, exp_rep.app_usage) != \
                (act_rep.app_usage_page, act_rep.app_usage):
            mismatches.append(
                f"report {act_id}: application usage "
                f"{act_rep.app_usage_page:#04x}/{act_rep.app_usage:#04x} != reports.py "
                f"{exp_rep.app_usage_page:#04x}/{exp_rep.app_usage:#04x}")
        exp_sig = [f.signature() for f in exp_rep.data_fields()]
        act_sig = [f.signature() for f in act_rep.data_fields()]
        if subset_fields:
            missing = [s for s in exp_sig if s not in set(act_sig)]
            if missing:
                mismatches.append(
                    f"report {act_id}: reports.py data-field signatures {missing} not present "
                    f"in firmware fields {act_sig}")
        elif exp_sig != act_sig:
            mismatches.append(
                f"report {act_id}: data-field signatures differ "
                f"(reports.py {exp_sig}, firmware {act_sig})")

    return ReportMapDiff(
        byte_exact=byte_exact,
        load_bearing_ok=bool(matched) and not mismatches,
        expected_reports=list(exp),
        actual_reports=list(act),
        matched_reports=matched,
        mismatches=mismatches,
    )


# --------------------------------------------------------------------------- #
# GATT tree helpers (dbus reads injected -> testable with plain dicts)
# --------------------------------------------------------------------------- #
def _uuid_of(ifaces: dict, iface_name: str) -> str:
    return str(ifaces.get(iface_name, {}).get("UUID", "")).lower()


def find_report_characteristics(objs: dict, dev_path: str, read_descriptor) -> dict[int, dict]:
    """Locate the input Report characteristics (0x2A4D) under `dev_path` + resolve each report id.

    `objs` is a BlueZ ObjectManager GetManagedObjects() dict (path -> {iface -> props}); it works
    equally with plain dicts in tests. `read_descriptor(path) -> bytes` reads a Report Reference
    (0x2908) descriptor value (a dbus ReadValue in production, a fake in tests).

    Returns {report_id: {"char_path": str, "report_type": int, "ref_path": str}} for every input
    Report characteristic whose 0x2908 descriptor is readable. A characteristic with no readable
    Report Reference is skipped (it cannot be attributed to a report id).
    """
    chrc_iface = "org.bluez.GattCharacteristic1"
    desc_iface = "org.bluez.GattDescriptor1"
    prefix = dev_path.rstrip("/") + "/"
    out: dict[int, dict] = {}
    for path, ifaces in objs.items():
        if chrc_iface not in ifaces or not path.startswith(prefix):
            continue
        if _uuid_of(ifaces, chrc_iface) != REPORT_UUID:
            continue
        ref_path = None
        for dpath, difaces in objs.items():
            if desc_iface not in difaces or not dpath.startswith(path.rstrip("/") + "/"):
                continue
            if _uuid_of(difaces, desc_iface) == REPORT_REF_UUID:
                ref_path = dpath
                break
        if ref_path is None:
            continue
        try:
            val = bytes(read_descriptor(ref_path))
        except Exception:  # noqa: BLE001 -- a descriptor we cannot read is simply skipped
            continue
        if len(val) < 2:
            continue
        out[val[0]] = {"char_path": path, "report_type": val[1], "ref_path": ref_path}
    return out


# --------------------------------------------------------------------------- #
# Notification verdict
# --------------------------------------------------------------------------- #
def notification_matches(received: bytes, expected: bytes, allow_trailing_zero: bool = False) -> bool:
    """True iff a received GATT-notification body matches reports.py's expected report body.

    Byte-for-byte by default. With `allow_trailing_zero`, the received body may be LONGER than
    expected as long as the extra trailing bytes are all zero -- this covers a firmware that pads the
    body (e.g. the Pico's TinyUSB relative mouse appends a pan byte = 0 to reports.py's 4-byte body).
    """
    if received == expected:
        return True
    if allow_trailing_zero and len(received) > len(expected):
        return received[:len(expected)] == expected and not any(received[len(expected):])
    return False


def notification_verdict(received: dict, expected: dict,
                         allow_trailing_zero: dict | None = None) -> dict:
    """Compare a set of received notifications to expected report bodies, keyed by report id.

    `received` / `expected` map report_id -> bytes. `allow_trailing_zero` maps report_id -> bool.
    Returns {"ok": bool, "checked": [...], "mismatches": [...], "missing": [...]}.
    """
    pad = allow_trailing_zero or {}
    mismatches: list[str] = []
    missing: list[int] = []
    checked: list[int] = []
    for rid, want in expected.items():
        if rid not in received:
            missing.append(rid)
            continue
        got = received[rid]
        checked.append(rid)
        if not notification_matches(got, want, allow_trailing_zero=pad.get(rid, False)):
            mismatches.append(
                f"report {rid}: got {bytes(got).hex()} want {bytes(want).hex()}")
    return {
        "ok": not mismatches and not missing,
        "checked": checked,
        "mismatches": mismatches,
        "missing": missing,
    }


# --------------------------------------------------------------------------- #
# Per-backend profiles -- shared by the live harness AND the offline tests
# --------------------------------------------------------------------------- #
@dataclass
class BackendProfile:
    """Everything the central self-test needs to know about one MCU HID backend.

    name             backend key ("pico" | "esp32" | "dongle")
    advert_name      the BLE advertised local name to scan for
    report_id_remap  expected reports.py id -> firmware id (empty = identical)
    byte_exact_map   True if the firmware serves reports.py's descriptor byte-for-byte
    mouse_report_id  the firmware's actual mouse report id (for the notification check)
    mouse_pad_zero   True if the firmware pads the mouse body with trailing zero byte(s)
    map_subset       True if the firmware builds its own descriptor -> subset field compare
    driver           how to drive it: "serial_pico" | "serial_esp32" | "daemon" | "net"
    """

    name: str
    advert_name: str
    report_id_remap: dict[int, int]
    byte_exact_map: bool
    mouse_report_id: int
    mouse_pad_zero: bool
    map_subset: bool
    driver: str


# reports.py canonical ids (kept local to avoid importing the reports module here)
_ID_KEYBOARD = 1
_ID_MOUSE = 2
_ID_ABS_MOUSE = 3
_ID_DIGITIZER = 4

BACKEND_PROFILES: dict[str, BackendProfile] = {
    # ESP32 firmware embeds reports.py REPORT_DESCRIPTOR byte-for-byte (kbd id 1 + mouse id 2). The
    # abs-pointer (id 3) + digitizer (id 4) reports exist only once the abs firmware is flashed;
    # the harness discovers whichever report ids the live Report Map actually declares.
    "esp32": BackendProfile(
        name="esp32", advert_name="wsb-hid-esp32", report_id_remap={},
        byte_exact_map=False, mouse_report_id=_ID_MOUSE, mouse_pad_zero=False,
        map_subset=False, driver="serial_esp32"),
    # SAME ESP32 board + firmware as "esp32" (same advertised name, same byte-for-byte report map),
    # but DRIVEN OVER TCP instead of USB serial. This is the end-to-end proof of the network backend:
    # the host adapter is the BLE central, a frame is injected over the LAN (NetTransport), and the
    # exact bytes must arrive as a GATT notification -- so the whole net -> board -> BLE path is real.
    "net": BackendProfile(
        name="net", advert_name="wsb-hid-esp32", report_id_remap={},
        byte_exact_map=False, mouse_report_id=_ID_MOUSE, mouse_pad_zero=False,
        map_subset=False, driver="net"),
    # arduino-pico builds its OWN combined keyboard(id 1)+mouse(id 3) descriptor (NOT reports.py's)
    # and its TinyUSB relative mouse body is 5 bytes = reports.py's 4 + pan=0. So: parse+load-bearing
    # compare (not byte-exact), mouse id remapped 2->3, and tolerate the trailing pan byte.
    "pico": BackendProfile(
        name="pico", advert_name="wsb-hid-pico", report_id_remap={_ID_MOUSE: 3},
        byte_exact_map=False, mouse_report_id=3, mouse_pad_zero=True,
        map_subset=True, driver="serial_pico"),
    # The dongle path drives our own ble_hid daemon (reports.py descriptor byte-for-byte, kbd 1 +
    # mouse 2). Attempted same-host (hci0 central -> hci1 peripheral); BlueZ may refuse it.
    "dongle": BackendProfile(
        name="dongle", advert_name="wsb-hid-kbd", report_id_remap={},
        byte_exact_map=True, mouse_report_id=_ID_MOUSE, mouse_pad_zero=False,
        map_subset=False, driver="daemon"),
}


def is_retryable_failure(result: dict) -> bool:
    """True if a FAILED self-test verdict is a TRANSIENT link failure worth retrying (vs a real bug).

    The central self-test is hardware-in-the-loop over BLE, and some hosts' LE-central stacks connect
    flakily (e.g. the BlueZ auto-connect stall on this dev box: a connect can abort, or the link can
    drop right after StartNotify so the injected report reaches the board with no central to notify).
    Those are environmental and should be retried. What must NEVER be retried is a genuine mismatch --
    the firmware served the wrong report map, or a notification arrived with the WRONG bytes -- because
    retrying would just mask a real regression.

    Retryable  = failed at connect / bond / discovery, OR reached inject but the expected notification
                 never ARRIVED (received empty) -- i.e. the link died before delivery.
    NOT retryable = the report map compare failed, or a notification arrived but its bytes differ from
                    what was injected (received non-empty and != expected).
    """
    if result.get("status") != "fail":
        return False
    rm = result.get("report_map")
    if rm is not None and not rm.get("ok", False):
        return False                      # wrong report map = real firmware/host drift
    notif = result.get("notifications")
    if notif:
        expected = notif.get("expected", {})
        received = notif.get("received", {})
        for rid, want in expected.items():
            got = received.get(rid, "")
            if got and got != want:
                return False              # a notification arrived with WRONG bytes = real bug
    return True                           # connect/link/delivery failure = transient, retry

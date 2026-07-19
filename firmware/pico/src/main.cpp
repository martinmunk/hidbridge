/*
 * HIDBRIDGE-pico -- BLE HID for a Raspberry Pi Pico 2 W: keyboard(1) + relative mouse(2).
 *
 * Part of the hidbridge toolbox: the PICO BACKEND. A ~USD 6 alternative to a USB Bluetooth
 * dongle for controlling an iPad/iPhone/Mac from Linux. Built on the earlephilhower arduino-pico
 * core, whose BTstack HID-over-GATT (HOG) implementation is iOS-compatible OUT OF THE BOX -- which
 * is the whole point: the Linux BlueZ/dongle path gets stuck on iOS not subscribing to the HID
 * input reports (a CCCD / Report-Reference GATT quirk); the Pico's HOG stack does it right.
 *
 * The device advertises as "HIDBRIDGE-pico" (distinct from the dongle's "HIDBRIDGE-dongle"). The host
 * drives it over USB-CDC serial with ONE versioned JSON-RPC 2.0 protocol (one JSON object per line):
 * the BOARD builds every report (type/key/move/click/scroll, the slot's layout applies board-side;
 * builders in ../../shared/hidbridge_rpc.h) AND owns device management (identities/connect/target/
 * advertise/park/disconnect/layout/ping -- the JSON replacements for the old single-char commands, in
 * picoCustom below). `hello` returns proto + a caps list the host feature-detects on. (The old
 * host-builds K/M frame relay AND the single-char device commands P/I/T/L/S/X/O were folded into these
 * methods and REMOVED -- any non-JSON line now returns "ERR unknowncmd". Contract:
 * ../../docs/bt-hid-protocol.md.) The Pico's BTstack wrapper holds ONE central at a time, so it
 * addresses slots by INDEX and reports multi=0.
 *
 * PER-BOND IDENTITY (host slots -- docs/bt-hid-mcu-approach.md): the board presents a DISTINCT BLE
 * identity/address PER bonded iPad. Slot 0 = the CYW43439 NATIVE public address, so an EXISTING bond
 * survives a reflash; slots >=1 = deterministic STATIC-RANDOM addresses set via BTstack
 * gap_random_address_set(). Each slot advertises a TARGET-scoped name (HIDBRIDGE-pico-<ipad>) so an iPad's
 * Bluetooth settings show which entry is meant for it (host sets it with I<n>:name). NOTE: the
 * arduino-pico PicoBluetoothBLEHID wrapper tracks a SINGLE connection handle (_con_handle), so the
 * Pico switches identities SEQUENTIALLY (deterministic time-sliced slot selection: select a slot ->
 * that iPad's identity advertises -> it connects), NOT simultaneously; it reports multi=0. Holding
 * two centrals at once needs a wrapper bypass (BTstack itself supports it) -- a tracked follow-up.
 * The ESP32 (NimBLE) backend DOES hold both at once (multi=1).
 *
 * IMPORTANT -- CUSTOM REPORT MAP. We do NOT use KeyboardBLE.begin()/MouseBLE.begin(), which build
 * arduino-pico's auto-generated report map (that map assigns the mouse report ID 3). Instead we
 * install OUR OWN HID report map -- the exact bytes of python/src/hidbridge/reports.py
 * REPORT_DESCRIPTOR (keyboard ID 1, relative mouse ID 2) -- directly via
 * PicoBluetoothBLEHID.startHID(). We still force-link KeyboardBLE + MouseBLE so the core's
 * __BLEInstallKeyboard/__BLEInstallMouse weak symbols become strong: startHID() sizes its report
 * storage from those (1 feature + 2 keyboard + 1 mouse = 4), which comfortably covers our 2 Input
 * reports (IDs 1..2), and the fixed BTstack ATT DB already exposes Input Report-Reference
 * characteristics for those report IDs. We never call the KeyboardBLE / MouseBLE report builders;
 * every report is sent as raw [reportID | body] bytes.
 */

#include <Arduino.h>
#include <ArduinoJson.h>       // rpc wire protocol: JSON-RPC 2.0 over newline-delimited JSON
#include "hidbridge_rpc.h"   // shared native HID builders + JSON-RPC dispatch (firmware/shared/)
#include <KeyboardBLE.h>
#include <MouseBLE.h>
#include <HID_Bluetooth.h>
#include <PicoBluetoothBLEHID.h>

// Force the KeyboardBLE + MouseBLE translation units to link so the weak __BLEInstallKeyboard /
// __BLEInstallMouse symbols become STRONG. startHID() reads them to size its report storage
// (feature + keyboard*2 + mouse = 4 reports), which is exactly our 4 Input reports (IDs 1..4).
// We do NOT call KeyboardBLE.begin()/MouseBLE.begin() -- we install our own report map below.
void *__attribute__((used)) __wsb_keep_keyboard = (void *)&KeyboardBLE;
void *__attribute__((used)) __wsb_keep_mouse = (void *)&MouseBLE;

// --------------------------------------------------------------------------------------------- //
// HID report map -- BYTE-IDENTICAL to python/src/hidbridge/reports.py REPORT_DESCRIPTOR (the single source
// of truth). python/tests/test_bt_hid_transport_pico.py extracts the bytes BETWEEN the two markers
// below and asserts they equal reports.REPORT_DESCRIPTOR, so this can never silently drift.
// --------------------------------------------------------------------------------------------- //
static const uint8_t HID_REPORT_MAP[] = {
    // WSB-HID-REPORT-MAP-BEGIN (generated from reports.REPORT_DESCRIPTOR -- keep byte-identical)
    // ---- Keyboard, Report ID 1 ----
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, 0x01, 0x05, 0x07, 0x19, 0xE0,
    0x29, 0xE7, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02,
    0x95, 0x01, 0x75, 0x08, 0x81, 0x03, 0x95, 0x06, 0x75, 0x08, 0x15, 0x00,
    0x26, 0xFF, 0x00, 0x05, 0x07, 0x19, 0x00, 0x29, 0xFF, 0x81, 0x00, 0xC0,
    // ---- Relative Mouse, Report ID 2 (5 buttons: L/R/M + Back(4) + Forward(5)) ----
    0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x85, 0x02, 0x09, 0x01, 0xA1, 0x00,
    0x05, 0x09, 0x19, 0x01, 0x29, 0x05, 0x15, 0x00, 0x25, 0x01, 0x95, 0x05,
    0x75, 0x01, 0x81, 0x02, 0x95, 0x01, 0x75, 0x03, 0x81, 0x03, 0x05, 0x01,
    0x09, 0x30, 0x09, 0x31, 0x09, 0x38, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08,
    0x95, 0x03, 0x81, 0x06, 0xC0, 0xC0,
    // WSB-HID-REPORT-MAP-END
};


// ---- Per-bond IDENTITY (host slots) ----------------------------------------------------------- //
// Number of BLE host slots (distinct identities). Slot 0 = native public address; slots >=1 =
// derived static-random addresses. Overridable at build time (-DNUM_SLOTS=N). Two iPads need 2.
#ifndef NUM_SLOTS
#define NUM_SLOTS 2
#endif

static const char *BASE_NAME = "HIDBRIDGE-pico";

// Compile-time firmware SemVer, surfaced in `hello` as `fw` (see picoFillHello). Defaults to
// "1.0.0-dev"; the `fw-v` release build stamps the tag's version with -DHB_FW_VERSION='"<X.Y.Z>"'.
// This is the firmware RELEASE version, INDEPENDENT of the wheel version and the wire-compat integer
// `proto` (hbmode1::PROTO_VERSION). See docs/releasing.md.
#ifndef HB_FW_VERSION
#define HB_FW_VERSION "1.0.0-dev"
#endif
static bd_addr_t slot_addr[NUM_SLOTS];        // BTstack bd_addr_t: big-endian (addr[0] = MSB)
static char      slot_name[NUM_SLOTS][24];
static bool      slot_addr_ready = false;     // slot addresses derived (needs HCI up)
static int       adv_slot = 0;                // slot currently advertising
static int       tgt_slot = 0;                // send-target slot (single-conn wrapper: informational)
static int       slot_layout[NUM_SLOTS] = {}; // per-slot keyboard LAYOUT (hbmode1::LAYOUT_US/DA), RAM-only
static int       conn_slot = -1;              // slot whose central is connected (-1 = none)
static hci_con_handle_t pico_con_handle = HCI_CON_HANDLE_INVALID;  // for X (disconnect)

// Build the MAIN LE advertising payload for a given per-slot LocalName: flags + complete local name.
// The CONSTANT HID service UUID + appearance moved to the SCAN RESPONSE (build_scan_response_data)
// so this main packet is just flags(3) + name(2+N) -- leaving room for a longer LocalName (e.g.
// HIDBRIDGE-pico-mini5) that would overflow the 31-byte AD if the UUID+appearance also sat here.
// Returns the byte count written into out[] (<=31).
static uint8_t build_adv_data(const char *name, uint8_t *out) {
    int i = 0;
    out[i++] = 0x02;
    out[i++] = BLUETOOTH_DATA_TYPE_FLAGS;
    out[i++] = 0x06;                          // LE General Discoverable, BR/EDR not supported
    int nlen = strlen(name);
    out[i++] = 1 + nlen;
    out[i++] = BLUETOOTH_DATA_TYPE_COMPLETE_LOCAL_NAME;
    memcpy(out + i, name, nlen);
    i += nlen;
    return (uint8_t)i;
}

// Build the SCAN RESPONSE: HID service UUID + appearance (constant across slots, so nothing here goes
// stale on a slot switch). A host that actively scans (iOS does) requests this and gets the keyboard
// icon + HID recognition.
static uint8_t build_scan_response_data(uint16_t appearance, uint8_t *out) {
    int i = 0;
    out[i++] = 0x03;
    out[i++] = BLUETOOTH_DATA_TYPE_COMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS;
    out[i++] = ORG_BLUETOOTH_SERVICE_HUMAN_INTERFACE_DEVICE & 0xff;
    out[i++] = ORG_BLUETOOTH_SERVICE_HUMAN_INTERFACE_DEVICE >> 8;
    out[i++] = 0x03;
    out[i++] = BLUETOOTH_DATA_TYPE_APPEARANCE;
    out[i++] = appearance & 0xff;
    out[i++] = appearance >> 8;
    return (uint8_t)i;
}

static uint8_t slot_adv_buf[31];
static uint8_t slot_scan_rsp_buf[31];

// Derive the slot address table once HCI is up: slot 0 = native public address; slots >=1 = the
// public address with the two MSBits of the MSB forced to 0b11 (BT "static random" signature) and
// the slot index XORed into the LSB, so each slot differs from public + from every other slot.
// BTstack bd_addr_t is big-endian: addr[0] = MSB, addr[5] = LSB.
static void derive_slot_addresses() {
    uint8_t atype;
    bd_addr_t base;
    gap_le_get_own_address(&atype, base);
    memcpy(slot_addr[0], base, 6);
    for (int k = 1; k < NUM_SLOTS; k++) {
        memcpy(slot_addr[k], base, 6);
        slot_addr[k][0] = (uint8_t)(base[0] | 0xC0);
        slot_addr[k][5] ^= (uint8_t)(0x40 + k);
    }
    slot_addr_ready = true;
}

// Select host slot n as the advertising identity (address + name) and (re)start advertising it.
static void select_slot_advertise(int n, const char *name) {
    if (n < 0 || n >= NUM_SLOTS) return;
    BluetoothLock l;
    if (!slot_addr_ready) derive_slot_addresses();
    if (name && name[0]) snprintf(slot_name[n], sizeof(slot_name[n]), "%s", name);

    gap_advertisements_enable(0);
    if (n == 0) {
        gap_random_address_set_mode(GAP_RANDOM_ADDRESS_TYPE_OFF);      // advertise the public address
    } else {
        gap_random_address_set_mode(GAP_RANDOM_ADDRESS_TYPE_STATIC);
        gap_random_address_set(slot_addr[n]);                         // distinct static-random id
    }
    uint8_t n_adv = build_adv_data(slot_name[n], slot_adv_buf);
    uint8_t n_sr  = build_scan_response_data(__BLEGetAppearance(), slot_scan_rsp_buf);
    uint16_t itv = 0x0030;
    bd_addr_t null_addr;
    memset(null_addr, 0, 6);
    gap_advertisements_set_params(itv, itv, 0, 0, null_addr, 0x07, 0x00);   // ADV_IND: scannable
    gap_advertisements_set_data(n_adv, slot_adv_buf);
    gap_scan_response_set_data(n_sr, slot_scan_rsp_buf);                     // constant across slots
    adv_slot = n;
    gap_advertisements_enable(1);
}

// Format a BTstack bd_addr_t (big-endian) as 12 hex chars, MSB first.
static void addr_hex(const bd_addr_t a, char *out, size_t cap) {
    snprintf(out, cap, "%02x%02x%02x%02x%02x%02x", a[0], a[1], a[2], a[3], a[4], a[5]);
}

#define LINE_MAX 1024   // >= a JSON-RPC line; single-char commands are tiny
static char line_buf[LINE_MAX];
static int  line_len = 0;

// ---- rpc (JSON-RPC): builders + dispatch live in ../../shared/hidbridge_rpc.h; the Pico
// provides only the BLE-notify sink + its capabilities. Serial-only, so always authenticated. ----
static bool picoNotify(uint8_t rid, const uint8_t *body, size_t len) {
    uint8_t report[1 + 8];
    report[0] = rid;
    memcpy(report + 1, body, len);
    return PicoBluetoothBLEHID.send(report, 1 + len);
}
static void picoFillHello(JsonObject r) {
    r["device"] = BASE_NAME;
    r["fw"] = HB_FW_VERSION;                      // firmware SemVer (release version)
    r["name"] = slot_name[(adv_slot >= 0 && adv_slot < NUM_SLOTS) ? adv_slot : 0];
    // CAPS = the comprehensive method/feature list the host feature-detects on (it does NOT branch on
    // `proto`). The single-char device commands were folded into JSON-RPC and REMOVED (see handle_line);
    // these caps advertise their JSON replacements. VERSIONING: additive (a new method -> a new cap,
    // proto UNCHANGED); proto (hbmode1::PROTO_VERSION) bumps only on a breaking change. The Pico holds
    // ONE central (single-connection wrapper), so `connect`/`target` select a slot by INDEX.
    JsonArray caps = r["caps"].to<JsonArray>();
    caps.add("keyboard"); caps.add("mouse"); caps.add("slots");
    caps.add("connect"); caps.add("target"); caps.add("advertise"); caps.add("park");
    caps.add("layout"); caps.add("ping");
    JsonArray la = r["layouts"].to<JsonArray>();
    for (int i = 0; i < hbmode1::LAYOUT_COUNT; i++) la.add(hbmode1::LAYOUT_NAMES[i]);
}

// Resolve a target slot from a request: an explicit "slot" index (the Pico addresses slots by index --
// its single-connection wrapper has no per-slot client labels), else `dflt`.
static int picoSlotFromParams(JsonObjectConst params, int dflt) {
    if (params["slot"].is<int>()) {
        int s = params["slot"].as<int>();
        return (s >= 0 && s < NUM_SLOTS) ? s : -1;
    }
    return dflt;
}

static void picoFillIdentity(JsonObject o, int i) {
    if (!slot_addr_ready) { BluetoothLock l; derive_slot_addresses(); }
    o["slot"] = i;
    o["client"] = "";                            // the Pico wrapper tracks no per-slot client labels
    char ah[16]; addr_hex(slot_addr[i], ah, sizeof(ah)); o["addr"] = ah;
    o["name"] = slot_name[i];
    bool up = PicoBluetoothBLEHID.connected();
    o["conn"] = (up && conn_slot == i);
    o["sub"]  = (up && conn_slot == i);          // wrapper "connected" == HID input reports enabled
    o["layout"] = hbmode1::layoutName(slot_layout[i]);
}

// The Pico's firmware-specific JSON-RPC methods -- the JSON replacements for the removed single-char
// device commands (I/T/S/L/X/O/P). Single-connection model: connect/target select a slot by INDEX.
static void picoCustom(const char *method, JsonObjectConst params, JsonObject result,
                       bool *handled, int *errCode, const char **errMsg) {
    if (!strcmp(method, "identities")) {         // the old `S`
        *handled = true;
        JsonArray arr = result["slots"].to<JsonArray>();
        for (int i = 0; i < NUM_SLOTS; i++) picoFillIdentity(arr.add<JsonObject>(), i);
        result["adv"] = adv_slot;
        result["tgt"] = tgt_slot;
        result["multi"] = false;                 // single-connection wrapper: one central at a time
        return;
    }
    if (!strcmp(method, "connect")) {            // the old `I<n>`: select slot + advertise
        *handled = true;
        int slot = picoSlotFromParams(params, -1);
        if (slot < 0) { *errCode = -32602; *errMsg = "missing/invalid 'slot'"; return; }
        select_slot_advertise(slot, nullptr);
        tgt_slot = slot;
        picoFillIdentity(result, slot);
        return;
    }
    if (!strcmp(method, "target")) {             // the old `T<n>`: set the send-target slot
        *handled = true;
        int slot = picoSlotFromParams(params, -1);
        if (slot < 0) { *errCode = -32602; *errMsg = "missing/invalid 'slot'"; return; }
        tgt_slot = slot;
        result["tgt"] = slot;
        return;
    }
    if (!strcmp(method, "advertise")) {          // the old `A` (esp32) / re-enable advertising
        *handled = true;
        { BluetoothLock l; gap_advertisements_enable(1); }
        result["advertising"] = true;
        return;
    }
    if (!strcmp(method, "disconnect") || !strcmp(method, "park")) {  // the old `X` / `O`
        *handled = true;
        bool had = false;
        { BluetoothLock l;
          if (!strcmp(method, "disconnect") && pico_con_handle != HCI_CON_HANDLE_INVALID) {
              gap_disconnect(pico_con_handle); had = true;
          }
          gap_advertisements_enable(0);
        }
        if (!strcmp(method, "park")) result["parked"] = true;
        else { result["slot"] = tgt_slot; result["disconnected"] = had; }
        return;
    }
    if (!strcmp(method, "layout")) {             // the old `L`: get/set the target slot's layout (RAM-only)
        *handled = true;
        int slot = picoSlotFromParams(params, tgt_slot);
        if (slot < 0 || slot >= NUM_SLOTS) { *errCode = -32602; *errMsg = "invalid 'slot'"; return; }
        const char *name = params["name"] | (const char *)nullptr;
        if (name && name[0]) {
            if (!hbmode1::isKnownLayout(name)) { *errCode = -32602; *errMsg = "unknown layout (see hello.layouts)"; return; }
            slot_layout[slot] = hbmode1::layoutFromName(name);
        }
        result["slot"] = slot;
        result["layout"] = hbmode1::layoutName(slot_layout[slot]);
        return;
    }
    if (!strcmp(method, "ping")) {               // the old `P`
        *handled = true;
        bool up = PicoBluetoothBLEHID.connected();
        result["device"] = BASE_NAME;
        result["conn"] = up;
        result["subscribed"] = up;               // wrapper "connected" == input reports enabled
        return;
    }
    // not one of ours -> leave *handled=false (dispatcher returns "method not found")
}

static void handle_line(char *line) {
    while (*line == ' ' || *line == '\t') line++;
    if (line[0] == 0) return;

    if (line[0] == '{') {                       // rpc: JSON-RPC (serial is trusted -> authed=true)
        // The active keyboard layout is the TARGET slot's, so `type`/`key` map to the right chars.
        hbmode1::setActiveLayout((tgt_slot >= 0 && tgt_slot < NUM_SLOTS) ? slot_layout[tgt_slot]
                                                                         : hbmode1::LAYOUT_US);
        hbmode1::handleJsonRpc(String(line), Serial, true, picoNotify, picoFillHello, picoCustom);
        return;
    }

    // The MCU wire is now ONE versioned JSON-RPC protocol: the board builds every report AND owns device
    // management (picoCustom: connect/target/identities/advertise/park/disconnect/layout/ping -- the JSON
    // replacements for the old single-char commands, which were REMOVED). Any non-JSON line is refused.
    Serial.println("ERR unknowncmd");
}

void setup() {
    Serial.begin(115200);
    // Per-slot default names (until the host sets a target-scoped one via I<n>:name). Slot 0 keeps
    // the legacy "HIDBRIDGE-pico" so an EXISTING bond still sees a familiar name; slots >=1 = -sN.
    for (int i = 0; i < NUM_SLOTS; i++) {
        if (i == 0) snprintf(slot_name[i], sizeof(slot_name[i]), "%s", BASE_NAME);
        else        snprintf(slot_name[i], sizeof(slot_name[i]), "%s-s%d", BASE_NAME, i);
    }
    // Track WHICH slot the single connection belongs to (the wrapper only exposes one con handle):
    // onOpened fires on HID input-report-enable (connected + subscribed) -> record the advertising
    // slot; onClosed clears it. This is the Pico's per-slot connect/subscribe signal for `S`.
    PicoBluetoothBLEHID.setOpenedCB([](uint8_t, uint16_t, uint8_t *packet, uint16_t) {
        conn_slot = adv_slot;
        pico_con_handle = hids_subevent_input_report_enable_get_con_handle(packet);
    });
    PicoBluetoothBLEHID.setClosedCB([](uint8_t, uint16_t, uint8_t *, uint16_t) {
        conn_slot = -1;
        pico_con_handle = HCI_CON_HANDLE_INVALID;
    });

    // Install OUR report map (keyboard 1 + rel mouse 2) directly, so the report IDs + layouts are
    // byte-identical to reports.py. Advertise as slot 0 = "HIDBRIDGE-pico".
    // __BLEGetAppearance() == 0x03C0 (generic HID) with both keyboard + mouse force-linked.
    PicoBluetoothBLEHID.startHID(slot_name[0], BASE_NAME, __BLEGetAppearance(),
                                 HID_REPORT_MAP, sizeof(HID_REPORT_MAP));
    Serial.println("READY name=HIDBRIDGE-pico");
}

void loop() {
    while (Serial.available()) {
        char c = (char)Serial.read();
        if (c == '\n' || c == '\r') {
            line_buf[line_len] = 0;
            if (line_len > 0) handle_line(line_buf);
            line_len = 0;
        } else if (line_len < LINE_MAX - 1) {
            line_buf[line_len++] = c;
        } else {
            line_len = 0;
            Serial.println("ERR toolong");
        }
    }
}

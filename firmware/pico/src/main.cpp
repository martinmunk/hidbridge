/*
 * HIDBRIDGE-pico -- BLE HID for a Raspberry Pi Pico 2 W: keyboard(1) + relative mouse(2)
 *                 + absolute mouse(3) + digitizer/touchscreen(4).
 *
 * Part of the hidbridge toolbox: the PICO BACKEND. A ~USD 6 alternative to a USB Bluetooth
 * dongle for controlling an iPad/iPhone/Mac from Linux. Built on the earlephilhower arduino-pico
 * core, whose BTstack HID-over-GATT (HOG) implementation is iOS-compatible OUT OF THE BOX -- which
 * is the whole point: the Linux BlueZ/dongle path gets stuck on iOS not subscribing to the HID
 * input reports (a CCCD / Report-Reference GATT quirk); the Pico's HOG stack does it right.
 *
 * The device advertises as "HIDBRIDGE-pico" (distinct from the dongle's "HIDBRIDGE-dongle"). The host
 * (wsb) drives it over USB-CDC serial with a tiny ASCII line protocol -- ONE HID report per line --
 * so python/src/hidbridge/reports.py stays the SINGLE SOURCE OF TRUTH for the report bytes:
 *
 *     K <16 hex>\n   keyboard report    (8-byte boot body: mod,resv,k1..k6)         -> report ID 1
 *     M <8 hex>\n    relative mouse      (4-byte body: buttons,dx,dy,wheel)          -> report ID 2
 *     A <10 hex>\n   absolute mouse      (5-byte body: buttons,Xlo,Xhi,Ylo,Yhi)      -> report ID 3
 *     D <12 hex>\n   digitizer/touch     (6-byte body: flags,contactID,Xlo,Xhi,Ylo,Yhi) -> ID 4
 *     P\n            ping  -> replies "PONG name=HIDBRIDGE-pico conn=<0|1>"
 *     I<n>[:name]\n  select host SLOT n as the advertising identity (+ optional name) + advertise
 *                    -> "OK slot=<n> addr=<12hex> name=<name> conn=<0|1>"
 *     T<n>\n         set the send-target slot -> "OK tgt=<n>"
 *     S\n            slots status -> "OK slots=<N> adv=<n> tgt=<n> multi=0 c0=<0|1> .. s0=<0|1> .."
 *     X\n            disconnect the current central (keep bond) + PARK -> "OK disc" / "OK noconn"
 *     O\n            park: stop advertising, keep connection + bonds -> "OK parked"
 * Reply per line: "OK" on success, "ERR <reason>" on failure.
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
 * arduino-pico's auto-generated report map (that map assigns the mouse report ID 3 and has no
 * absolute-pointer or digitizer report). Instead we install OUR OWN HID report map -- the exact
 * bytes of python/src/hidbridge/reports.py REPORT_DESCRIPTOR (keyboard ID 1, relative mouse ID 2, absolute
 * mouse ID 3, digitizer ID 4) -- directly via PicoBluetoothBLEHID.startHID(). We still force-link
 * KeyboardBLE + MouseBLE so the core's __BLEInstallKeyboard/__BLEInstallMouse weak symbols become
 * strong: startHID() sizes its report storage from those (1 feature + 2 keyboard + 1 mouse = 4),
 * which exactly covers our 4 Input reports (IDs 1..4), and the fixed BTstack ATT DB already exposes
 * Input Report-Reference characteristics for report IDs 1,2,3,4. We never call the KeyboardBLE /
 * MouseBLE report builders; every report is sent as raw [reportID | body] bytes.
 */

#include <Arduino.h>
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
    // ---- Absolute Mouse, Report ID 3 ----
    0x05, 0x01, 0x09, 0x02, 0xA1, 0x01,
    0x85, 0x03, 0x09, 0x01, 0xA1, 0x00, 0x05, 0x09, 0x19, 0x01, 0x29, 0x03,
    0x15, 0x00, 0x25, 0x01, 0x95, 0x03, 0x75, 0x01, 0x81, 0x02, 0x95, 0x01,
    0x75, 0x05, 0x81, 0x03, 0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x15, 0x00,
    0x26, 0xFF, 0x7F, 0x75, 0x10, 0x95, 0x02, 0x81, 0x02, 0xC0, 0xC0,
    // ---- Digitizer / Touch Screen, Report ID 4 ----
    0x05,
    0x0D, 0x09, 0x04, 0xA1, 0x01, 0x85, 0x04, 0x09, 0x22, 0xA1, 0x02, 0x09,
    0x42, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x01, 0x81, 0x02, 0x09,
    0x32, 0x81, 0x02, 0x95, 0x06, 0x81, 0x03, 0x75, 0x08, 0x95, 0x01, 0x09,
    0x51, 0x81, 0x02, 0x05, 0x01, 0x15, 0x00, 0x26, 0xFF, 0x7F, 0x75, 0x10,
    0x95, 0x01, 0x09, 0x30, 0x81, 0x02, 0x09, 0x31, 0x81, 0x02, 0xC0, 0xC0,
    // WSB-HID-REPORT-MAP-END
};

// reports.py report body lengths (bytes AFTER the report ID) for each kind.
static const int KBD_BODY = 8;        // mod,resv,k1..k6                        -> report ID 1
static const int MOUSE_BODY = 4;      // buttons,dx,dy,wheel                    -> report ID 2
static const int ABS_MOUSE_BODY = 5;  // buttons,Xlo,Xhi,Ylo,Yhi               -> report ID 3
static const int DIGITIZER_BODY = 6;  // flags,contactID,Xlo,Xhi,Ylo,Yhi       -> report ID 4

// reportIDs -- MUST match the report map above / reports.py.
static const uint8_t RID_KEYBOARD = 1;
static const uint8_t RID_MOUSE = 2;
static const uint8_t RID_ABS_MOUSE = 3;
static const uint8_t RID_DIGITIZER = 4;

// ---- Per-bond IDENTITY (host slots) ----------------------------------------------------------- //
// Number of BLE host slots (distinct identities). Slot 0 = native public address; slots >=1 =
// derived static-random addresses. Overridable at build time (-DNUM_SLOTS=N). Two iPads need 2.
#ifndef NUM_SLOTS
#define NUM_SLOTS 2
#endif

static const char *BASE_NAME = "HIDBRIDGE-pico";
static bd_addr_t slot_addr[NUM_SLOTS];        // BTstack bd_addr_t: big-endian (addr[0] = MSB)
static char      slot_name[NUM_SLOTS][24];
static bool      slot_addr_ready = false;     // slot addresses derived (needs HCI up)
static int       adv_slot = 0;                // slot currently advertising
static int       tgt_slot = 0;                // send-target slot (single-conn wrapper: informational)
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

#define LINE_MAX 64
static char line_buf[LINE_MAX];
static int  line_len = 0;

static int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

// Parse a (whitespace-tolerant) hex string into out[]. Returns byte count, or -1 on bad
// nibble / odd length / overflow.
static int parse_hex(const char *s, uint8_t *out, int max) {
    int n = 0;
    while (*s) {
        if (*s == ' ' || *s == '\t') { s++; continue; }
        int hi = hexval(*s++);
        if (!*s) return -1;             // odd number of nibbles
        int lo = hexval(*s++);
        if (hi < 0 || lo < 0) return -1;
        if (n >= max) return -1;
        out[n++] = (uint8_t)((hi << 4) | lo);
    }
    return n;
}

// Emit one HID input report: [reportID | body] as a BLE HID notification, ack OK/ERR.
static void send_report(uint8_t report_id, const uint8_t *body, int body_len) {
    uint8_t report[1 + 8];   // 8 = max body (keyboard); abs(5)/digitizer(6)/mouse(4) all fit
    report[0] = report_id;
    memcpy(report + 1, body, body_len);
    bool ok = PicoBluetoothBLEHID.send(report, 1 + body_len);
    Serial.println(ok ? "OK" : "ERR notconnected");
}

static void handle_line(char *line) {
    while (*line == ' ' || *line == '\t') line++;
    if (line[0] == 0) return;

    char kind = line[0];
    if (kind == 'P' || kind == 'p') {
        Serial.print("PONG name=HIDBRIDGE-pico conn=");
        Serial.println(PicoBluetoothBLEHID.connected() ? 1 : 0);
        return;
    }
    if (kind == 'I' || kind == 'i') {
        // I<n>[:name] -- select host slot n as the advertising identity (+ optional name) + advertise.
        char *rest = line + 1;
        char *colon = strchr(rest, ':');
        const char *nm = nullptr;
        if (colon) { *colon = 0; nm = colon + 1; }
        int n = atoi(rest);
        if (rest[0] == 0 || n < 0 || n >= NUM_SLOTS) { Serial.println("ERR badslot"); return; }
        select_slot_advertise(n, (nm && *nm) ? nm : nullptr);
        char ah[16];
        addr_hex(slot_addr[n], ah, sizeof(ah));
        Serial.print("OK slot=");
        Serial.print(n);
        Serial.print(" addr=");
        Serial.print(ah);
        Serial.print(" name=");
        Serial.print(slot_name[n]);
        Serial.print(" conn=");
        Serial.println((conn_slot == n) ? 1 : 0);
        return;
    }
    if (kind == 'T' || kind == 't') {
        int n = atoi(line + 1);
        if (line[1] == 0 || n < 0 || n >= NUM_SLOTS) { Serial.println("ERR badslot"); return; }
        tgt_slot = n;
        Serial.print("OK tgt=");
        Serial.println(n);
        return;
    }
    if (kind == 'S' || kind == 's') {
        // multi=0: the single-connection PicoBluetoothBLEHID wrapper cannot hold two centrals at once.
        Serial.print("OK slots=");
        Serial.print(NUM_SLOTS);
        Serial.print(" adv=");
        Serial.print(adv_slot);
        Serial.print(" tgt=");
        Serial.print(tgt_slot);
        Serial.print(" multi=0");
        bool up = PicoBluetoothBLEHID.connected();
        for (int i = 0; i < NUM_SLOTS; i++) {
            Serial.print(" c");
            Serial.print(i);
            Serial.print("=");
            Serial.print((up && conn_slot == i) ? 1 : 0);
        }
        for (int i = 0; i < NUM_SLOTS; i++) {
            Serial.print(" s");
            Serial.print(i);
            Serial.print("=");
            Serial.print((up && conn_slot == i) ? 1 : 0);   // wrapper "connected" == input reports enabled
        }
        Serial.println();
        return;
    }
    if (kind == 'X' || kind == 'x') {
        // Disconnect the current central (keep the bond) + PARK (stop advertising), so the released
        // iPad is free to attach to ANOTHER board. Host brings this board back with I<n>.
        BluetoothLock l;
        if (pico_con_handle != HCI_CON_HANDLE_INVALID) {
            gap_disconnect(pico_con_handle);
            gap_advertisements_enable(0);
            Serial.println("OK disc");
        } else {
            gap_advertisements_enable(0);
            Serial.println("OK noconn");
        }
        return;
    }
    if (kind == 'O' || kind == 'o') {
        // Park: stop advertising, keep any existing connection + bonds. Resume with I<n>.
        BluetoothLock l;
        gap_advertisements_enable(0);
        Serial.println("OK parked");
        return;
    }

    uint8_t body[8];
    int n = parse_hex(line + 1, body, sizeof(body));
    if (kind == 'K' || kind == 'k') {
        if (n != KBD_BODY) { Serial.println("ERR badframe"); return; }
        send_report(RID_KEYBOARD, body, KBD_BODY);
    } else if (kind == 'M' || kind == 'm') {
        if (n != MOUSE_BODY) { Serial.println("ERR badframe"); return; }
        send_report(RID_MOUSE, body, MOUSE_BODY);
    } else if (kind == 'A' || kind == 'a') {
        if (n != ABS_MOUSE_BODY) { Serial.println("ERR badframe"); return; }
        send_report(RID_ABS_MOUSE, body, ABS_MOUSE_BODY);
    } else if (kind == 'D' || kind == 'd') {
        if (n != DIGITIZER_BODY) { Serial.println("ERR badframe"); return; }
        send_report(RID_DIGITIZER, body, DIGITIZER_BODY);
    } else {
        Serial.println("ERR unknowncmd");
    }
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

    // Install OUR report map (keyboard 1 + rel mouse 2 + abs mouse 3 + digitizer 4) directly, so
    // the report IDs + layouts are byte-identical to reports.py. Advertise as slot 0 = "HIDBRIDGE-pico".
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

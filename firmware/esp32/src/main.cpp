// wsb-hid-esp32 -- iOS-compatible BLE HID keyboard+mouse peripheral, driven over USB serial.
//
// PURPOSE (see docs/bt-hid-toolbox-README.md "ESP32 backend"): a ~$5 ESP32 is an alternative HID BACKEND
// for bt_hid, for a contributor who has an ESP32 instead of a USB Bluetooth dongle. Where the
// Linux/BlueZ dongle path fights iOS over the HID report CCCD / Report-Reference subscription,
// NimBLE-Arduino serves HID-over-GATT (HOG) correctly + iOS-compatibly out of the box, so this
// backend Just Works.
//
// SINGLE SOURCE OF TRUTH: the host (wsb) builds the exact HID report BYTES in
// python/src/hidbridge/reports.py and streams them here over USB serial; this firmware only relays them as
// BLE HID input notifications. The REPORT_MAP below is a byte-for-byte copy of
// reports.py REPORT_DESCRIPTOR (keyboard reportID 1, 8-byte body; mouse reportID 2, 4-byte body;
// absolute-mouse reportID 3, 5-byte body; digitizer/touchscreen reportID 4, 6-byte body).
// python/tests/test_bt_hid_esp32.py asserts the two stay identical -- do NOT edit one
// without the other.
//
// LIBRARY: NimBLE-Arduino 2.x (lightweight, current, correct HOG) on esp32 core 3.x (IDF 5.x).
// BUILD: PlatformIO project in firmware/esp32/ (platformio.ini + this src/main.cpp).
//   pio run                      # compile
//   pio run -t upload            # compile + flash (default env matches the connected board)
// See firmware/esp32/build_flash.sh for the wrapper + one-time setup.
//
// SERIAL PROTOCOL (115200 baud, newline-terminated ASCII lines; host -> ESP32):
//   K<hex>   keyboard report body, 8 bytes = 16 hex chars  -> notify keyboard input report
//   M<hex>   mouse report body, 4 bytes = 8 hex chars       -> notify mouse input report
//   A<hex>   abs-mouse report body, 5 bytes = 10 hex chars  -> notify abs-mouse input report
//   D<hex>   digitizer report body, 6 bytes = 12 hex chars  -> notify digitizer input report
//   P        ping                        -> "OK wsb-hid-esp32 subscribed=<0|1> conn=<0|1>"
//   A        (re)start advertising (bare 'A', no hex body)  -> "OK adv"
//   V        version/status                                 -> "OK wsb-hid-esp32 <state>"
//   X        disconnect the target slot's central (keep bond) + PARK -> "OK disc" / "OK noconn"
//   O        park: stop advertising, keep connections + bonds -> "OK parked"
//   I<n>[:name]  select host SLOT n as the advertising identity (+ optional per-slot name) + advertise
//                -> "OK slot=<n> addr=<12hex> name=<name> conn=<0|1>"
//   T<n>     set the SEND-target slot (K/M/A/D notify ONLY that slot's connection) -> "OK tgt=<n>"
//   S        slots status -> "OK slots=<N> adv=<n> tgt=<n> multi=1 c0=<0|1> c1=.. s0=<0|1> s1=.."
// NOTE: 'A' is overloaded -- a bare "A\n" (re)starts advertising; "A"+10 hex is an abs-mouse report.
// They never collide because an abs-mouse frame always carries its 5-byte (10-hex) body.
// ESP32 -> host replies are single lines: "OK ...", "OK noconn" (accepted but no central), or
// "ERR ...". Asynchronous link events are emitted as "EVT connected" / "EVT disconnected", and HID
// report SUBSCRIPTION as "EVT ready" / "EVT unsubscribed" (iOS wrote/cleared the report CCCDs).
//
// PER-BOND IDENTITY (host slots -- docs/bt-hid-mcu-approach.md): the board presents a DISTINCT BLE
// identity/address PER bonded iPad, so ONE board can hold two iPads at once on distinct addresses
// (no time-slice). Slot 0 = the board's NATIVE public address, so an EXISTING bond survives a
// reflash. Slots >=1 = deterministic STATIC-RANDOM addresses derived from the public address. NimBLE
// holds MULTIPLE centrals AT ONCE (CONFIG_BT_NIMBLE_MAX_CONNECTIONS>1) and can notify a SPECIFIC
// connection, so the ESP32 can hold e.g. mini6 on slot 0 AND mini5 on slot 1 SIMULTANEOUSLY and
// drive each independently (T<n> picks which). The classic ESP32 (BT4.2) has NO extended
// advertising, so only ONE identity ADVERTISES at a time -- a second/reconnecting central is served
// by selecting its slot (I<n>) and advertising -- but once connected, connections are held
// simultaneously. Each slot advertises a TARGET-scoped name (wsb-esp32-<ipad>) so an iPad's
// Bluetooth settings show which entry is meant for it (host sets it via I<n>:name).
//
// READINESS (subscribed=): a link being up (conn=1) is NOT "ready to use" -- after a (re)connect iOS
// spends ~1-3s reading the report map + writing the report CCCDs before it acts on input reports, so
// a keystroke sent in that window is dropped. onSubscribe() below tracks when iOS ENABLES the
// keyboard input report (its CCCD) PER slot and exposes the target slot as subscribed=<0|1>, the
// real "ready" signal the host (HidSession) awaits before sending. `subscribed=` is placed BEFORE
// `conn=` so an older host's endswith("conn=1") parse still holds.

#include <Arduino.h>
#include <NimBLEDevice.h>
#include <NimBLEHIDDevice.h>
#include <NimBLEServer.h>

static const char* DEVICE_NAME = "wsb-hid-esp32";

// Number of BLE host slots (distinct identities). Slot 0 = native public address; slots >=1 =
// derived static-random addresses. Overridable at build time (-DNUM_SLOTS=N). Two iPads need 2.
#ifndef NUM_SLOTS
#define NUM_SLOTS 2
#endif

// The ESP32 must hold >=NUM_SLOTS centrals connected AT ONCE for simultaneous per-bond identities.
// NimBLE's default connection budget is 3 (nimconfig.h); we also set it via build_flags for clarity.
// This static_assert fails the BUILD if the budget is ever configured below the slot count.
#ifndef CONFIG_BT_NIMBLE_MAX_CONNECTIONS
#define CONFIG_BT_NIMBLE_MAX_CONNECTIONS 3
#endif
static_assert(CONFIG_BT_NIMBLE_MAX_CONNECTIONS >= NUM_SLOTS,
              "CONFIG_BT_NIMBLE_MAX_CONNECTIONS must be >= NUM_SLOTS to hold all slots at once");

// ---- HID report map: byte-identical to reports.py REPORT_DESCRIPTOR ----
static const uint8_t REPORT_MAP[] = {
  // ---- Keyboard, Report ID 1 ----
  0x05, 0x01,        // Usage Page (Generic Desktop)
  0x09, 0x06,        // Usage (Keyboard)
  0xA1, 0x01,        // Collection (Application)
  0x85, 0x01,        //   Report ID (1)
  0x05, 0x07,        //   Usage Page (Key Codes)
  0x19, 0xE0,        //   Usage Minimum (224)
  0x29, 0xE7,        //   Usage Maximum (231)
  0x15, 0x00,        //   Logical Minimum (0)
  0x25, 0x01,        //   Logical Maximum (1)
  0x75, 0x01,        //   Report Size (1)
  0x95, 0x08,        //   Report Count (8)
  0x81, 0x02,        //   Input (Data,Var,Abs)  -- modifier byte
  0x95, 0x01,        //   Report Count (1)
  0x75, 0x08,        //   Report Size (8)
  0x81, 0x03,        //   Input (Const)         -- reserved byte
  0x95, 0x06,        //   Report Count (6)
  0x75, 0x08,        //   Report Size (8)
  0x15, 0x00,        //   Logical Minimum (0)
  0x26, 0xFF, 0x00,  //   Logical Maximum (255)
  0x05, 0x07,        //   Usage Page (Key Codes)
  0x19, 0x00,        //   Usage Minimum (0)
  0x29, 0xFF,        //   Usage Maximum (255)
  0x81, 0x00,        //   Input (Data,Array)    -- 6 key slots
  0xC0,              // End Collection
  // ---- Mouse, Report ID 2 ----
  0x05, 0x01,        // Usage Page (Generic Desktop)
  0x09, 0x02,        // Usage (Mouse)
  0xA1, 0x01,        // Collection (Application)
  0x85, 0x02,        //   Report ID (2)
  0x09, 0x01,        //   Usage (Pointer)
  0xA1, 0x00,        //   Collection (Physical)
  0x05, 0x09,        //     Usage Page (Buttons)
  0x19, 0x01,        //     Usage Minimum (1)
  0x29, 0x05,        //     Usage Maximum (5)  -- L/R/M + Back(4) + Forward(5)
  0x15, 0x00,        //     Logical Minimum (0)
  0x25, 0x01,        //     Logical Maximum (1)
  0x95, 0x05,        //     Report Count (5)
  0x75, 0x01,        //     Report Size (1)
  0x81, 0x02,        //     Input (Data,Var,Abs)  -- 5 buttons
  0x95, 0x01,        //     Report Count (1)
  0x75, 0x03,        //     Report Size (3)
  0x81, 0x03,        //     Input (Const)         -- 3-bit padding (buttons byte stays 1 byte)
  0x05, 0x01,        //     Usage Page (Generic Desktop)
  0x09, 0x30,        //     Usage (X)
  0x09, 0x31,        //     Usage (Y)
  0x09, 0x38,        //     Usage (Wheel)
  0x15, 0x81,        //     Logical Minimum (-127)
  0x25, 0x7F,        //     Logical Maximum (127)
  0x75, 0x08,        //     Report Size (8)
  0x95, 0x03,        //     Report Count (3)
  0x81, 0x06,        //     Input (Data,Var,Rel)  -- X, Y, Wheel
  0xC0,              //   End Collection
  0xC0,              // End Collection
  // ---- Absolute Mouse, Report ID 3 ----
  0x05, 0x01,        // Usage Page (Generic Desktop)
  0x09, 0x02,        // Usage (Mouse)
  0xA1, 0x01,        // Collection (Application)
  0x85, 0x03,        //   Report ID (3)
  0x09, 0x01,        //   Usage (Pointer)
  0xA1, 0x00,        //   Collection (Physical)
  0x05, 0x09,        //     Usage Page (Buttons)
  0x19, 0x01,        //     Usage Minimum (1)
  0x29, 0x03,        //     Usage Maximum (3)
  0x15, 0x00,        //     Logical Minimum (0)
  0x25, 0x01,        //     Logical Maximum (1)
  0x95, 0x03,        //     Report Count (3)
  0x75, 0x01,        //     Report Size (1)
  0x81, 0x02,        //     Input (Data,Var,Abs)   -- 3 buttons
  0x95, 0x01,        //     Report Count (1)
  0x75, 0x05,        //     Report Size (5)
  0x81, 0x03,        //     Input (Const)          -- 5-bit padding
  0x05, 0x01,        //     Usage Page (Generic Desktop)
  0x09, 0x30,        //     Usage (X)
  0x09, 0x31,        //     Usage (Y)
  0x15, 0x00,        //     Logical Minimum (0)
  0x26, 0xFF, 0x7F,  //     Logical Maximum (32767)
  0x75, 0x10,        //     Report Size (16)
  0x95, 0x02,        //     Report Count (2)
  0x81, 0x02,        //     Input (Data,Var,Abs)   -- X, Y ABSOLUTE (no Rel bit)
  0xC0,              //   End Collection
  0xC0,              // End Collection
  // ---- Digitizer / Touch Screen, Report ID 4 ----
  0x05, 0x0D,        // Usage Page (Digitizers)
  0x09, 0x04,        // Usage (Touch Screen)
  0xA1, 0x01,        // Collection (Application)
  0x85, 0x04,        //   Report ID (4)
  0x09, 0x22,        //   Usage (Finger)
  0xA1, 0x02,        //   Collection (Logical)
  0x09, 0x42,        //     Usage (Tip Switch)
  0x15, 0x00,        //     Logical Minimum (0)
  0x25, 0x01,        //     Logical Maximum (1)
  0x75, 0x01,        //     Report Size (1)
  0x95, 0x01,        //     Report Count (1)
  0x81, 0x02,        //     Input (Data,Var,Abs)   -- Tip Switch (bit 0)
  0x09, 0x32,        //     Usage (In Range)
  0x81, 0x02,        //     Input (Data,Var,Abs)   -- In Range (bit 1)
  0x95, 0x06,        //     Report Count (6)
  0x81, 0x03,        //     Input (Const)          -- 6-bit padding to fill byte 0
  0x75, 0x08,        //     Report Size (8)
  0x95, 0x01,        //     Report Count (1)
  0x09, 0x51,        //     Usage (Contact Identifier)
  0x81, 0x02,        //     Input (Data,Var,Abs)   -- Contact ID (byte 1)
  0x05, 0x01,        //     Usage Page (Generic Desktop)
  0x15, 0x00,        //     Logical Minimum (0)
  0x26, 0xFF, 0x7F,  //     Logical Maximum (32767)
  0x75, 0x10,        //     Report Size (16)
  0x95, 0x01,        //     Report Count (1)
  0x09, 0x30,        //     Usage (X)
  0x81, 0x02,        //     Input (Data,Var,Abs)   -- X ABSOLUTE
  0x09, 0x31,        //     Usage (Y)
  0x81, 0x02,        //     Input (Data,Var,Abs)   -- Y ABSOLUTE
  0xC0,              //   End Collection
  0xC0,              // End Collection
};

static const uint8_t KBD_REPORT_ID = 1;
static const uint8_t MOUSE_REPORT_ID = 2;
static const uint8_t ABS_MOUSE_REPORT_ID = 3;
static const uint8_t DIGITIZER_REPORT_ID = 4;
static const size_t  KBD_BODY_LEN = 8;
static const size_t  MOUSE_BODY_LEN = 4;
static const size_t  ABS_MOUSE_BODY_LEN = 5;
static const size_t  DIGITIZER_BODY_LEN = 6;

static NimBLEHIDDevice*    hid = nullptr;
static NimBLECharacteristic* inputKbd = nullptr;
static NimBLECharacteristic* inputMouse = nullptr;
static NimBLECharacteristic* inputAbsMouse = nullptr;
static NimBLECharacteristic* inputDigitizer = nullptr;
static NimBLEServer*       server = nullptr;

// ---- per-slot identity + connection state (host slots) ----
// slotAddr[0] = native public address (captured at setup); slotAddr[k>=1] = derived static-random.
// slotConn[k] = the conn handle of the central connected on slot k (or NONE). slotSub[k] = iOS has
// enabled the keyboard report CCCD on slot k (readiness). advSlot = the slot currently ADVERTISING;
// tgtSlot = the slot K/M/A/D reports are notified to. slotName[k] = that slot's advertised name.
static NimBLEAddress       slotAddr[NUM_SLOTS];
static uint16_t            slotConn[NUM_SLOTS];
static volatile bool       slotSub[NUM_SLOTS];
static char                slotName[NUM_SLOTS][24];
static int                 advSlot = 0;
static int                 tgtSlot = 0;
static bool                parked = false;   // true = intentionally NOT advertising (host parked it)

// Forward declarations: the server callbacks (below) resume/point advertising; defined after setup().
static void selectSlotAdvertise(int n, const char* name);
static void startAdvertisingSlot();

static int slotOfHandle(uint16_t h) {
  for (int i = 0; i < NUM_SLOTS; i++) {
    if (slotConn[i] == h) return i;
  }
  return -1;
}

static void defaultSlotName(int n, char* out, size_t cap) {
  // Fallback name until the host sets a target-scoped one via I<n>:name. wsb-hid-esp32 for slot 0
  // (matches the legacy DEVICE_NAME so an old bond still sees a familiar name), wsb-hid-esp32-sN else.
  if (n == 0) snprintf(out, cap, "%s", DEVICE_NAME);
  else        snprintf(out, cap, "%s-s%d", DEVICE_NAME, n);
}

// ---- server callbacks: track link PER slot + keep advertising the dropped slot ----
class ServerCB : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer* s, NimBLEConnInfo& info) override {
    // The central connected on whatever identity we were advertising -> record it against advSlot.
    if (advSlot >= 0 && advSlot < NUM_SLOTS) slotConn[advSlot] = info.getConnHandle();
    Serial.println("EVT connected");
  }
  void onDisconnect(NimBLEServer* s, NimBLEConnInfo& info, int reason) override {
    int slot = slotOfHandle(info.getConnHandle());
    if (slot >= 0) {
      slotConn[slot] = BLE_HS_CONN_HANDLE_NONE;
      slotSub[slot] = false;              // link gone -> reports no longer enabled on this slot
    }
    Serial.println("EVT disconnected");
    // Resume advertising the slot the HOST currently has selected (advSlot) -- do NOT hijack the
    // single legacy advertiser to the dropped slot. (The old code re-pointed advertising to the
    // dropped slot, so a mini6 flap while we were advertising slot1 for mini5's pair switched the
    // advertiser back to slot0 -- mini5 then never saw wsb-esp32-mini5.) On the classic ESP32 only
    // ONE identity advertises at a time; the host picks it with I<n>, and a dropped bonded device
    // reconnects when the host next advertises ITS slot. BUT if the host PARKED us (X/O), stay
    // parked -- so a deliberately-released iPad is free to attach to ANOTHER board (deterministic
    // cross-board handoff), instead of us immediately re-grabbing it.
    if (!parked) startAdvertisingSlot();
  }
};

// ---- characteristic callbacks: track when iOS SUBSCRIBES to the keyboard input report, PER slot --
class KbdSubCB : public NimBLECharacteristicCallbacks {
  void onSubscribe(NimBLECharacteristic* chr, NimBLEConnInfo& info, uint16_t subValue) override {
    int slot = slotOfHandle(info.getConnHandle());
    if (slot < 0) return;
    bool nowSub = (subValue != 0);
    if (nowSub != slotSub[slot]) {
      slotSub[slot] = nowSub;
      Serial.println(nowSub ? "EVT ready" : "EVT unsubscribed");
    }
  }
};
static KbdSubCB kbdSubCB;

static void startAdvertisingSlot() {
  // Build the advertising payload EXPLICITLY + FRESH each slot. (The earlier auto-build path called
  // adv->addServiceUUID()/setName() repeatedly -> service UUIDs ACCUMULATED across slot switches and
  // the name lived in a scan response that did not refresh, so a slot could advertise the WRONG
  // slot's name -- proven by btmon: slot1's address broadcast slot0's name. Building explicit adv
  // data with clearData() each time fixes it.) Everything (flags+name+appearance+HID UUID = ~28B)
  // fits in the 31-byte main packet, so no scan response is needed.
  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  adv->stop();
  adv->clearData();
  adv->enableScanResponse(false);
  NimBLEAdvertisementData d;
  d.setFlags(0x06);                           // LE General Discoverable, BR/EDR not supported
  d.setName(slotName[advSlot]);               // the ACTIVE slot's target-scoped name
  d.setAppearance(0x03C1);                    // HID Keyboard
  d.addServiceUUID(hid->getHidService()->getUUID());
  adv->setAdvertisementData(d);
  adv->start();
}

// Select host slot n as the advertising identity (set its address + name) and (re)start advertising.
// If `name` is non-null + non-empty, it becomes slot n's advertised name (persisted for reconnects).
static void selectSlotAdvertise(int n, const char* name) {
  if (n < 0 || n >= NUM_SLOTS) return;
  NimBLEDevice::stopAdvertising();            // ble_hs_id_set_rnd requires advertising stopped
  if (n == 0) {
    NimBLEDevice::setOwnAddrType(BLE_OWN_ADDR_PUBLIC);   // native identity (existing bond survives)
  } else {
    NimBLEDevice::setOwnAddr(slotAddr[n]);               // ble_hs_id_set_rnd(<derived static-random>)
    NimBLEDevice::setOwnAddrType(BLE_OWN_ADDR_RANDOM);
  }
  if (name != nullptr && name[0] != '\0') {
    snprintf(slotName[n], sizeof(slotName[n]), "%s", name);
  }
  advSlot = n;
  parked = false;                             // selecting a slot means we ARE advertising again
  startAdvertisingSlot();
}

static void deriveSlotAddresses() {
  // slot 0 = the board's native public address (captured now); slots >=1 = deterministic
  // static-random addresses derived from it: force the two MSBits of the MSB to 0b11 (the BT
  // "static random" signature) and XOR the slot index into the LSB so each slot is DISTINCT from
  // the public address and from every other slot.
  //
  // BYTE ORDER (critical): NimBLEDevice::getAddress().getBase()->val is LITTLE-endian (val[5]=MSB,
  // val[0]=LSB), but NimBLEAddress(const uint8_t[6], type) REVERSE-COPIES its input (it expects
  // HUMAN order, index 0 = MSB). So we build the derived address in HUMAN order: h[0]=MSB, h[5]=LSB.
  slotAddr[0] = NimBLEDevice::getAddress();
  const uint8_t* base = slotAddr[0].getBase()->val;   // little-endian
  uint8_t human[6];
  for (int i = 0; i < 6; i++) human[i] = base[5 - i];  // human order: human[0] = MSB
  for (int k = 1; k < NUM_SLOTS; k++) {
    uint8_t h[6];
    memcpy(h, human, 6);
    h[0] = (uint8_t)(human[0] | 0xC0);                 // MSB top bits -> 0b11 (static random)
    h[5] ^= (uint8_t)(0x40 + k);                       // vary the LSB per slot (distinct)
    slotAddr[k] = NimBLEAddress(h, BLE_ADDR_RANDOM);   // reverse_copy -> val[5]=MSB=0b11.. (valid)
  }
}

void setup() {
  Serial.begin(115200);
  delay(100);

  for (int i = 0; i < NUM_SLOTS; i++) {
    slotConn[i] = BLE_HS_CONN_HANDLE_NONE;
    slotSub[i] = false;
    defaultSlotName(i, slotName[i], sizeof(slotName[i]));
  }

  NimBLEDevice::init(DEVICE_NAME);
  // iOS-compatible HOG bonding: bond=yes, MITM=no (Just Works), Secure Connections=yes.
  NimBLEDevice::setSecurityAuth(true, false, true);
  NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);

  server = NimBLEDevice::createServer();
  server->setCallbacks(new ServerCB());

  hid = new NimBLEHIDDevice(server);
  inputKbd = hid->getInputReport(KBD_REPORT_ID);
  inputKbd->setCallbacks(&kbdSubCB);           // track iOS enabling the keyboard report (readiness)
  inputMouse = hid->getInputReport(MOUSE_REPORT_ID);
  inputAbsMouse = hid->getInputReport(ABS_MOUSE_REPORT_ID);
  inputDigitizer = hid->getInputReport(DIGITIZER_REPORT_ID);

  hid->setManufacturer("wsb");
  hid->setPnp(0x02, 0x05AC, 0x820A, 0x0001);   // Apple vendor id, keyboard-ish product
  hid->setHidInfo(0x00, 0x01);                 // country 0, flags: remote-wake
  hid->setReportMap((uint8_t*)REPORT_MAP, sizeof(REPORT_MAP));
  hid->setBatteryLevel(100);

  server->start();                             // starts the HID + battery + devinfo services
  deriveSlotAddresses();                       // needs init() done (public address available)
  selectSlotAdvertise(0, nullptr);             // advertise slot 0 (native identity) by default
  Serial.println("OK wsb-hid-esp32 ready");
}

// ---- hex helpers ----
static int hexNibble(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

// Parse `hex` (2*expect chars) into out[expect]. Returns true on success.
static bool parseHexBody(const String& hex, uint8_t* out, size_t expect) {
  if (hex.length() != expect * 2) return false;
  for (size_t i = 0; i < expect; i++) {
    int hi = hexNibble(hex[2 * i]);
    int lo = hexNibble(hex[2 * i + 1]);
    if (hi < 0 || lo < 0) return false;
    out[i] = (uint8_t)((hi << 4) | lo);
  }
  return true;
}

// Format a NimBLEAddress as 12 hex chars, MSB first (val[] is little-endian, so print val[5..0]).
static void addrHex(const NimBLEAddress& a, char* out, size_t cap) {
  const uint8_t* v = a.getBase()->val;
  snprintf(out, cap, "%02x%02x%02x%02x%02x%02x", v[5], v[4], v[3], v[2], v[1], v[0]);
}

static void sendReportToTarget(NimBLECharacteristic* chr, const uint8_t* body, size_t len) {
  chr->setValue((uint8_t*)body, len);
  uint16_t h = (tgtSlot >= 0 && tgtSlot < NUM_SLOTS) ? slotConn[tgtSlot] : BLE_HS_CONN_HANDLE_NONE;
  if (h != BLE_HS_CONN_HANDLE_NONE) {
    chr->notify(h);                            // drive ONLY the target slot's central
    Serial.println("OK");
  } else {
    Serial.println("OK noconn");
  }
}

static bool anyConnected() {
  for (int i = 0; i < NUM_SLOTS; i++) {
    if (slotConn[i] != BLE_HS_CONN_HANDLE_NONE) return true;
  }
  return false;
}

static void printSlotsStatus() {
  Serial.print("OK slots=");
  Serial.print(NUM_SLOTS);
  Serial.print(" adv=");
  Serial.print(advSlot);
  Serial.print(" tgt=");
  Serial.print(tgtSlot);
  Serial.print(" multi=1");                    // NimBLE holds distinct identities simultaneously
  for (int i = 0; i < NUM_SLOTS; i++) {
    Serial.print(" c");
    Serial.print(i);
    Serial.print("=");
    Serial.print(slotConn[i] != BLE_HS_CONN_HANDLE_NONE ? "1" : "0");
  }
  for (int i = 0; i < NUM_SLOTS; i++) {
    Serial.print(" s");
    Serial.print(i);
    Serial.print("=");
    Serial.print(slotSub[i] ? "1" : "0");
  }
  Serial.println();
}

static void handleLine(String line) {
  line.trim();
  if (line.length() == 0) return;
  char cmd = line[0];
  String arg = line.substring(1);

  if (cmd == 'K') {
    uint8_t body[KBD_BODY_LEN];
    if (!parseHexBody(arg, body, KBD_BODY_LEN)) { Serial.println("ERR badkbd"); return; }
    sendReportToTarget(inputKbd, body, KBD_BODY_LEN);
  } else if (cmd == 'M') {
    uint8_t body[MOUSE_BODY_LEN];
    if (!parseHexBody(arg, body, MOUSE_BODY_LEN)) { Serial.println("ERR badmouse"); return; }
    sendReportToTarget(inputMouse, body, MOUSE_BODY_LEN);
  } else if (cmd == 'D') {
    uint8_t body[DIGITIZER_BODY_LEN];
    if (!parseHexBody(arg, body, DIGITIZER_BODY_LEN)) { Serial.println("ERR baddigi"); return; }
    sendReportToTarget(inputDigitizer, body, DIGITIZER_BODY_LEN);
  } else if (cmd == 'P') {
    // Report the TARGET slot's state (default slot 0 = legacy single-central behaviour).
    // subscribed= BEFORE conn= so a legacy host's endswith("conn=1") parse still works.
    bool sub = (tgtSlot >= 0 && tgtSlot < NUM_SLOTS) && slotSub[tgtSlot];
    bool con = (tgtSlot >= 0 && tgtSlot < NUM_SLOTS) &&
               slotConn[tgtSlot] != BLE_HS_CONN_HANDLE_NONE;
    Serial.print("OK wsb-hid-esp32 subscribed=");
    Serial.print(sub ? "1" : "0");
    Serial.print(" conn=");
    Serial.println(con ? "1" : "0");
  } else if (cmd == 'A') {
    // 'A' is overloaded: bare "A" -> (re)start advertising the current slot; "A"+10 hex -> abs-mouse.
    if (arg.length() == 0) {
      NimBLEDevice::startAdvertising();
      Serial.println("OK adv");
    } else {
      uint8_t body[ABS_MOUSE_BODY_LEN];
      if (!parseHexBody(arg, body, ABS_MOUSE_BODY_LEN)) { Serial.println("ERR badabs"); return; }
      sendReportToTarget(inputAbsMouse, body, ABS_MOUSE_BODY_LEN);
    }
  } else if (cmd == 'I') {
    // I<n>[:name] -- select host slot n as the advertising identity (+ optional per-slot name) +
    // advertise it. Adds a distinct identity so a second/other iPad can bond+connect on ITS address
    // while any already-connected slot stays connected (simultaneous per-bond identities).
    int sep = arg.indexOf(':');
    String nStr = (sep >= 0) ? arg.substring(0, sep) : arg;
    String nm = (sep >= 0) ? arg.substring(sep + 1) : String("");
    int n = nStr.toInt();
    if (nStr.length() == 0 || n < 0 || n >= NUM_SLOTS) { Serial.println("ERR badslot"); return; }
    selectSlotAdvertise(n, nm.length() ? nm.c_str() : nullptr);
    char ah[16];
    addrHex(slotAddr[n], ah, sizeof(ah));
    Serial.print("OK slot=");
    Serial.print(n);
    Serial.print(" addr=");
    Serial.print(ah);
    Serial.print(" name=");
    Serial.print(slotName[n]);
    Serial.print(" conn=");
    Serial.println(slotConn[n] != BLE_HS_CONN_HANDLE_NONE ? "1" : "0");
  } else if (cmd == 'T') {
    // T<n> -- route subsequent K/M/A/D reports to slot n's connection.
    int n = arg.toInt();
    if (arg.length() == 0 || n < 0 || n >= NUM_SLOTS) { Serial.println("ERR badslot"); return; }
    tgtSlot = n;
    Serial.print("OK tgt=");
    Serial.println(n);
  } else if (cmd == 'S') {
    printSlotsStatus();
  } else if (cmd == 'V') {
    Serial.print("OK wsb-hid-esp32 ");
    Serial.println(anyConnected() ? "connected" : "advertising");
  } else if (cmd == 'X') {
    // Disconnect the TARGET slot's central but KEEP the bond, then PARK (stop advertising) so the
    // released iPad is free to attach to ANOTHER board (deterministic cross-board handoff). The host
    // brings this board back with I<n>. Other slots' connections are untouched.
    uint16_t h = (tgtSlot >= 0 && tgtSlot < NUM_SLOTS) ? slotConn[tgtSlot] : BLE_HS_CONN_HANDLE_NONE;
    parked = true;                            // onDisconnect must NOT auto-re-advertise
    if (h != BLE_HS_CONN_HANDLE_NONE && server != nullptr) {
      server->disconnect(h);
      Serial.println("OK disc");
    } else {
      Serial.println("OK noconn");
    }
    NimBLEDevice::stopAdvertising();
  } else if (cmd == 'O') {
    // Park: stop advertising but KEEP existing connections + bonds. Lets the OTHER board's test run
    // without this board grabbing an iPad. Resume with I<n>.
    parked = true;
    NimBLEDevice::stopAdvertising();
    Serial.println("OK parked");
  } else {
    Serial.println("ERR unknown");
  }
}

void loop() {
  static String buf;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') {
      handleLine(buf);
      buf = "";
    } else if (c != '\r') {
      buf += c;
      if (buf.length() > 128) buf = "";  // guard against runaway input
    }
  }
  delay(2);
}

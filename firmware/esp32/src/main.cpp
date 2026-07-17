// HIDBRIDGE-esp32 -- iOS-compatible BLE HID keyboard+mouse peripheral, driven over USB serial.
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
// TRANSPORTS: the SAME line protocol is served over USB serial AND (on a board with an Ethernet PHY,
// built with -DHIDBRIDGE_NET) over TCP on port 3333. A command is answered on the channel it arrived
// on; async EVT lines are broadcast to serial + every AUTHENTICATED network client. See "NETWORK".
//
// SERIAL PROTOCOL (115200 baud, newline-terminated ASCII lines; host -> ESP32):
//   K<hex>   keyboard report body, 8 bytes = 16 hex chars  -> notify keyboard input report
//   M<hex>   mouse report body, 4 bytes = 8 hex chars       -> notify mouse input report
//   A<hex>   abs-mouse report body, 5 bytes = 10 hex chars  -> notify abs-mouse input report
//   D<hex>   digitizer report body, 6 bytes = 12 hex chars  -> notify digitizer input report
//   P        ping                        -> "OK HIDBRIDGE-esp32 subscribed=<0|1> conn=<0|1>"
//   A        (re)start advertising (bare 'A', no hex body)  -> "OK adv"
//   V        version/status                                 -> "OK HIDBRIDGE-esp32 <state>"
//   X        disconnect the target slot's central (keep bond) + PARK -> "OK disc" / "OK noconn"
//   O        park: stop advertising, keep connections + bonds -> "OK parked"
//   I<n>[:name]  select host SLOT n as the advertising identity (+ optional per-slot name) + advertise
//                -> "OK slot=<n> addr=<12hex> name=<name> conn=<0|1>"
//   T<n>     set the SEND-target slot (K/M/A/D notify ONLY that slot's connection) -> "OK tgt=<n>"
//   S        slots status -> "OK slots=<N> adv=<n> tgt=<n> multi=1 c0=<0|1> c1=.. s0=<0|1> s1=.."
//   N        network status -> "OK net eth=<0|1> ip=<a.b.c.d> port=<n> mdns=<host> token=<0|1>
//                              clients=<n>"   (eth=0 / ip=0.0.0.0 on a build without -DHIDBRIDGE_NET)
//   W<token> SET the network auth token (>=8 chars) in NVS. SERIAL ONLY -- refused over TCP, so the
//            secret can only be provisioned over the physically-attached cable. -> "OK token set"
//            (the token itself is NEVER echoed, logged, or readable back off the board).
//   @<token> AUTHENTICATE this TCP session -> "OK auth" / "ERR auth". See "NETWORK" below.
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
// simultaneously. Each slot advertises a TARGET-scoped name (HIDBRIDGE-esp32-<ipad>) so an iPad's
// Bluetooth settings show which entry is meant for it (host sets it via I<n>:name).
//
// READINESS (subscribed=): a link being up (conn=1) is NOT "ready to use" -- after a (re)connect iOS
// spends ~1-3s reading the report map + writing the report CCCDs before it acts on input reports, so
// a keystroke sent in that window is dropped. onSubscribe() below tracks when iOS ENABLES the
// keyboard input report (its CCCD) PER slot and exposes the target slot as subscribed=<0|1>, the
// real "ready" signal the host (HidSession) awaits before sending. `subscribed=` is placed BEFORE
// `conn=` so an older host's endswith("conn=1") parse still holds.
//
// NETWORK (-DHIDBRIDGE_NET; env:esp32-ethernet-kit -- an ESP32 with a wired Ethernet PHY):
// the board joins the LAN over Ethernet (DHCP), advertises itself as `hidbridge-esp32.local` (mDNS),
// and serves the SAME line protocol on TCP 3333 -- so the host can drive the HID target with no USB
// cable at all (`hidbridge --backend net --host hidbridge-esp32.local`). USB serial keeps working
// exactly as before; the two channels are interchangeable and may be used at the same time.
//
// AUTH + ENCRYPTION -- a TCP port that injects keystrokes into a bonded, possibly UNLOCKED iPad (and
// can type its passcode) is a REMOTE INPUT-INJECTION surface: anyone who can reach the port could type
// into the target, and anyone who can sniff it could read the passcode. So TCP 3333 is TLS-PSK ONLY
// (see the TLS note below) and FAILS CLOSED:
//   * a shared secret is stored in NVS and is settable ONLY over USB serial (`W<token>`) -- never
//     over TCP, never compiled into the image, never echoed back;
//   * that same token is the TLS pre-shared KEY, so the report bytes (INCLUDING the passcode frames
//     `hidbridge unlock` emits) are ENCRYPTED on the wire, AND completing the handshake proves the
//     peer holds the token without it ever being transmitted -- the handshake is the authentication;
//   * if NO token has been provisioned, the TLS port is not even opened -- a board merely plugged into
//     the LAN cannot be driven over it at all;
//   * a `@<token>` line is still accepted (over the encrypted channel) for the existing client flow,
//     but is redundant now that the handshake authenticates; the compare is constant-time.
// The one remaining cleartext surface is the OPTIONAL control webpage on :80 (browsers cannot speak
// TLS-PSK); it is HTTP with an X-Auth-Token header -- LAN-only, and no passcode path uses it. Prefer
// the USB path for `unlock` if even the LAN is hostile. See docs/bt-hid-network-backend.md.

#include <Arduino.h>
#include <ArduinoJson.h>       // Mode 1 wire protocol: JSON-RPC 2.0 over newline-delimited JSON
#include "hidbridge_mode1.h"   // shared native HID builders + JSON-RPC dispatch (firmware/shared/)
#include <NimBLEDevice.h>
#include <NimBLEHIDDevice.h>
#include <NimBLEServer.h>
#include <Preferences.h>      // NVS: the device nickname (Mode 1) + the network token

#ifdef HIDBRIDGE_NET
#include <ETH.h>
#include <ESPmDNS.h>
#include <NetworkClient.h>
#include <NetworkServer.h>
#include <Preferences.h>
#include <WebServer.h>       // Mode 1 control webpage + /rpc endpoint
#include <ArduinoOTA.h>      // network firmware update (over Ethernet/WiFi)
// The raw JSON-RPC / frame port (3333) is TLS-PSK ONLY -- see the TLS note below. The NVS token is
// the pre-shared key. arduino-esp32 has no TLS *server* class, and the prebuilt esp_tls has PSK
// verification compiled out (CONFIG_ESP_TLS_PSK_VERIFICATION unset), so we drive mbedTLS directly:
// one shared server config with the PSK, and per session a non-blocking handshake + I/O over a raw
// lwip socket driven from netPoll. mbedTLS itself ships with PSK + AES-GCM + SSL_SRV_C enabled.
#include <mbedtls/ssl.h>
#include <mbedtls/net_sockets.h>
#include <mbedtls/ctr_drbg.h>
#include <mbedtls/entropy.h>
#include <lwip/sockets.h>
#include <fcntl.h>
#ifdef HIDBRIDGE_WIFI
#include <WiFi.h>            // WiFi station (an alternative to Ethernet for the net path)
#endif
#endif

static const char* DEVICE_NAME = "HIDBRIDGE-esp32";

// Number of BLE host slots = distinct REMEMBERED identities (each its own address + bond + label).
// Slot 0 = native public address; slots >=1 = derived static-random addresses. This is how MANY
// devices the board remembers, NOT how many connect at once -- the two are decoupled (see the
// connection budget below). Overridable at build time (-DNUM_SLOTS=N). NVS bond/CCCD storage must fit
// NUM_SLOTS (raised in platformio.ini build_flags).
#ifndef NUM_SLOTS
#define NUM_SLOTS 10
#endif

// CONCURRENT connections are capped separately by NimBLE's budget (RAM per live link). The board
// remembers NUM_SLOTS devices but only advertises/holds a few at a time; connecting one advertises
// THAT slot's identity so exactly its bonded device reconnects. So this can be far below NUM_SLOTS --
// it only needs to be >= how many you want connected simultaneously (>=1).
#ifndef CONFIG_BT_NIMBLE_MAX_CONNECTIONS
#define CONFIG_BT_NIMBLE_MAX_CONNECTIONS 3
#endif
static_assert(CONFIG_BT_NIMBLE_MAX_CONNECTIONS >= 1,
              "need at least one concurrent connection");
static_assert(NUM_SLOTS >= 1 && NUM_SLOTS <= 20,
              "NUM_SLOTS out of range (each slot needs a distinct derived address + bond/CCCD storage)");

// --------------------------------------------------------------------------- //
// NETWORK backend (-DHIDBRIDGE_NET) -- Ethernet PHY + TCP line server + mDNS.
//
// The PHY wiring differs PER BOARD, so every pin/mode is a build flag with the Espressif
// ESP32-Ethernet-Kit (IP101GRI) as the default. Other boards override in platformio.ini, e.g.
//   Olimex ESP32-POE : -DHB_ETH_PHY_TYPE=ETH_PHY_LAN8720 -DHB_ETH_PIN_POWER=12
//                      -DHB_ETH_CLK_MODE=ETH_CLOCK_GPIO17_OUT
//   WT32-ETH01       : -DHB_ETH_PHY_TYPE=ETH_PHY_LAN8720 -DHB_ETH_PIN_POWER=16
//                      -DHB_ETH_CLK_MODE=ETH_CLOCK_GPIO0_IN
// A WRONG PHY/clock config is the usual cause of "no link light / never gets an IP".
// --------------------------------------------------------------------------- //
#ifdef HIDBRIDGE_NET
#ifndef HB_ETH_PHY_TYPE
#define HB_ETH_PHY_TYPE   ETH_PHY_IP101          // ESP32-Ethernet-Kit: IP101GRI
#endif
#ifndef HB_ETH_PHY_ADDR
#define HB_ETH_PHY_ADDR   1
#endif
#ifndef HB_ETH_PIN_MDC
#define HB_ETH_PIN_MDC    23
#endif
#ifndef HB_ETH_PIN_MDIO
#define HB_ETH_PIN_MDIO   18
#endif
#ifndef HB_ETH_PIN_POWER
#define HB_ETH_PIN_POWER  5                      // enables the kit's 50MHz RMII oscillator
#endif
#ifndef HB_ETH_CLK_MODE
#define HB_ETH_CLK_MODE   ETH_CLOCK_GPIO0_IN     // 50MHz clock from that oscillator INTO GPIO0
#endif
#ifndef HB_NET_PORT
#define HB_NET_PORT       3333
#endif
#ifndef HB_MDNS_HOST
#define HB_MDNS_HOST      "hidbridge-esp32"      // -> hidbridge-esp32.local
#endif
#ifndef HB_MAX_NET_CLIENTS
// Each live TLS session holds mbedTLS record buffers (~16KB in + ~16KB out with the stock content
// length), so bound concurrency tightly on top of the BLE + WiFi/ETH stacks: 2 is plenty for the
// self-test + a control client.
#define HB_MAX_NET_CLIENTS 2
#endif
#define HB_MIN_TOKEN_LEN  8
#define HB_MAX_AUTH_FAILS 5

// --------------------------------------------------------------------------- //
// TLS-PSK on the JSON-RPC / frame port (3333). The port is TLS-ONLY -- there is no plaintext TCP
// path. The pre-shared key IS the NVS token (the same secret the web `X-Auth-Token` and OTA password
// reuse), so completing the handshake PROVES the peer holds the token WITHOUT it ever crossing the
// wire: the handshake is the authentication (`authed` flips true when it finishes, no `@<token>`
// line needed -- though one is still accepted for the existing Mode 2 client flow). A board with no
// token provisioned opens no TLS port at all (fail-closed). The control webpage stays plain HTTP on
// :80 (browsers cannot speak TLS-PSK) -- that is the one remaining cleartext surface, LAN-only.
// arduino-esp32 ships a TLS *client* but no TLS *server*, so we drive esp_tls's async PSK server
// over a raw non-blocking lwip socket from netPoll (below).
// --------------------------------------------------------------------------- //
// One connected TLS client. A session lives EMPTY (!inUse) -> HANDSHAKING -> READY (`authed`). `buf`
// accumulates a decrypted line the same way the serial reader does. The mbedTLS ssl/net contexts are
// mbedtls_ssl_init()'d on accept and mbedtls_ssl_free()'d on close (netCloseSession).
struct NetSession {
  bool                inUse       = false;
  bool                handshaking = false;
  bool                authed      = false;    // TRUE once the PSK handshake completes -- handshake == auth
  bool                wantClose   = false;
  mbedtls_ssl_context ssl;                    // per-connection TLS state
  mbedtls_net_context nc;                     // holds the accepted socket fd (mbedtls BIO owns it)
  String              buf;
  uint8_t             fails       = 0;
};
static int                      g_listenFd = -1;     // raw TLS listen socket (replaces NetworkServer)
static mbedtls_ssl_config       g_tlsConf;           // shared server config: PSK, TLS1.2, one cipher
static mbedtls_ctr_drbg_context g_drbg;              // RNG for the handshake
static mbedtls_entropy_context  g_entropy;           // seeds g_drbg (ESP32 hardware RNG source)
static String                   g_pskKey;            // the token bytes, held for conf_psk's lifetime
static bool                     g_tlsReady = false;  // config built + listen socket open
static const int g_pskCiphers[] = { MBEDTLS_TLS_PSK_WITH_AES_128_GCM_SHA256, 0 };
static NetSession    netSessions[HB_MAX_NET_CLIENTS];
static Preferences   prefs;
static bool          ethUp = false;
static WebServer     webServer(80);          // Mode 1 control page + /rpc
static IPAddress     hbLocalIP();            // local IP of the active interface (ETH/WiFi); def. below
#else
// No network on this board: NetSession is never defined, but handleLine still takes a NetSession*
// (always nullptr here) so there is ONE command handler shared by both builds -- a serial-only board
// and an Ethernet board run byte-identical command code, which is what keeps the two transports from
// drifting apart. A pointer to an incomplete type is all that needs.
struct NetSession;
#endif  // HIDBRIDGE_NET

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
static char                slotName[NUM_SLOTS][40];   // advertised LocalName = HIDBRIDGE-<nick>[-<client>]
static char                slotClient[NUM_SLOTS][24]; // per-slot CLIENT label (empty = unassigned)
static int                 advSlot = 0;
static int                 tgtSlot = 0;
static bool                parked = false;   // true = intentionally NOT advertising (host parked it)

// The device NICKNAME (Mode 1). Advertised BLE base name = "HIDBRIDGE-<nick>"; per-slot with a client
// = "HIDBRIDGE-<nick>-<client>". Persisted in NVS; defaults to a unique "esp32-<mac-suffix>" so two
// boards coexist out of the box. DEVICE_NAME above stays the fixed FIRMWARE tag (banners/ping/host
// reset-detection) -- only the advertised name follows the nickname.
static char                g_nick[24] = "";
static char                g_advName[40] = "HIDBRIDGE-esp32";
// mDNS hostname, derived from the nickname so MULTIPLE boards on one LAN don't collide on a fixed
// name: "hidbridge-<nick>.local" (e.g. hidbridge-esp32-ee03.local). Built at boot from g_nick.
static char                g_mdnsHost[48] = "hidbridge-esp32";

// Forward declarations: the server callbacks (below) resume/point advertising; defined after setup().
static void selectSlotAdvertise(int n, const char* name);
static void startAdvertisingSlot();
#ifdef HIDBRIDGE_NET
static void netSetup();                        // Ethernet + TCP line server; called from setup()
static bool netTlsSetup();                      // build the PSK config + open the TLS port (def. below)
#endif

static int slotOfHandle(uint16_t h) {
  for (int i = 0; i < NUM_SLOTS; i++) {
    if (slotConn[i] == h) return i;
  }
  return -1;
}

// Build slot n's advertised LocalName from the nickname + its client label: a client-assigned slot is
// "HIDBRIDGE-<nick>-<client>"; slot 0 with no client is the bare "HIDBRIDGE-<nick>"; slot k>=1 with no
// client is "HIDBRIDGE-<nick>-sk".
static void buildSlotName(int n) {
  if (slotClient[n][0]) snprintf(slotName[n], sizeof(slotName[n]), "%s-%s", g_advName, slotClient[n]);
  else if (n == 0)      snprintf(slotName[n], sizeof(slotName[n]), "%s", g_advName);
  else                  snprintf(slotName[n], sizeof(slotName[n]), "%s-s%d", g_advName, n);
}

// Load the nickname from NVS, or derive a unique default from the public MAC's last 2 bytes. Must run
// AFTER NimBLEDevice::init() (the address is available then). Rebuilds g_advName.
static void loadNickname() {
  Preferences pr;
  pr.begin("hidbridge", true);
  String n = pr.getString("nick", "");
  pr.end();
  if (n.length()) {
    snprintf(g_nick, sizeof(g_nick), "%s", n.c_str());
  } else {
    const uint8_t* v = NimBLEDevice::getAddress().getBase()->val;   // little-endian: v[0]=LSB
    snprintf(g_nick, sizeof(g_nick), "esp32-%02x%02x", v[1], v[0]);
  }
  snprintf(g_advName, sizeof(g_advName), "HIDBRIDGE-%s", g_nick);
}

// Per-slot CLIENT labels persist in NVS ("cli<slot>") so a reboot/reflash keeps them in step with the
// BLE bonds (which NimBLE already persists). Without this a reflash wiped the RAM-only labels while
// the bonds survived, leaving a paired iPad reconnecting to a "(free slot)" with no name.
static void persistSlotClient(int slot) {
  char key[8]; snprintf(key, sizeof(key), "cli%d", slot);
  Preferences pr; pr.begin("hidbridge", false);
  if (slotClient[slot][0]) pr.putString(key, slotClient[slot]);
  else                     pr.remove(key);
  pr.end();
}
static void loadSlotClients() {
  Preferences pr; pr.begin("hidbridge", true);
  for (int i = 0; i < NUM_SLOTS; i++) {
    char key[8]; snprintf(key, sizeof(key), "cli%d", i);
    String v = pr.getString(key, "");
    snprintf(slotClient[i], sizeof(slotClient[i]), "%s", v.c_str());
  }
  pr.end();
}

// Build the mDNS hostname "hidbridge-<nick>" from g_nick, sanitised to hostname-safe chars
// ([a-z0-9-], others -> '-'). Unique per board, so two boards on the LAN don't collide.
static void buildMdnsHost() {
  const char* prefix = "hidbridge-";
  size_t i = 0;
  for (; prefix[i] && i < sizeof(g_mdnsHost) - 1; i++) g_mdnsHost[i] = prefix[i];
  for (const char* p = g_nick; *p && i < sizeof(g_mdnsHost) - 1; p++) {
    char c = *p;
    if (c >= 'A' && c <= 'Z') c += 32;
    g_mdnsHost[i++] = ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-') ? c : '-';
  }
  g_mdnsHost[i] = 0;
}

#ifdef HIDBRIDGE_NET
// ---- TLS session I/O + teardown --------------------------------------------------------------- //
// Write `n` bytes through the encrypted channel, looping over partial writes / transient WANT_WRITE.
// The JSON-RPC reply + EVT lines are small, so a bounded spin is fine; a hard error just gives up
// (the session is torn down on the next read).
static bool tlsWrite(mbedtls_ssl_context* ssl, const uint8_t* data, size_t n) {
  size_t off = 0;
  int spins = 0;
  while (off < n) {
    int w = mbedtls_ssl_write(ssl, data + off, n - off);
    if (w > 0) { off += w; spins = 0; continue; }
    if (w == MBEDTLS_ERR_SSL_WANT_WRITE || w == MBEDTLS_ERR_SSL_WANT_READ) {
      if (++spins > 4000) return false;    // stalled peer -- do not busy-wait forever
      continue;
    }
    return false;                          // real error
  }
  return true;
}

// A Print that funnels handleLine's reply bytes into one TLS session. handleLine writes many small
// chunks (println of a reply, digit by digit); buffering here would need a flush hook it does not
// call, so we write straight through -- mbedTLS coalesces into records itself.
struct TlsPrinter : public Print {
  mbedtls_ssl_context* ssl;
  explicit TlsPrinter(mbedtls_ssl_context* s) : ssl(s) {}
  size_t write(uint8_t c) override { return tlsWrite(ssl, &c, 1) ? 1 : 0; }
  size_t write(const uint8_t* b, size_t n) override { return tlsWrite(ssl, b, n) ? n : 0; }
};

// Close a session and release its TLS + socket state. mbedtls_net_free() closes the owned fd.
static void netCloseSession(NetSession& s) {
  if (s.inUse) {
    mbedtls_ssl_free(&s.ssl);
    mbedtls_net_free(&s.nc);
  }
  s.inUse = false;
  s.handshaking = false;
  s.authed = false;
  s.wantClose = false;
  s.buf = "";
  s.fails = 0;
}
#endif  // HIDBRIDGE_NET

// ---- async events: broadcast to serial + every AUTHENTICATED network client ----------------- //
// An EVT line ("EVT connected" / "EVT ready" / ...) is UNSOLICITED: it is not a reply to any one
// channel's command, so it must reach every attached host, not just whoever spoke last. Sessions that
// have not finished the TLS handshake are excluded -- link state is not something an unauthenticated
// peer may observe (once the handshake completes, the PSK proved they hold the token).
static void evt(const char* msg) {
  Serial.println(msg);
#ifdef HIDBRIDGE_NET
  size_t len = strlen(msg);
  for (int i = 0; i < HB_MAX_NET_CLIENTS; i++) {
    NetSession& s = netSessions[i];
    if (s.authed) {
      if (!tlsWrite(&s.ssl, (const uint8_t*)msg, len) || !tlsWrite(&s.ssl, (const uint8_t*)"\r\n", 2))
        s.wantClose = true;                // dead peer -- reap it in the next netPoll
    }
  }
#endif
}

// ---- network auth token (NVS) ---------------------------------------------------------------- //
// Stored in NVS, written ONLY from the serial channel (`W<token>`), never echoed back. `hasToken()`
// is the FAIL-CLOSED gate: with no token provisioned, `@` can never succeed, so an unprovisioned
// board on the LAN cannot be driven over TCP at all.
#ifdef HIDBRIDGE_NET
static bool hasToken() {
  prefs.begin("hidbridge", true);
  bool has = prefs.isKey("token") && prefs.getString("token", "").length() >= HB_MIN_TOKEN_LEN;
  prefs.end();
  return has;
}

static String getToken() {                     // read the NVS token (for the OTA password only)
  prefs.begin("hidbridge", true);
  String t = prefs.getString("token", "");
  prefs.end();
  return t;
}

static bool tokenEquals(const String& given) {
  prefs.begin("hidbridge", true);
  String tok = prefs.getString("token", "");
  prefs.end();
  if (tok.length() < HB_MIN_TOKEN_LEN) return false;      // fail closed: unprovisioned
  // CONSTANT-TIME compare: a length-then-memcmp check leaks the shared secret one byte at a time to
  // anyone who can time our replies. Compare every byte and fold the differences.
  bool lenOk = (given.length() == tok.length());
  size_t n = tok.length();
  uint8_t diff = lenOk ? 0 : 1;
  for (size_t i = 0; i < n; i++) {
    char g = (i < given.length()) ? given[i] : '\0';
    diff |= (uint8_t)(tok[i] ^ g);
  }
  return diff == 0;
}
#endif  // HIDBRIDGE_NET

// ---- server callbacks: track link PER slot + keep advertising the dropped slot ----
class ServerCB : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer* s, NimBLEConnInfo& info) override {
    // The central connected on whatever identity we were advertising -> record it against advSlot.
    if (advSlot >= 0 && advSlot < NUM_SLOTS) slotConn[advSlot] = info.getConnHandle();
    evt("EVT connected");
  }
  void onDisconnect(NimBLEServer* s, NimBLEConnInfo& info, int reason) override {
    int slot = slotOfHandle(info.getConnHandle());
    if (slot >= 0) {
      slotConn[slot] = BLE_HS_CONN_HANDLE_NONE;
      slotSub[slot] = false;              // link gone -> reports no longer enabled on this slot
    }
    evt("EVT disconnected");
    // Resume advertising the slot the HOST currently has selected (advSlot) -- do NOT hijack the
    // single legacy advertiser to the dropped slot. (The old code re-pointed advertising to the
    // dropped slot, so a mini6 flap while we were advertising slot1 for mini5's pair switched the
    // advertiser back to slot0 -- mini5 then never saw HIDBRIDGE-esp32-mini5.) On the classic ESP32 only
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
      evt(nowSub ? "EVT ready" : "EVT unsubscribed");
    }
  }
};
static KbdSubCB kbdSubCB;

static void startAdvertisingSlot() {
  // Build the advertising payload EXPLICITLY + FRESH each slot. (The earlier auto-build path called
  // adv->addServiceUUID()/setName() repeatedly -> service UUIDs ACCUMULATED across slot switches and
  // the name lived in a scan response that did not refresh, so a slot could advertise the WRONG
  // slot's name -- proven by btmon: slot1's address broadcast slot0's name. Building explicit adv
  // data each time fixes it.)
  //
  // PACKET SPLIT: the CHANGING per-slot name stays in the MAIN packet (flags + name); the CONSTANT
  // HID appearance + service UUID go in the SCAN RESPONSE. The name in the main packet is why the
  // stale-name bug is gone (it is rebuilt every slot); putting only the constant bits in the scan
  // response means there is nothing there to go stale, AND it frees room -- flags(3)+name fits a
  // longer LocalName (e.g. HIDBRIDGE-esp32-mini6, 21 bytes) that would overflow the 31-byte AD if
  // appearance+UUID also sat in the main packet. iOS actively scans, so it still gets the scan
  // response (keyboard icon + HID UUID).
  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  adv->stop();
  adv->clearData();
  NimBLEAdvertisementData d;
  d.setFlags(0x06);                           // LE General Discoverable, BR/EDR not supported
  d.setName(slotName[advSlot]);               // the ACTIVE slot's target-scoped name (main packet)
  adv->setAdvertisementData(d);
  NimBLEAdvertisementData sr;                  // scan response: constant across slots
  sr.setAppearance(0x03C1);                   // HID Keyboard (the iPad Bluetooth-list icon)
  sr.addServiceUUID(hid->getHidService()->getUUID());
  adv->setScanResponseData(sr);
  adv->enableScanResponse(true);
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
    slotClient[i][0] = 0;
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

  hid->setManufacturer("hidbridge");
  hid->setPnp(0x02, 0x05AC, 0x820A, 0x0001);   // Apple vendor id, keyboard-ish product
  hid->setHidInfo(0x00, 0x01);                 // country 0, flags: remote-wake
  hid->setReportMap((uint8_t*)REPORT_MAP, sizeof(REPORT_MAP));
  hid->setBatteryLevel(100);

  server->start();                             // starts the HID + battery + devinfo services
  deriveSlotAddresses();                       // needs init() done (public address available)
  loadNickname();                              // NVS nick, or a unique esp32-<mac> default
  loadSlotClients();                           // NVS per-slot client labels (survive reboot/reflash)
  buildMdnsHost();                             // hidbridge-<nick> (unique per board)
  for (int i = 0; i < NUM_SLOTS; i++) buildSlotName(i);   // advertised names from the nickname
  selectSlotAdvertise(0, nullptr);             // advertise slot 0 (native identity) by default
  Serial.println("OK HIDBRIDGE-esp32 ready");

#ifdef HIDBRIDGE_NET
  // AFTER BLE is up: bring Ethernet + the TCP line server up. Ethernet (EMAC) and the BLE radio are
  // independent peripherals, so they coexist; DHCP runs in the background and the board keeps
  // serving serial the whole time (no blocking wait for a lease).
  netSetup();
#endif
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

// Notify ONE report to the target slot's central. Returns true if it actually notified (a central is
// connected on the target slot), false if there was none. Shared by Mode 2 (raw frame relay) and
// Mode 1 (the native report builders below).
static bool notifyTarget(NimBLECharacteristic* chr, const uint8_t* body, size_t len) {
  if (chr == nullptr) return false;
  chr->setValue((uint8_t*)body, len);
  uint16_t h = (tgtSlot >= 0 && tgtSlot < NUM_SLOTS) ? slotConn[tgtSlot] : BLE_HS_CONN_HANDLE_NONE;
  if (h == BLE_HS_CONN_HANDLE_NONE) return false;
  chr->notify(h);                              // drive ONLY the target slot's central
  return true;
}

static NimBLECharacteristic* charForReportId(uint8_t rid) {
  switch (rid) {
    case KBD_REPORT_ID:       return inputKbd;
    case MOUSE_REPORT_ID:     return inputMouse;
    case ABS_MOUSE_REPORT_ID: return inputAbsMouse;
    case DIGITIZER_REPORT_ID: return inputDigitizer;
  }
  return nullptr;
}

static void sendReportToTarget(NimBLECharacteristic* chr, const uint8_t* body, size_t len,
                               Print& out) {
  out.println(notifyTarget(chr, body, len) ? "OK" : "OK noconn");
}

static bool anyConnected() {
  for (int i = 0; i < NUM_SLOTS; i++) {
    if (slotConn[i] != BLE_HS_CONN_HANDLE_NONE) return true;
  }
  return false;
}

static void printSlotsStatus(Print& out) {
  out.print("OK slots=");
  out.print(NUM_SLOTS);
  out.print(" adv=");
  out.print(advSlot);
  out.print(" tgt=");
  out.print(tgtSlot);
  out.print(" multi=1");                       // NimBLE holds distinct identities simultaneously
  for (int i = 0; i < NUM_SLOTS; i++) {
    out.print(" c");
    out.print(i);
    out.print("=");
    out.print(slotConn[i] != BLE_HS_CONN_HANDLE_NONE ? "1" : "0");
  }
  for (int i = 0; i < NUM_SLOTS; i++) {
    out.print(" s");
    out.print(i);
    out.print("=");
    out.print(slotSub[i] ? "1" : "0");
  }
  out.println();
}

// Network status (`N`). Answers on a non-net build too, so a host can always ask "do you have a
// network path?" and get a definite no rather than "ERR unknown".
static void printNetStatus(Print& out) {
#ifdef HIDBRIDGE_NET
  int clients = 0;
  for (int i = 0; i < HB_MAX_NET_CLIENTS; i++) {
    if (netSessions[i].inUse) clients++;    // a live TLS session (handshaking or ready)
  }
  out.print("OK net eth=");
  out.print(ethUp ? "1" : "0");
  out.print(" ip=");
  out.print(ethUp ? hbLocalIP().toString() : String("0.0.0.0"));
  out.print(" port=");
  out.print(HB_NET_PORT);
  out.print(" mdns=");
  out.print(g_mdnsHost); out.print(".local");
  out.print(" token=");
  out.print(hasToken() ? "1" : "0");           // provisioned? (never the token itself)
  out.print(" clients=");
  out.println(clients);
#else
  out.println("OK net eth=0 ip=0.0.0.0 port=0 mdns= token=0 clients=0");
#endif
}

// AUTH GATE. `sess == nullptr` means the line came from USB SERIAL, which is trusted by construction
// (it requires physical access to the board -- the same access that could just reflash it). A TCP
// session may ONLY ping (P/V/N) and authenticate (@) until `@<token>` succeeds; everything that can
// move the pointer, press a key, or reconfigure the BLE identities is refused with "ERR auth".
#ifdef HIDBRIDGE_NET
static bool commandAllowed(NetSession* sess, char cmd, Print& out) {
  if (sess == nullptr) return true;                        // serial: trusted
  if (cmd == '@' || cmd == 'P' || cmd == 'V' || cmd == 'N') return true;
  if (!sess->authed) { out.println("ERR auth"); return false; }
  return true;
}
#endif

// ---- Mode 1 (JSON-RPC): the report builders + dispatch live in ../../shared/hidbridge_mode1.h; the
// ESP32 provides only the BLE-notify sink + its capabilities (net when the Ethernet build is on). ----
static bool esp32Notify(uint8_t rid, const uint8_t* body, size_t len) {
  return notifyTarget(charForReportId(rid), body, len);
}
static void esp32FillHello(JsonObject r) {
  r["device"] = DEVICE_NAME;
  r["nick"] = g_nick;
  r["name"] = slotName[(advSlot >= 0 && advSlot < NUM_SLOTS) ? advSlot : 0];
  JsonArray caps = r["caps"].to<JsonArray>();
  caps.add("keyboard"); caps.add("mouse"); caps.add("abs"); caps.add("digitizer");
  caps.add("slots"); caps.add("nickname"); caps.add("connect"); caps.add("frames");
#ifdef HIDBRIDGE_NET
  caps.add("net");
  r["mdns"] = String(g_mdnsHost) + ".local";     // how to reach this specific board over the network
#endif
}

// ---- Mode 1 firmware-specific methods: nickname + connection management BY CLIENT NAME. These touch
// NVS / the slot+identity system, so they live here (not in the shared header) and are passed to
// handleJsonRpc as the `custom` handler. ----
static int findSlotForClient(const char* client) {
  for (int i = 0; i < NUM_SLOTS; i++) if (!strcasecmp(slotClient[i], client)) return i;
  return -1;
}
static int allocSlotForClient(const char* client) {
  int s = findSlotForClient(client); if (s >= 0) return s;
  for (int i = 0; i < NUM_SLOTS; i++) if (!slotClient[i][0]) return i;
  return -1;
}
// Resolve a target slot from a request: an explicit "slot" index wins (so the webpage can act on an
// UNNAMED but bonded slot), else look up "client" by name. `dflt` is returned when neither is given
// (tgtSlot for disconnect; -1 to force an explicit choice for connect/unpair).
static int slotFromParams(JsonObjectConst params, int dflt) {
  if (params["slot"].is<int>()) {
    int s = params["slot"].as<int>();
    return (s >= 0 && s < NUM_SLOTS) ? s : -1;
  }
  const char* client = params["client"] | (const char*)nullptr;
  if (client && client[0]) return findSlotForClient(client);
  return dflt;
}
static void fillIdentity(JsonObject o, int i) {
  o["slot"] = i;
  o["client"] = slotClient[i];
  char ah[16]; addrHex(slotAddr[i], ah, sizeof(ah)); o["addr"] = ah;
  o["name"] = slotName[i];
  o["conn"] = slotConn[i] != BLE_HS_CONN_HANDLE_NONE;
  o["sub"]  = (bool)slotSub[i];
  o["bonded"] = NimBLEDevice::isBonded(slotAddr[i]);   // a stored LTK exists for this identity
}

static void esp32Custom(const char* method, JsonObjectConst params, JsonObject result,
                        bool* handled, int* errCode, const char** errMsg) {
  if (!strcmp(method, "nickname")) {
    *handled = true;
    const char* name = params["name"] | (const char*)nullptr;
    if (name && name[0]) {
      for (const char* p = name; *p; p++)
        if (*p == ' ' || *p == ':' || *p == '\n' || *p == '\r') {
          *errCode = -32602; *errMsg = "nickname must not contain space/colon/newline"; return;
        }
      if (strlen(name) > 20) { *errCode = -32602; *errMsg = "nickname too long (max 20)"; return; }
      Preferences pr; pr.begin("hidbridge", false); pr.putString("nick", name); pr.end();
      snprintf(g_nick, sizeof(g_nick), "%s", name);
      snprintf(g_advName, sizeof(g_advName), "HIDBRIDGE-%s", g_nick);
      for (int i = 0; i < NUM_SLOTS; i++) buildSlotName(i);
      selectSlotAdvertise((advSlot >= 0 && advSlot < NUM_SLOTS) ? advSlot : 0, nullptr);  // re-advertise
    }
    result["nick"] = g_nick;
    result["name"] = g_advName;
    return;
  }
  if (!strcmp(method, "identities")) {
    *handled = true;
    JsonArray arr = result["slots"].to<JsonArray>();
    for (int i = 0; i < NUM_SLOTS; i++) fillIdentity(arr.add<JsonObject>(), i);
    result["adv"] = advSlot; result["tgt"] = tgtSlot;
    return;
  }
  if (!strcmp(method, "pair")) {
    *handled = true;
    const char* client = params["client"] | (const char*)nullptr;
    if (!client || !client[0]) { *errCode = -32602; *errMsg = "missing 'client'"; return; }
    int slot = allocSlotForClient(client);
    if (slot < 0) { *errCode = -32000; *errMsg = "no free identity slot"; return; }
    snprintf(slotClient[slot], sizeof(slotClient[slot]), "%s", client);
    buildSlotName(slot);
    persistSlotClient(slot);                // survive a reboot/reflash, like the bond does
    selectSlotAdvertise(slot, nullptr);     // bring this identity up + advertise (the iPad (re)connects)
    tgtSlot = slot;                          // route reports to it
    fillIdentity(result, slot);
    return;
  }
  if (!strcmp(method, "connect")) {
    *handled = true;
    int slot = slotFromParams(params, -1);   // by "slot" index or "client" name
    if (slot < 0) { *errCode = -32000; *errMsg = "unknown client/slot (pair it first)"; return; }
    selectSlotAdvertise(slot, nullptr);     // bring this identity up + advertise (the iPad (re)connects)
    tgtSlot = slot;                          // route reports to it
    fillIdentity(result, slot);
    return;
  }
  if (!strcmp(method, "disconnect")) {
    *handled = true;
    int slot = slotFromParams(params, tgtSlot);   // default: the current target
    if (slot < 0) { *errCode = -32000; *errMsg = "unknown client/slot"; return; }
    uint16_t h = slotConn[slot];
    parked = true;                           // onDisconnect must NOT auto-re-advertise
    if (h != BLE_HS_CONN_HANDLE_NONE && server != nullptr) server->disconnect(h);
    NimBLEDevice::stopAdvertising();
    result["slot"] = slot;
    result["disconnected"] = (h != BLE_HS_CONN_HANDLE_NONE);
    return;
  }
  if (!strcmp(method, "unpair")) {
    // Forget a device: drop any live link, DELETE the BLE bond, and clear+persist the empty label.
    // Unlike disconnect (which parks but keeps the bond so the iPad silently reconnects), unpair
    // makes the iPad have to pair again -- so the slot is genuinely free to hand to another device.
    // Takes a "slot" index OR a "client" name, so an UNNAMED-but-bonded slot can still be forgotten.
    // NOTE: this only forgets the bond on THIS side; the iPad keeps its own -- see the BLE note in
    // docs. The peer must ALSO "Forget This Device" or it will keep trying with a stale key.
    *handled = true;
    int slot = slotFromParams(params, -1);
    if (slot < 0) { *errCode = -32602; *errMsg = "missing/invalid 'client' or 'slot'"; return; }
    uint16_t h = slotConn[slot];
    parked = true;                           // onDisconnect must NOT auto-re-advertise
    if (h != BLE_HS_CONN_HANDLE_NONE && server != nullptr) server->disconnect(h);
    NimBLEDevice::deleteBond(slotAddr[slot]);
    slotClient[slot][0] = 0;
    buildSlotName(slot);
    persistSlotClient(slot);                 // removes the NVS label key
    if (advSlot == slot) NimBLEDevice::stopAdvertising();
    result["slot"] = slot;
    result["unpaired"] = true;
    return;
  }
#ifdef HIDBRIDGE_WIFI
  if (!strcmp(method, "wifi")) {          // provision WiFi credentials (station mode)
    *handled = true;
    const char* ssid = params["ssid"] | (const char*)nullptr;
    const char* pass = params["pass"] | "";
    if (!ssid || !ssid[0]) { *errCode = -32602; *errMsg = "missing 'ssid'"; return; }
    Preferences wp; wp.begin("hidbridge", false);
    wp.putString("wifi_ssid", ssid); wp.putString("wifi_pass", pass); wp.end();
    WiFi.begin(ssid, pass);               // (re)connect with the new credentials
    result["ssid"] = ssid; result["connecting"] = true;
    return;
  }
#endif
  // not one of ours -> leave *handled=false (dispatcher returns "method not found")
}


// `sess` is the originating TCP session, or nullptr when the line came from USB serial.
static void handleLine(String line, Print& out, NetSession* sess) {
  line.trim();
  if (line.length() == 0) return;
  if (line[0] == '{') {                                             // Mode 1: JSON-RPC
    bool authed = true;
#ifdef HIDBRIDGE_NET
    authed = (sess == nullptr || sess->authed);                     // TCP session must authenticate
#else
    (void)sess;
#endif
    hbmode1::handleJsonRpc(line, out, authed, esp32Notify, esp32FillHello, esp32Custom);
    return;
  }
  char cmd = line[0];                                               // Mode 2: single-char frames
  String arg = line.substring(1);

#ifdef HIDBRIDGE_NET
  if (!commandAllowed(sess, cmd, out)) return;
#else
  (void)sess;
#endif

  if (cmd == 'K') {
    uint8_t body[KBD_BODY_LEN];
    if (!parseHexBody(arg, body, KBD_BODY_LEN)) { out.println("ERR badkbd"); return; }
    sendReportToTarget(inputKbd, body, KBD_BODY_LEN, out);
  } else if (cmd == 'M') {
    uint8_t body[MOUSE_BODY_LEN];
    if (!parseHexBody(arg, body, MOUSE_BODY_LEN)) { out.println("ERR badmouse"); return; }
    sendReportToTarget(inputMouse, body, MOUSE_BODY_LEN, out);
  } else if (cmd == 'D') {
    uint8_t body[DIGITIZER_BODY_LEN];
    if (!parseHexBody(arg, body, DIGITIZER_BODY_LEN)) { out.println("ERR baddigi"); return; }
    sendReportToTarget(inputDigitizer, body, DIGITIZER_BODY_LEN, out);
  } else if (cmd == 'P') {
    // Report the TARGET slot's state (default slot 0 = legacy single-central behaviour).
    // subscribed= BEFORE conn= so a legacy host's endswith("conn=1") parse still works.
    bool sub = (tgtSlot >= 0 && tgtSlot < NUM_SLOTS) && slotSub[tgtSlot];
    bool con = (tgtSlot >= 0 && tgtSlot < NUM_SLOTS) &&
               slotConn[tgtSlot] != BLE_HS_CONN_HANDLE_NONE;
    out.print("OK HIDBRIDGE-esp32 subscribed=");
    out.print(sub ? "1" : "0");
    out.print(" conn=");
    out.println(con ? "1" : "0");
  } else if (cmd == 'A') {
    // 'A' is overloaded: bare "A" -> (re)start advertising the current slot; "A"+10 hex -> abs-mouse.
    if (arg.length() == 0) {
      NimBLEDevice::startAdvertising();
      out.println("OK adv");
    } else {
      uint8_t body[ABS_MOUSE_BODY_LEN];
      if (!parseHexBody(arg, body, ABS_MOUSE_BODY_LEN)) { out.println("ERR badabs"); return; }
      sendReportToTarget(inputAbsMouse, body, ABS_MOUSE_BODY_LEN, out);
    }
  } else if (cmd == 'I') {
    // I<n>[:name] -- select host slot n as the advertising identity (+ optional per-slot name) +
    // advertise it. Adds a distinct identity so a second/other iPad can bond+connect on ITS address
    // while any already-connected slot stays connected (simultaneous per-bond identities).
    int sep = arg.indexOf(':');
    String nStr = (sep >= 0) ? arg.substring(0, sep) : arg;
    String nm = (sep >= 0) ? arg.substring(sep + 1) : String("");
    int n = nStr.toInt();
    if (nStr.length() == 0 || n < 0 || n >= NUM_SLOTS) { out.println("ERR badslot"); return; }
    selectSlotAdvertise(n, nm.length() ? nm.c_str() : nullptr);
    char ah[16];
    addrHex(slotAddr[n], ah, sizeof(ah));
    out.print("OK slot=");
    out.print(n);
    out.print(" addr=");
    out.print(ah);
    out.print(" name=");
    out.print(slotName[n]);
    out.print(" conn=");
    out.println(slotConn[n] != BLE_HS_CONN_HANDLE_NONE ? "1" : "0");
  } else if (cmd == 'T') {
    // T<n> -- route subsequent K/M/A/D reports to slot n's connection.
    int n = arg.toInt();
    if (arg.length() == 0 || n < 0 || n >= NUM_SLOTS) { out.println("ERR badslot"); return; }
    tgtSlot = n;
    out.print("OK tgt=");
    out.println(n);
  } else if (cmd == 'S') {
    printSlotsStatus(out);
  } else if (cmd == 'N') {
    printNetStatus(out);
  } else if (cmd == 'V') {
    out.print("OK HIDBRIDGE-esp32 ");
    out.println(anyConnected() ? "connected" : "advertising");
  } else if (cmd == 'W') {
    // W<token> -- provision the network auth secret. SERIAL ONLY: accepting this over TCP would let
    // an already-authenticated (or, worse, a future unauthenticated) peer ROTATE the secret and lock
    // the owner out, so the only way in is the physical cable. The token is never echoed back.
#ifdef HIDBRIDGE_NET
    if (sess != nullptr) { out.println("ERR auth"); return; }        // never over the network
    if (arg.length() < HB_MIN_TOKEN_LEN) { out.println("ERR shorttoken"); return; }
    prefs.begin("hidbridge", false);
    prefs.putString("token", arg);
    prefs.end();
    // The TLS PSK is built ONCE at boot (netTlsSetup), so a token set on a board that booted WITHOUT
    // one leaves the TLS port closed until it is opened. Bring it up right now so provisioning "just
    // works" without a reboot (the common fresh-board case). A token CHANGE on an already-listening
    // board can't hot-swap the baked-in key -- that still needs a reboot; say so.
    if (!g_tlsReady && netTlsSetup()) out.println("OK token set (TLS enabled)");
    else if (g_tlsReady)             out.println("OK token set (reboot to apply as the new TLS key)");
    else                             out.println("OK token set (reboot to enable TLS)");
#else
    out.println("ERR nonet");
#endif
  } else if (cmd == '@') {
    // @<token> -- authenticate THIS TCP session (see the AUTH note in the header).
#ifdef HIDBRIDGE_NET
    if (sess == nullptr) { out.println("OK auth"); return; }         // serial is already trusted
    if (!hasToken()) { out.println("ERR notoken"); return; }         // fail closed: unprovisioned
    if (tokenEquals(arg)) {
      sess->authed = true;
      sess->fails = 0;
      out.println("OK auth");
    } else {
      out.println("ERR auth");
      if (++sess->fails >= HB_MAX_AUTH_FAILS) sess->wantClose = true;   // stop online guessing
    }
#else
    out.println("ERR nonet");
#endif
  } else if (cmd == 'X') {
    // Disconnect the TARGET slot's central but KEEP the bond, then PARK (stop advertising) so the
    // released iPad is free to attach to ANOTHER board (deterministic cross-board handoff). The host
    // brings this board back with I<n>. Other slots' connections are untouched.
    uint16_t h = (tgtSlot >= 0 && tgtSlot < NUM_SLOTS) ? slotConn[tgtSlot] : BLE_HS_CONN_HANDLE_NONE;
    parked = true;                            // onDisconnect must NOT auto-re-advertise
    if (h != BLE_HS_CONN_HANDLE_NONE && server != nullptr) {
      server->disconnect(h);
      out.println("OK disc");
    } else {
      out.println("OK noconn");
    }
    NimBLEDevice::stopAdvertising();
  } else if (cmd == 'O') {
    // Park: stop advertising but KEEP existing connections + bonds. Lets the OTHER board's test run
    // without this board grabbing an iPad. Resume with I<n>.
    parked = true;
    NimBLEDevice::stopAdvertising();
    out.println("OK parked");
  } else {
    out.println("ERR unknown");
  }
}

// ---- network service: accept clients + feed their lines through the SAME handleLine ------------ //
#ifdef HIDBRIDGE_NET
// Build the shared PSK server config (token = pre-shared key) and open the non-blocking TLS listen
// socket. No token provisioned -> nothing opened: an unprovisioned board exposes NO network HID
// surface (fail-closed), matching the OTA gate. Returns true iff the listener is up.
static bool netTlsSetup() {
  g_pskKey = getToken();
  if (g_pskKey.length() < HB_MIN_TOKEN_LEN) {
    Serial.println("note: net TLS disabled (provision a token to enable encrypted TCP)");
    return false;
  }
  mbedtls_ssl_config_init(&g_tlsConf);
  mbedtls_ctr_drbg_init(&g_drbg);
  mbedtls_entropy_init(&g_entropy);
  static const char* kPers = "hidbridge-tls";
  int rc = mbedtls_ctr_drbg_seed(&g_drbg, mbedtls_entropy_func, &g_entropy,
                                 (const unsigned char*)kPers, strlen(kPers));
  if (rc == 0)
    rc = mbedtls_ssl_config_defaults(&g_tlsConf, MBEDTLS_SSL_IS_SERVER,
                                     MBEDTLS_SSL_TRANSPORT_STREAM, MBEDTLS_SSL_PRESET_DEFAULT);
  if (rc == 0) {
    // The client (Python) authenticates by proving the SAME token as PSK for identity "hidbridge".
    rc = mbedtls_ssl_conf_psk(&g_tlsConf,
                              (const unsigned char*)g_pskKey.c_str(), g_pskKey.length(),
                              (const unsigned char*)"hidbridge", 9);
  }
  if (rc != 0) {
    Serial.print("ERR tls config "); Serial.println(rc);
    return false;
  }
  mbedtls_ssl_conf_authmode(&g_tlsConf, MBEDTLS_SSL_VERIFY_NONE);  // PSK carries no certificate
  mbedtls_ssl_conf_rng(&g_tlsConf, mbedtls_ctr_drbg_random, &g_drbg);
  mbedtls_ssl_conf_ciphersuites(&g_tlsConf, g_pskCiphers);        // TLS_PSK_WITH_AES_128_GCM_SHA256
  mbedtls_ssl_conf_min_tls_version(&g_tlsConf, MBEDTLS_SSL_VERSION_TLS1_2);
  mbedtls_ssl_conf_max_tls_version(&g_tlsConf, MBEDTLS_SSL_VERSION_TLS1_2);

  g_listenFd = socket(AF_INET, SOCK_STREAM, 0);
  if (g_listenFd < 0) { Serial.println("ERR tls socket"); return false; }
  int one = 1;
  setsockopt(g_listenFd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
  struct sockaddr_in addr;
  memset(&addr, 0, sizeof(addr));
  addr.sin_family = AF_INET;
  addr.sin_addr.s_addr = htonl(INADDR_ANY);
  addr.sin_port = htons(HB_NET_PORT);
  if (bind(g_listenFd, (struct sockaddr*)&addr, sizeof(addr)) != 0 ||
      listen(g_listenFd, HB_MAX_NET_CLIENTS) != 0) {
    Serial.println("ERR tls bind/listen");
    close(g_listenFd);
    g_listenFd = -1;
    return false;
  }
  fcntl(g_listenFd, F_SETFL, fcntl(g_listenFd, F_GETFL, 0) | O_NONBLOCK);
  g_tlsReady = true;
  Serial.println("OK net TLS-PSK enabled");
  return true;
}

static void netPoll() {
  if (!g_tlsReady) return;                     // no token provisioned -> no TLS port (fail-closed)

  // 1) Accept at most one new connection per poll (non-blocking). Wrap it in a TLS-PSK session in
  //    the first free slot; if the board is full or we cannot set it up, drop the raw socket.
  int fd = accept(g_listenFd, nullptr, nullptr);
  if (fd >= 0) {
    int slot = -1;
    for (int i = 0; i < HB_MAX_NET_CLIENTS; i++)
      if (!netSessions[i].inUse) { slot = i; break; }
    if (slot < 0) {
      close(fd);                               // all session slots in use
    } else {
      fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) | O_NONBLOCK);  // async handshake + reads
      int one = 1;
      setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));  // tiny latency-sensitive frames
      NetSession& s = netSessions[slot];
      mbedtls_ssl_init(&s.ssl);
      mbedtls_net_init(&s.nc);
      s.nc.fd = fd;
      s.inUse = true;
      s.authed = false;
      s.wantClose = false;
      s.buf = "";
      s.fails = 0;
      // set_bio drives the socket through mbedtls_net_send/recv, which return WANT_READ/WRITE on a
      // non-blocking fd -- exactly what makes the handshake + reads below non-blocking.
      if (mbedtls_ssl_setup(&s.ssl, &g_tlsConf) != 0) {
        netCloseSession(s);                    // out of memory for the record buffers
      } else {
        mbedtls_ssl_set_bio(&s.ssl, &s.nc, mbedtls_net_send, mbedtls_net_recv, nullptr);
        s.handshaking = true;
      }
    }
  }

  // 2) Drive each live session: finish its handshake, then read + dispatch decrypted lines.
  for (int i = 0; i < HB_MAX_NET_CLIENTS; i++) {
    NetSession& s = netSessions[i];
    if (!s.inUse) continue;
    if (s.wantClose) { netCloseSession(s); continue; }

    if (s.handshaking) {
      int r = mbedtls_ssl_handshake(&s.ssl);
      if (r == 0) {
        s.handshaking = false;
        s.authed = true;                       // the completed PSK handshake IS the authentication
      } else if (r != MBEDTLS_ERR_SSL_WANT_READ && r != MBEDTLS_ERR_SSL_WANT_WRITE) {
        netCloseSession(s);                    // handshake failed (wrong PSK / bad client / reset)
      }
      continue;                                // no app data until the handshake is done
    }

    // Ready: pull whatever plaintext is available this poll and accumulate lines.
    uint8_t chunk[512];
    for (;;) {
      int n = mbedtls_ssl_read(&s.ssl, chunk, sizeof(chunk));
      if (n == MBEDTLS_ERR_SSL_WANT_READ || n == MBEDTLS_ERR_SSL_WANT_WRITE) break;   // drained
      if (n <= 0) { netCloseSession(s); break; }   // 0 / close-notify / error -> drop the session
      TlsPrinter out(&s.ssl);
      for (int k = 0; k < n; k++) {
        char c = (char)chunk[k];
        if (c == '\n') {
          String line = s.buf;
          s.buf = "";
          handleLine(line, out, &s);
        } else if (c != '\r') {
          s.buf += c;
          if (s.buf.length() > 1024) s.buf = "";  // runaway-input guard (>=JSON-RPC line size)
        }
      }
      if (s.wantClose) { netCloseSession(s); break; }
    }
  }
}

// The local IP of whichever interface this build uses (Ethernet or WiFi). `ethUp` = "the network is
// up" for either (kept the name for compatibility).
static IPAddress hbLocalIP() {
#ifdef HIDBRIDGE_WIFI
  return WiFi.localIP();
#else
  return ETH.localIP();
#endif
}

static void onNetEvent(arduino_event_id_t event) {
  switch (event) {
    case ARDUINO_EVENT_ETH_CONNECTED:
      Serial.println("EVT net link");
      break;
    case ARDUINO_EVENT_ETH_GOT_IP:
#ifdef HIDBRIDGE_WIFI
    case ARDUINO_EVENT_WIFI_STA_GOT_IP:
#endif
      ethUp = true;
      Serial.print("EVT net up ip=");
      Serial.println(hbLocalIP().toString());
      break;
    case ARDUINO_EVENT_ETH_DISCONNECTED:
    case ARDUINO_EVENT_ETH_STOP:
#ifdef HIDBRIDGE_WIFI
    case ARDUINO_EVENT_WIFI_STA_DISCONNECTED:
#endif
      ethUp = false;
      Serial.println("EVT net down");
      break;
    default:
      break;
  }
}

// A Print that captures into a String, so handleJsonRpc's reply can be sent as an HTTP body.
struct StringPrinter : public Print {
  String s;
  size_t write(uint8_t c) override { s += (char)c; return 1; }
  size_t write(const uint8_t* b, size_t n) override { s.concat((const char*)b, n); return n; }
};

// Self-contained control page (one PROGMEM string, no external assets). It POSTs the SAME Mode 1
// JSON-RPC to /rpc that the serial/TCP clients speak, with the token as an X-Auth-Token header. Flow:
// (1) unlock -- enter the token, verified by an authed call; (2) Devices -- pair/connect/disconnect
// identities by name; the HID controls stay disabled UNTIL a device is connected; (3) live control --
// pointer-lock the pad so the real mouse + keyboard drive the target. Mouse/scroll deltas are
// CONFLATED: at most one request is in flight and deltas accumulate meanwhile, so total motion is
// preserved at any latency (discrete keys/clicks go through an in-order queue instead).
static const char CONTROL_PAGE[] PROGMEM = R"HTML(<!doctype html><html><head>
<meta name=viewport content="width=device-width,initial-scale=1"><title>hidbridge control</title>
<style>:root{color-scheme:light dark}
body{font-family:system-ui,sans-serif;max-width:44em;margin:1.4em auto;padding:0 1em}
button,input{font-size:1em;padding:.4em .6em;margin:.15em}button{cursor:pointer}
fieldset{margin:.6em 0;border:1px solid #8886;border-radius:8px}fieldset[disabled]{opacity:.45}
.hide{display:none}.row{display:flex;flex-wrap:wrap;align-items:center;gap:.35em}
#out{white-space:pre-wrap;background:#8881;padding:.5em;border-radius:6px;min-height:1.3em;font:.85em ui-monospace,monospace}
table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:.25em .4em;border-bottom:1px solid #8883}
.dot{display:inline-block;width:.7em;height:.7em;border-radius:50%;background:#999}.on{background:#2ecc71}.warn{background:#f1c40f}
#pad{height:9em;margin:.4em 0;border:2px dashed #8888;border-radius:8px;display:flex;flex-direction:column;
align-items:center;justify-content:center;text-align:center;user-select:none;cursor:pointer}
#pad.live{border-color:#2ecc71;background:#2ecc7122}small{opacity:.7}</style>
</head><body><h1>hidbridge control</h1>

<section id=login>
<p>Enter the board token to unlock control.</p>
<div class=row><input id=tok type=password size=26 placeholder="net.token" autocomplete=off>
<button onclick=unlock()>Unlock</button></div>
<p><small>This page is plain HTTP on your LAN -- keystrokes here are NOT encrypted (unlike the TLS
client path). Use it only on a network you trust.</small></p></section>

<section id=app class=hide>
<div class=row style="justify-content:space-between"><b id=board>hidbridge</b>
<button onclick=lock()>Lock</button></div>

<fieldset><legend>Devices</legend><table><tbody id=slots></tbody></table><small id=cap></small>
<div class=row><input id=newclient size=12 placeholder="new name" autocomplete=off>
<button onclick=pairNew()>Pair new</button><button onclick=refresh()>Refresh</button></div>
<small>Pair advertises a free slot so an iPad can bond (accept on the iPad). Connect brings a paired
identity back up and targets it. A filled star marks the current target.</small></fieldset>

<div id=notready><em>Connect a device to enable the controls below.</em></div>

<fieldset id=controls disabled><legend>Keyboard &amp; pointer</legend>
<div class=row><input id=txt size=20 placeholder="text to type"><button onclick=type_()>type</button>
<button onclick="key('enter')">enter</button><button onclick="key('backspace')">bksp</button>
<button onclick="key('tab')">tab</button><button onclick="key('esc')">esc</button></div>
<div class=row>tap x<input id=tx type=number step=.01 min=0 max=1 value=.5 style=width:4.2em>
y<input id=ty type=number step=.01 min=0 max=1 value=.5 style=width:4.2em><button onclick=tap()>tap</button>
<small>0..1 fraction of the screen (.5,.5 = centre)</small></div>
<div id=pad><span id=padlbl>Click to take live control</span>
<small>your real mouse + keyboard drive the device -- Esc releases</small></div>
<small>Live control sends RELATIVE moves (what iOS acts on via AssistiveTouch). Movement is conflated:
one request in flight, deltas summed meanwhile, so the total motion is the same at any latency. Ctrl/
Alt/Cmd + a key sends a shortcut (e.g. Cmd+C); a bare key types. Click = tap (no drag).</small>
</fieldset></section>

<div id=out></div><script>
let TOK=null,id=0,ready=false,live=false;
const $=s=>document.getElementById(s),show=(el,on)=>el.classList.toggle('hide',!on);
async function rpc(m,p){const r=await fetch('/rpc',{method:'POST',headers:{'X-Auth-Token':TOK||''},
body:JSON.stringify({jsonrpc:'2.0',id:++id,method:m,params:p||{}})});
const j=await r.json();if(j&&j.error)throw new Error(j.error.message||'error');return j.result||{}}
const log=x=>$('out').textContent=(typeof x=='string')?x:JSON.stringify(x);

async function unlock(){TOK=$('tok').value;try{
const info=await rpc('hello');await rpc('identities');
localStorage.setItem('hbtok',TOK);            // remembered for next time (cleared only by Lock)
$('board').textContent=info.name||info.device||'hidbridge';
show($('login'),false);show($('app'),true);refresh();startPoll();log('unlocked')}
catch(e){TOK=null;log('unlock failed: '+e.message)}}
function lock(){TOK=null;localStorage.removeItem('hbtok');stopPoll();if(live)release();show($('app'),false);show($('login'),true);$('tok').value='';log('locked')}

let poll=null;const startPoll=()=>poll=setInterval(refresh,3000),stopPoll=()=>{if(poll)clearInterval(poll);poll=null};
async function refresh(){try{renderSlots(await rpc('identities'))}catch(e){log(e.message)}}
function renderSlots(r){const tb=$('slots');tb.innerHTML='';let tgtConn=false,used=0;const all=r.slots||[];
all.forEach(s=>{if(!(s.client||s.bonded||s.conn))return;used++;   // hide empty capacity rows
const tr=document.createElement('tr');const isTgt=s.slot===r.tgt;
const st=s.conn?(s.sub?'ready':'connecting'):(s.bonded?'bonded (idle)':'idle'),cls=s.conn?(s.sub?'on':'warn'):'';
if(isTgt&&s.conn)tgtConn=true;
tr.innerHTML='<td><span class="dot '+cls+'"></span></td><td>'+(s.client?s.client:'<i>(unnamed)</i>')+
(isTgt?' &#9733;':'')+'</td><td><small>'+st+'</small></td><td class=row></td>';
const acts=tr.lastElementChild;   // act BY SLOT so an unnamed-but-bonded slot can still be managed
const c=document.createElement('button');c.textContent='connect';c.onclick=()=>act('connect',{slot:s.slot});
const d=document.createElement('button');d.textContent='disconnect';d.onclick=()=>act('disconnect',{slot:s.slot});
const u=document.createElement('button');u.textContent='unpair';
u.onclick=()=>{if(confirm('Unpair '+(s.client||'this device')+'? Deletes the bond on THIS side only -- you must ALSO Forget it in the device's own Bluetooth settings.'))act('unpair',{slot:s.slot})};
acts.append(c,d,u);tb.append(tr)});
$('cap').textContent='using '+used+' of '+all.length+' remembered slots';
setReady(tgtConn)}
function setReady(on){ready=on;$('controls').disabled=!on;show($('notready'),!on);if(!on&&live)release()}
async function act(m,p){try{await rpc(m,p);refresh()}catch(e){log(e.message)}}
async function pairNew(){const n=$('newclient').value.trim();if(!n)return;
try{await rpc('pair',{client:n});$('newclient').value='';refresh()}catch(e){log(e.message)}}

const type_=()=>rpc('type',{text:$('txt').value}).then(log,e=>log(e.message));
const key=(name,mod)=>rpc('key',{name,mod:mod||0}).then(log,e=>log(e.message));
const tap=()=>rpc('tap',{x:+$('tx').value,y:+$('ty').value}).then(log,e=>log(e.message));

// conflation sender: at most one request in flight; discrete events queued in order, motion summed.
let busy=false,pdx=0,pdy=0,psc=0,q=[];
function pump(){if(busy||!ready)return;let job=null;
if(q.length)job=q.shift();
else if(pdx||pdy){job={m:'move',p:{dx:pdx,dy:pdy}};pdx=pdy=0}
else if(psc){job={m:'scroll',p:{clicks:psc}};psc=0}
if(!job)return;busy=true;rpc(job.m,job.p).then(()=>{sent++;if(live)setPadLbl()}).catch(()=>{}).finally(()=>{busy=false;pump()})}
const NAMED={Enter:'enter',Backspace:'backspace',Tab:'tab',Delete:'delete',' ':'space',
ArrowRight:'right',ArrowLeft:'left',ArrowUp:'up',ArrowDown:'down',Home:'home',End:'end',PageUp:'pageup',PageDown:'pagedown'};
// Esc is NOT forwarded: the browser reserves it to exit pointer lock (that is how you release live
// control). Use the on-screen 'esc' button to actually send Escape to the device.
function onKey(e){if(!live)return;if(e.key==='Escape')return;e.preventDefault();
const mod=(e.ctrlKey?1:0)|(e.shiftKey?2:0)|(e.altKey?4:0)|(e.metaKey?8:0);
const nm=NAMED[e.key];
if(nm)q.push({m:'key',p:{name:nm,mod}});
else if(e.key.length===1){if(mod&13)q.push({m:'key',p:{name:e.key.toLowerCase(),mod}});
else q.push({m:'type',p:{text:e.key}})}else return;pump()}
function onMove(e){if(!live)return;pdx+=e.movementX;pdy+=e.movementY;pump()}
function onBtn(e){if(!live)return;e.preventDefault();q.push({m:'click',p:{button:['left','middle','right'][e.button]||'left'}});pump()}
function onWheel(e){if(!live)return;e.preventDefault();psc+=e.deltaY>0?-1:1;pump()}
const pad=$('pad');let sent=0;
// LIVE is tied STRICTLY to the pointer-lock state, so the box can never claim LIVE while the cursor
// is actually free. Clicking the pad requests the lock; goLive/release fire only from
// pointerlockchange (locked -> goLive, unlocked -> release). Handlers sit on the DOCUMENT so a locked
// mouse is captured no matter where the OS thinks the pointer is.
function setPadLbl(){$('padlbl').textContent=live?('LIVE -- '+sent+' sent -- Esc to release'):'Click to take live control'}
function goLive(){live=true;sent=0;pad.classList.add('live');setPadLbl();
document.addEventListener('mousemove',onMove);document.addEventListener('mousedown',onBtn);
document.addEventListener('wheel',onWheel,{passive:false});document.addEventListener('keydown',onKey)}
function release(){live=false;pad.classList.remove('live');setPadLbl();
if(document.pointerLockElement===pad)document.exitPointerLock();
document.removeEventListener('mousemove',onMove);document.removeEventListener('mousedown',onBtn);
document.removeEventListener('wheel',onWheel);document.removeEventListener('keydown',onKey)}
pad.addEventListener('click',()=>{if(!live&&ready){const p=pad.requestPointerLock();if(p&&p.catch)p.catch(()=>{})}});
document.addEventListener('pointerlockchange',()=>{const lk=document.pointerLockElement===pad;
if(lk&&!live)goLive();else if(!lk&&live)release()});
document.addEventListener('pointerlockerror',()=>log('pointer lock was blocked by the browser -- live mouse control needs it. Keyboard + tap/click buttons still work.'));
{const t=localStorage.getItem('hbtok');if(t){$('tok').value=t;unlock()}}   // auto-unlock if remembered
</script></body></html>)HTML";

static void handleWebRoot() { webServer.send_P(200, "text/html", CONTROL_PAGE); }

static void handleWebRpc() {
  // The X-Auth-Token header must equal the NVS token (fail-closed -- with no token provisioned, only
  // `hello` works). Unlike the TLS-PSK TCP path, THIS webpage is plain HTTP (browsers cannot do
  // TLS-PSK), so its report bytes cross the LAN in the clear -- the one remaining cleartext surface.
  bool authed = webServer.hasHeader("X-Auth-Token") && tokenEquals(webServer.header("X-Auth-Token"));
  StringPrinter sp;
  hbmode1::handleJsonRpc(webServer.arg("plain"), sp, authed, esp32Notify, esp32FillHello, esp32Custom);
  webServer.send(200, "application/json", sp.s);
}

static void netSetup() {
  // Create the NVS namespace up front. Opening it READ-ONLY (hasToken/tokenEquals) before it has
  // ever been written logs an ESP error to the UART -- "nvs_open failed: NOT_FOUND" -- which
  // interleaves with and CORRUPTS a protocol reply line (seen garbling an `N` response). A one-time
  // read-write begin()/end() materialises the namespace so every later read-only open is clean.
  prefs.begin("hidbridge", false);
  prefs.end();

  Network.onEvent(onNetEvent);
#ifdef HIDBRIDGE_WIFI
  // WiFi station: connect with NVS-provisioned credentials (set over serial via the `wifi` method).
  // Async -- the servers below start now and begin serving once STA gets an IP (see onNetEvent).
  {
    Preferences wp; wp.begin("hidbridge", true);
    String ssid = wp.getString("wifi_ssid", ""), pass = wp.getString("wifi_pass", "");
    wp.end();
    WiFi.mode(WIFI_STA);
    WiFi.setHostname(g_mdnsHost);
    if (ssid.length()) {
      WiFi.begin(ssid.c_str(), pass.c_str());
      Serial.print("wifi connecting ssid="); Serial.println(ssid);
    } else {
      Serial.println("note: WiFi has no credentials (set them with the `wifi` JSON method over serial)");
    }
  }
#else
  if (!ETH.begin(HB_ETH_PHY_TYPE, HB_ETH_PHY_ADDR, HB_ETH_PIN_MDC, HB_ETH_PIN_MDIO,
                 HB_ETH_PIN_POWER, HB_ETH_CLK_MODE)) {
    Serial.println("ERR eth init");            // wrong PHY/pins for this board, or no PHY at all
    return;
  }
  ETH.setHostname(g_mdnsHost);
#endif
  netTlsSetup();                               // TLS-PSK listener on HB_NET_PORT (or none, unprovisioned)
  static const char* kHdrs[] = { "X-Auth-Token" };
  webServer.collectHeaders(kHdrs, 1);
  webServer.on("/", handleWebRoot);
  webServer.on("/rpc", HTTP_POST, handleWebRpc);
  webServer.begin();
  if (MDNS.begin(g_mdnsHost)) {
    MDNS.addService("hidbridge", "tcp", HB_NET_PORT);   // the raw JSON-RPC / frame line port
    MDNS.addService("http", "tcp", 80);                 // the control webpage
  }
  // OTA firmware update over the network. Gated behind the token (reused as the OTA password) so it is
  // fail-closed like the rest: an unprovisioned board cannot be reflashed over the LAN. The default
  // partition already has two app slots, so no partition change is needed.
  {
    String tok = getToken();
    if (tok.length() >= HB_MIN_TOKEN_LEN) {
      ArduinoOTA.setHostname(g_mdnsHost);
      ArduinoOTA.setPassword(tok.c_str());
      ArduinoOTA.begin();
      Serial.println("OK ota enabled");
    } else {
      Serial.println("note: OTA disabled (provision a token to enable)");
    }
  }
  Serial.print("OK net listening port=");
  Serial.print(HB_NET_PORT);
  Serial.print(" mdns=");
  Serial.print(g_mdnsHost); Serial.print(".local");
  Serial.print(" token=");
  Serial.println(hasToken() ? "1" : "0");      // 0 = unprovisioned: TCP cannot drive HID yet
}
#endif  // HIDBRIDGE_NET

void loop() {
  static String buf;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') {
      handleLine(buf, Serial, nullptr);        // serial: the trusted channel
      buf = "";
    } else if (c != '\r') {
      buf += c;
      if (buf.length() > 1024) buf = "";  // runaway-input guard (>=JSON-RPC line size)
    }
  }
#ifdef HIDBRIDGE_NET
  netPoll();
  webServer.handleClient();
  ArduinoOTA.handle();
#endif
  delay(2);
}

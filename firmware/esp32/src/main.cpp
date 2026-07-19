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
// reports.py REPORT_DESCRIPTOR (keyboard reportID 1, 8-byte body; mouse reportID 2, 4-byte body).
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
// on; async event notifications are broadcast to serial + every AUTHENTICATED network client. See "NETWORK".
//
// WIRE PROTOCOL (115200 baud serial / TCP, newline-terminated ASCII lines; host -> ESP32). The wire is
// ONE versioned JSON-RPC 2.0 protocol (proto 2 -- EVERYTHING is JSON): the BOARD builds every report
// AND owns device management.
//   report methods : type/key/wake/move/click/press/release/scroll/frames (slot's layout board-side)
//   device methods : hello, identities, connect, target, disconnect, pair, unpair, nickname, layout,
//                    advertise, park, ping, net_status, provision  (builders + dispatch in
//                    ../../shared/hidbridge_rpc.h; the slot/net methods are esp32Custom below)
// `hello` returns proto + a COMPREHENSIVE caps list; the host feature-detects on caps. `provision`
// (the net auth token) is SERIAL-ONLY -- refused over TCP so the secret can only be set over the cable.
// (The old host-builds K/M frame relay AND the single-char device commands P/A/I/T/S/N/V/W/X/O were
// folded into these methods and REMOVED. The `@<token>` TCP auth line is GONE too: a TCP session is
// authenticated by the TLS-PSK handshake alone. Full contract: ../../docs/bt-hid-protocol.md.)
// Replies are one JSON object per line (result / JSON-RPC error). A non-JSON line yields a JSON-RPC
// parse error {id:null}. Asynchronous link/readiness state is an id-less JSON-RPC NOTIFICATION --
//   {"jsonrpc":"2.0","method":"event","params":{"event":"connected|disconnected|ready|unsubscribed","slot":N}}
// ("ready"/"unsubscribed" = iOS wrote/cleared the report CCCDs -- the readiness signal the host awaits
// before the first key). The old plain-text "EVT ..." frames were removed.
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
//   * the handshake is the SOLE app auth: there is no `@<token>` line (removed with proto 2). A TCP
//     session serves JSON-RPC the moment its PSK handshake completes; a wrong PSK never gets that far.
// The one remaining cleartext surface is the OPTIONAL control webpage on :80 (browsers cannot speak
// TLS-PSK); it is HTTP with an X-Auth-Token header -- LAN-only, and no passcode path uses it. Prefer
// the USB path for `unlock` if even the LAN is hostile. See docs/bt-hid-network-backend.md.

#include <Arduino.h>
#include <ArduinoJson.h>       // rpc wire protocol: JSON-RPC 2.0 over newline-delimited JSON
#include "hidbridge_rpc.h"   // shared native HID builders + JSON-RPC dispatch (firmware/shared/)
#include <NimBLEDevice.h>
#include <NimBLEHIDDevice.h>
#include <NimBLEServer.h>
#include <Preferences.h>      // NVS: the device nickname (rpc) + the network token

#ifdef HIDBRIDGE_NET
#include <ETH.h>
#include <ESPmDNS.h>
#include <NetworkClient.h>
#include <NetworkServer.h>
#include <Preferences.h>
#include <WebServer.h>       // serves the rpc control webpage HTML (:80) -- no /rpc drive endpoint
#include <WebSocketsServer.h> // rpc WS DRIVE CHANNEL for the control page (port 81) -- see "WS DRIVE"
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

// Compile-time firmware SemVer, surfaced in `hello` as `fw`. Defaults to "1.0.0-dev"; the `fw/release/`
// release build stamps the tag's version with -DHB_FW_VERSION='"<X.Y.Z>"' (nested quotes: the flag
// must pass a C string literal). This is the firmware RELEASE version and is INDEPENDENT of both the
// wheel version and the wire-compat integer `proto` (hbmode1::PROTO_VERSION). The OTA live test
// (test_bt_hid_ota_live.py) also uses hello.fw to prove an over-the-air flash swapped the running
// image -- it reads hello.fw before and after each espota push. See docs/releasing.md.
#ifndef HB_FW_VERSION
#define HB_FW_VERSION "1.0.0-dev"
#endif

// Number of BLE host slots = distinct REMEMBERED identities (each its own address + bond + label).
// Slot 0 = native public address; slots >=1 = derived static-random addresses. This is how MANY
// devices the board remembers, NOT how many connect at once -- the two are decoupled (see the
// connection budget below). Overridable at build time (-DNUM_SLOTS=N). NVS bond/CCCD storage must fit
// NUM_SLOTS (raised in platformio.ini build_flags).
#ifndef NUM_SLOTS
#define NUM_SLOTS 16
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

// --------------------------------------------------------------------------- //
// TLS-PSK on the JSON-RPC / frame port (3333). The port is TLS-ONLY -- there is no plaintext TCP
// path. The pre-shared key IS the NVS token (the same secret the web `X-Auth-Token` and OTA password
// reuse), so completing the handshake PROVES the peer holds the token WITHOUT it ever crossing the
// wire: the handshake is the SOLE authentication (`authed` flips true when it finishes -- there is no
// `@<token>` app-line anymore, and a wrong PSK fails the handshake outright). A board with no token
// provisioned opens no TLS port at all (fail-closed). The control webpage stays plain HTTP on
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
  int                 drivenSlot  = -1;       // the BLE slot THIS session drives/claims (-1 = none)
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
static WebServer     webServer(80);          // serves the rpc control page HTML (no /rpc drive route)
// WS DRIVE CHANNEL (port 81) -- each browser tab holds ONE WebSocket open while it is open, and drives
// the board over it (the same rpc JSON-RPC the raw port carries). The point is the LIVE connection:
// when a page closes / the tab dies / the laptop sleeps / the network drops, ITS WebSocket closes and
// the board deterministically PARKS the slot that page was driving IFF no other session still drives it
// (ref-counted, see slotClaimCount) -- "no connection without an active customer", with no
// heartbeat/timeout guessing. Each WS client owns its OWN claimed slot (g_wsDrivenSlot[num]), so two
// tabs can drive two different devices at once. It is PLAIN ws:// (browsers cannot speak TLS-PSK), so
// the token crosses the LAN in clear exactly like the :80 HTTP page -- LAN-only, do NOT confuse it with
// the encrypted TLS-PSK :3333 client path.
static WebSocketsServer wsServer(81);
static bool          wsAuthed[WEBSOCKETS_SERVER_CLIENT_MAX];       // per WS client: token accepted?
static int           g_wsDrivenSlot[WEBSOCKETS_SERVER_CLIENT_MAX]; // per WS client: slot it claims (-1=none)
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
};

static const uint8_t KBD_REPORT_ID = 1;
static const uint8_t MOUSE_REPORT_ID = 2;

static NimBLEHIDDevice*    hid = nullptr;
static NimBLECharacteristic* inputKbd = nullptr;
static NimBLECharacteristic* inputMouse = nullptr;
static NimBLEServer*       server = nullptr;

// ---- per-slot identity + connection state (host slots) ----
// slotAddr[0] = native public address (captured at setup); slotAddr[k>=1] = derived static-random.
// slotConn[k] = the conn handle of the central connected on slot k (or NONE). slotSub[k] = iOS has
// enabled the keyboard report CCCD on slot k (readiness). advSlot = the slot currently ADVERTISING;
// g_activeTarget = the slot the IN-FLIGHT dispatch notifies reports to (see below). slotName[k] = that
// slot's advertised name.
static NimBLEAddress       slotAddr[NUM_SLOTS];
static uint16_t            slotConn[NUM_SLOTS];
static volatile bool       slotSub[NUM_SLOTS];
static char                slotName[NUM_SLOTS][40];   // advertised LocalName = HIDBRIDGE-<nick>[-<client>]
static char                slotClient[NUM_SLOTS][24]; // per-slot CLIENT label (empty = unassigned)
static int                 slotLayout[NUM_SLOTS];     // per-slot keyboard LAYOUT (hbmode1::LAYOUT_US/DA)
// The BLE advertising payload is 31 bytes: flags(3) + Complete Local Name(2 header + N) => N <= 26.
// A name OVER this silently fails to advertise -- NimBLE keeps the PREVIOUS advert, so the board would
// broadcast a name that disagrees with what it reports (caught by test_bt_hid_naming_live.py). So
// pair/nickname REJECT over-budget names, and startAdvertisingSlot truncates as a last-ditch safety
// net. The full name still lives in the GAP Device Name characteristic (0x2A00, no 31-byte cap).
static const size_t        HB_MAX_ADV_NAME = 26;
static int                 advSlot = 0;
// ROUTING is PER SESSION now, not one global drive target. g_activeTarget holds the slot the CURRENTLY
// DISPATCHING line routes reports to (notifyTarget reads it): the dispatcher loads it from the calling
// session's claimed slot BEFORE the line and stores any change back AFTER (a connect/pair/T moves the
// session's single claim). -1 = the caller claims no slot yet (JSON-RPC reports answer "conn":false).
static int                 g_activeTarget = -1;
// The USB SERIAL channel is itself one persistent session, so it owns ONE claimed slot exactly like a
// TCP session or a WS client (-1 = none). Serial never "closes", so its claim only moves, it is never
// ref-count-parked on a session-end.
static int                 g_serialDrivenSlot = -1;
static bool                parked = false;   // true = intentionally NOT advertising (host parked it)
// Channel origin of the line CURRENTLY dispatching: true = it arrived over TCP/TLS, false = USB serial.
// Set by handleLine before each dispatch so esp32Custom can enforce the SERIAL-ONLY provisioning gate
// (`provision` is refused over the network, exactly like the old `W<token>` single-char command).
static bool                g_lineFromNet = false;

// The device NICKNAME (rpc). Advertised BLE base name = "HIDBRIDGE-<nick>"; per-slot with a client
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
// Ref-counted per-session targeting helpers (defined after parkTargetSlot). Forward-declared here
// because netCloseSession (a TCP session ending) parks its driven slot only if it was the last claimer.
static bool parkTargetSlot(int slot);
static int  slotClaimCount(int slot);
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

// Per-slot keyboard LAYOUT persists in NVS ("lay<slot>") exactly like the client label, so each
// bonded device remembers its own locale ("da" for a Danish host, "us" default) across reboot/reflash.
static void persistSlotLayout(int slot) {
  char key[8]; snprintf(key, sizeof(key), "lay%d", slot);
  Preferences pr; pr.begin("hidbridge", false);
  pr.putString(key, hbmode1::layoutName(slotLayout[slot]));
  pr.end();
}
static void loadSlotLayouts() {
  Preferences pr; pr.begin("hidbridge", true);
  for (int i = 0; i < NUM_SLOTS; i++) {
    char key[8]; snprintf(key, sizeof(key), "lay%d", i);
    String v = pr.getString(key, "us");
    slotLayout[i] = hbmode1::layoutFromName(v.c_str());
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
// The JSON-RPC reply + event notification lines are small, so a bounded spin is fine; a hard error gives up
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

// Close a session and release its TLS + socket state. mbedtls_net_free() closes the owned fd. SESSION
// END is a claim-release point: drop this session's slot claim and, if it was the LAST session driving
// that slot, park it (drop the central, keep the bond, stop advertising) -- the ref-counted twin of the
// WS page-closed park. drivenSlot is cleared BEFORE the count so this session is excluded from it.
static void netCloseSession(NetSession& s) {
  if (s.inUse) {
    int slot = s.drivenSlot;
    s.drivenSlot = -1;
    mbedtls_ssl_free(&s.ssl);
    mbedtls_net_free(&s.nc);
    if (slot >= 0 && slotClaimCount(slot) == 0) parkTargetSlot(slot);
  }
  s.inUse = false;
  s.handshaking = false;
  s.authed = false;
  s.wantClose = false;
  s.buf = "";
  s.drivenSlot = -1;
}
#endif  // HIDBRIDGE_NET

// ---- async event fan-out queue (SPSC) -------------------------------------------------------- //
// evtEvent() can run in the NimBLE HOST TASK (onConnect/onDisconnect/onSubscribe callbacks), but NEITHER
// the WebSocketsServer NOR a net session's mbedtls_ssl_context is safe to touch cross-task (both are
// driven from the LOOP task). So evtEvent() only ENQUEUES a COPY of the formatted JSON line here;
// loop() drains it to the WS clients AND the TCP sessions (drainEvts) after wsServer.loop(). Single
// producer (evtEvent) / single consumer (loop drain); a full queue drops the event (the page also polls
// every 3s, so a lost push is self-healing).
#ifdef HIDBRIDGE_NET
#define WS_EVT_Q   8
#define WS_EVT_LEN 96                          // one JSON `event` notification line (+ NUL) fits easily
// The queue now carries the FORMATTED JSON line (it holds a dynamic slot number), copied in by the
// producer, so it is a small ring of fixed buffers rather than static string pointers.
static char             g_wsEvtQ[WS_EVT_Q][WS_EVT_LEN];
static volatile uint8_t g_wsEvtHead = 0, g_wsEvtTail = 0;
static void wsEnqueueEvt(const char* msg) {
  uint8_t nt = (uint8_t)((g_wsEvtTail + 1) & (WS_EVT_Q - 1));
  if (nt == g_wsEvtHead) return;               // full -> drop (polling backstops it)
  strncpy(g_wsEvtQ[g_wsEvtTail], msg, WS_EVT_LEN - 1);
  g_wsEvtQ[g_wsEvtTail][WS_EVT_LEN - 1] = '\0';
  g_wsEvtTail = nt;
}
#endif

// ---- async events: broadcast to serial + every AUTHENTICATED network client ----------------- //
// An async event is UNSOLICITED: it is not a reply to any one channel's command, so it must reach
// every attached host, not just whoever spoke last. Sessions that have not finished the TLS handshake
// are excluded -- link state is not something an unauthenticated peer may observe (once the handshake
// completes, the PSK proved they hold the token).
//
// WIRE SHAPE (proto 2): a JSON-RPC NOTIFICATION (no id) --
//   {"jsonrpc":"2.0","method":"event","params":{"event":"ready|connected|disconnected|unsubscribed","slot":N}}
// The old plain-text "EVT ready"/"EVT connected"/... frames are gone; the whole wire is JSON now.
//
// CROSS-TASK SAFETY (this is load-bearing, not tidiness): evtEvent() can run in the NimBLE HOST TASK
// (onConnect/onDisconnect/onSubscribe). A per-session mbedtls_ssl_context is NOT safe to touch from two
// tasks -- if it wrote a net client's TLS record from the host task while netPoll() (loop task)
// read/wrote the SAME context, the record state corrupted and the peer aborted the connection with an
// EOF (Python saw ssl.SSLEOFError). That is exactly what killed a net session whenever a slot
// release/park dropped its BLE central: parkTargetSlot -> server->disconnect() -> onDisconnect (host
// task) -> the disconnected event raced the loop task on that session's ssl. So evtEvent() only
// FORMATS the JSON line and ENQUEUES a COPY of it; loop() drains it to BOTH the WS pages AND the TCP
// sessions (drainEvts), where every ssl touch is single-threaded. Serial is a raw UART and stays direct.
static void evtEvent(const char* event, int slot) {
  char buf[96];                              // matches WS_EVT_LEN (the NET queue's per-slot buffer)
  snprintf(buf, sizeof(buf),
           "{\"jsonrpc\":\"2.0\",\"method\":\"event\",\"params\":{\"event\":\"%s\",\"slot\":%d}}",
           event, slot);
  Serial.println(buf);
#ifdef HIDBRIDGE_NET
  wsEnqueueEvt(buf);                          // drained to WS pages + TCP sessions in loop() (drainEvts)
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
    evtEvent("connected", advSlot);
  }
  void onDisconnect(NimBLEServer* s, NimBLEConnInfo& info, int reason) override {
    int slot = slotOfHandle(info.getConnHandle());
    if (slot >= 0) {
      slotConn[slot] = BLE_HS_CONN_HANDLE_NONE;
      slotSub[slot] = false;              // link gone -> reports no longer enabled on this slot
    }
    evtEvent("disconnected", slot);
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
      evtEvent(nowSub ? "ready" : "unsubscribed", slot);
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
  // Sync the GAP Device Name characteristic (0x2A00) to this slot's name. WITHOUT this the peer sees
  // TWO different names: the per-slot Complete Local Name while SCANNING (from the advert), then the
  // fixed init() name "HIDBRIDGE-esp32" once it CONNECTS and reads 0x2A00 -- so a paired device showed
  // up as bare "HIDBRIDGE-esp32", dropping the -<nick>-<client> suffix. iOS caches the GAP name at
  // pair time, so setting it here (before the peer bonds) is what makes "...-16pro" stick.
  NimBLEDevice::setDeviceName(slotName[advSlot]);   // GAP name (0x2A00): the FULL name, no 31-byte cap
  char advName[HB_MAX_ADV_NAME + 1];
  snprintf(advName, sizeof(advName), "%s", slotName[advSlot]);   // truncate to fit the advert packet
  NimBLEAdvertisementData d;
  d.setFlags(0x06);                           // LE General Discoverable, BR/EDR not supported
  d.setName(advName);                         // the ACTIVE slot's target-scoped name (main packet)
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
    slotLayout[i] = hbmode1::LAYOUT_US;         // default each slot to US until set (loadSlotLayouts)
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

  hid->setManufacturer("hidbridge");
  hid->setPnp(0x02, 0x05AC, 0x820A, 0x0001);   // Apple vendor id, keyboard-ish product
  hid->setHidInfo(0x00, 0x01);                 // country 0, flags: remote-wake
  hid->setReportMap((uint8_t*)REPORT_MAP, sizeof(REPORT_MAP));
  hid->setBatteryLevel(100);

  server->start();                             // starts the HID + battery + devinfo services
  deriveSlotAddresses();                       // needs init() done (public address available)
  loadNickname();                              // NVS nick, or a unique esp32-<mac> default
  loadSlotClients();                           // NVS per-slot client labels (survive reboot/reflash)
  loadSlotLayouts();                           // NVS per-slot keyboard layout (us/da/uk), same lifetime
  buildMdnsHost();                             // hidbridge-<nick> (unique per board)
  for (int i = 0; i < NUM_SLOTS; i++) buildSlotName(i);   // advertised names from the nickname
  // Boot PARKED: advertise NOTHING until a customer explicitly asks for a device (connect/pair). This
  // enforces "no connection without an active customer" -- on power-on the board talks to no one, so
  // no bonded host auto-reconnects behind your back. (Previously it auto-advertised slot 0.)
  parked = true;
  Serial.println("OK HIDBRIDGE-esp32 ready");

#ifdef HIDBRIDGE_NET
  // AFTER BLE is up: bring Ethernet + the TCP line server up. Ethernet (EMAC) and the BLE radio are
  // independent peripherals, so they coexist; DHCP runs in the background and the board keeps
  // serving serial the whole time (no blocking wait for a lease).
  netSetup();
#endif
}

// Format a NimBLEAddress as 12 hex chars, MSB first (val[] is little-endian, so print val[5..0]).
static void addrHex(const NimBLEAddress& a, char* out, size_t cap) {
  const uint8_t* v = a.getBase()->val;
  snprintf(out, cap, "%02x%02x%02x%02x%02x%02x", v[5], v[4], v[3], v[2], v[1], v[0]);
}

// Notify ONE report to the ACTIVE target slot's central. Returns true if it actually notified (a
// central is connected on that slot), false if there was none. Routes via g_activeTarget, which the
// dispatcher has set to the CALLING SESSION's claimed slot before this line -- so two sessions driving
// two slots each hit their own central. The rpc BLE-notify sink (esp32Notify, below) calls it.
static bool notifyTarget(NimBLECharacteristic* chr, const uint8_t* body, size_t len) {
  if (chr == nullptr) return false;
  chr->setValue((uint8_t*)body, len);
  uint16_t h = (g_activeTarget >= 0 && g_activeTarget < NUM_SLOTS) ? slotConn[g_activeTarget]
                                                                   : BLE_HS_CONN_HANDLE_NONE;
  if (h == BLE_HS_CONN_HANDLE_NONE) return false;
  chr->notify(h);                              // drive ONLY the active session's target central
  return true;
}

static NimBLECharacteristic* charForReportId(uint8_t rid) {
  switch (rid) {
    case KBD_REPORT_ID:       return inputKbd;
    case MOUSE_REPORT_ID:     return inputMouse;
  }
  return nullptr;
}

// ---- rpc (JSON-RPC): the report builders + dispatch live in ../../shared/hidbridge_rpc.h; the
// ESP32 provides only the BLE-notify sink + its capabilities (net when the Ethernet build is on). ----
static bool esp32Notify(uint8_t rid, const uint8_t* body, size_t len) {
  return notifyTarget(charForReportId(rid), body, len);
}
static void esp32FillHello(JsonObject r) {
  r["device"] = DEVICE_NAME;
  r["fw"] = HB_FW_VERSION;                        // firmware SemVer (release version + OTA-swap check)
  r["nick"] = g_nick;
  r["name"] = slotName[(advSlot >= 0 && advSlot < NUM_SLOTS) ? advSlot : 0];
  // The REAL radio state (not `parked`): the board advertises only transiently while a connect/pair
  // brings a slot up, so the page shows "advertising as <advName>" only when this is actually true.
  r["advertising"] = NimBLEDevice::getAdvertising()->isAdvertising();
  r["advName"] = slotName[(advSlot >= 0 && advSlot < NUM_SLOTS) ? advSlot : 0];
  // CAPS = the COMPREHENSIVE list of methods/features this firmware supports. The host FEATURE-DETECTS
  // on this (it does not branch on `proto`) and refuses cleanly (IncompatibleFirmware) before calling a
  // method a cap is missing for. VERSIONING RULE: the JSON-RPC surface is ADDITIVE -- an optional new
  // method is announced by ADDING a cap here (proto UNCHANGED); `proto` is bumped ONLY on a BREAKING
  // change (a method/field repurposed or removed). Folding the single-char device commands into
  // JSON-RPC was additive (new methods + new caps), so proto stays PROTO_VERSION; the host's
  // MIN_FIRMWARE_PROTO tracks it.
  JsonArray caps = r["caps"].to<JsonArray>();
  caps.add("keyboard"); caps.add("mouse");
  caps.add("slots"); caps.add("nickname"); caps.add("connect"); caps.add("frames");
  caps.add("layout");                            // per-slot keyboard layout (per-device)
  // consolidated single-char device commands, now JSON-RPC methods (see dispatchLine):
  caps.add("target");                            // T<n>: claim a slot as the send-target (no advertise)
  caps.add("advertise");                         // A: (re)start advertising the current slot
  caps.add("park");                              // O: stop advertising, keep connections + bonds
  caps.add("net_status");                        // N: Ethernet/TCP state (answered on serial-only too)
  caps.add("ping");                              // P: liveness + readiness of the session's target slot
  // The layouts this board accepts -- the control page builds its per-device dropdown from THIS list
  // (not a hardcoded JS array), so adding a layout in hidbridge_rpc.h is enough. Single-sourced.
  JsonArray la = r["layouts"].to<JsonArray>();
  for (int i = 0; i < hbmode1::LAYOUT_COUNT; i++) la.add(hbmode1::LAYOUT_NAMES[i]);
#ifdef HIDBRIDGE_NET
  caps.add("net");
  caps.add("provision");                         // W<token>: set the net auth token (SERIAL ONLY)
  r["mdns"] = String(g_mdnsHost) + ".local";     // how to reach this specific board over the network
#endif
}

// ---- rpc firmware-specific methods: nickname + connection management BY CLIENT NAME. These touch
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
// (the caller's g_activeTarget for disconnect/layout; -1 to force an explicit choice for connect/unpair).
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
  o["layout"] = hbmode1::layoutName(slotLayout[i]);    // this device's keyboard layout (us/da/uk)
}

// Park ONE slot: drop its central (keep the bond), stop advertising, and mark the board parked so
// onDisconnect does NOT auto-re-advertise. This is the shared body of the `disconnect` custom method
// AND the WS-owner-left lifecycle path -- both must have byte-identical park semantics. Returns whether
// a live central was actually dropped.
static bool parkTargetSlot(int slot) {
  if (slot < 0 || slot >= NUM_SLOTS) return false;
  uint16_t h = slotConn[slot];
  bool had = (h != BLE_HS_CONN_HANDLE_NONE);
  parked = true;                             // onDisconnect must NOT auto-re-advertise
  if (had && server != nullptr) server->disconnect(h);
  NimBLEDevice::stopAdvertising();
  return had;
}

// # of live SESSIONS (serial + net + WS) currently claiming `slot` as their drive target. A slot is
// parked only when this hits 0 -- so ONE driver dropping does not yank a slot ANOTHER driver still
// holds (ref-counted claims). Each session records its claim in g_serialDrivenSlot / NetSession.
// drivenSlot / g_wsDrivenSlot[num].
static int slotClaimCount(int slot) {
  if (slot < 0) return 0;
  int n = 0;
  if (g_serialDrivenSlot == slot) n++;
#ifdef HIDBRIDGE_NET
  for (int i = 0; i < HB_MAX_NET_CLIENTS; i++)
    if (netSessions[i].inUse && netSessions[i].drivenSlot == slot) n++;
  for (int i = 0; i < WEBSOCKETS_SERVER_CLIENT_MAX; i++)
    if (g_wsDrivenSlot[i] == slot) n++;
#endif
  return n;
}

// Drop EVERY session's claim on `slot`. Used by unpair: deleting a bond is inherently global, so no
// session may keep driving that identity afterwards. Also clears the in-flight g_activeTarget.
static void clearAllClaims(int slot) {
  if (slot < 0) return;
  if (g_serialDrivenSlot == slot) g_serialDrivenSlot = -1;
  if (g_activeTarget == slot)     g_activeTarget = -1;
#ifdef HIDBRIDGE_NET
  for (int i = 0; i < HB_MAX_NET_CLIENTS; i++)
    if (netSessions[i].drivenSlot == slot) netSessions[i].drivenSlot = -1;
  for (int i = 0; i < WEBSOCKETS_SERVER_CLIENT_MAX; i++)
    if (g_wsDrivenSlot[i] == slot) g_wsDrivenSlot[i] = -1;
#endif
}

// After dispatching ONE session's line: store the (possibly moved) claim back into that session's slot,
// and if the session's PREVIOUS slot (`old`) now has zero claimers, release it. This is the ref-counted
// half of "connect/pair/T claims a slot; disconnect + session-end release it". `store` points at the
// session's own drivenSlot; the write-back happens BEFORE the count so this session is already excluded.
//
// RELEASE has two shapes so a claim MOVE does not kill the fresh advert: if the session moved to a NEW
// target (g_activeTarget >= 0, already being advertised by connect/pair's selectSlotAdvertise), only DROP
// the old slot's central -- keep the new advert up. If it released entirely (g_activeTarget < 0, from
// disconnect/X/session-end), full parkTargetSlot (drop central AND stop advertising).
static void finishRoute(int* store, int old) {
  *store = g_activeTarget;
  if (old < 0 || old == g_activeTarget || slotClaimCount(old) != 0) return;
  if (g_activeTarget >= 0) {
    uint16_t h = slotConn[old];                // moved to a new (advertising) target -> just drop old central
    if (h != BLE_HS_CONN_HANDLE_NONE && server != nullptr) server->disconnect(h);
  } else {
    parkTargetSlot(old);                       // released outright -> drop central + stop advertising
  }
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
      // "HIDBRIDGE-<nick>" (the base advertised name) must fit the 31-byte BLE budget => nick <= 16.
      if (strlen("HIDBRIDGE-") + strlen(name) > HB_MAX_ADV_NAME) {
        *errCode = -32602;
        *errMsg = "nickname too long: HIDBRIDGE-<nick> must be <= 26 chars (nickname max 16)"; return;
      }
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
    result["adv"] = advSlot;                     // index of the advertising-identity slot (a pointer)
    result["advertising"] = NimBLEDevice::getAdvertising()->isAdvertising();  // ACTUAL radio state
    result["advName"] = slotName[(advSlot >= 0 && advSlot < NUM_SLOTS) ? advSlot : 0];  // its adv name
    result["tgt"] = g_activeTarget;              // the CALLING session's own claimed slot (per-session)
    return;
  }
  if (!strcmp(method, "layout")) {
    // Get or set a SLOT's keyboard layout (per device). Addressed like connect/unpair: an explicit
    // "slot" index or a "client" name, else the caller's current target (g_activeTarget). With a "name"
    // ("us"/"da"/"uk") it sets + persists; without, it just reports. Changeable any time (not only at pair).
    *handled = true;
    int slot = slotFromParams(params, g_activeTarget);
    if (slot < 0 || slot >= NUM_SLOTS) { *errCode = -32602; *errMsg = "unknown/invalid 'client' or 'slot'"; return; }
    const char* name = params["name"] | (const char*)nullptr;
    if (name && name[0]) {
      if (!hbmode1::isKnownLayout(name)) {       // validate against the single-sourced list (hello.layouts)
        *errCode = -32602; *errMsg = "unknown layout (see hello.layouts)"; return;
      }
      slotLayout[slot] = hbmode1::layoutFromName(name);
      persistSlotLayout(slot);                // survive a reboot/reflash, like the client label does
    }
    result["slot"] = slot;
    result["client"] = slotClient[slot];
    result["layout"] = hbmode1::layoutName(slotLayout[slot]);
    return;
  }
  if (!strcmp(method, "pair")) {
    *handled = true;
    const char* client = params["client"] | (const char*)nullptr;
    if (!client || !client[0]) { *errCode = -32602; *errMsg = "missing 'client'"; return; }
    // Reject a label that would push the advertised name (g_advName + "-" + client) past the 31-byte
    // BLE budget -- else the board would report a name it cannot actually advertise.
    if (strlen(g_advName) + 1 + strlen(client) > HB_MAX_ADV_NAME) {
      *errCode = -32602;
      *errMsg = "label too long: advertised name would exceed 26 chars; shorten the label or the nickname";
      return;
    }
    int slot = allocSlotForClient(client);
    if (slot < 0) { *errCode = -32000; *errMsg = "no free identity slot"; return; }
    snprintf(slotClient[slot], sizeof(slotClient[slot]), "%s", client);
    buildSlotName(slot);
    persistSlotClient(slot);                // survive a reboot/reflash, like the bond does
    selectSlotAdvertise(slot, nullptr);     // bring this identity up + advertise (the iPad (re)connects)
    g_activeTarget = slot;                   // CLAIM it for this session (finishRoute stores it back)
    fillIdentity(result, slot);
    return;
  }
  if (!strcmp(method, "connect")) {
    *handled = true;
    int slot = slotFromParams(params, -1);   // by "slot" index or "client" name
    if (slot < 0) { *errCode = -32000; *errMsg = "unknown client/slot (pair it first)"; return; }
    selectSlotAdvertise(slot, nullptr);     // bring this identity up + advertise (the iPad (re)connects)
    g_activeTarget = slot;                   // CLAIM it for THIS session; if it had another claim, that
    fillIdentity(result, slot);              // one moves here (finishRoute parks the old iff last claimer)
    return;
  }
  if (!strcmp(method, "disconnect")) {
    // Release the CALLING session's claim (default: its current target g_activeTarget) and park the
    // slot only if NO other session still drives it (finishRoute, ref-counted). An explicit slot the
    // caller does NOT itself claim is parked only when it is already idle (0 claimers) -- never yanked
    // from another live driver.
    *handled = true;
    int slot = slotFromParams(params, g_activeTarget);
    if (slot < 0 || slot >= NUM_SLOTS) { *errCode = -32000; *errMsg = "unknown client/slot"; return; }
    bool had = slotConn[slot] != BLE_HS_CONN_HANDLE_NONE;
    if (slot == g_activeTarget)          g_activeTarget = -1;    // drop my claim; finishRoute parks iff last
    else if (slotClaimCount(slot) == 0)  parkTargetSlot(slot);  // an idle slot nobody drives -> park now
    result["slot"] = slot;
    result["disconnected"] = had;
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
    clearAllClaims(slot);                    // no session may keep driving a now-unbonded identity
    if (advSlot == slot) NimBLEDevice::stopAdvertising();
    result["slot"] = slot;
    result["unpaired"] = true;
    return;
  }
  if (!strcmp(method, "target")) {
    // Claim slot n as THIS session's send-target WITHOUT (re)advertising (the old `T<n>`). Assumes an
    // already-live central; handleLine/finishRoute stores the moved claim + parks the old slot iff last.
    *handled = true;
    int slot = slotFromParams(params, -1);
    if (slot < 0 || slot >= NUM_SLOTS) { *errCode = -32602; *errMsg = "missing/invalid 'slot' or 'client'"; return; }
    g_activeTarget = slot;                    // CLAIM it (finishRoute writes it back into the session)
    result["tgt"] = slot;
    return;
  }
  if (!strcmp(method, "advertise")) {
    // (Re)start advertising the current identity slot (the old `A`).
    *handled = true;
    parked = false;
    NimBLEDevice::startAdvertising();
    result["advertising"] = NimBLEDevice::getAdvertising()->isAdvertising();
    return;
  }
  if (!strcmp(method, "park")) {
    // Stop advertising but KEEP existing connections + bonds (the old `O`). Resume with connect/advertise.
    *handled = true;
    parked = true;
    NimBLEDevice::stopAdvertising();
    result["parked"] = true;
    return;
  }
  if (!strcmp(method, "net_status")) {
    // The board's Ethernet/TCP state (the old `N`). Answered on a serial-only build too (eth=false),
    // so a host can always ask 'do you have a network path?'. `token` = a secret is PROVISIONED (never
    // the secret). Keys match rpc.net_status()'s consumers (cli net-status).
    *handled = true;
#ifdef HIDBRIDGE_NET
    int clients = 0;
    for (int i = 0; i < HB_MAX_NET_CLIENTS; i++) if (netSessions[i].inUse) clients++;
    result["eth"] = ethUp;
    result["ip"] = ethUp ? hbLocalIP().toString() : String("0.0.0.0");
    result["port"] = HB_NET_PORT;
    result["mdns"] = String(g_mdnsHost) + ".local";
    result["token"] = hasToken();
    result["clients"] = clients;
#else
    result["eth"] = false;
    result["ip"] = "0.0.0.0";
    result["port"] = 0;
    result["mdns"] = "";
    result["token"] = false;
    result["clients"] = 0;
#endif
    return;
  }
  if (!strcmp(method, "provision")) {
    // Set the network auth token (the old `W<token>`). SERIAL ONLY -- refused over TCP so the secret
    // can only be set across the physical cable (g_lineFromNet is the channel-origin gate). The token
    // is never echoed back. Same >=8-char minimum + hot-enable-TLS behaviour as the old command.
    *handled = true;
#ifdef HIDBRIDGE_NET
    if (g_lineFromNet) { *errCode = -32001; *errMsg = "provision is serial-only (refused over the network)"; return; }
    const char* token = params["token"] | (const char*)nullptr;
    if (!token || strlen(token) < HB_MIN_TOKEN_LEN) { *errCode = -32602; *errMsg = "token too short (>= 8 chars)"; return; }
    prefs.begin("hidbridge", false);
    prefs.putString("token", token);
    prefs.end();
    // A token set on a board that booted WITHOUT one leaves the TLS port closed until opened; bring it
    // up now so provisioning "just works" without a reboot. A CHANGE on a live board needs a reboot to
    // re-key TLS -- report which happened.
    bool enabled = false;
    if (!g_tlsReady && netTlsSetup()) enabled = true;
    result["token_set"] = true;
    result["tls"] = g_tlsReady;
    result["tls_enabled_now"] = enabled;
#else
    *errCode = -32000; *errMsg = "this firmware has no network build";
#endif
    return;
  }
  if (!strcmp(method, "ping")) {
    // Liveness + readiness of the CALLING session's target slot (the old `P`). subscribed = iOS enabled
    // the reports (readiness); conn = link up. -1 target => both false.
    *handled = true;
    bool sub = (g_activeTarget >= 0 && g_activeTarget < NUM_SLOTS) && slotSub[g_activeTarget];
    bool con = (g_activeTarget >= 0 && g_activeTarget < NUM_SLOTS) &&
               slotConn[g_activeTarget] != BLE_HS_CONN_HANDLE_NONE;
    result["device"] = DEVICE_NAME;
    result["subscribed"] = sub;
    result["conn"] = con;
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


// Dispatch ONE already-routed line (g_activeTarget is set to the caller's claimed slot by handleLine).
// `sess` is the originating TCP session, or nullptr when the line came from USB serial.
static void dispatchLine(String line, Print& out, NetSession* sess) {
  line.trim();
  if (line.length() == 0) return;
  // The MCU wire is ONE uniform JSON-RPC protocol (proto 2): the board builds every report AND owns
  // device management (esp32Custom: connect/target/advertise/park/net_status/provision/ping/...). A
  // non-JSON line deserialize-fails INSIDE handleJsonRpc -> a JSON-RPC parse error {id:null} (there
  // are no plain-text ERR frames anymore). The old `@<token>` app-line was REMOVED: a TCP session is
  // authenticated by the TLS-PSK handshake alone (see netPoll: s.authed flips on handshake complete),
  // so there is no separate auth line to handle here.
  bool authed = true;
#ifdef HIDBRIDGE_NET
  authed = (sess == nullptr || sess->authed);   // TCP session authed by the TLS-PSK handshake; serial trusted
#else
  (void)sess;
#endif
  // The ACTIVE keyboard layout for this dispatch is the caller's TARGET slot's -- so `type`/`key`
  // land the right characters on whatever host g_activeTarget points at (laptop3=da, a US host=us).
  hbmode1::setActiveLayout((g_activeTarget >= 0 && g_activeTarget < NUM_SLOTS) ? slotLayout[g_activeTarget]
                                                                               : hbmode1::LAYOUT_US);
  hbmode1::handleJsonRpc(line, out, authed, esp32Notify, esp32FillHello, esp32Custom);
}

// The single per-session entry for serial + TCP lines. Before dispatch, load THIS session's claimed
// slot into the routing global g_activeTarget (so its JSON-RPC reports hit ITS target); after
// dispatch, store back any claim the line moved (connect/pair/T/disconnect/X) and ref-count-park a
// released slot (finishRoute). Serial (sess == nullptr) uses the one g_serialDrivenSlot; a TCP session
// uses its own drivenSlot. This is the SAME before/after-dispatch pattern the WS path (wsOnText) uses.
static void handleLine(String line, Print& out, NetSession* sess) {
  int* store = &g_serialDrivenSlot;
#ifdef HIDBRIDGE_NET
  if (sess != nullptr) store = &sess->drivenSlot;
#endif
  int old = *store;
  g_activeTarget = old;
#ifdef HIDBRIDGE_NET
  g_lineFromNet = (sess != nullptr);            // channel origin: the serial-only provisioning gate
#else
  g_lineFromNet = false;                        // no network build -> every line is trusted serial
#endif
  dispatchLine(line, out, sess);
  finishRoute(store, old);
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
      s.drivenSlot = -1;                       // a fresh session claims no slot until it connects/T's
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

// Network bring-up state as a JSON `event` notification (proto 2). Serial-only: these fire before any
// TCP/WS client can be connected (link down, or just got its IP), so there is nothing to broadcast to.
static void onNetEvent(arduino_event_id_t event) {
  switch (event) {
    case ARDUINO_EVENT_ETH_CONNECTED:
      Serial.println("{\"jsonrpc\":\"2.0\",\"method\":\"event\",\"params\":{\"event\":\"net\",\"link\":true}}");
      break;
    case ARDUINO_EVENT_ETH_GOT_IP:
#ifdef HIDBRIDGE_WIFI
    case ARDUINO_EVENT_WIFI_STA_GOT_IP:
#endif
      ethUp = true;
      Serial.print("{\"jsonrpc\":\"2.0\",\"method\":\"event\",\"params\":{\"event\":\"net\",\"link\":true,\"ip\":\"");
      Serial.print(hbLocalIP().toString());
      Serial.println("\"}}");
      break;
    case ARDUINO_EVENT_ETH_DISCONNECTED:
    case ARDUINO_EVENT_ETH_STOP:
#ifdef HIDBRIDGE_WIFI
    case ARDUINO_EVENT_WIFI_STA_DISCONNECTED:
#endif
      ethUp = false;
      Serial.println("{\"jsonrpc\":\"2.0\",\"method\":\"event\",\"params\":{\"event\":\"net\",\"link\":false}}");
      break;
    default:
      break;
  }
}

// Self-contained control page (one PROGMEM string, no external assets). It drives the board ONLY over
// its WebSocket (:81), speaking the SAME rpc JSON-RPC the serial/TCP clients speak (there is no HTTP
// /rpc drive endpoint -- driving is WS/TCP/serial only, a persistent session per driver). Flow:
// (1) unlock -- enter the token, verified over the WS; (2) Devices -- pair/connect/disconnect
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

<!-- Board IDENTITY header, shown on BOTH the login and app screens: #board = the board NICKNAME (its
persistent name, not a slot), #advind appears ONLY while the radio is actually advertising an identity. -->
<div class=row style="justify-content:space-between;align-items:baseline"><b id=board>hidbridge</b>
<span id=advind class=hide title="the board is advertising this identity right now"><small>advertising as <b id=advname></b></small></span></div>

<section id=login>
<p>Enter the board token to unlock control.</p>
<div class=row><input id=tok type=password size=26 placeholder="net.token" autocomplete=off
onkeydown="if(event.key=='Enter')unlock()">
<button onclick=unlock()>Unlock</button></div>
<p><small>This page and its ws:// drive link are plaintext on your LAN -- the token and keystrokes
here are NOT encrypted (unlike the TLS client path). Use it only on a network you trust.</small></p></section>

<section id=app class=hide>
<div class=row style="justify-content:flex-end"><span id=wss class=dot title="not connected to the board"></span>
<button onclick=lock()>Lock</button></div>
<div class=row><small>nickname</small><input id=nick size=10 placeholder="nickname" autocomplete=off
onkeydown="if(event.key=='Enter')setNick()">
<button onclick=setNick()>Rename</button>
<small>advertised name is HIDBRIDGE-&lt;nickname&gt;[-&lt;device&gt;] -- keep it short (BLE name budget)</small></div>

<fieldset><legend>Devices</legend><table><tbody id=slots></tbody></table>
<div class=row><input id=newclient size=12 placeholder="new name" autocomplete=off
onkeydown="if(event.key=='Enter')pairNew()">
<button onclick=pairNew()>Pair new</button><button onclick=refresh()>Refresh</button></div>
<small>Pair advertises a free slot so a device can bond (accept the pairing on that device). Connect
brings a paired identity back up and claims it for THIS page (its own driver session). A filled star
marks the device this page is driving.</small></fieldset>

<div id=notready><em>Connect a device to enable the controls below.</em></div>

<fieldset id=controls disabled><legend>Keyboard &amp; pointer</legend>
<div class=row><input id=txt size=20 placeholder="text to type"><button onclick=type_()>type</button>
<button onclick="key('enter')">enter</button><button onclick="key('backspace')">bksp</button>
<button onclick="key('tab')">tab</button><button onclick="key('esc')">esc</button></div>
<div id=pad><span id=padlbl>Click to take live control</span>
<small>your real mouse + keyboard drive the device -- Esc releases</small></div>
<small>Live control sends RELATIVE moves (what the paired device acts on). Movement is conflated:
one request in flight, deltas summed meanwhile, so the total motion is the same at any latency. Ctrl/
Alt/Cmd + a key sends a shortcut (e.g. Cmd+C); a bare key types. Click = tap (no drag).</small>
<small>If diagonal motion comes out only horizontal/vertical, you are on <b>Firefox under Wayland</b> --
its Pointer Lock drops one axis (a Firefox bug, not this tool). Use <b>Chromium</b>, or run Firefox
under X11.</small>
</fieldset></section>

<div id=out></div><script>
let TOK=null,id=0,ready=false,live=false,LAYOUTS=['us'];   // LAYOUTS filled from hello.layouts at unlock
const $=s=>document.getElementById(s),show=(el,on)=>el.classList.toggle('hide',!on);
const log=x=>$('out').textContent=(typeof x=='string')?x:JSON.stringify(x);
// Header = the board NICKNAME (persistent identity), shown on login + app. advind = the transient
// "advertising as <advName>" badge, driven by hello.advertising / identities.advertising (real radio
// state, not `parked`) -- hidden whenever the board isn't actually advertising.
function setBoard(i){$('board').textContent=(i&&i.nick)?('HIDBRIDGE-'+i.nick):((i&&i.device)||'hidbridge')}
function setAdv(i){const on=!!(i&&i.advertising);show($('advind'),on);if(on)$('advname').textContent=i.advName||i.name||''}

// ---- WS DRIVE CHANNEL. While unlocked the page holds ONE WebSocket to :81 open and drives the board
// over it EXCLUSIVELY (every JSON-RPC goes over the WS, replies id-matched -- there is no HTTP fallback).
// This WS IS the driver session: the board routes this page's commands to the slot this page connected,
// and ties that slot's lifecycle to THIS socket -- if it closes (tab shut, laptop asleep, network drop,
// or a ping/pong timeout on a hung page) the board parks that slot (unless another session still drives
// it). If the WS is down, driving stops and the page shows a disconnected state until it reconnects.
let WS=null,wsReady=false,pending={};
function setWsState(on){const d=$('wss');if(!d)return;d.classList.toggle('on',on);
d.title=on?'live link to board':'disconnected -- driving is paused'}
function onEvt(){refresh()}   // any server-pushed `event` notification -> re-read identities (live update)
function wsConnect(){return new Promise(res=>{let settled=false;const fin=v=>{if(!settled){settled=true;res(v)}};
let sock;try{sock=new WebSocket('ws://'+location.hostname+':81/')}catch(e){return fin(false)}
WS=sock;const to=setTimeout(()=>{try{sock.close()}catch(e){}fin(false)},4000);
sock.onopen=()=>{try{sock.send('@'+(TOK||''))}catch(e){}};   // authenticate first, before any rpc
sock.onmessage=ev=>{const raw=ev.data;
let j;try{j=JSON.parse(raw)}catch(e){return}
if(j&&j.method==='event'){onEvt(j);return}   // async JSON-RPC event notification -> refresh live state
if(!wsReady){if(j.result&&j.result.auth===true){wsReady=true;clearTimeout(to);setWsState(true);fin(true)}
else if(j.error){clearTimeout(to);try{sock.close()}catch(e){}fin(false)}return}
const cb=pending[j.id];if(cb){delete pending[j.id];j.error?cb.rej(new Error(j.error.message||'error')):cb.res(j.result||{})}};
sock.onclose=()=>{const was=wsReady;wsReady=false;if(WS===sock)WS=null;clearTimeout(to);setWsState(false);
for(const k in pending){pending[k].rej(new Error('connection closed'));delete pending[k]}
fin(false);if(was)onWsDown()};
sock.onerror=()=>{}})}
function onWsDown(){if(live)release();setReady(false);
log('live link to the board dropped -- the target has been parked. Reconnecting...');
if(TOK)setTimeout(()=>{if(TOK&&!wsReady)wsConnect().then(ok=>{if(ok){log('live link restored');refresh()}})},1500)}

// WS-ONLY: every command goes over the live WebSocket session. No socket -> reject (the page shows a
// disconnected state and stops live control; wsConnect reopens it). There is no HTTP /rpc fallback.
function rpc(m,p){const mid=++id;
if(!(wsReady&&WS))return Promise.reject(new Error('not connected to the board'));
return new Promise((res,rej)=>{pending[mid]={res,rej};
try{WS.send(JSON.stringify({jsonrpc:'2.0',id:mid,method:m,params:p||{}}))}catch(e){delete pending[mid];return rej(e)}
setTimeout(()=>{if(pending[mid]){delete pending[mid];rej(new Error('timeout'))}},8000)})}

async function unlock(){TOK=$('tok').value;try{
if(!await wsConnect())throw new Error('could not open the live link to the board (check the token + network)');
const info=await rpc('hello');await rpc('identities');
LAYOUTS=(info.layouts&&info.layouts.length)?info.layouts:['us'];   // dropdown options come from the board
localStorage.setItem('hbtok',TOK);            // remembered for next time (cleared only by Lock)
setBoard(info);setAdv(info);
$('nick').value=info.nick||'';
show($('login'),false);show($('app'),true);refresh();startPoll();log('unlocked')}
catch(e){TOK=null;log('unlock failed: '+e.message)}}
function lock(){TOK=null;localStorage.removeItem('hbtok');stopPoll();if(live)release();
if(WS){try{WS.close()}catch(e){}}WS=null;wsReady=false;setWsState(false);
show($('app'),false);show($('login'),true);$('tok').value='';log('locked')}
async function setNick(){const n=$('nick').value.trim();if(!n)return;
try{const r=await rpc('nickname',{name:n});setBoard({nick:r.nick||n});$('nick').value=r.nick||n;
log('renamed to '+(r.nick||n)+' (advertised as '+(r.name||'')+')');refresh()}catch(e){log(e.message)}}

let poll=null;const startPoll=()=>poll=setInterval(refresh,3000),stopPoll=()=>{if(poll)clearInterval(poll);poll=null};
async function refresh(){try{renderSlots(await rpc('identities'))}catch(e){log(e.message)}}
function renderSlots(r){setAdv(r);const tb=$('slots');tb.innerHTML='';let tgtConn=false;   // live adv badge from the periodic refresh
(r.slots||[]).forEach(s=>{if(!(s.client||s.bonded||s.conn))return;   // only set-up slots; else empty
const tr=document.createElement('tr');const isTgt=s.slot===r.tgt;
const st=s.conn?(s.sub?'ready':'connecting'):(s.bonded?'bonded (idle)':'idle'),cls=s.conn?(s.sub?'on':'warn'):'';
if(isTgt&&s.conn)tgtConn=true;
tr.innerHTML='<td><span class="dot '+cls+'"></span></td><td>'+(s.client?s.client:'<i>(unnamed)</i>')+
(isTgt?' &#9733;':'')+'</td><td><small>'+st+'</small></td><td class=row></td>';
const acts=tr.lastElementChild;   // act BY SLOT so an unnamed-but-bonded slot can still be managed
const c=document.createElement('button');c.textContent='connect';c.onclick=()=>act('connect',{slot:s.slot});
const d=document.createElement('button');d.textContent='disconnect';d.onclick=()=>act('disconnect',{slot:s.slot});
const u=document.createElement('button');u.textContent='unpair';
u.onclick=()=>{if(confirm('Unpair '+(s.client||'this device')+'? Deletes the bond on THIS side only -- also Forget it in its own Bluetooth settings afterwards.'))act('unpair',{slot:s.slot})};
// per-device keyboard layout -- the char->scancode map for THIS host, persisted per slot. Options come
// from the board (hello.layouts, stored in LAYOUTS) so this dropdown can't drift from what it accepts.
const ly=document.createElement('select');ly.title='keyboard layout of this device';
LAYOUTS.forEach(n=>{const o=document.createElement('option');o.value=n;o.textContent=n;if((s.layout||'us')===n)o.selected=true;ly.append(o)});
ly.onchange=()=>act('layout',{slot:s.slot,name:ly.value});
acts.append(c,d,u,ly);tb.append(tr)});
setReady(tgtConn)}
function setReady(on){ready=on;$('controls').disabled=!on;show($('notready'),!on);if(!on&&live)release()}
async function act(m,p){try{await rpc(m,p);refresh()}catch(e){log(e.message)}}
async function pairNew(){const n=$('newclient').value.trim();if(!n)return;
try{await rpc('pair',{client:n});$('newclient').value='';refresh()}catch(e){log(e.message)}}

const type_=()=>rpc('type',{text:$('txt').value}).then(log,e=>log(e.message));
const key=(name,mod)=>rpc('key',{name,mod:mod||0}).then(log,e=>log(e.message));

// Sender: at most one request in flight (ack-gated). Pointer MOTION is coalesced into one queued
// 'move' and flushed once per animation frame -- this bounds the rate AND combines the axis-separated
// pointer events the browser delivers, so diagonal motion stays diagonal instead of stair-stepping.
// Discrete events keep their order + flush at once; holding a button (press..release) makes the board
// carry it through the moves in between = click-and-drag.
let busy=false,q=[],pendX=0,pendY=0,moveT=null;
// Ack-gated sender (one request in flight). MOTION is accumulated over a fixed WINDOW then sent as ONE
// combined (dx,dy) -- the PiKVM "squash": under Pointer Lock the browser can deliver single-axis deltas,
// so summing a window's worth turns an interleaved X/Y stream into a true diagonal report AND caps the
// BLE report rate. NOTE: Firefox on a WAYLAND session mangles the X axis before JS sees it (no web-side
// fix -- use Chromium); this squash still helps every other browser/input. See the note by the pad.
const MOVE_WINDOW=16;
function flush(){if(busy||!ready||!q.length)return;const job=q.shift();busy=true;
rpc(job.m,job.p).then(()=>{sent++;if(live)setPadLbl()}).catch(()=>{}).finally(()=>{busy=false;flush()})}
function pushMove(dx,dy){const t=q[q.length-1];if(t&&t.m==='move'){t.p.dx+=dx;t.p.dy+=dy}else q.push({m:'move',p:{dx,dy}})}
function flushMotion(){moveT=null;if(pendX||pendY){pushMove(pendX,pendY);pendX=pendY=0;flush()}}
function enq(job){flushMotion();q.push(job);flush()}   // a discrete event flushes pending motion first (keeps drag order)
const BTN=e=>['left','middle','right'][e.button]||'left';
const NAMED={Enter:'enter',Backspace:'backspace',Tab:'tab',Delete:'delete',' ':'space',
ArrowRight:'right',ArrowLeft:'left',ArrowUp:'up',ArrowDown:'down',Home:'home',End:'end',PageUp:'pageup',PageDown:'pagedown'};
// Esc is NOT forwarded: the browser reserves it to exit pointer lock. Use the on-screen 'esc' button.
function onKey(e){if(!live)return;if(e.key==='Escape')return;e.preventDefault();
const mod=(e.ctrlKey?1:0)|(e.shiftKey?2:0)|(e.altKey?4:0)|(e.metaKey?8:0);const nm=NAMED[e.key];
if(nm)enq({m:'key',p:{name:nm,mod}});
else if(e.key.length===1){if(mod&13)enq({m:'key',p:{name:e.key.toLowerCase(),mod}});else enq({m:'type',p:{text:e.key}})}}
function onMove(e){if(!live)return;pendX+=e.movementX;pendY+=e.movementY;
if(!moveT)moveT=setTimeout(flushMotion,MOVE_WINDOW)}
function onDown(e){if(!live)return;e.preventDefault();enq({m:'press',p:{button:BTN(e)}})}
function onUp(e){if(!live)return;e.preventDefault();enq({m:'release',p:{button:BTN(e)}})}
function onWheel(e){if(!live)return;e.preventDefault();const t=q[q.length-1],c=e.deltaY>0?-1:1;
if(t&&t.m==='scroll')t.p.clicks+=c;else enq({m:'scroll',p:{clicks:c}})}
const pad=$('pad');let sent=0;
// LIVE is tied STRICTLY to the pointer-lock state, so the box can never claim LIVE while the cursor
// is actually free. Clicking the pad requests the lock; goLive/release fire only from
// pointerlockchange (locked -> goLive, unlocked -> release). Handlers sit on the DOCUMENT so a locked
// mouse is captured no matter where the OS thinks the pointer is.
function setPadLbl(){$('padlbl').textContent=live?('LIVE -- '+sent+' sent -- Esc to release'):'Click to take live control'}
function goLive(){live=true;sent=0;q=[];pendX=pendY=0;if(moveT){clearTimeout(moveT);moveT=null}pad.classList.add('live');setPadLbl();
document.addEventListener('mousemove',onMove);document.addEventListener('mousedown',onDown);
document.addEventListener('mouseup',onUp);
document.addEventListener('wheel',onWheel,{passive:false});document.addEventListener('keydown',onKey)}
function release(){live=false;if(moveT){clearTimeout(moveT);moveT=null}pendX=pendY=0;pad.classList.remove('live');setPadLbl();
if(document.pointerLockElement===pad)document.exitPointerLock();
document.removeEventListener('mousemove',onMove);document.removeEventListener('mousedown',onDown);
document.removeEventListener('mouseup',onUp);
document.removeEventListener('wheel',onWheel);document.removeEventListener('keydown',onKey)}
// Prefer RAW deltas (unadjustedMovement) -- bypasses OS/browser pointer acceleration for 1:1 control on
// browsers that support the options form (Chromium). Firefox uses the old void-returning API and ignores
// the option (harmless). Fall back to a plain lock if the options form is rejected.
pad.addEventListener('click',()=>{if(!live&&ready){
let p=null;try{p=pad.requestPointerLock({unadjustedMovement:true})}catch(e){}
if(p&&p.catch){p.catch(()=>{try{pad.requestPointerLock()}catch(e){}})}
else if(!p){try{pad.requestPointerLock()}catch(e){}}}});
document.addEventListener('pointerlockchange',()=>{const lk=document.pointerLockElement===pad;
if(lk&&!live)goLive();else if(!lk&&live)release()});
document.addEventListener('pointerlockerror',()=>log('pointer lock was blocked by the browser -- live mouse control needs it. Keyboard + click buttons still work.'));
// Pre-auth: `hello` is answered before the token is accepted, so the board NICKNAME (+ adv badge) can
// show on the LOGIN screen too -- open a throwaway WS, ask hello only, populate, close. No drive access.
function loadHello(){let s;try{s=new WebSocket('ws://'+location.hostname+':81/')}catch(e){return}
s.onopen=()=>{try{s.send(JSON.stringify({jsonrpc:'2.0',id:0,method:'hello'}))}catch(e){}};
s.onmessage=ev=>{let j;try{j=JSON.parse(ev.data)}catch(e){return}
if(j.result&&j.result.nick!==undefined){setBoard(j.result);setAdv(j.result);try{s.close()}catch(e){}}};}
{const t=localStorage.getItem('hbtok');if(t){$('tok').value=t;unlock()}else loadHello()}   // auto-unlock if remembered, else just show the board name
</script></body></html>)HTML";

static void handleWebRoot() { webServer.send_P(200, "text/html", CONTROL_PAGE); }

// NOTE: there is deliberately NO HTTP /rpc DRIVE endpoint. Driving is stateful -- a persistent SESSION
// (WS client / TCP session / serial) owns one BLE claim -- so a stateless per-POST /rpc has no session
// to route to and was removed. The page drives over its WebSocket (:81); curl/scripts use TCP 3333.

// ---- WS DRIVE CHANNEL (port 81) --------------------------------------------------------------- //
// A Print that funnels one JSON-RPC reply into ONE WS text frame. handleJsonRpc writes a reply as many
// small chunks then a trailing newline (println); we accumulate until that newline and flush the whole
// line as a single sendTXT, so the browser gets one clean frame per reply (never fragmented).
struct WsPrinter : public Print {
  uint8_t num; String buf;
  explicit WsPrinter(uint8_t n) : num(n) {}
  size_t write(uint8_t c) override {
    if (c == '\n')      { wsServer.sendTXT(num, buf); buf = ""; }
    else if (c != '\r') { buf += (char)c; }
    return 1;
  }
  size_t write(const uint8_t* b, size_t n) override { for (size_t i = 0; i < n; i++) write(b[i]); return n; }
};

// Verify a token from a WS client and flip its authed flag. Cleartext ws:// (LAN-only) -- same trust
// model as the :80 page. Uses the SAME constant-time tokenEquals + fail-closed hasToken gate as TCP.
static void wsTryAuth(uint8_t num, const String& token, Print& out) {
  if (hasToken() && tokenEquals(token)) {
    wsAuthed[num] = true;
    out.println("{\"jsonrpc\":\"2.0\",\"id\":null,\"result\":{\"auth\":true}}");
  } else {
    out.println("{\"jsonrpc\":\"2.0\",\"id\":null,\"error\":{\"code\":-32001,\"message\":\"auth failed\"}}");
  }
}

// One text frame from a WS client. Fail-closed: until this client authenticates (an `@<token>` line or
// {"method":"auth","params":{"token":...}}) only `hello` is answered. Once authed, the line goes to the
// SAME hbmode1::handleJsonRpc dispatcher the TCP + serial paths use, with a WsPrinter routing the reply
// back over THIS client. Each WS client is its OWN driver SESSION: its claimed slot lives in
// g_wsDrivenSlot[num], loaded into g_activeTarget before dispatch and stored back after (a connect/pair/
// disconnect moves this client's claim; finishRoute ref-count-parks a released slot). This is the same
// before/after-dispatch routing as handleLine -- so two tabs drive two devices independently.
static void wsOnText(uint8_t num, uint8_t* payload, size_t length) {
  String line; line.reserve(length + 1);
  for (size_t i = 0; i < length; i++) { char c = (char)payload[i]; if (c != '\r' && c != '\n') line += c; }
  line.trim();
  if (line.length() == 0) return;
  WsPrinter out(num);
  g_lineFromNet = true;                         // the WS is a network channel: the serial-only
                                                // provisioning gate must refuse `provision` here too

  if (!wsAuthed[num]) {
    if (line[0] == '@') { wsTryAuth(num, line.substring(1), out); return; }
    JsonDocument d;
    if (deserializeJson(d, line) == DeserializationError::Ok) {
      const char* m = d["method"] | "";
      if (!strcmp(m, "auth")) { wsTryAuth(num, String((const char*)(d["params"]["token"] | "")), out); return; }
      if (strcmp(m, "hello") != 0) {
        out.println("{\"jsonrpc\":\"2.0\",\"id\":null,\"error\":{\"code\":-32001,\"message\":\"auth required\"}}");
        return;
      }
    }
    hbmode1::handleJsonRpc(line, out, false, esp32Notify, esp32FillHello, esp32Custom);   // hello only
    return;
  }

  // Authed: route this client's line to ITS claimed slot (before/after-dispatch, like handleLine).
  int old = g_wsDrivenSlot[num];
  g_activeTarget = old;
  // Same per-slot layout gate as the serial/TCP path: pick this client's TARGET slot's keyboard layout
  // before dispatch so `type`/`key` over the WS land the right characters on whatever host g_activeTarget
  // points at (laptop3=da, a US host=us, independently).
  hbmode1::setActiveLayout((g_activeTarget >= 0 && g_activeTarget < NUM_SLOTS) ? slotLayout[g_activeTarget]
                                                                               : hbmode1::LAYOUT_US);
  hbmode1::handleJsonRpc(line, out, true, esp32Notify, esp32FillHello, esp32Custom);
  finishRoute(&g_wsDrivenSlot[num], old);      // store this client's (moved) claim; ref-count-park the old
}

// The WS lifecycle callback. THE core feature is WStype_DISCONNECTED: when a driving page's socket
// closes (tab closed, laptop asleep, network dropped, or the built-in ping/pong timed out a hung page),
// release THAT client's slot claim and PARK it IFF no other session still drives it (ref-counted, via
// slotClaimCount) -- deterministic, no timeout guessing, and it never yanks a slot a second tab holds.
static void wsEvent(uint8_t num, WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      wsAuthed[num] = false;                 // each new socket must (re)authenticate
      g_wsDrivenSlot[num] = -1;              // a fresh client claims no slot until it connects
      break;
    case WStype_DISCONNECTED: {
      wsAuthed[num] = false;
      int slot = g_wsDrivenSlot[num];        // the slot this client was driving (if any)
      g_wsDrivenSlot[num] = -1;              // release BEFORE counting so this client is excluded
      if (slot >= 0 && slotClaimCount(slot) == 0) {
        parkTargetSlot(slot);                // last driver of this slot went away -> park it
        Serial.println("note: ws client left -- parked its (now unclaimed) target");
      }
      break;
    }
    case WStype_TEXT:
      wsOnText(num, payload, length);
      break;
    default:                                 // BIN/PING/PONG/ERROR: nothing to do (heartbeat is auto)
      break;
  }
}

// Drain queued `event` notifications to every AUTHENTICATED client -- WS pages AND TCP sessions (called
// from loop()). Runs in the LOOP task, so touching wsServer AND each net session's ssl here is safe: this
// is the single place a net session's TLS record is written for an unsolicited event, so it never races
// netPoll's read/reply on the same context (the cross-task hazard evtEvent() must not have).
static void drainEvts() {
  while (g_wsEvtHead != g_wsEvtTail) {
    const char* msg = (const char*)g_wsEvtQ[g_wsEvtHead];
    g_wsEvtHead = (uint8_t)((g_wsEvtHead + 1) & (WS_EVT_Q - 1));
    size_t len = strlen(msg);
    for (int i = 0; i < HB_MAX_NET_CLIENTS; i++) {
      NetSession& s = netSessions[i];
      if (s.authed &&
          (!tlsWrite(&s.ssl, (const uint8_t*)msg, len) || !tlsWrite(&s.ssl, (const uint8_t*)"\r\n", 2)))
        s.wantClose = true;                  // dead peer -- reap it in the next netPoll
    }
    for (uint8_t i = 0; i < WEBSOCKETS_SERVER_CLIENT_MAX; i++)
      if (wsAuthed[i]) wsServer.sendTXT(i, msg);
  }
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
  webServer.on("/", handleWebRoot);            // serve the control page HTML only -- no /rpc drive endpoint
  webServer.begin();
  // WS drive channel (:81). enableHeartbeat pings each client every 15s and drops it after 2 missed
  // pongs (6s), so a HARD-crashed page (no clean close) is still detected -> WStype_DISCONNECTED ->
  // its target is parked (ref-counted). That is what makes "page died" deterministic without timeouts.
  for (int i = 0; i < WEBSOCKETS_SERVER_CLIENT_MAX; i++) { wsAuthed[i] = false; g_wsDrivenSlot[i] = -1; }
  wsServer.begin();
  wsServer.onEvent(wsEvent);
  wsServer.enableHeartbeat(15000, 3000, 2);
  if (MDNS.begin(g_mdnsHost)) {
    MDNS.addService("hidbridge", "tcp", HB_NET_PORT);   // the raw JSON-RPC / frame line port
    MDNS.addService("http", "tcp", 80);                 // the control webpage
    MDNS.addService("ws", "tcp", 81);                   // the control-page WS drive channel
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
  wsServer.loop();                           // WS drive channel: reads frames + drives ping/pong
  drainEvts();                               // push queued event notifications to the WS pages + TCP sessions
  ArduinoOTA.handle();
#endif
  delay(2);
}

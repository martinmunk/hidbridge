// hidbridge_mode1.h -- Mode 1 native HID report layer + JSON-RPC dispatch, SHARED by the ESP32
// (NimBLE) and Pico (BTstack) firmwares.
//
// "Tell the board WHAT; it knows HOW." A deliberate C++ DUPLICATE of
// python/src/hidbridge/reports.py (keymap + report builders) -- the board owns the HID descriptor and
// report building, so no host code / no codegen lives in the firmware. The duplication vs reports.py
// is guarded against drift by a host test (python/tests/test_bt_hid_mode1.py) that dry-runs each
// command and asserts the emitted bytes match reports.py. This header removes the OTHER duplication
// (esp32 vs pico) by being shared; only the BLE-notify sink + the hello capabilities differ per board
// and are passed in as callbacks.
//
// Wire protocol: JSON-RPC 2.0, one JSON object per line.
//   request : {"jsonrpc":"2.0","id":<n>,"method":<m>,"params":{...}}
//   response: {"jsonrpc":"2.0","id":<n>,"result":{...}} | {..,"error":{"code":..,"message":..}}
//   a params "dry":true returns result.frames (built report bytes as hex) WITHOUT notifying.
#pragma once
#include <Arduino.h>
#include <strings.h>
#include <ArduinoJson.h>

namespace hbmode1 {

static const int      PROTO_VERSION   = 1;
static const uint8_t  MOD_LSHIFT_BIT  = 0x02;
static const uint8_t  RID_KEYBOARD    = 1;
static const uint8_t  RID_MOUSE       = 2;

// ---- keymap (mirrors reports.py) ----
// Unshifted printable char -> HID usage (page 0x07), or -1. Mirrors reports._BASE_USAGE.
inline int baseUsage(char c) {
  if (c >= 'a' && c <= 'z') return 0x04 + (c - 'a');
  if (c >= '1' && c <= '9') return 0x1E + (c - '1');
  if (c == '0') return 0x27;
  switch (c) {
    case '\n': return 0x28; case '\t': return 0x2B; case ' ': return 0x2C;
    case '-': return 0x2D; case '=': return 0x2E; case '[': return 0x2F; case ']': return 0x30;
    case '\\': return 0x31; case ';': return 0x33; case '\'': return 0x34; case '`': return 0x35;
    case ',': return 0x36; case '.': return 0x37; case '/': return 0x38;
  }
  return -1;
}

// char -> (usage, shift). Mirrors reports.char_to_usage. Returns false if unmapped.
inline bool charToUsage(char c, uint8_t* usage, bool* shift) {
  int u = baseUsage(c);
  if (u >= 0) { *usage = (uint8_t)u; *shift = false; return true; }
  if (c >= 'A' && c <= 'Z') { *usage = 0x04 + (c - 'A'); *shift = true; return true; }
  static const char shf[] = "!@#$%^&*()_+{}|:\"~<>?";     // reports._SHIFTED_SYMBOLS keys
  static const char bas[] = "1234567890-=[]\\;'`,./";      //   -> their unshifted base char
  for (int i = 0; shf[i]; i++)
    if (c == shf[i]) { *usage = (uint8_t)baseUsage(bas[i]); *shift = true; return true; }
  return false;
}

// Named key -> usage, or -1. Mirrors reports.KEY_NAME_TO_USAGE.
inline int keyNameToUsage(const char* name) {
  static const struct { const char* n; uint8_t u; } tbl[] = {
    {"enter",0x28},{"return",0x28},{"esc",0x29},{"escape",0x29},{"backspace",0x2A},{"delete",0x2A},
    {"tab",0x2B},{"space",0x2C},{"right",0x4F},{"left",0x50},{"down",0x51},{"up",0x52},
    {"home",0x4A},{"end",0x4D},{"pageup",0x4B},{"pagedown",0x4E},
    {"f1",0x3A},{"f2",0x3B},{"f3",0x3C},{"f4",0x3D},{"f5",0x3E},{"f6",0x3F},
    {"f7",0x40},{"f8",0x41},{"f9",0x42},{"f10",0x43},{"f11",0x44},{"f12",0x45},
  };
  for (auto& e : tbl) if (strcasecmp(name, e.n) == 0) return e.u;
  return -1;
}

// Mouse button name (or 1..5) -> bit. Mirrors reports._BUTTON_NAME.
inline int buttonNameToBit(const char* name) {
  if (!strcasecmp(name,"left")   || !strcmp(name,"1")) return 0x01;
  if (!strcasecmp(name,"right")  || !strcmp(name,"2")) return 0x02;
  if (!strcasecmp(name,"middle") || !strcmp(name,"3")) return 0x04;
  if (!strcasecmp(name,"back")   || !strcmp(name,"4")) return 0x08;
  if (!strcasecmp(name,"forward")|| !strcmp(name,"5")) return 0x10;
  return -1;
}

inline int clampi(int v, int lo, int hi) { return v < lo ? lo : (v > hi ? hi : v); }

// ---- report sink: notify a report (firmware-specific), or (dry) collect rid+body hex ----
struct ReportSink {
  bool dry = false;
  String frames;
  int built = 0, notified = 0;
  bool (*notify)(uint8_t rid, const uint8_t* body, size_t len) = nullptr;   // firmware-provided
};

inline void emitReport(ReportSink& s, uint8_t rid, const uint8_t* body, size_t len) {
  s.built++;
  if (s.dry) {
    if (s.built > 1) s.frames += ',';
    char hx[3];
    snprintf(hx, sizeof(hx), "%02x", rid); s.frames += hx;
    for (size_t i = 0; i < len; i++) { snprintf(hx, sizeof(hx), "%02x", body[i]); s.frames += hx; }
  } else if (s.notify && s.notify(rid, body, len)) {
    s.notified++;
  }
}

// ---- builders: each mirrors the same-named reports.py function, byte-for-byte ----
inline void emitKeyUsage(ReportSink& s, uint8_t mod, uint8_t usage) {
  uint8_t down[8] = { mod, 0, usage, 0, 0, 0, 0, 0 };  emitReport(s, RID_KEYBOARD, down, 8);
  uint8_t up[8]   = { 0, 0, 0, 0, 0, 0, 0, 0 };        emitReport(s, RID_KEYBOARD, up, 8);
}
inline bool emitType(ReportSink& s, const char* text) {
  for (const char* p = text; *p; ++p) {
    uint8_t usage; bool shift;
    if (!charToUsage(*p, &usage, &shift)) return false;
    emitKeyUsage(s, shift ? MOD_LSHIFT_BIT : 0, usage);
  }
  return true;
}
inline void emitMouse(ReportSink& s, uint8_t buttons, int dx, int dy, int wheel) {
  uint8_t b[4] = { buttons, (uint8_t)(clampi(dx,-128,127) & 0xFF),
                   (uint8_t)(clampi(dy,-128,127) & 0xFF), (uint8_t)(clampi(wheel,-128,127) & 0xFF) };
  emitReport(s, RID_MOUSE, b, 4);
}
// Buttons currently HELD down (a bitmask), for click-and-drag: press() sets a bit, release() clears
// it, and every move/scroll report carries the held buttons so the drag survives the motion. 0 in the
// idle state, so a normal move still reports buttons=0 (byte-identical to reports.py -- the drift
// guard stays green).
static uint8_t g_heldButtons = 0;
inline void emitMove(ReportSink& s, int dx, int dy) {
  int xs[80], ys[80], nx = 0, ny = 0, r;
  for (r = dx; r != 0 && nx < 80; ) { int st = clampi(r,-127,127); xs[nx++] = st; r -= st; }
  for (r = dy; r != 0 && ny < 80; ) { int st = clampi(r,-127,127); ys[ny++] = st; r -= st; }
  int n = nx > ny ? nx : ny; if (n < 1) n = 1;
  for (int i = 0; i < n; i++) emitMouse(s, g_heldButtons, i < nx ? xs[i] : 0, i < ny ? ys[i] : 0, 0);
}
inline void emitScroll(ReportSink& s, int clicks) {
  bool any = false;
  for (int r = clicks; r != 0; ) { int st = clampi(r,-127,127); emitMouse(s,g_heldButtons,0,0,st); r -= st; any = true; }
  if (!any) emitMouse(s, g_heldButtons, 0, 0, 0);
}
inline void emitClick(ReportSink& s, uint8_t button) {
  emitMouse(s, (uint8_t)(g_heldButtons | button), 0, 0, 0);
  emitMouse(s, g_heldButtons, 0, 0, 0);
}
// A dry run must NOT mutate the held-button state (it only shows the bytes that WOULD be sent).
inline void emitPress(ReportSink& s, uint8_t button) {
  uint8_t nb = g_heldButtons | button; if (!s.dry) g_heldButtons = nb; emitMouse(s, nb, 0, 0, 0);
}
inline void emitRelease(ReportSink& s, uint8_t button) {
  uint8_t nb = (uint8_t)(g_heldButtons & ~button); if (!s.dry) g_heldButtons = nb; emitMouse(s, nb, 0, 0, 0);
}

inline int hexNibble(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

// Parse "<ridHex><bodyHex>" (a dry-run frame) into rid + body. Returns body length, or -1 on bad hex.
inline int parseFrame(const char* hx, uint8_t* rid, uint8_t* body, size_t cap) {
  size_t n = strlen(hx);
  if (n < 2 || (n % 2)) return -1;
  int hi = hexNibble(hx[0]), lo = hexNibble(hx[1]);
  if (hi < 0 || lo < 0) return -1;
  *rid = (uint8_t)((hi << 4) | lo);
  size_t nb = 0;
  for (size_t i = 2; i + 1 < n && nb < cap; i += 2) {
    int a = hexNibble(hx[i]), b = hexNibble(hx[i + 1]);
    if (a < 0 || b < 0) return -1;
    body[nb++] = (uint8_t)((a << 4) | b);
  }
  return (int)nb;
}

inline void framesToJson(ReportSink& s, JsonArray arr) {
  int start = 0, len = s.frames.length();
  while (start < len) {
    int comma = s.frames.indexOf(',', start);
    if (comma < 0) comma = len;
    arr.add(s.frames.substring(start, comma));
    start = comma + 1;
  }
}

// A firmware-specific method handler (nickname, pair, connect, ...). Set *handled=true if `method`
// is yours; then either fill `result` (success) or set *errCode (a JSON-RPC error code) + *errMsg.
typedef void (*CustomFn)(const char* method, JsonObjectConst params, JsonObject result,
                         bool* handled, int* errCode, const char** errMsg);

// The JSON-RPC dispatcher. `authed` gates every method except hello (serial is trusted => true; an
// unauthenticated TCP session => false). `notify` is the firmware's report sink. `fillHello` lets the
// firmware add its device name + capabilities to the hello result. `custom` (optional) handles
// firmware-specific methods (slots/nickname/connection) not built in here.
inline void handleJsonRpc(const String& line, Print& out, bool authed,
                          bool (*notify)(uint8_t, const uint8_t*, size_t),
                          void (*fillHello)(JsonObject),
                          CustomFn custom = nullptr) {
  JsonDocument doc;
  DeserializationError perr = deserializeJson(doc, line);
  JsonDocument resp;
  resp["jsonrpc"] = "2.0";
  if (perr) {
    resp["id"] = nullptr;
    resp["error"]["code"] = -32700; resp["error"]["message"] = "parse error";
    serializeJson(resp, out); out.println(); return;
  }
  const char* method = doc["method"] | "";
  if (doc["id"].isNull()) resp["id"] = nullptr; else resp["id"] = doc["id"];
  JsonObjectConst params = doc["params"].as<JsonObjectConst>();
  bool dry = params["dry"] | false;

  if (!strcmp(method, "hello")) {                        // capabilities -- always allowed
    JsonObject r = resp["result"].to<JsonObject>();
    r["proto"] = PROTO_VERSION;
    JsonArray modes = r["modes"].to<JsonArray>(); modes.add("frames"); modes.add("rpc");
    if (fillHello) fillHello(r);
    serializeJson(resp, out); out.println(); return;
  }
  if (!authed) {                                         // every input method needs auth over TCP
    resp["error"]["code"] = -32001; resp["error"]["message"] = "auth required";
    serializeJson(resp, out); out.println(); return;
  }

  ReportSink s; s.dry = dry; s.notify = notify;
  bool known = true; const char* argErr = nullptr;

  if (!strcmp(method, "type")) {
    const char* text = params["text"] | (const char*)nullptr;
    if (!text) argErr = "missing 'text'";
    else if (!emitType(s, text)) argErr = "unmappable character in 'text'";
  } else if (!strcmp(method, "key")) {
    const char* name = params["name"] | (const char*)nullptr;
    int usage = name ? keyNameToUsage(name) : -1;
    // Fall back to a single printable char ("c", "/", ...) so modifier shortcuts work: name:"c"
    // mod:0x08 -> LGUI+c (Cmd+C). The explicit `mod` is authoritative; the char's own shift is
    // ignored (the caller sets modifiers). This is what the browser passthrough uses for Cmd/Ctrl+key.
    if (usage < 0 && name && name[0] && name[1] == '\0') {
      uint8_t u; bool sh;
      if (charToUsage(name[0], &u, &sh)) usage = u;
    }
    if (usage < 0) argErr = "unknown 'name'";
    else emitKeyUsage(s, (uint8_t)(params["mod"] | 0), (uint8_t)usage);
  } else if (!strcmp(method, "wake")) {
    emitKeyUsage(s, 0, 0x2C);                             // space -- a benign wake keystroke
  } else if (!strcmp(method, "move")) {
    emitMove(s, params["dx"] | 0, params["dy"] | 0);
  } else if (!strcmp(method, "click")) {
    int bit = buttonNameToBit(params["button"] | "left");
    if (bit < 0) argErr = "unknown 'button'"; else emitClick(s, (uint8_t)bit);
  } else if (!strcmp(method, "press")) {       // hold a button DOWN (for click-and-drag)
    int bit = buttonNameToBit(params["button"] | "left");
    if (bit < 0) argErr = "unknown 'button'"; else emitPress(s, (uint8_t)bit);
  } else if (!strcmp(method, "release")) {     // release a held button
    int bit = buttonNameToBit(params["button"] | "left");
    if (bit < 0) argErr = "unknown 'button'"; else emitRelease(s, (uint8_t)bit);
  } else if (!strcmp(method, "scroll")) {
    emitScroll(s, params["clicks"] | 0);
  } else if (!strcmp(method, "frames")) {
    // Mode 2 semantics inside JSON-RPC: the host sends PRE-BUILT report frames ("<ridHex><bodyHex>",
    // the same shape a dry run returns) and the board just relays them. Unifies "one protocol".
    JsonArrayConst arr = params["frames"].as<JsonArrayConst>();
    if (arr.isNull()) argErr = "missing 'frames'";
    else for (JsonVariantConst f : arr) {
      const char* hx = f.as<const char*>();
      uint8_t rid, body[8];
      int n = hx ? parseFrame(hx, &rid, body, sizeof(body)) : -1;
      if (n < 0) { argErr = "bad frame hex"; break; }
      emitReport(s, rid, body, (size_t)n);
    }
  } else {
    known = false;
  }

  if (!known && custom) {                       // firmware-specific method (nickname/pair/connect/...)
    bool handled = false; int ec = 0; const char* em = nullptr;
    JsonDocument cr;
    custom(method, params, cr.to<JsonObject>(), &handled, &ec, &em);
    if (handled) {
      if (ec) { resp["error"]["code"] = ec; resp["error"]["message"] = em; }
      else    { resp["result"] = cr; }
      serializeJson(resp, out); out.println(); return;
    }
  }

  if (!known)      { resp["error"]["code"] = -32601; resp["error"]["message"] = "method not found"; }
  else if (argErr) { resp["error"]["code"] = -32602; resp["error"]["message"] = argErr; }
  else {
    JsonObject r = resp["result"].to<JsonObject>();
    if (dry) { r["n"] = s.built; framesToJson(s, r["frames"].to<JsonArray>()); }
    else     { r["sent"] = s.notified; r["conn"] = (s.notified > 0 || s.built == 0); }
  }
  serializeJson(resp, out); out.println();
}

}  // namespace hbmode1

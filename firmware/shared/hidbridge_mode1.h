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
static const uint8_t  RID_ABS_MOUSE   = 3;
static const uint8_t  RID_DIGITIZER   = 4;

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

// Screen fraction (0..1) -> u16 absolute axis [0..32767]. Mirrors reports.frac_to_abs INCLUDING
// Python round()'s round-half-to-EVEN, so the emitted bytes match exactly.
inline uint16_t fracToAbs(double frac) {
  double x = frac * 32767.0, fl = floor(x), diff = x - fl; long r;
  if (diff < 0.5)      r = (long)fl;
  else if (diff > 0.5) r = (long)fl + 1;
  else { long f = (long)fl; r = (f % 2 == 0) ? f : f + 1; }
  return (uint16_t)clampi((int)r, 0, 32767);
}

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
inline void emitMove(ReportSink& s, int dx, int dy) {
  int xs[80], ys[80], nx = 0, ny = 0, r;
  for (r = dx; r != 0 && nx < 80; ) { int st = clampi(r,-127,127); xs[nx++] = st; r -= st; }
  for (r = dy; r != 0 && ny < 80; ) { int st = clampi(r,-127,127); ys[ny++] = st; r -= st; }
  int n = nx > ny ? nx : ny; if (n < 1) n = 1;
  for (int i = 0; i < n; i++) emitMouse(s, 0, i < nx ? xs[i] : 0, i < ny ? ys[i] : 0, 0);
}
inline void emitScroll(ReportSink& s, int clicks) {
  bool any = false;
  for (int r = clicks; r != 0; ) { int st = clampi(r,-127,127); emitMouse(s,0,0,0,st); r -= st; any = true; }
  if (!any) emitMouse(s, 0, 0, 0, 0);
}
inline void emitClick(ReportSink& s, uint8_t button) {
  emitMouse(s, button, 0, 0, 0);
  emitMouse(s, 0, 0, 0, 0);
}
inline void emitAbs(ReportSink& s, uint8_t buttons, uint16_t x, uint16_t y) {
  uint8_t b[5] = { buttons, (uint8_t)(x & 0xFF), (uint8_t)(x >> 8),
                   (uint8_t)(y & 0xFF), (uint8_t)(y >> 8) };
  emitReport(s, RID_ABS_MOUSE, b, 5);
}
inline void emitMoveAbs(ReportSink& s, double xf, double yf) {
  emitAbs(s, 0, fracToAbs(xf), fracToAbs(yf));
}
inline void emitTap(ReportSink& s, double xf, double yf, uint8_t button) {
  uint16_t x = fracToAbs(xf), y = fracToAbs(yf);
  emitAbs(s, 0, x, y); emitAbs(s, button, x, y); emitAbs(s, 0, x, y);
}
inline void emitTouch(ReportSink& s, double xf, double yf) {
  uint16_t x = fracToAbs(xf), y = fracToAbs(yf);
  uint8_t down[6] = { 0x03, 0, (uint8_t)(x&0xFF),(uint8_t)(x>>8),(uint8_t)(y&0xFF),(uint8_t)(y>>8) };
  emitReport(s, RID_DIGITIZER, down, 6);       // tip + in_range
  uint8_t lift[6] = { 0, 0, 0, 0, 0, 0 };
  emitReport(s, RID_DIGITIZER, lift, 6);
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

// The JSON-RPC dispatcher. `authed` gates every method except hello (serial is trusted => true; an
// unauthenticated TCP session => false). `notify` is the firmware's report sink. `fillHello` lets the
// firmware add its device name + capabilities to the hello result.
inline void handleJsonRpc(const String& line, Print& out, bool authed,
                          bool (*notify)(uint8_t, const uint8_t*, size_t),
                          void (*fillHello)(JsonObject)) {
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
    if (usage < 0) argErr = "unknown 'name'";
    else emitKeyUsage(s, (uint8_t)(params["mod"] | 0), (uint8_t)usage);
  } else if (!strcmp(method, "wake")) {
    emitKeyUsage(s, 0, 0x2C);                             // space -- a benign wake keystroke
  } else if (!strcmp(method, "move")) {
    emitMove(s, params["dx"] | 0, params["dy"] | 0);
  } else if (!strcmp(method, "click")) {
    int bit = buttonNameToBit(params["button"] | "left");
    if (bit < 0) argErr = "unknown 'button'"; else emitClick(s, (uint8_t)bit);
  } else if (!strcmp(method, "scroll")) {
    emitScroll(s, params["clicks"] | 0);
  } else if (!strcmp(method, "moveabs")) {
    emitMoveAbs(s, params["x"] | 0.0, params["y"] | 0.0);
  } else if (!strcmp(method, "tap")) {
    int bit = buttonNameToBit(params["button"] | "left");
    if (bit < 0) argErr = "unknown 'button'";
    else emitTap(s, params["x"] | 0.0, params["y"] | 0.0, (uint8_t)bit);
  } else if (!strcmp(method, "touch")) {
    emitTouch(s, params["x"] | 0.0, params["y"] | 0.0);
  } else {
    known = false;
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

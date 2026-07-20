"""Offline tests for structured MCU logging over the JSON-RPC wire (the "logs" capability).

The firmware emits id-less JSON-RPC `log` NOTIFICATIONS on the same channel as everything else --
{"jsonrpc":"2.0","method":"log","params":{"level":"info","msg":"...","slot":N?}} -- so the operator
gets error/warn/info/debug from the board whether driving over serial or net (TLS). Two halves:

  * host (rpc.py): the demux that already captures id-less `event` notifications ALSO routes a `log`
    notification to Python's logging (`hidbridge.mcu`) at the mapped level, and `set_log_level` shapes
    the runtime-filter method (capability-gated on "logs");
  * firmware (shared header + both main.cpp): a source-parse guard that the log path + set_log_level +
    the "logs" cap exist on BOTH boards (mirrors the other structural drift guards). Pure text -- no
    serial, no BLE, no board.
"""

import json
import logging
import pathlib
import re

import pytest

from hidbridge import rpc

ROOT = pathlib.Path(__file__).resolve().parents[2]
ESP32 = ROOT / "firmware/esp32/src/main.cpp"
PICO = ROOT / "firmware/pico/src/main.cpp"
HEADER = ROOT / "firmware/shared/hidbridge_rpc.h"


class RpcFake:
    """Minimal write/readline channel: replies to each request via `handler`, emitting `noise` lines
    (async notifications) first. (A trimmed copy of test_bt_hid_rpc.RpcFake for the log-routing tests.)"""

    def __init__(self, handler=None, noise=None):
        self.requests = []
        self._queue = []
        self._handler = handler
        self._noise = list(noise or [])

    def write(self, b):
        req = json.loads(b.decode())
        self.requests.append(req)
        for ln in self._noise:
            self._queue.append(ln)
        self._noise = []
        if self._handler is not None:
            res = self._handler(req["method"], req.get("params", {}))
            env = {"jsonrpc": "2.0", "id": req["id"]}
            env.update({"error": res["error"]} if isinstance(res, dict) and "error" in res
                       else {"result": res})
            self._queue.append(env)
        return len(b)

    def readline(self):
        if not self._queue:
            return b""
        item = self._queue.pop(0)
        return (json.dumps(item) + "\n").encode() if not isinstance(item, str) else (item + "\n").encode()

    def close(self):
        pass


# --------------------------------------------------------------------------- #
# host: a `log` notification routes to the hidbridge.mcu logger at the right level
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("level,pylevel", [
    ("error", logging.ERROR), ("warn", logging.WARNING),
    ("info", logging.INFO), ("debug", logging.DEBUG),
])
def test_log_notification_routes_to_logger_at_mapped_level(level, pylevel, caplog):
    note = {"jsonrpc": "2.0", "method": "log", "params": {"level": level, "msg": "hello world"}}
    fake = RpcFake(handler=lambda m, p: {"ok": 1}, noise=[note])
    c = rpc.RpcClient(fake)
    with caplog.at_level(logging.DEBUG, logger="hidbridge.mcu"):
        c.call("ping")
    recs = [r for r in caplog.records if r.name == "hidbridge.mcu"]
    assert len(recs) == 1
    assert recs[0].levelno == pylevel
    assert "hello world" in recs[0].getMessage()


def test_log_notification_includes_slot_when_present(caplog):
    note = {"jsonrpc": "2.0", "method": "log",
            "params": {"level": "debug", "msg": "subscribe report=mouse", "slot": 1}}
    fake = RpcFake(handler=lambda m, p: {"ok": 1}, noise=[note])
    c = rpc.RpcClient(fake)
    with caplog.at_level(logging.DEBUG, logger="hidbridge.mcu"):
        c.call("ping")
    rec = [r for r in caplog.records if r.name == "hidbridge.mcu"][0]
    assert "1" in rec.getMessage() and "subscribe report=mouse" in rec.getMessage()
    assert c.last_log == {"level": "debug", "msg": "subscribe report=mouse", "slot": 1}


def test_log_notification_does_not_disturb_the_pending_reply():
    # a `log` notification interleaved before the reply must be skipped for the pending call (like `event`)
    note = {"jsonrpc": "2.0", "method": "log", "params": {"level": "info", "msg": "boot"}}
    fake = RpcFake(handler=lambda m, p: {"sent": 1}, noise=[note])
    assert rpc.RpcClient(fake).call("wake") == {"sent": 1}


# --------------------------------------------------------------------------- #
# host: set_log_level shapes the right JSON and is capability-gated on "logs"
# --------------------------------------------------------------------------- #
def _cap_handler(caps, seen=None):
    def h(m, p):
        if seen is not None:
            seen.append((m, p))
        return {"proto": 2, "caps": list(caps)} if m == "hello" else {"level": p.get("level")}
    return h


def test_set_log_level_shapes_json_when_supported():
    seen = []
    c = rpc.RpcClient(RpcFake(handler=_cap_handler(["logs"], seen)))
    assert c.set_log_level("debug") == {"level": "debug"}
    assert seen == [("hello", {}), ("set_log_level", {"level": "debug"})]


def test_set_log_level_gated_on_logs_capability():
    c = rpc.RpcClient(RpcFake(handler=_cap_handler(["keyboard", "mouse"])))
    with pytest.raises(rpc.RpcError, match="lacks capability"):
        c.set_log_level("info")


# --------------------------------------------------------------------------- #
# firmware source-parse guard: the log path + set_log_level + "logs" cap exist on BOTH boards
# --------------------------------------------------------------------------- #
def _nows(s: str) -> str:
    return "".join(s.split())


def test_shared_header_has_log_primitives_and_level_filter():
    src = HEADER.read_text()
    # the shared level enum + name mapping + runtime filter default (info) live in the header, so both
    # boards use ONE implementation with a firmware-provided sink (mirrors the report builders).
    assert re.search(r"LOG_ERROR\b.*LOG_WARN\b.*LOG_INFO\b.*LOG_DEBUG\b", src, re.S), \
        "the shared header must define the log-level enum (error/warn/info/debug)"
    assert "g_logLevel" in src and "LOG_INFO" in src, "a runtime level filter defaulting to info"
    assert "logLevelFromName" in src and "logLevelName" in src, "level name <-> value mapping"
    assert "setLogSink" in src, "a firmware-provided log sink hook (like the report notify sink)"
    # the log notification is an id-less JSON-RPC `log` method with a level + msg
    ns = _nows(src)
    assert r'\"method\":\"log\"' in ns, "logJson must format an id-less `log` notification"
    assert r'\"level\":\"' in ns and r'\"msg\":\"' in ns, "the log params carry level + msg"


def test_set_log_level_method_is_dispatched_in_shared_header():
    # set_log_level is a built-in JSON-RPC method (shared dispatcher), so BOTH boards accept it and it
    # returns the new level. It sets g_logLevel via the shared level-name parser.
    ns = _nows(HEADER.read_text())
    assert 'strcmp(method,"set_log_level")' in ns, "handleJsonRpc must dispatch set_log_level"
    assert "g_logLevel=" in ns, "set_log_level must update the runtime level filter"


def test_both_firmwares_install_a_log_sink_and_advertise_logs_cap():
    for path, sink in ((ESP32, "esp32LogSink"), (PICO, "picoLogSink")):
        src = path.read_text()
        ns = _nows(src)
        assert "setLogSink(" in ns, f"{path.name} must register a log sink in setup()"
        assert sink in src, f"{path.name} must define its firmware-specific log sink {sink}"
        # the new "logs" capability is advertised in this board's hello fill
        assert 'caps.add("logs")' in ns.replace(" ", "") or 'caps.add("logs")' in ns, \
            f"{path.name} must advertise the 'logs' capability"


def test_esp32_error_prints_became_error_logs():
    # the diagnostic Serial.println("ERR ...") prints were CONVERTED to error-level log() calls so they
    # broadcast structured over both channels -- the raw plain-text ERR frames must be gone.
    src = ESP32.read_text()
    assert 'Serial.println("ERR eth init")' not in src, "ERR eth init must become an error log"
    assert 'Serial.println("ERR tls socket")' not in src, "ERR tls socket must become an error log"
    assert "LOG_ERROR" in src, "the esp32 must emit error-level logs (converted ERR prints)"


def test_esp32_debug_logs_which_input_report_was_subscribed():
    # THE signal that would have made the readiness bug obvious: a debug log naming WHICH input report
    # (keyboard vs mouse) a host subscribed to. The per-report callback must carry a name and log it.
    ns = _nows(ESP32.read_text())
    assert "LOG_DEBUG" in ESP32.read_text(), "esp32 must emit debug-level logs"
    assert 'ReportSubCB(slotKbdSub,"keyboard")' in ns or 'kbdSubCB(slotKbdSub,"keyboard")' in ns, \
        "the keyboard subscribe callback must know its report name for the debug log"
    assert 'ReportSubCB(slotMouseSub,"mouse")' in ns or 'mouseSubCB(slotMouseSub,"mouse")' in ns, \
        "the mouse subscribe callback must know its report name for the debug log"

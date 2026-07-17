# Two ways to drive a board: Mode 1 (smart firmware) and Mode 2 (radio)

hidbridge's MCU firmware (ESP32 / Pico) can be driven two ways over the same channel (USB serial or
TCP). A line is routed by its first character: a `{` is Mode 1 (JSON-RPC); anything else is a Mode 2
frame/management command. Both modes coexist on one board at one time.

## Mode 1 -- "tell the board WHAT; it knows HOW" (the smart-firmware path)

The board owns everything about the HID: the report descriptor and the report *building*. The host
sends high-level INTENT and the firmware turns it into HID reports and BLE notifications.

- Wire format: **JSON-RPC 2.0**, one JSON object per line.
- The report layer is written **natively in C++** in `firmware/shared/hidbridge_mode1.h`, shared by
  both firmwares. It is a deliberate DUPLICATE of `python/src/hidbridge/reports.py` (keymap + report
  builders) -- the firmware carries no host code and no codegen. The duplication is guarded against
  drift by `python/tests/test_bt_hid_mode1.py`, which dry-runs every command and asserts the bytes
  the firmware builds equal `reports.py`'s.

### Protocol

```
request  {"jsonrpc":"2.0","id":<n>,"method":"<m>","params":{...}}
response {"jsonrpc":"2.0","id":<n>,"result":{...}}
         {"jsonrpc":"2.0","id":<n>,"error":{"code":<c>,"message":"..."}}
```

Methods:

| method    | params                          | builds (reports.py)        |
|-----------|---------------------------------|----------------------------|
| `hello`   | -- (allowed unauthenticated)    | capabilities handshake     |
| `type`    | `{text}`                        | `type_text`                |
| `key`     | `{name, mod?}`                  | `key_reports`              |
| `wake`    | --                              | `key_reports("space")`     |
| `move`    | `{dx, dy}`                      | `mouse_move_reports`       |
| `click`   | `{button?}`                     | `mouse_click_reports`      |
| `scroll`  | `{clicks}`                      | `mouse_scroll_reports`     |

(Relative-only: the report set is keyboard + relative mouse. Absolute-pointer methods -- `moveabs` /
`tap` / `touch` -- were removed with the absolute mouse + digitizer reports; see the top-level README.)

- Any method with a `"dry": true` param returns `result.frames` (the built report bytes as
  `<reportID hex><body hex>` strings) WITHOUT notifying -- the verification / no-BLE path.
- `result` for a real send is `{sent: <n>, conn: <bool>}` (`conn:false` = accepted but no central
  connected -- the Mode 2 "OK noconn" equivalent).
- Errors use standard JSON-RPC codes: `-32700` parse error, `-32601` method not found, `-32602`
  invalid params, `-32001` auth required (an unauthenticated TCP session may only call `hello`).

`hello` returns `{proto, device, name, modes:["frames","rpc"], caps:[...]}` -- the version +
capabilities handshake, so the host adapts to older firmware instead of assuming.

### Using it

```bash
# CLI: --rpc routes the normal verbs through Mode 1 (the board builds the reports)
hidbridge --rpc --backend esp32 --port /dev/ttyUSB3 type "hello world"
hidbridge --rpc --backend esp32 --port /dev/ttyUSB3 move 40 0
hidbridge --rpc --backend net --host hidbridge-<nick>.local status   # shows the hello handshake
```

```python
from hidbridge.mode1 import open_client
with open_client("esp32:/dev/ttyUSB3") as c:
    print(c.hello())              # {proto, device, name, modes, caps}
    c.type_text("hello")
    c.move(40, 0)
```

## Mode 2 -- "just a radio" (the frame-relay path)

The host builds the exact report bytes (`reports.py` + `controller.py`) and the board relays them as
BLE notifications -- the board is a dumb pipe. This is the original path and it is unchanged.

- Wire format: compact newline lines. `K<hex>` keyboard, `M<hex>` relative mouse; plus management
  (`I`/`T`/`S`/`X`/`O`/`P`/`V`/`N`/`W`/`@`). See the firmware header comment for the full grammar.
- This is the right path when the *host* must own the logic -- e.g. so all three backends (BlueZ,
  Pico, ESP32) emit byte-identical reports from one shared report layer, or so a passcode is expanded
  to frames host-side rather than sent as a string.

```bash
hidbridge --backend esp32 --port /dev/ttyUSB3 type "hello"    # host builds frames (default)
```

## Which to use

- **Mode 2** is the default and the one to use for the multi-backend guarantee (identical bytes from
  BlueZ / Pico / ESP32), for host-side-only logic, and for the lowest firmware surface.
- **Mode 1** makes a single board self-contained: it can be driven by anything that speaks JSON-RPC
  over a socket/serial with no host report layer at all. Useful when the board is the whole product,
  or for a non-Python client.

Both are the same firmware; you can switch per command.

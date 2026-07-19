# How a board is driven: the MCU speaks JSON-RPC (the board builds the reports)

hidbridge's MCU firmware (ESP32 / Pico) is driven over one channel (USB serial or TCP) with a line
protocol routed by the FIRST character of each line:

- `{` -> **JSON-RPC 2.0** (one JSON object per line): the report + device-management methods.
- anything else -> a **single-char DEVICE command** (`P`/`A`/`V`/`X`/`O`/`I`/`T`/`S`/`N`/`W`/`@`):
  ping, advertise, park, select-slot, status, provision, auth. See the firmware header for the grammar.

There is ONE report path on the MCU wire: JSON-RPC. The board owns the HID report descriptor and the
report *building* -- the host sends high-level INTENT and the firmware turns it into HID reports and
BLE notifications. (History: the firmware also used to accept a host-built `K<hex>`/`M<hex>` frame
relay -- "just a radio", the host builds the exact bytes. That path was removed; the board is in
charge now. The BlueZ **dongle** backend still host-builds via `reports.py`, but over its own daemon
socket -- NOT this MCU channel.)

## rpc -- "tell the board WHAT; it knows HOW"

- Wire format: **JSON-RPC 2.0**, one JSON object per line.
- The report layer is written **natively in C++** in `firmware/shared/hidbridge_rpc.h`, shared by
  both firmwares. It is a deliberate DUPLICATE of `python/src/hidbridge/reports.py` (keymap + report
  builders) -- the firmware carries no host code and no codegen. The duplication is guarded against
  drift by `python/tests/test_bt_hid_rpc.py`, which dry-runs every command and asserts the bytes
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
| `frames`  | `{frames:[<hex>,...]}`          | relays PRE-BUILT bytes     |

(Relative-only: the report set is keyboard + relative mouse. Absolute-pointer methods -- `moveabs` /
`tap` / `touch` -- were removed with the absolute mouse + digitizer reports; see the top-level README.)

- `frames` is the one method where the HOST supplies pre-built report bytes (`<reportID hex><body
  hex>`) inside JSON-RPC -- the board just notifies them. It exists for the central byte-parity
  self-test (drive a KNOWN report two ways -- `frames` vs a high-level verb -- and assert the SAME
  GATT notification arrives) and is NOT the everyday path.
- Any method with a `"dry": true` param returns `result.frames` (the built report bytes as
  `<reportID hex><body hex>` strings) WITHOUT notifying -- the verification / no-BLE path.
- `result` for a real send is `{sent: <n>, conn: <bool>}` (`conn:false` = accepted but no central
  connected on the target slot).
- Errors use standard JSON-RPC codes: `-32700` parse error, `-32601` method not found, `-32602`
  invalid params, `-32001` auth required (an unauthenticated TCP session may only call `hello`).

`hello` returns `{proto, device, name, modes:["frames","rpc"], caps:[...]}` -- the version +
capabilities handshake, so the host adapts to older firmware instead of assuming. (`modes` names the
two JSON-RPC report styles -- `rpc` = high-level intent, `frames` = host-supplied bytes -- both over
JSON-RPC; it does NOT refer to the removed K/M frame lines.)

### Using it

The production driver is `hidbridge.HidBridge` / `board.McuBoard`: it opens the board, self-identifies
via `hello`, and drives with the high-level verbs (the board builds the reports; the target slot's
per-slot keyboard layout applies board-side).

```python
from hidbridge import HidBridge
with HidBridge("/dev/ttyUSB3") as b:      # a serial MCU; a host/IP for the net backend
    print(b.board_info())                 # {board, fw, device, ...} from the hello handshake
    b.type("hello world")
    b.move(40, 0)
```

```bash
hidbridge --backend esp32 --port /dev/ttyUSB3 type "hello world"   # the board builds the reports
hidbridge --backend net --host hidbridge-<nick>.local status       # shows the hello handshake
```

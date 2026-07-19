# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`hidbridge` drives any BLE-HID (HOG) host from Linux -- phone, tablet, laptop, or desktop: type text,
press keys, move/click the relative pointer, enter a lock-screen passcode. Two halves in one repo
because they share a strict contract (the HID report descriptor + the serial wire protocol):
`python/` (the package + CLI) and `firmware/` (Pico 2 W / ESP32 PlatformIO firmware).

Support is deliberately RELATIVE-ONLY (keyboard report ID 1 + relative mouse ID 2). Absolute-mouse
and digitizer/touch reports were removed on purpose: they made desktop hosts (GNOME/libinput) treat
the device as a touchscreen and hide the cursor, and were proven-negative on the tested hosts. The
code + git history keep the door open to revisit an absolute path later.

`hidbridge` is a **placeholder name pending a rename**. The code predates it: modules, env vars
(`BT_HID_*`), config paths (`~/.config/bt_hid/`), and docstrings still say `bt_hid`. That mismatch is
expected -- don't "fix" it piecemeal.

## Working practices (read this first)

- **DELEGATE long or self-contained work to SUBAGENTS -- do NOT grind it all inline.** The main agent
  is an ORCHESTRATOR: its reasoning degrades over a long inline session, so hand off anything
  self-contained or parallelizable to a subagent (the Agent/Task tool), keep the CONCLUSION (not the
  file dumps), and stay responsive to the user. Things that belong in a subagent: a board flash +
  verify, a `pio run` across both boards, a live-hardware HID test cycle, a broad read-only
  search/audit, a remote-host probe over SSH, a long install-then-verify. Give it a crisp goal +
  definition-of-done + guardrails; it has its own context. Don't run two subagents editing the SAME
  files. **Reach for a subagent BEFORE doing a big task yourself -- this is the main habit to build.**
- **Run long / live-monitoring commands in the BACKGROUND, not the foreground.** A blocking foreground
  Bash call (a multi-second serial probe, a `pio device monitor` tail, a sampling loop, an `apt`
  install, a live-HID test) makes you UNABLE to read the user's messages until it returns -- their
  input lands late and you act on stale state. Keep foreground Bash SHORT (a few seconds); run
  anything longer with `run_in_background: true` (you get a completion notification) and keep working
  meanwhile. A PreToolUse hook (`.claude/hooks/block-unbounded-bash.sh`) ENFORCES this: it blocks long
  foreground commands, blocks `ssh`/`scp` from the MAIN agent (delegate remote work to a subagent),
  and blocks unbounded `until`/`while true` wait-loops. If it blocks you, that IS the nudge -- background
  it or delegate it, don't fight it.
- **Do parallel code work in a git WORKTREE, not the primary checkout** (`.claude/worktrees/`), so a
  subagent's edits don't collide with the primary or each other. Integrate branches back with a real
  MERGE commit, never a rebase.
- **Never a destructive git op in a tree with uncommitted work.** `git reset --hard`, `git checkout --`,
  `git clean`, stash-drop can silently destroy work (yours or the user's WIP). Use explicit
  `git add <paths>` (never `git add -A` -- it stages stray files). Commit + push as you go (see
  Conventions).

## Commands

Python (from `python/`). A venv already exists at `python/.venv`, built with
`include-system-site-packages = true` so the dongle backend's apt-installed `dbus`/`gi` remain
visible; run the tools through it:

```bash
.venv/bin/python -m pytest -q    # offline suite (260 pass, ~21 skip with no hardware attached)
.venv/bin/python -m pytest tests/test_bt_hid_reports.py::test_name -x     # a single test
.venv/bin/python -m ruff check .
uv build                         # wheel + sdist. NOT `python -m build`: this host has no
                                 # ensurepip/python3-venv, so the build backend can't bootstrap.

pip install -e ".[dev,mcu]"      # dev tooling + the serial (Pico/ESP32) backend
pip install -e ".[dongle]"       # BlueZ backend; see the system-python caveat below
```

Firmware (needs PlatformIO Core; from `firmware/pico/` or `firmware/esp32/`):

```bash
pio run                          # compile
pio run -t upload                # compile + flash (port auto-detected)
pio device monitor -b 115200
```

There is no CI yet.

## Test gating

The suite is offline by default and must stay that way: `pytest` on a box with no hardware runs
clean. Tests needing real hardware gate on an env var and skip otherwise -- `BT_HID_LOOPBACK=1` (two
adapters + root), `BT_HID_CENTRAL=1` (built-in adapter + an MCU), `BT_HID_HW_ESP32=1`, and the
`*_live.py` files (`BT_HID_LIVE_*`, need a real bonded host). `BT_HID_PICO_PORT` / `BT_HID_ESP32_PORT`
point at the boards. New tests for pure logic must not reach for hardware; new hardware tests must
follow the skip convention.

## Architecture

Layers, top to bottom -- each is injectable at the layer below, which is why the whole
connect/act/disconnect lifecycle unit-tests with no hardware:

- **`reports.py`** -- pure stdlib, zero bluetooth deps. `REPORT_DESCRIPTOR` plus the byte builders for
  keyboard (ID 1) + relative mouse (ID 2) -- the relative-only report set. The single source of truth
  for every byte on the wire. Keep it import-clean (no socket/dbus/gi) -- it must import under both
  uv's CPython and the system python.
- **`controller.py`** -- `HidController` turns actions into `(kind, body)` frames and hands them to a
  pluggable transport. `RecordingTransport` asserts exact frames in tests.
- **`conn.py`** -- `HidSession`, the backend-agnostic connect-on-demand context manager: enter =
  connect that target, exit = disconnect but keep the bond. Backends declare traits via
  `capabilities` (e.g. `per_device_identity`).
- **`cli.py`** -- thin wrapper over the two above.

Backends are duck-typed and discovered by module name, so **adding one means dropping in a file, not
editing a registry**: `transport_<name>.py` exposes `make_transport(**opts)` (report path);
`link_<name>.py` implements the `LinkBackend` lifecycle (connect/disconnect path). The two shipped
families:

- **dongle** -- the host's own BlueZ stack. The `ble_hid.py` daemon exports the HID-over-GATT
  peripheral and the CLI talks to it over a per-adapter Unix socket; device management is BlueZ D-Bus
  scoped to `/org/bluez/hciN`. Needs the **system python** (`dbus` + PyGObject + `AF_BLUETOOTH` only
  exist there) and usually root. A venv sees them only if built with `--system-site-packages`.
- **mcu** (`pico`, `esp32`) -- the board runs its own BLE stack and owns its bond database, reached
  over USB serial. Needs only pyserial: no dbus, no daemon, no root. This path exists because BlueZ
  loses the fight with some hosts over HID input-report CCCD / Report-Reference subscription; BTstack
  and NimBLE serve HOG the way those hosts expect.
- **net** -- an Ethernet-equipped ESP32 (`esp32-ethernet-kit` firmware, `-DHIDBRIDGE_NET`) serving the
  same line protocol over TCP 3333 + mDNS. `transport_net`/`link_net` are thin subclasses of the
  esp32 transport/link -- the wire changes (a `socket_link.SocketLine` instead of a tty), the protocol
  and command layer are inherited verbatim. Token-authenticated, fail-closed (see below).

Both backends emit identical report bytes, so behaviour is transport-independent.

## One versioned JSON-RPC protocol on the MCU wire (see `docs/bt-hid-protocol.md`)

The ESP32/Pico firmware is driven over one channel by **one JSON-RPC 2.0 protocol**: the board builds
every report AND owns device management, and the host sends INTENT
(`{"jsonrpc":"2.0","id":1,"method":"type","params":{"text":"hi"}}`). The report layer is native C++ in
`firmware/shared/hidbridge_rpc.h` (shared by both firmwares), a **deliberate duplicate of `reports.py`**
guarded by `test_bt_hid_rpc.py` (dry-run each command, assert equality with `reports.py`). Host side:
`board.McuBoard` + `rpc.py`. The slot/identity + network methods are firmware-specific `custom`
handlers (`esp32Custom` / `picoCustom`).

- **REMOVED: the old `K`/`M<hex>` raw frame relay AND the single-char DEVICE commands
  (`P A I T S N V W X O`).** All of them were folded into JSON-RPC methods (`ping`/`connect`/`target`/
  `identities`/`park`/`advertise`/`disconnect`/`net_status`/`provision`/...) and deleted from both
  firmwares. The ONLY non-JSON line still accepted is `@<token>` (a TCP session's auth line). A removed
  single-char line now returns `ERR unknown`. Multi-backend byte-parity rests on the **dongle** path
  (still host-builds via `reports.py`/`controller.py`, over its daemon socket -- NOT the MCU wire) plus
  the rpc drift guard. `reports.py`'s builders stay (the dongle + drift guard use them).
- **VERSION-INDEPENDENCE (the wheel and the firmware ship separately).** The contract is negotiated via
  `hello`, which returns `proto` (an integer) + a COMPREHENSIVE `caps` list. The host FEATURE-DETECTS on
  `caps` (never branches on `proto`) and gates optional methods on the cap, raising the typed
  `IncompatibleFirmware` on a missing cap / older `proto` / "method not found" reply -- never a silent
  no-op. `bridge.MIN_FIRMWARE_PROTO` is the oldest firmware the wheel supports. **Policy: the protocol
  is ADDITIVE** -- a new optional method adds a cap and leaves `proto` unchanged; `proto`
  (`hbmode1::PROTO_VERSION`) bumps ONLY on a breaking change. `reports.py` + the report descriptor are
  firmware<->target internals, NOT part of the host<->MCU contract -- which is why the wheel no longer
  depends on firmware report internals.

## Invariants that will bite you

- **rpc firmware `hidbridge_rpc.h` duplicates `reports.py` on purpose (no codegen) -- keep them
  in sync.** `test_bt_hid_rpc.py::test_firmware_dry_run_matches_reports_py` (gated `BT_HID_RPC=1`)
  is the drift guard; run it after touching either the keymap/builders in `reports.py` or the header.

- **Firmware/host report-map parity.** The firmware installs `REPORT_DESCRIPTOR` byte-for-byte;
  `python/tests/test_bt_hid_esp32.py` parses `firmware/esp32/src/main.cpp` and fails on drift. Never
  edit the report map on one side alone.
- **The reset-free serial open in `serial_link.py` is load-bearing, not clever code.** It leaves the
  tty at baud B0 on close so the kernel skips `tty_port_raise_dtr_rts()` on the next open -- otherwise
  RTS pulses EN, the ESP32 reboots, and its bonded BLE link to the host dies. pyserial's documented
  `dtr=False; rts=False` does *not* prevent this (the kernel raise happens inside `os.open`). Any
  refactor of the open path must preserve this.
- **Readiness gate before the first keystroke.** Some hosts have a post-connect activation window
  where reports are dropped. Wait for `EVT ready` / `subscribed=1` (MCU) or the daemon's `status`
  subscription reply (dongle) -- that's what `HidSession` does.
- **The passcode is a secret with a hard rule.** Read at run time from `~/.config/bt_hid/idevice.pin`
  (0600, outside the repo), expanded to HID frames locally, never logged, never in a repr, never sent
  as text. `reports.passcode_reports()` keeps no copy. The **network auth token**
  (`~/.config/bt_hid/net.token`, 0600, git-ignored) follows the same rule -- never logged/repr'd, and
  provisioned onto the board over USB only (the firmware honors the JSON-RPC `provision` method on the
  serial channel and REFUSES it over TCP/TLS + the WS channel -- the serial-only gate, same as the old
  `W<token>`).
- **The `net` TCP path is remote input-injection and must stay fail-closed.** It is **TLS-PSK
  encrypted**: the provisioned token is the pre-shared key, so no token => no TLS port opens => the
  board can't be driven over the LAN, and the report bytes (incl. passcode frames) are encrypted on
  the wire. Security rests entirely on the token secrecy. The control web page on :80 stays plain HTTP
  (browsers can't speak TLS-PSK) -- the one cleartext surface. All firmware network code is behind
  `#ifdef HIDBRIDGE_NET`; the `esp32dev` serial build is unaffected.
- **Per-adapter naming isn't cosmetic.** `naming.py` derives the socket path, D-Bus object paths, and
  advertised name from the `hciN` slug so multiple daemons coexist. Keying on the BD address would
  break: the TP-Link UB500 dongles here *share* one, non-reprogrammable.
- **A BlueZ dongle has one BLE identity for all its bonds**, so it can't be steered to "the right
  host" by identity and holds one at a time; `targets.py::resolve()` warns on that collision. MCU
  boards get per-bond identities ("host slots", `I<n>`/`T<n>`) -- the ESP32 remembers up to ten and
  can hold two connected at once. The obvious alternative -- ONE shared identity + directed
  advertising (`ADV_DIRECT_IND`) to a chosen bonded peer -- is NOT viable on these boards: it needs
  the controller resolving list (RPA resolution from the bond IRK), which the classic ESP32 lacks and
  arduino-pico's precompiled BTstack has compiled out. That is WHY we use distinct per-slot
  identities, not a workaround. Evidence + the prototype branch: `docs/bt-hid-directed-advertising.md`.

## Conventions

The ruff ruleset in `pyproject.toml` is deliberately scoped: pyflakes correctness is enforced, but
the dense one-liner report/bit-parser style (`E701`, `E702`, `import a,b,c`) is house style and
tolerated. Don't reformat it. Module docstrings here carry the *why* -- the hardware quirk, the
verdict, the date it was verified live. Match that when you add one.

**Commit + push as you go.** At every stable point (a feature works, a fix is verified, a refactor
lands green), make a focused git commit and `git push origin main` -- don't batch a session's work
into one big commit. Keep messages descriptive; note what was verified live. A pre-commit hook blocks
confusable Unicode (em/en dashes, smart quotes) -- use ASCII `--` and straight quotes. `git commit -m`
with backticks/`$()` in the message gets shell-mangled, and a local bash guard trips on the words
`until`/`while` in a command; write the message to a file and use `git commit -F <file>` to avoid both.

## Further reading

`docs/` holds the deep dives (MCU approach and host slots, multi-device routing, absolute pointer,
trust-overlay dismissal, integration notes, and why shared-identity + directed advertising was
rejected in favour of per-slot identities: `bt-hid-directed-advertising.md`). The MCU wire protocol --
the JSON-RPC method vocabulary, the `hello` handshake (`proto` + `caps`), the version-independence
policy, and the `EVT` events -- is documented in `docs/bt-hid-protocol.md` + tabulated in
`firmware/README.md`.

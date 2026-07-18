"""bt_hid CLI -- drive the BLE HID keyboard+mouse peripheral (dongle or Pico backend).

Runs under the SYSTEM python (`/usr/bin/python3`) because the DONGLE backend uses `dbus`. Report
commands are sent to the running `ble_hid` daemon over its Unix control socket; pairing/device
management uses BlueZ D-Bus scoped to the DONGLE adapter.

BACKENDS (`--backend`):
  dongle (default)  the USB Bluetooth dongle via BlueZ + the `ble_hid` daemon (all subcommands).
  pico              a Raspberry Pi Pico 2 W running the BTstack HOG firmware (firmware/pico/),
                    reached over USB serial. Report commands (wake/unlock/type/key/move/click/
                    tap-trust/status) route through the shared HidController + `reports` layer so
                    the exact same report BYTES are emitted; pairing is done in the paired device's
                    Bluetooth settings (see `pair-mode`). Minimal contributor hardware: a ~USD 6 Pico instead
                    of a BT dongle. `pico` needs no dbus/daemon.
  esp32             an ESP32 (NimBLE HOG firmware) over USB serial -- same report layer as pico.
  net               an Ethernet-equipped ESP32 running the same firmware, reached over TCP (no USB
                    cable). Address it with --host <mDNS-name|IP> (default port 3333). The path is
                    token-authenticated; set the token on the board over USB first with
                    `net-provision`. `net` needs no dbus/daemon/serial.

Subcommands:
  adapters              show detected adapters + which is the dongle (the one we use)
  pair-mode             bring the dongle up discoverable + advertising as a keyboard (runs daemon)
  net-provision         set the network auth token on an Ethernet board (over USB serial)
  net-status            show the board's Ethernet/TCP state (link, IP, mDNS, token, clients)
  devices               list bonded devices on the dongle
  connect <name|mac>    make the dongle available to a target so it (re)connects; wait for link
  disconnect [name|mac] drop the active BLE link (target stays bonded)
  wake                  nudge the connected device awake (a keystroke)
  unlock <passcode>     type the passcode at the lock screen + Enter   (NEVER logged)
  type <text>           type text
  key <name> [--mod M]  press a named key (enter/esc/space/arrows/f1..)
  move <dx> <dy>        move the pointer (relative)
  click [left|right|middle|back|forward]   click a mouse button (back=btn4, forward=btn5)
  tap-trust             helper: nudge the relative pointer toward a dialog button + click
  status                show daemon + link status

SECURITY: `unlock` expands the passcode to HID frames LOCALLY and sends only frames to the daemon;
the passcode string is never logged, never sent as text, never stored.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys

from hidbridge import adapter as A  # noqa: E402
from hidbridge import naming as N  # noqa: E402
from hidbridge import reports as R  # noqa: E402
from hidbridge import targets as TG  # noqa: E402

# The control socket the report subcommands talk to. Resolved per-adapter in main() (so `bt_hid
# --adapter hci2 type ...` reaches the hci2 daemon, not a hard-coded single socket). BT_HID_SOCK
# pins an exact path (highest priority). Starts as the legacy default until main() resolves it.
CONTROL_SOCK = N.control_sock("default", env_override=os.environ.get("BT_HID_SOCK"))

# HID backend: "dongle" (default, BlueZ + ble_hid daemon) or an alternative transport backend
# (e.g. "esp32" = an ESP32 BLE-HID board over USB serial). Non-dongle backends route the report
# commands through a HidController transport instead of the daemon socket. Set once in main().
_ALT_TRANSPORT = None      # a controller transport when --backend is non-dongle, else None
_RPC_CLIENT = None         # an rpc.RpcClient when --rpc, else None
_BACKEND = "dongle"

BLUEZ = "org.bluez"
DBUS_OM = "org.freedesktop.DBus.ObjectManager"
DBUS_PROP = "org.freedesktop.DBus.Properties"
DEVICE_IFACE = "org.bluez.Device1"


# --------------------------------------------------------------------------- #
# daemon control socket
# --------------------------------------------------------------------------- #
def _alt_send(cmd: dict) -> dict:
    """Route a daemon-style command dict through the selected alternative backend transport.

    Report commands map 1:1 onto HidController methods so `reports.py` stays the single source of
    truth for the emitted bytes; the transport (e.g. ESP32 over serial) just relays them.
    """
    from hidbridge.controller import HidController
    c = HidController(_ALT_TRANSPORT)
    t = cmd.get("cmd")
    if t == "ping":
        if hasattr(_ALT_TRANSPORT, "ping"):
            return _ALT_TRANSPORT.ping()
        return {"ok": True, "backend": _BACKEND}
    if t == "type":
        return c.type_text(cmd["text"])
    if t == "key":
        return c.send_key(cmd["name"], int(cmd.get("modifiers", 0)))
    if t == "move":
        return c.move(int(cmd["dx"]), int(cmd["dy"]))
    if t == "click":
        return c.click(cmd.get("button", "left"))
    if t == "reports":
        frames = [(f["kind"], bytes.fromhex(f["hex"])) for f in cmd["frames"]]
        return _ALT_TRANSPORT.send(frames)
    return {"ok": False, "error": f"cmd {t!r} unsupported on backend {_BACKEND!r}"}


def _rpc_send(cmd: dict) -> dict:
    """Route a daemon-style command dict to the Mode 1 (JSON-RPC) client -- the board builds the
    reports. Mirrors _alt_send, but tells the board WHAT to do instead of sending it frames."""
    from hidbridge.rpc import RpcError
    c = _RPC_CLIENT
    t = cmd.get("cmd")
    try:
        if t == "ping":
            return {"ok": True, "backend": "rpc", "resp": c.hello()}
        res = None
        if t == "type":
            res = c.type_text(cmd["text"])
        elif t == "key":
            res = c.key(cmd["name"], int(cmd.get("modifiers", 0)))
        elif t == "move":
            res = c.move(int(cmd["dx"]), int(cmd["dy"]))
        elif t == "click":
            res = c.click(cmd.get("button", "left"))
        elif t == "reports":
            # Mode 2 semantics through JSON-RPC's `frames` method: relay pre-built report bytes.
            _rid = {"keyboard": 1, "mouse": 2}
            hexframes = ["%02x" % _rid[f["kind"]] + f["hex"] for f in cmd["frames"]]
            res = c.frames(hexframes)
        else:
            return {"ok": False, "error": f"cmd {t!r} unsupported in --rpc (Mode 1) mode"}
        # a report method returns {sent, conn}; conn False == accepted but no central (like OK noconn)
        return {"ok": True, "backend": "rpc", "sent": res.get("sent"), "conn": res.get("conn")}
    except RpcError as e:
        return {"ok": False, "error": f"rpc: {e}"}


def _send(cmd: dict, timeout=5.0) -> dict:
    if _RPC_CLIENT is not None:
        return _rpc_send(cmd)
    if _ALT_TRANSPORT is not None:
        try:
            return _alt_send(cmd)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{_BACKEND} backend: {e}"}
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(CONTROL_SOCK)
    except OSError as e:
        return {"ok": False, "error": f"daemon not running ({e}); start it with: "
                                      "sudo hidbridge/bt_hid pair-mode"}
    with s:
        s.sendall(json.dumps(cmd).encode())
        try:
            data = s.recv(65536)
        except socket.timeout:
            return {"ok": False, "error": "daemon timeout"}
    try:
        return json.loads(data.decode())
    except ValueError:
        return {"ok": False, "error": "bad daemon response"}


def _require_ok(resp: dict):
    if not resp.get("ok"):
        print(f"error: {resp.get('error')}", file=sys.stderr)
        sys.exit(1)


# --------------------------------------------------------------------------- #
# BlueZ device helpers (scoped to the dongle adapter)
# --------------------------------------------------------------------------- #
def _bus():
    import dbus
    return dbus.SystemBus()


def _dongle_path(prefer=None):
    info = A.resolve_dongle(prefer)
    return info, f"/org/bluez/{info['hci']}"


def _managed():
    import dbus
    bus = _bus()
    om = dbus.Interface(bus.get_object(BLUEZ, "/"), DBUS_OM)
    return bus, om.GetManagedObjects()


def _devices_on(adapter_path):
    """Return [(path, props)] for Device1 objects under the given adapter."""
    _, objs = _managed()
    out = []
    for path, ifaces in objs.items():
        if DEVICE_IFACE in ifaces and path.startswith(adapter_path + "/"):
            out.append((path, ifaces[DEVICE_IFACE]))
    return out


def _find_device(adapter_path, target):
    t = target.lower()
    for path, props in _devices_on(adapter_path):
        addr = str(props.get("Address", "")).lower()
        name = str(props.get("Alias", props.get("Name", ""))).lower()
        if t == addr or t in name or path.lower().endswith(t.replace(":", "_")):
            return path, props
    return None, None


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #
def cmd_adapters(args):
    adapters = A.list_adapters()
    try:
        chosen = A.resolve_dongle(args.adapter, adapters)["hci"]
    except (A.NoDongleError, A.AmbiguousDongleError, A.BuiltinRefusedError):
        chosen = None
    dupes = A.duplicate_address_groups(adapters)
    dup_hcis = {h for hcis in dupes.values() for h in hcis}
    for a in adapters:
        mark = "  <-- USED" if a["hci"] == chosen else (
            "  (built-in, refused)" if a["kind"] == "builtin" else "")
        dup = "  !! DUPLICATE ADDRESS" if a["hci"] in dup_hcis else ""
        print(f"{a['hci']}  {a['kind']:7s}  {a.get('address','?'):17s}  vendor={a['usb_vendor']} "
              f"removable={a['removable']:9s} {a['product_name']}{mark}{dup}")
    if dupes:
        print("\nWARNING: duplicate BD addresses present (routing hazard) -- see: bt_hid preflight",
              file=sys.stderr)
    if chosen is None:
        print("\nNo single USB dongle selected. Plug one in, or disambiguate (bt_hid preflight).",
              file=sys.stderr)
        sys.exit(2)


def cmd_preflight(args):
    """Audit all adapters + surface the multi-adapter hazards (same-MAC trap, no dongle)."""
    rep = A.preflight()
    print("bt_hid preflight -- adapter audit")
    for a in rep["adapters"]:
        print(f"  {a['hci']}  {a['kind']:7s}  {a.get('address','?'):17s}  {a.get('product_name','')}")
    print(f"\ndongles: {', '.join(rep['dongles']) or '(none)'}   "
          f"built-in (refused): {', '.join(rep['builtin']) or '(none)'}")
    if rep["duplicate_address_groups"]:
        print("\nduplicate BD address groups:")
        for addr, hcis in sorted(rep["duplicate_address_groups"].items()):
            print(f"  {addr} -> {', '.join(hcis)}")
    if rep["warnings"]:
        print("\nWARNINGS:")
        for w in rep["warnings"]:
            print(f"  - {w}")
    print(f"\nPREFLIGHT {'OK' if rep['ok'] else 'FAILED (duplicate-address hazard present)'}")
    sys.exit(0 if rep["ok"] else 3)


def cmd_pair_mode(args):
    if _ALT_TRANSPORT is not None:
        # Alternative backends (e.g. ESP32) self-advertise from firmware; just kick advertising and
        # print the one-time pairing steps. There is no host daemon. The firmware advertises as
        # HIDBRIDGE-<backend> (e.g. HIDBRIDGE-esp32), a distinct name from the dongle's HIDBRIDGE-dongle.
        name = f"HIDBRIDGE-{_BACKEND}"
        if hasattr(_ALT_TRANSPORT, "advertise"):
            resp = _ALT_TRANSPORT.advertise()
            print(f"advertising: {resp.get('resp') or resp}")
        print(f"\n{_BACKEND} backend is advertising as {name!r}.\n"
              f"One-time pairing (physical): on the paired device open its Bluetooth settings, tap "
              f"{name!r}\nunder Other Devices, and tap Pair. After that it reconnects on its own.\n"
              f"Then drive it with: bt_hid --backend {_BACKEND} type/key/move/click/...")
        return
    # Delegate to the daemon (foreground). It sets the dongle discoverable + advertising.
    # Only forward --name if the operator pinned one; otherwise the daemon derives the per-adapter
    # default (HIDBRIDGE-dongle-<hciN>) so two daemons never share a name.
    from hidbridge import ble_hid
    ble_hid.main((["--name", args.name] if args.name else [])
                 + (["--adapter", args.adapter] if args.adapter else [])
                 + (["--force-builtin"] if args.force_builtin else []))


def cmd_devices(args):
    info, apath = _dongle_path(args.adapter)
    devs = _devices_on(apath)
    if not devs:
        print(f"No bonded devices on {info['hci']}. Run pair-mode + pair from the device.")
        return
    print(f"Bonded/known devices on {info['hci']} ({info.get('product_name')}):")
    for path, props in devs:
        print(f"  {props.get('Address')}  paired={bool(props.get('Paired'))} "
              f"connected={bool(props.get('Connected'))}  {props.get('Alias', props.get('Name',''))}")


def cmd_connect(args):
    import dbus
    target = args.target or getattr(args, "_bond", None)
    if not target:
        print("connect: give a device name/MAC, or use --device <name> to resolve the bond.",
              file=sys.stderr)
        sys.exit(1)
    args.target = target
    info, apath = _dongle_path(args.adapter)
    bus = _bus()
    # disconnect any other currently-connected central so the target can take the single HID link
    for path, props in _devices_on(apath):
        if props.get("Connected") and args.target.lower() not in (
                str(props.get("Address", "")).lower(),
                str(props.get("Alias", "")).lower()):
            try:
                dbus.Interface(bus.get_object(BLUEZ, path), DEVICE_IFACE).Disconnect()
            except dbus.DBusException:
                pass
    path, props = _find_device(apath, args.target)
    if path is None:
        print(f"target {args.target!r} is not bonded on {info['hci']}; pair it first "
              f"(pair-mode + tap it on the device).", file=sys.stderr)
        sys.exit(1)
    # As a BLE peripheral we cannot page the central; ensure advertising is up (daemon) and the
    # target reconnects to us. Try a Connect() too (harmless if it is the central that reconnects).
    try:
        dbus.Interface(bus.get_object(BLUEZ, path), DEVICE_IFACE).Connect()
    except dbus.DBusException as e:
        print(f"note: Connect() returned {e.get_dbus_name()}; relying on the target reconnecting "
              f"to our advertisement (this is normal for BLE HID).")
    print(f"targeting {props.get('Alias', props.get('Address'))}; make sure pair-mode/daemon is "
          f"running so it advertises. Check: bt_hid status")


def cmd_disconnect(args):
    import dbus
    info, apath = _dongle_path(args.adapter)
    bus = _bus()
    targets = _devices_on(apath)
    target = args.target or getattr(args, "_bond", None)
    if target:
        p, _ = _find_device(apath, target)
        targets = [(p, None)] if p else []
    n = 0
    for path, _ in targets:
        try:
            dbus.Interface(bus.get_object(BLUEZ, path), DEVICE_IFACE).Disconnect()
            n += 1
        except dbus.DBusException:
            pass
    print(f"disconnected {n} link(s) on {info['hci']} (devices stay bonded).")


def cmd_wake(args):
    # A brief keypress that does not type a character on the lock screen but wakes the device.
    _require_ok(_send({"cmd": "key", "name": "space"}))
    print("sent wake keystroke.")


def _layout_of(args) -> str:
    """The target host's keyboard layout for character->scancode mapping ('us' default, 'da')."""
    return (getattr(args, "layout", None) or R.DEFAULT_LAYOUT)


def cmd_unlock(args):
    # Expand LOCALLY to frames; never send/log the passcode text.
    frames = [{"kind": "keyboard", "hex": r.data.hex()}
              for r in R.passcode_reports(args.passcode, press_enter=not args.no_enter,
                                          layout=_layout_of(args))]
    _require_ok(_send({"cmd": "reports", "frames": frames}))
    print("sent passcode + Enter." if not args.no_enter else "sent passcode.")


def cmd_type(args):
    layout = _layout_of(args)
    if layout != R.DEFAULT_LAYOUT:
        # Non-US layout: expand HOST-SIDE with reports.py (the single source of truth) and send the
        # pre-built frames -- works uniformly across every Mode 2 backend (dongle daemon / MCU serial)
        # without teaching each one about layouts. For --rpc (Mode 1) the board owns the layout,
        # selected PER SLOT with `hidbridge --rpc ... ` + the board's `layout` method / web UI.
        try:
            frames = [{"kind": "keyboard", "hex": r.data.hex()}
                      for r in R.type_text(args.text, layout=layout)]
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)
        _require_ok(_send({"cmd": "reports", "frames": frames}))
    else:
        _require_ok(_send({"cmd": "type", "text": args.text}))
    print(f"typed {len(args.text)} chars ({layout}).")


def cmd_key(args):
    layout = _layout_of(args)
    if layout != R.DEFAULT_LAYOUT and len(args.name) == 1:
        # A single printable char is layout-dependent; expand host-side. Named keys (enter/f5/...)
        # are layout-independent and take the normal path.
        try:
            frames = [{"kind": "keyboard", "hex": r.data.hex()}
                      for r in R.key_reports(args.name, args.mod, layout=layout)]
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)
        _require_ok(_send({"cmd": "reports", "frames": frames}))
    else:
        _require_ok(_send({"cmd": "key", "name": args.name, "modifiers": args.mod}))
    print(f"pressed {args.name}.")


def cmd_move(args):
    _require_ok(_send({"cmd": "move", "dx": args.dx, "dy": args.dy}))
    print(f"moved ({args.dx},{args.dy}).")


def cmd_click(args):
    _require_ok(_send({"cmd": "click", "button": args.button}))
    print(f"clicked {args.button}.")


def cmd_tap_trust(args):
    # Heuristic helper: a Trust dialog's confirm button is upper-right of centre. With a relative
    # pointer we cannot land absolutely, so we nudge right+up then click. Prefer on-screen
    # verification for reliability (see README caveat).
    _require_ok(_send({"cmd": "move", "dx": 120, "dy": -40}))
    _require_ok(_send({"cmd": "click", "button": "left"}))
    print("tap-trust: nudged toward the Trust button + clicked (verify on screen).")


def _read_or_create_net_token(explicit=None):
    """Return the network auth token, creating a random 0600 ~/.config/bt_hid/net.token if needed.

    Priority: --token > BT_HID_NET_TOKEN > the file. If none exists and none was given, mint a strong
    random token and persist it 0600 (like the PIN file: outside the repo, never committed). Returns
    the token string; the caller sends it to the board over serial and must not log it.
    """
    from hidbridge import transport_net as TN
    tok = TN.load_token(explicit)
    if tok:
        return tok, False
    import secrets
    tok = secrets.token_urlsafe(18)          # ~24 chars, >> the firmware's 8-char minimum
    path = TN.TOKEN_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, tok.encode("ascii"))
    finally:
        os.close(fd)
    return tok, True


def cmd_net_provision(args):
    """Set the board's network auth token over USB serial (the ONLY channel the firmware accepts it).

    Provisions the SAME token the host will present over TCP, so `--backend net` then just works.
    """
    from hidbridge.transport_esp32 import Esp32Transport, detect_port
    from hidbridge.serial_link import open_serial
    token, created = _read_or_create_net_token(args.token)
    dev = args.esp32_port or detect_port()
    if not dev:
        print("error: no ESP32 serial port found; pass --esp32-port /dev/ttyUSBx "
              "(the board must be connected over USB for provisioning)", file=sys.stderr)
        sys.exit(2)
    try:
        ser = open_serial(dev, 115200, 2.0, prefer_raw=True)
    except Exception as e:  # noqa: BLE001
        print(f"error: cannot open {dev}: {e}", file=sys.stderr)
        sys.exit(2)
    tp = Esp32Transport(port=ser)
    try:
        res = tp.set_net_token(token)          # W<token> over serial; never logs the token
        net = tp.net_status()
    finally:
        tp.close()
    if not res.get("ok"):
        print(f"error: board rejected the token: {res.get('resp')!r}", file=sys.stderr)
        sys.exit(1)
    where = "created" if created else "using existing"
    print(f"provisioned network token on {dev} ({where} ~/.config/bt_hid/net.token).")
    if net.get("ok"):
        ip = net.get("ip") or "?"
        mdns = net.get("mdns") or "?"
        print(f"board network: eth={'up' if net.get('eth') else 'down'} ip={ip} "
              f"mdns={mdns} port={net.get('port')}")
        print(f"drive it with: hidbridge --backend net --host {mdns} type \"hello\"")
    else:
        print("note: this firmware has no network build; reflash env:esp32-ethernet-kit "
              "(pio run -e esp32-ethernet-kit -t upload).")


def cmd_net_status(args):
    """Query the board's Ethernet/TCP state. Over TCP (--backend net) or USB (--backend esp32)."""
    if _ALT_TRANSPORT is not None and hasattr(_ALT_TRANSPORT, "net_status"):
        try:
            net = _ALT_TRANSPORT.net_status()
        except Exception as e:  # noqa: BLE001
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)
        if not net.get("ok"):
            print(f"net-status: {net.get('resp')!r}")
            return
        print(f"eth={'up' if net.get('eth') else 'down'} ip={net.get('ip')} "
              f"port={net.get('port')} mdns={net.get('mdns')} "
              f"token={'set' if net.get('token') else 'UNSET'} clients={net.get('clients')}")
        return
    print("net-status needs a board transport; use --backend net --host <board> "
          "or --backend esp32 --port <tty>", file=sys.stderr)
    sys.exit(2)


def cmd_status(args):
    if _RPC_CLIENT is not None:
        resp = _send({"cmd": "ping"})
        if resp.get("ok"):
            h = resp.get("resp", {})
            print(f"Mode 1 (rpc) board {h.get('device')!r}: proto={h.get('proto')} "
                  f"modes={h.get('modes')} caps={h.get('caps')}")
        else:
            print(f"rpc: {resp.get('error')}")
        return
    if _ALT_TRANSPORT is not None:
        resp = _send({"cmd": "ping"})
        if resp.get("ok"):
            state = "connected" if resp.get("connected") else "advertising (no central)"
            print(f"{_BACKEND} backend UP on {resp.get('port')}: {resp.get('resp')} [{state}]")
        else:
            print(f"{_BACKEND} backend: {resp.get('error') or resp.get('resp')}")
        return
    resp = _send({"cmd": "ping"})
    if resp.get("ok"):
        print(f"daemon UP on {resp.get('hci')} as {resp.get('name')!r}")
    else:
        print(f"daemon: {resp.get('error')}")
    try:
        info, apath = _dongle_path(args.adapter)
        for path, props in _devices_on(apath):
            if props.get("Connected"):
                print(f"connected central: {props.get('Address')} "
                      f"{props.get('Alias', props.get('Name',''))}")
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# targets registry (device -> backend target + bond) subcommands
# --------------------------------------------------------------------------- #
def cmd_targets_list(args):
    reg = TG.TargetRegistry.load()
    if not reg.names():
        print(f"no devices registered (config: {reg.path or TG.config_path()}).\n"
              f"add one: bt_hid targets add <name> <target> <bond>")
        return
    print(f"registered devices (config: {reg.path or TG.config_path()}):")
    for name in reg.names():
        r = reg.resolve(name)
        note = f"  # {r.note}" if r.note else ""
        print(f"  {name:10s} -> target={r.target!r} bond={r.bond!r} "
              f"[backend={r.backend}, per_device_identity={r.per_device_identity}]{note}")
        if r.shared_mac_warning:
            print(f"     WARNING: {r.shared_mac_warning}", file=sys.stderr)


def cmd_targets_add(args):
    reg = TG.TargetRegistry.load()
    try:
        reg.add(args.name, target=args.target, bond=args.bond, note=args.note)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    path = reg.save()
    print(f"added {args.name!r} -> target={args.target!r} bond={args.bond!r} (saved {path})")
    r = reg.resolve(args.name)
    if r.shared_mac_warning:
        print(f"WARNING: {r.shared_mac_warning}", file=sys.stderr)


def cmd_targets_remove(args):
    reg = TG.TargetRegistry.load()
    try:
        reg.remove(args.name)
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    path = reg.save()
    print(f"removed {args.name!r} (saved {path})")


def _apply_device(args):
    """Resolve --device NAME through the registry and fold it into args (adapter/backend/port/bond).

    So `bt_hid --device mini6 click back` needs no --adapter/--backend: the registry says which
    backend + bond drive mini6. The routing-ambiguity WARNING (shared-MAC dongle) is printed once.
    Explicit --adapter/--backend/--port still win over the registry (do not override an operator's
    pin). Sets args._bond so connect/disconnect can (dis)connect the right device.
    """
    name = getattr(args, "device", None)
    if not name:
        return
    reg = TG.TargetRegistry.load()
    try:
        r = reg.resolve(name)
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
    backend, arg = TG._conn._parse_target(r.target)
    family = "dongle" if backend in TG._conn._DONGLE_ALIASES else backend
    if family == "dongle":
        if not getattr(args, "adapter", None):
            args.adapter = arg
    else:
        if (getattr(args, "backend", None) or "dongle") == "dongle":
            args.backend = family
        if not getattr(args, "port", None) and not getattr(args, "pico_port", None):
            args.port = arg
    args._bond = r.bond
    if r.shared_mac_warning:
        print(f"WARNING: {r.shared_mac_warning}", file=sys.stderr)


def build_parser():
    p = argparse.ArgumentParser(prog="hidbridge", description=__doc__.splitlines()[0])
    p.add_argument("--backend", default=os.environ.get("BT_HID_BACKEND", "dongle"),
                   help="HID backend: 'dongle' (default, BlueZ+ble_hid) or an alternative "
                        "transport, e.g. 'esp32' (an ESP32 BLE-HID board over USB serial), "
                        "'pico' (a Raspberry Pi Pico 2 W over USB serial), or 'net' (an "
                        "Ethernet-equipped ESP32 over TCP; see --host + `net-provision`)")
    p.add_argument("--device", default=os.environ.get("BT_HID_DEVICE"),
                   help="drive a registered device by friendly name (e.g. 'mini6'); resolves its "
                        "backend target + bond from the targets registry (bt_hid targets). Explicit "
                        "--adapter/--backend/--port override it.")
    p.add_argument("--adapter", default=os.environ.get("BT_HID_ADAPTER"),
                   help="pin hciN or MAC (default: auto-detect the USB dongle)")
    p.add_argument("--port", default=os.environ.get("BT_HID_ESP32_PORT"),
                   help="serial device for a serial backend (e.g. --backend esp32 --port /dev/ttyUSB1)")
    p.add_argument("--pico-port", default=os.environ.get("BT_HID_PICO_PORT"),
                   help="pico backend: serial device (default: auto-detect the Pico's /dev/ttyACM*)")
    p.add_argument("--host", default=os.environ.get("BT_HID_NET_HOST"),
                   help="network backend host (mDNS name or IP), e.g. --backend net "
                        "--host hidbridge-esp32.local")
    p.add_argument("--layout", default=os.environ.get("BT_HID_LAYOUT", R.DEFAULT_LAYOUT),
                   choices=sorted(R.LAYOUTS),
                   help="keyboard layout the TARGET host uses ('us' default, 'da' Danish, 'uk' British). Decides "
                        "which scancode+modifier each character maps to for type/key/unlock (Mode 2). "
                        "For --rpc/Mode 1 the board owns the layout, set PER DEVICE with its `layout` "
                        "method / web UI.")
    p.add_argument("--rpc", action="store_true", default=bool(os.environ.get("BT_HID_RPC")),
                   help="Mode 1: tell the BOARD what to do (JSON-RPC); it builds the HID reports "
                        "itself, instead of the host building frames. Needs an MCU backend "
                        "(--backend esp32/pico/net + --port/--host).")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("adapters", help="show detected adapters + which dongle is used")
    sp.set_defaults(func=cmd_adapters)

    sp = sub.add_parser("preflight", help="audit adapters + warn on same-MAC / no-dongle hazards")
    sp.set_defaults(func=cmd_preflight)

    sp = sub.add_parser("pair-mode", help="dongle discoverable + advertising as a keyboard")
    sp.add_argument("--name", default=os.environ.get("BT_HID_NAME"),
                    help="advertised name (default: HIDBRIDGE-dongle-<hciN>, per-adapter)")
    sp.add_argument("--force-builtin", action="store_true")
    sp.set_defaults(func=cmd_pair_mode)

    sp = sub.add_parser("net-provision",
                        help="set the network auth token on an Ethernet board (over USB serial)")
    sp.add_argument("--esp32-port", default=os.environ.get("BT_HID_ESP32_PORT"),
                    help="the board's USB serial device (default: autodetect the ESP32 bridge)")
    sp.add_argument("--token", help="the shared secret (>=8 chars); default: read/create "
                                    "~/.config/bt_hid/net.token")
    sp.set_defaults(func=cmd_net_provision)

    sp = sub.add_parser("net-status",
                        help="show the board's Ethernet/TCP state (link, IP, mDNS, token, clients)")
    sp.set_defaults(func=cmd_net_status)

    sp = sub.add_parser("devices", help="list bonded devices on the dongle")
    sp.set_defaults(func=cmd_devices)

    sp = sub.add_parser("connect", help="target a bonded device for reconnect")
    sp.add_argument("target", nargs="?", help="name/MAC (default: the --device bond)")
    sp.set_defaults(func=cmd_connect)

    sp = sub.add_parser("disconnect", help="drop the active BLE link")
    sp.add_argument("target", nargs="?")
    sp.set_defaults(func=cmd_disconnect)

    sp = sub.add_parser("wake", help="wake the connected device")
    sp.set_defaults(func=cmd_wake)

    sp = sub.add_parser("unlock", help="type the passcode at the lock screen (never logged)")
    sp.add_argument("passcode")
    sp.add_argument("--no-enter", action="store_true")
    sp.set_defaults(func=cmd_unlock)

    sp = sub.add_parser("type", help="type text")
    sp.add_argument("text")
    sp.set_defaults(func=cmd_type)

    sp = sub.add_parser("key", help="press a named key")
    sp.add_argument("name")
    sp.add_argument("--mod", type=lambda x: int(x, 0), default=0, help="modifier byte (e.g. 0x08=GUI)")
    sp.set_defaults(func=cmd_key)

    sp = sub.add_parser("move", help="move the pointer (relative)")
    sp.add_argument("dx", type=int)
    sp.add_argument("dy", type=int)
    sp.set_defaults(func=cmd_move)

    sp = sub.add_parser("click", help="click a mouse button")
    sp.add_argument("button", nargs="?", default="left",
                    choices=["left", "right", "middle", "back", "forward", "4", "5"])
    sp.set_defaults(func=cmd_click)

    sp = sub.add_parser("tap-trust", help="helper: nudge to Trust button + click (approx)")
    sp.set_defaults(func=cmd_tap_trust)

    sp = sub.add_parser("status", help="daemon + link status")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("targets", help="list/add/remove device->backend target mappings")
    sp.set_defaults(func=cmd_targets_list)          # bare `targets` lists
    tsub = sp.add_subparsers(dest="targets_action")
    tl = tsub.add_parser("list", help="list registered devices + resolved backend/warnings")
    tl.set_defaults(func=cmd_targets_list)
    ta = tsub.add_parser("add", help="add/overwrite a device mapping")
    ta.add_argument("name", help="friendly name, e.g. mini6")
    ta.add_argument("target", help="HidSession target: hciN / MAC / 'pico:/dev/ttyACM0' / ...")
    ta.add_argument("bond", help="target-device identity: MAC / BLE-name substring / path suffix")
    ta.add_argument("--note", default="", help="optional human note")
    ta.set_defaults(func=cmd_targets_add)
    tr = tsub.add_parser("remove", help="remove a device mapping")
    tr.add_argument("name")
    tr.set_defaults(func=cmd_targets_remove)
    return p


def _init_backend(args):
    """If a non-dongle backend is selected, build its transport once and route report cmds to it.

    Backend-agnostic: 'esp32', 'pico', or any future 'transport_<name>' rides make_transport().
    The serial device comes from --port (generic) or --pico-port (pico alias); if neither is
    given, each transport falls back to its own env var + hardware autodetect.
    """
    global _ALT_TRANSPORT, _BACKEND, _RPC_CLIENT
    _BACKEND = (getattr(args, "backend", None) or "dongle").lower()

    # Mode 1 (--rpc): drive the board over JSON-RPC; it builds the reports. Needs an MCU/net target.
    if getattr(args, "rpc", False):
        from hidbridge import rpc
        if _BACKEND == "net":
            host = getattr(args, "host", None) or os.environ.get("BT_HID_NET_HOST")
            target = "net:" + host if host else "net:"
        else:
            fam = _BACKEND if _BACKEND in ("esp32", "pico") else "esp32"
            port = getattr(args, "port", None) or getattr(args, "pico_port", None)
            target = f"{fam}:{port}" if port else fam
        try:
            _RPC_CLIENT = rpc.open_client(target)
        except Exception as e:  # noqa: BLE001
            print(f"error: --rpc (Mode 1) client unavailable: {e}", file=sys.stderr)
            sys.exit(2)
        return

    if _BACKEND in ("dongle", "daemon", "ble", "socket"):
        _ALT_TRANSPORT = None
        return
    from hidbridge.controller import make_transport
    opts = {}
    if _BACKEND == "net":
        # network backend: addressed by host (mDNS name or IP), not a serial port. --host wins, then
        # a --port that is really a host:port (kept for symmetry), then BT_HID_NET_HOST.
        host = getattr(args, "host", None) or os.environ.get("BT_HID_NET_HOST")
        if host:
            opts["host"] = host
    else:
        port = getattr(args, "port", None) or getattr(args, "pico_port", None)
        if port:
            opts["port"] = port
    try:
        _ALT_TRANSPORT = make_transport(_BACKEND, **opts)
    except Exception as e:  # noqa: BLE001
        print(f"error: {_BACKEND} backend unavailable: {e}", file=sys.stderr)
        sys.exit(2)


def _resolve_control_sock(args):
    """Point the report commands at the control socket of the TARGETED dongle's daemon.

    BT_HID_SOCK pins an exact path (wins). Otherwise, for the dongle backend, resolve the adapter
    the operator targeted (--adapter / auto) and derive its per-adapter socket, so `bt_hid --adapter
    hci2 type ...` reaches the hci2 daemon. Best-effort: if the adapter cannot be resolved (e.g.
    the `adapters`/`preflight` audit commands, or nothing plugged in) we leave the default and let
    the individual command report a useful error.
    """
    global CONTROL_SOCK
    if os.environ.get("BT_HID_SOCK"):
        return
    if _ALT_TRANSPORT is not None:
        return
    try:
        info = A.resolve_dongle(getattr(args, "adapter", None))
    except (A.NoDongleError, A.AmbiguousDongleError, A.BuiltinRefusedError):
        return
    CONTROL_SOCK = N.control_sock(A.adapter_slug(info))


def main(argv=None):
    args = build_parser().parse_args(argv)
    _apply_device(args)          # fold --device NAME (registry) into adapter/backend/port/bond
    _init_backend(args)
    _resolve_control_sock(args)
    args.func(args)


if __name__ == "__main__":
    main()

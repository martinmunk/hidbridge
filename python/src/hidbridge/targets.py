"""bt_hid target registry -- map a friendly iOS-device name to a backend target + bond identity.

the maintainer's ask (task #34): "some way to switch the bt device to control a specific paired device" --
so a test or a user can say `bt_hid --device mini6 click back` and NOT have to remember that mini6
lives on `hci1` bonded as `mini6`, while mini5 lives on `hci2`. This module is the small persisted
map behind that + a PURE resolver:

    friendly name  ->  (HidSession target string, bond identity)
    "mini6"        ->  ("hci1", "mini6")            # a BlueZ dongle
    "labpad"       ->  ("pico:/dev/ttyACM0", "AA:BB:CC:..")  # an MCU backend

`target` is exactly the `HidSession(target, ...)` selector (see conn.make_link_backend): hciN / a
MAC / a USB-path / None for the dongle, or "pico:PORT" / "esp32:PORT" / "<name>:<arg>" for an MCU.
`bond` is the iOS-device identity passed to `HidSession(target, bond)` -- a MAC / a BLE-name
substring / a BlueZ object-path suffix (the same grammar BluezLinkManager.find_device accepts).

THE ROUTING-AMBIGUITY WARNING (why this module carries weight): the two wsb TP-Link UB500 dongles
SHARE a BD address (E8:48:B8:C8:20:00, not reprogrammable), and even a single BlueZ dongle exposes
ONE BLE identity for ALL its bonds. So a dongle backend CANNOT be steered to "the right iPad" by
identity -- it relies on exclusive-connect + a distinct bond, and only ONE iPad at a time. When two
registered devices both sit on the (identity-less) dongle family, `resolve()` surfaces a clear
warning naming the collision, and points at the clean fix: per-device-identity MCU backends (Pico/
ESP32 pick the advertising address per bond) or genuinely separate dongles. MCU-backed devices each
own their identity, so they never trip the warning.

Everything here is PURE (JSON in/out + string parsing; backend traits are read from a hardware-free
LinkBackend construction), so the registry + resolver + warning unit-test offline with no hardware.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from . import conn as _conn
from . import naming as _naming

# Sane project defaults seeded when no config file exists yet. The two iPads we actually drive:
# mini6 (iPadOS-26, paints) and mini5 (iPadOS 18.7 / 26.5.2). These reflect the LIVE wsb rig (verified
# 2026-07-13 by an esp32/pico probe + the on-disk BlueZ bonds; see docs/testbed/inventory.yaml):
# mini6's PRIMARY board is the ESP32 (wsb-hid-esp32) on the CP2102 bridge /dev/ttyUSB1, and mini5's
# PRIMARY board is the Raspberry Pi Pico 2 W (wsb-hid-pico) on /dev/ttyACM0. Both are MCU backends, so
# they never trip the shared-MAC dongle warning and can be driven at the same time.
#
# MULTI-BOARD ROUTES (2026-07-14, task: both boards bonded to mini5 + per-board mini5<->mini6 switch):
# an MCU board holds ITS OWN bond database in firmware, so ONE board can be bonded to BOTH iPads and
# switched between them (time-sliced -- see the firmware-capability note below). To express "each
# board can reach each iPad" through the registry, we add BOARD-QUALIFIED routes: `<ipad>-<board>`.
# So mini5 is reachable on the Pico (name `mini5`, the primary) AND on the ESP32 (`mini5-esp32`);
# mini6 on the ESP32 (`mini6`, primary) AND on the Pico (`mini6-pico`). `--device mini5-esp32` then
# drives mini5 from the ESP32; switching a board between iPads = drive its two routes in turn.
#
# FIRMWARE-CAPABILITY CAVEAT (important -- the mechanism is NOT distinct simultaneous identities):
# the shipped Pico/ESP32 firmware advertises ONE fixed BLE identity per board with UNDIRECTED
# advertising and a multi-bond database (like a single-identity multi-host keyboard). It has NO
# directed-advertising / host-slot / per-bond-address command, so switching which BONDED iPad a board
# talks to is TIME-SLICED: disconnect the current central and let the desired (awake, in-range) iPad
# reconnect. It is NOT deterministic when BOTH bonded iPads are simultaneously present. The
# `per_device_identity=True` backend capability is the ROUTE to distinct identities (an MCU CAN do it
# in firmware) but the address-per-bond firmware feature is still a tracked follow-up
# (docs/bt-hid-mcu-approach.md). An operator overrides any route with `bt_hid targets add`.
#
# PER-BOND IDENTITY (host slots, 2026-07-14): each (board, iPad) route carries a `slot` = the board's
# BLE host-slot index that iPad bonds to. Slot 0 is the board's NATIVE identity (its public address,
# so an EXISTING bond survives the per-bond-identity reflash); slots >=1 are distinct static-random
# identities the firmware derives. Because the ESP32 (NimBLE) can hold MULTIPLE centrals connected AT
# ONCE on distinct identities (capability simultaneous_identities=True), mini6 (slot 0) + mini5
# (slot 1) can be held on ONE ESP32 SIMULTANEOUSLY -- no time-slice. The Pico maps distinct slots too
# but its wrapper is single-connection, so it switches slots time-sliced (see the resolver methods).
DEFAULT_ENTRIES: dict[str, dict] = {
    "mini6": {"target": "esp32:/dev/ttyUSB1", "bond": "mini6", "slot": 0,
              "note": "iPad mini 6 (iPadOS 26); ESP32 wsb-hid-esp32 /dev/ttyUSB1 slot 0 (native id)"},
    "mini5": {"target": "pico:/dev/ttyACM0", "bond": "mini5", "slot": 0,
              "note": "iPad mini 5 (iPadOS 26.5.2); Pico 2 W wsb-hid-pico /dev/ttyACM0 slot 0 (native id)"},
    "mini5-esp32": {"target": "esp32:/dev/ttyUSB1", "bond": "mini5", "slot": 1,
                    "note": "mini5 on the ESP32 slot 1 (static-random id) -> ESP32 holds mini5+mini6 at once"},
    "mini6-pico": {"target": "pico:/dev/ttyACM0", "bond": "mini6", "slot": 1,
                   "note": "mini6 on the Pico slot 1 (static-random id); time-sliced with mini5 on the Pico"},
}


# --------------------------------------------------------------------------- #
# config path (mirrors ~/.config/bt_hid/ where idevice.pin lives)
# --------------------------------------------------------------------------- #
def config_path() -> str:
    """Path of the targets registry JSON.

    Priority: BT_HID_TARGETS (pin an exact file, wins) -> $XDG_CONFIG_HOME/bt_hid/targets.json ->
    ~/.config/bt_hid/targets.json. The target map is NOT secret (unlike idevice.pin), so it lives
    beside the other bt_hid config as a plain world-readable file.
    """
    env = os.environ.get("BT_HID_TARGETS")
    if env:
        return env
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "bt_hid", "targets.json")


# --------------------------------------------------------------------------- #
# data model
# --------------------------------------------------------------------------- #
@dataclass
class TargetEntry:
    """One registered device: a friendly name -> a HidSession target + a bond identity + host slot."""

    name: str
    target: str          # HidSession target selector (hciN / MAC / "pico:PORT" / ...)
    bond: str            # iOS-device identity for HidSession (MAC / name substring / path suffix)
    slot: int = 0        # the board's BLE host-slot this iPad bonds to (0 = native id; >=1 = extra id)
    note: str = ""

    def to_dict(self) -> dict:
        return {"target": self.target, "bond": self.bond, "slot": self.slot, "note": self.note}

    def identity_name(self) -> str:
        """Target-scoped advertised BLE name for this (board, iPad) identity (wsb-<board>-<target>)."""
        return _naming.slot_identity_name(_backend_family(self.target), self.bond)


@dataclass
class ResolvedTarget:
    """The resolver's answer for one device: everything needed to open a HidSession + any warning."""

    name: str
    target: str
    bond: str
    backend: str                       # parsed backend family: "dongle"/"pico"/"esp32"/...
    per_device_identity: bool | None   # backend capability (False=dongle, True=MCU, None=unknown MCU)
    slot: int = 0                      # the board's BLE host-slot for this iPad
    identity_name: str = ""            # target-scoped advertised name (wsb-<board>-<target>)
    simultaneous_identities: bool | None = None  # can this board hold multiple identities AT ONCE?
    shared_mac_warning: str | None = None
    note: str = ""


# --------------------------------------------------------------------------- #
# backend introspection (hardware-free: constructing a LinkBackend touches no hardware)
# --------------------------------------------------------------------------- #
def _backend_family(target: str) -> str:
    """Parsed backend family for a target string ('pico:/dev/x' -> 'pico'; 'hci1'/MAC -> 'dongle')."""
    backend, _arg = _conn._parse_target(target)
    return "dongle" if backend in _conn._DONGLE_ALIASES else backend


def _per_device_identity(target: str) -> bool | None:
    """The backend's per_device_identity capability, read from a hardware-free construction.

    DongleLinkBackend/PicoLinkBackend/etc. only STORE their args at construction (hardware is not
    touched until prepare()), so this is safe to call offline. Unknown/broken targets -> None.
    """
    try:
        return _conn.make_link_backend(target).capabilities.get("per_device_identity")
    except Exception:  # noqa: BLE001  (an exotic backend that probes at construction -> unknown)
        return None


def _simultaneous_identities(target: str) -> bool | None:
    """The backend's simultaneous_identities capability (can it hold >1 identity connected AT ONCE?).

    True = ESP32 (NimBLE, MAX_CONNECTIONS>1 + per-conn notify); False = Pico (single-conn wrapper) /
    dongle (one adapter identity); None = unknown/broken target. Read hardware-free from a
    LinkBackend construction, like _per_device_identity.
    """
    try:
        return _conn.make_link_backend(target).capabilities.get("simultaneous_identities")
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #
class TargetRegistry:
    """A load/modify/save map of friendly device name -> TargetEntry, plus the resolver."""

    def __init__(self, entries: dict[str, TargetEntry] | None = None, path: str | None = None):
        self.entries: dict[str, TargetEntry] = dict(entries or {})
        self.path = path

    # ---- persistence ----
    @classmethod
    def load(cls, path: str | None = None) -> "TargetRegistry":
        """Load the registry from `path` (default config_path()). A MISSING file seeds the defaults."""
        path = path or config_path()
        if not os.path.exists(path):
            entries = {name: TargetEntry(name=name, **spec) for name, spec in DEFAULT_ENTRIES.items()}
            return cls(entries, path=path)
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        entries = {}
        for name, spec in raw.items():
            entries[name] = TargetEntry(
                name=name, target=spec.get("target", ""), bond=spec.get("bond", ""),
                slot=int(spec.get("slot", 0) or 0), note=spec.get("note", ""))
        return cls(entries, path=path)

    def save(self, path: str | None = None) -> str:
        """Write the registry as plain JSON (0644 -- not secret). Returns the path written."""
        path = path or self.path or config_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        data = {name: e.to_dict() for name, e in sorted(self.entries.items())}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
        os.chmod(path, 0o644)
        self.path = path
        return path

    # ---- mutation ----
    def add(self, name: str, target: str, bond: str, slot: int = 0, note: str = "") -> TargetEntry:
        """Add or overwrite a device entry. name/target/bond are required; slot defaults to 0."""
        if not (name and name.strip()):
            raise ValueError("device name must not be blank")
        if not (target and target.strip()):
            raise ValueError("target must not be blank (e.g. 'hci1', 'pico:/dev/ttyACM0')")
        if not (bond and bond.strip()):
            raise ValueError("bond identity must not be blank (a MAC / BLE-name substring)")
        if int(slot) < 0:
            raise ValueError("slot must be a non-negative integer")
        e = TargetEntry(name=name.strip(), target=target.strip(), bond=bond.strip(),
                        slot=int(slot), note=(note or "").strip())
        self.entries[e.name] = e
        return e

    def remove(self, name: str) -> None:
        if name not in self.entries:
            raise KeyError(f"no such device {name!r}; known: {', '.join(self.names()) or '(none)'}")
        del self.entries[name]

    # ---- read ----
    def names(self) -> list[str]:
        return sorted(self.entries)

    def get(self, name: str) -> TargetEntry:
        try:
            return self.entries[name]
        except KeyError:
            raise KeyError(
                f"no such device {name!r}; known: {', '.join(self.names()) or '(none)'}") from None

    def items(self):
        return self.entries.items()

    # ---- resolver ----
    def _shared_mac_collisions(self, name: str) -> list[str]:
        """OTHER registered devices that share the identity-less dongle family with `name`.

        The dongle family (per_device_identity is False) cannot be steered to a specific iPad by
        identity -- and the two UB500 dongles even share a BD address -- so any two dongle-backed
        devices are a routing-ambiguity hazard. MCU backends (per_device_identity True/None) own a
        per-bond identity and never collide.
        """
        this = self.entries[name]
        if _per_device_identity(this.target) is not False:
            return []
        return [
            other for other, e in self.entries.items()
            if other != name and _per_device_identity(e.target) is False
        ]

    def resolve(self, name: str) -> ResolvedTarget:
        """Resolve a friendly device name to a ResolvedTarget (target + bond + backend + warning)."""
        e = self.get(name)
        backend = _backend_family(e.target)
        pdi = _per_device_identity(e.target)
        warning = None
        collisions = self._shared_mac_collisions(name)
        if collisions:
            warning = (
                f"device {name!r} uses the {backend} backend, which has NO per-device BLE identity "
                f"(the wsb UB500 dongles even share one BD address). It shares that identity-less "
                f"dongle family with: {', '.join(sorted(collisions))}. Only ONE of these can be "
                f"driven at a time and routing relies on exclusive-connect + the distinct bond "
                f"({e.bond!r}); simultaneous multi-iPad control needs per-device-identity MCU "
                f"backends (pico:/esp32:) or genuinely separate, distinctly-addressed dongles.")
        return ResolvedTarget(name=name, target=e.target, bond=e.bond, backend=backend,
                              per_device_identity=pdi, slot=e.slot,
                              identity_name=e.identity_name(),
                              simultaneous_identities=_simultaneous_identities(e.target),
                              shared_mac_warning=warning, note=e.note)

    def resolve_all(self) -> dict[str, ResolvedTarget]:
        return {name: self.resolve(name) for name in self.names()}

    # ---- per-bond identity: which routes live on ONE board, and can it hold them AT ONCE? ----
    def routes_for_board(self, target: str) -> list[ResolvedTarget]:
        """Every registered route on the SAME physical board as `target` (same backend + device arg).

        e.g. routes_for_board('esp32:/dev/ttyUSB1') -> the mini6 (slot 0) + mini5 (slot 1) routes on
        that ESP32. Matches on the exact target string so two boards of the same family don't merge.
        """
        return [self.resolve(n) for n in self.names() if self.entries[n].target == target]

    def simultaneous_hold_plan(self, target: str) -> list[ResolvedTarget]:
        """The set of routes on board `target` that can be held CONNECTED simultaneously.

        One route per DISTINCT (bond, slot): a board holds each bonded iPad on its own slot/identity.
        Returned sorted by slot. Meaningful when can_hold_simultaneously(target) is True (ESP32); for
        a single-connection board it still lists the distinct-slot routes (they are time-sliced).
        """
        seen: dict[tuple, ResolvedTarget] = {}
        for r in self.routes_for_board(target):
            seen.setdefault((r.bond, r.slot), r)
        return sorted(seen.values(), key=lambda r: r.slot)

    def can_hold_simultaneously(self, target: str) -> bool:
        """True iff board `target` can hold >=2 distinct-identity iPads CONNECTED at the same time.

        Requires the backend's simultaneous_identities capability AND >=2 registered routes on that
        board with DISTINCT slots + DISTINCT bonds. The ESP32 (NimBLE) qualifies; the Pico does not
        (its wrapper is single-connection) even though it maps distinct slots.
        """
        if _simultaneous_identities(target) is not True:
            return False
        plan = self.simultaneous_hold_plan(target)
        return len({r.bond for r in plan}) >= 2 and len({r.slot for r in plan}) >= 2

    def routes_for_bond(self, bond: str) -> list[ResolvedTarget]:
        """Every registered route that reaches the iPad identified by `bond`, resolved.

        The switching mechanism for the MCU boards is "one iPad, several boards bonded to it": this
        returns all entries whose bond identity equals `bond` (e.g. every board bonded to mini5),
        so a caller / test can assert BOTH boards enumerate a route to that iPad. Sorted by name."""
        return [self.resolve(name) for name in self.names() if self.entries[name].bond == bond]

    def backends_for_bond(self, bond: str) -> set[str]:
        """The set of distinct backend families (e.g. {'pico','esp32'}) bonded to `bond`."""
        return {r.backend for r in self.routes_for_bond(bond)}

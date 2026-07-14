"""bt_hid per-adapter naming -- PURE, importable, no bluetooth deps.

The ORIGINAL single-daemon design hard-coded ONE control socket (`/run/bt_hid.sock`), ONE D-Bus
application object path (`/org/martinmunk/bthid`), ONE agent path, and ONE advertised name. So two
`ble_hid` daemons on two different dongles COLLIDE: both try to export the same D-Bus objects on the
system bus and bind the same Unix socket -- the second fails or corrupts the first.

This module derives a DISTINCT set of runtime names for each adapter from a stable per-adapter
`slug`, so N daemons can run side by side (dongleA<->iPadA and dongleB<->iPadB concurrently), each
fully isolated.

Why `hciN` is the slug (not the BD address): the wsb TP-Link UB500 dongles SHARE a BD address
(a known UB500 V1 defect; not runtime-reprogrammable), so the address is NOT unique. The kernel
still assigns each physical dongle a DISTINCT `hciN`, and every BlueZ D-Bus object is already
per-adapter (`/org/bluez/hciN/...`). Runtime artifacts (sockets in /run, D-Bus objects on the
session/system bus) are per-boot, and `hciN` is stable + collision-free within a boot -- so keying
isolation on `hciN` is correct even when addresses collide. Operators still TARGET a dongle by a
stable id (MAC / USB path / product / hciN) via `adapter.resolve_dongle`, which resolves to the
concrete `hciN` at runtime; the slug is then that hci.

These functions are pure (string in, string out) so they unit-test offline.
"""

from __future__ import annotations

import os
import re

# D-Bus object-path root for our GATT application + pairing agent. A path element must match
# [A-Za-z0-9_], so the slug is sanitised before it is appended.
DBUS_ROOT = "/org/martinmunk/bthid"


def _sanitize(slug: str) -> str:
    """Reduce an adapter slug to a filesystem- and D-Bus-path-safe token (e.g. 'hci1')."""
    s = re.sub(r"[^0-9A-Za-z]", "_", (slug or "").strip())
    return s or "default"


def runtime_dir() -> str:
    """Base dir for per-adapter control sockets (a subdir so we never clash with unrelated files).

    Root daemons land in /run/bt_hid; an unprivileged run uses $XDG_RUNTIME_DIR/bt_hid.
    """
    base = os.environ.get("XDG_RUNTIME_DIR") or "/run"
    return os.path.join(base, "bt_hid")


def control_sock(slug: str, *, env_override: str | None = None) -> str:
    """Path of the daemon's Unix control socket for a given adapter slug.

    An explicit `env_override` (BT_HID_SOCK) always wins -- it lets a test or an operator pin an
    exact socket path (and preserves the historical single-socket behaviour). Otherwise the path is
    derived per-adapter so daemons on different dongles never collide.
    """
    if env_override:
        return env_override
    return os.path.join(runtime_dir(), f"{_sanitize(slug)}.sock")


def app_path(slug: str) -> str:
    """D-Bus object path of this adapter's GATT application (unique per adapter)."""
    return f"{DBUS_ROOT}/{_sanitize(slug)}"


def agent_path(slug: str) -> str:
    """D-Bus object path of this adapter's pairing agent (unique per adapter)."""
    return f"{DBUS_ROOT}/{_sanitize(slug)}/agent"


def slot_identity_name(board: str, target: str) -> str:
    """Advertised BLE name for a per-bond IDENTITY (host slot), scoped to its TARGET iPad.

    A board presents a DISTINCT identity per bonded iPad; each identity advertises a name that names
    the iPad it is FOR, so in an iPad's Bluetooth settings you can tell which entry is meant for it
    (the plain `wsb-hid-esp32` only named the host). Convention: `wsb-<board>-<target>`, e.g.
    `wsb-esp32-mini6`, `wsb-pico-mini5`. Both components are sanitised (BLE LocalName is free-form
    but we keep it to a safe token). `board` is the backend family (esp32/pico); `target` is the
    iPad's friendly bond name.
    """
    return f"wsb-{_sanitize(board).lower()}-{_sanitize(target).lower()}"


def default_name(slug: str) -> str:
    """Default advertised BLE name for this adapter's daemon (distinct per adapter).

    Two daemons must not advertise the same LocalName (the iPad would show one ambiguous entry),
    so the default embeds the slug: `wsb-hid-hci1`, `wsb-hid-hci2`, ... An operator can still pin
    any name via --name / BT_HID_NAME.
    """
    return f"wsb-hid-{_sanitize(slug)}"

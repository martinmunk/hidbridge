"""bt_hid -- Linux Bluetooth-HID peripheral toolbox (keyboard + mouse to an iPad/iPhone).

Import `bt_hid.reports` for the PURE report layer (stdlib only). The emulator / adapter modules
pull in bluetooth deps (AF_BLUETOOTH sockets, dbus) that only exist under the system python, so
they are NOT imported here -- import them explicitly when needed.

`HidBridge` is the driving entry point (`with HidBridge(...) as bridge: bridge.connect(...) as s`),
with `session()` a single-host shorthand. The `bridge` module imports only the stdlib-clean
controller/reports layers at load time (the real serial/net/dongle board drivers are pulled in
lazily), so `import hidbridge` stays free of bluetooth/serial deps.
"""

from .bridge import (  # noqa: E402
    CapacityError,
    ConnectTimeout,
    HidBridge,
    HidbridgeError,
    HidSession,
    NotBonded,
    Unsupported,
    session,
)


def discover():
    """List plugged-in hidbridge boards: [{port, board, fw, device}] (vendor-gated + handshake).

    Lazy-imports the serial drivers so `import hidbridge` stays free of serial/dbus deps.
    """
    from .board import discover as _discover
    return _discover()


__all__ = [
    "HidBridge", "HidSession", "session", "discover",
    "HidbridgeError", "NotBonded", "ConnectTimeout", "Unsupported", "CapacityError",
]

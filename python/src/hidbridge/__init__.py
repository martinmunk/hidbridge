"""bt_hid -- Linux Bluetooth-HID peripheral toolbox (keyboard + mouse to an iPad/iPhone).

Import `bt_hid.reports` for the PURE report layer (stdlib only). The emulator / adapter modules
pull in bluetooth deps (AF_BLUETOOTH sockets, dbus) that only exist under the system python, so
they are NOT imported here -- import them explicitly when needed.

`hidbridge.open()` is the driving entry point (Bridge -> connect() -> Session). The `bridge` module
imports only the stdlib-clean controller/reports layers at load time (the real serial/net/dongle
board drivers are pulled in lazily), so `import hidbridge` stays free of bluetooth/serial deps.
"""

from .bridge import (  # noqa: E402
    Bridge,
    CapacityError,
    ConnectTimeout,
    HidbridgeError,
    NotBonded,
    Session,
    Unsupported,
    open,
)

__all__ = [
    "open", "Bridge", "Session",
    "HidbridgeError", "NotBonded", "ConnectTimeout", "Unsupported", "CapacityError",
]

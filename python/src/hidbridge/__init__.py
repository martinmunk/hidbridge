"""bt_hid -- Linux Bluetooth-HID peripheral toolbox (keyboard + mouse to an iPad/iPhone).

Import `bt_hid.reports` for the PURE report layer (stdlib only). The emulator / adapter modules
pull in bluetooth deps (AF_BLUETOOTH sockets, dbus) that only exist under the system python, so
they are NOT imported here -- import them explicitly when needed.
"""

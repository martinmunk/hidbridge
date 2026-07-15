# External references

Links gathered during the architecture discussion (2026-07-15) about where HID logic should live
(smart firmware vs thin "radio" pipe) and prior art. See also `docs/prior-art.md` for the full survey.

## HID emitter architecture (host-builds-reports vs smart firmware)

- PiKVM USB-OTG HID (DeepWiki, third-party auto-generated): https://deepwiki.com/pikvm/pikvm/7.1-usb-otg-hid
- PiKVM Bluetooth HID docs (official; note: BT mouse is relative-only): https://docs.pikvm.org/bluetooth_hid/
- PiKVM docs index: https://docs.pikvm.org/
- PiKVM `kvmd` daemon (primary source; HID under `kvmd/plugins/hid/`): https://github.com/pikvm/kvmd
- serial-hid-kvm (clean "host builds intent -> dumb CH9329 serial->USB-HID chip"): https://github.com/sunasaji/serial-hid-kvm
- kvm-serial (CH9329 / CH9350L serial HID bridge): https://github.com/sjmf/kvm-serial
- TinyPilot (WebSocket -> USB HID gadget): https://github.com/tiny-pilot/tinypilot

## Smart-firmware model (descriptor + keymap baked in, high-level commands)

- ESP32-BLE-Keyboard: https://github.com/T-vK/ESP32-BLE-Keyboard
- asterics/esp32_mouse_keyboard (UART `$`-command protocol + multi-host bond switching): https://github.com/asterics/esp32_mouse_keyboard

## iPad-control use case

- VKCOM/devicehub (browser -> ESP32 BLE HID mouse -> iOS AssistiveTouch; documents relative dead-reckoning): https://github.com/VKCOM/devicehub

## Provenance note

The PiKVM USB path is verified as "smart host, thin emitter" (Linux USB Gadget subsystem, descriptor
in gadget config, software writes report bytes to /dev/hidg0). The PiKVM *Bluetooth* internal
architecture was NOT verified from the docs; only its relative-only limitation is confirmed. The other
project characterizations came from the research subagent (see docs/prior-art.md) and are not all
independently re-verified.

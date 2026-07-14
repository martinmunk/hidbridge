#!/usr/bin/env bash
# Bring the USB Bluetooth dongle up as a discoverable BLE HID keyboard so an iPad/iPhone can pair.
# Runs the daemon in the foreground (Ctrl-C to stop). Uses sudo (BlueZ adapter config needs root).
# Scoped to the DONGLE only; refuses the built-in adapter unless BT_HID_FORCE_BUILTIN=1.
#
# Multi-adapter: set BT_HID_ADAPTER=hciN to pin a specific dongle (default: auto-detect the sole
# dongle -- errors if ambiguous; run `tools/bt_hid/bt_hid preflight`). The advertised name defaults
# to HIDBRIDGE-dongle-<hciN> (per-adapter) unless BT_HID_NAME is set.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# --adapter is a PARENT (pre-subcommand) option; --name/--force-builtin are pair-mode options.
PARENT=()
[ -n "${BT_HID_ADAPTER:-}" ] && PARENT+=(--adapter "$BT_HID_ADAPTER")
SUB=(pair-mode)
[ -n "${BT_HID_NAME:-}" ] && SUB+=(--name "$BT_HID_NAME")
[ "${BT_HID_FORCE_BUILTIN:-0}" = "1" ] && SUB+=(--force-builtin)
echo "Starting BLE HID keyboard on the dongle (${BT_HID_ADAPTER:-auto}); pair from the iPad now."
exec sudo -n BT_HID_ADAPTER="${BT_HID_ADAPTER:-}" BT_HID_NAME="${BT_HID_NAME:-}" \
     /usr/bin/python3 "$HERE/bt_hid" "${PARENT[@]}" "${SUB[@]}"

#!/usr/bin/env bash
# Manage the bt_hid BLE HID daemon as a systemd unit (persists across shells + reboots).
# Branch-tracked (unit template = bt-hid.service.in) so it is reviewable + cleanly removable --
# NOT a hand-installed artifact. The unit is a templated instance bt-hid@<ADAPTER>.service: the
# instance parameter is the ADAPTER id (hciN), so you can run one daemon PER dongle concurrently
# (bt-hid@hci1 to iPadA + bt-hid@hci2 to iPadB). Each instance gets its own per-adapter control
# socket + D-Bus objects + advertised name (see tools/bt_hid/naming.py).
#
# Usage:
#   sudo tools/bt_hid/service.sh install [ADAPTER]   # install + enable + start (default: auto dongle)
#   sudo tools/bt_hid/service.sh start   [ADAPTER]
#   sudo tools/bt_hid/service.sh stop    [ADAPTER]
#   sudo tools/bt_hid/service.sh status  [ADAPTER]
#   sudo tools/bt_hid/service.sh logs    [ADAPTER]
#   sudo tools/bt_hid/service.sh uninstall [ADAPTER] # stop + disable + remove the unit file
#
# ADAPTER defaults to the auto-resolved single dongle (errors if ambiguous -- run `bt_hid preflight`).
# For a quick throwaway run without installing a persistent unit, use:  tools/bt_hid/pair-mode.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
UNIT_TEMPLATE="/etc/systemd/system/bt-hid@.service"
CMD="${1:-status}"
# Resolve the adapter instance: explicit arg, else the auto-detected sole dongle's hciN.
default_adapter() {
  /usr/bin/python3 "$HERE/bt_hid" adapters 2>/dev/null | awk '/<-- USED/{print $1; exit}'
}
ADAPTER="${2:-$(default_adapter || true)}"
[ -n "$ADAPTER" ] || { echo "no adapter given and none auto-resolved; run: $HERE/bt_hid preflight"; exit 2; }
INSTANCE="bt-hid@${ADAPTER}.service"

need_root() { [ "$(id -u)" = "0" ] || { echo "run with sudo"; exit 1; }; }

install_unit() {
  need_root
  sed "s#__REPO__#${REPO}#g" "$HERE/bt-hid.service.in" > "$UNIT_TEMPLATE"
  systemctl daemon-reload
  echo "installed $UNIT_TEMPLATE (REPO=$REPO)"
}

case "$CMD" in
  install)
    install_unit
    systemctl enable --now "$INSTANCE"
    systemctl --no-pager status "$INSTANCE" | head -6 || true
    ;;
  start)   need_root; [ -f "$UNIT_TEMPLATE" ] || install_unit; systemctl start "$INSTANCE"; systemctl --no-pager status "$INSTANCE" | head -5 || true ;;
  stop)    need_root; systemctl stop "$INSTANCE" || true ;;
  status)  systemctl --no-pager status "$INSTANCE" | head -8 || true ;;
  logs)    journalctl -u "$INSTANCE" --no-pager -n "${3:-40}" ;;
  uninstall)
    need_root
    systemctl disable --now "$INSTANCE" 2>/dev/null || true
    rm -f "$UNIT_TEMPLATE"
    systemctl daemon-reload
    echo "removed $UNIT_TEMPLATE"
    ;;
  *) echo "usage: $0 {install|start|stop|status|logs|uninstall} [ADAPTER]"; exit 2 ;;
esac

#!/usr/bin/env bash
# Build + flash the wsb-hid-esp32 firmware with PlatformIO.
#
# One-time toolchain (idempotent -- this script installs what is missing):
#   - PlatformIO Core (`pio`), installed to ~/.local/bin via `uv tool install platformio`
#     (falls back to pipx / pip --user). The pioarduino platform + ESP32 toolchain + NimBLE-Arduino
#     are fetched by PlatformIO itself on the first `pio run` (see platformio.ini).
#
# Usage:
#   firmware/esp32/build_flash.sh                 # build the default env (esp32dev)
#   firmware/esp32/build_flash.sh /dev/ttyUSB1    # build + flash to that port
#   PIO_ENV=esp32-s3-devkitc-1 firmware/esp32/build_flash.sh /dev/ttyACM0
#
# The default env (esp32dev = classic ESP32) matches the board detected on wsb. For another ESP32
# variant pass PIO_ENV=<env> (envs are defined in platformio.ini). If a PORT is given the chip is
# probed with esptool to warn on an env/chip mismatch.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-}"
PIO_ENV="${PIO_ENV:-esp32dev}"
export PATH="$HOME/.local/bin:$PATH"

if ! command -v pio >/dev/null 2>&1; then
  echo "[esp32] installing PlatformIO Core (pio) ..."
  if command -v uv >/dev/null 2>&1; then
    uv tool install platformio
  elif command -v pipx >/dev/null 2>&1; then
    pipx install platformio
  else
    python3 -m pip install --user platformio
  fi
fi

cd "$HERE"

if [ -n "$PORT" ]; then
  CHIP="$(uv run --with esptool esptool --port "$PORT" chip_id 2>/dev/null \
            | grep -iE 'Chip type|Detecting chip' | grep -oiE 'ESP32-?[A-Z0-9]*' | head -1 || true)"
  [ -n "$CHIP" ] && echo "[esp32] port $PORT reports chip '$CHIP' (building env $PIO_ENV)"
  echo "[esp32] building + flashing env=$PIO_ENV to $PORT ..."
  pio run -e "$PIO_ENV" -t upload --upload-port "$PORT"
  echo "[esp32] done. The board now advertises as 'wsb-hid-esp32'."
  echo "        Watch it: pio device monitor -b 115200 -p $PORT"
else
  echo "[esp32] building env=$PIO_ENV (no flash; pass a serial port to flash) ..."
  pio run -e "$PIO_ENV"
  echo "[esp32] build OK. Flash with:  $0 /dev/ttyUSB1"
fi

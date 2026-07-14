#!/usr/bin/env bash
# Build the HIDBRIDGE-pico firmware (Raspberry Pi Pico 2 W BLE HID) with PlatformIO.
#
# Compiles src/main.cpp for board rpipico2w (arduino-pico core + BTstack HOG) and leaves the
# flashable image at .pio/build/rpipico2w/firmware.uf2. This is OFFLINE-safe: it only compiles;
# it does NOT flash the Pico (flashing drops the paired iPad's BLE link -- see README + the gated
# flash step). To flash, use `pio run -t upload` (BOOTSEL) or copy the .uf2 to the BOOTSEL drive.
#
#   ./build.sh            # compile
#   ./build.sh -v         # compile, verbose
# Any extra args are passed through to `pio run`.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# Locate the PlatformIO CLI (pip install platformio, or the pio bundled in ~/.platformio).
PIO="${PIO:-pio}"
if ! command -v "$PIO" >/dev/null 2>&1; then
    if [ -x "$HOME/.platformio/penv/bin/pio" ]; then
        PIO="$HOME/.platformio/penv/bin/pio"
    else
        echo "ERROR: PlatformIO 'pio' not found. Install it: pip install platformio" >&2
        exit 1
    fi
fi

echo "Building HIDBRIDGE-pico firmware (board rpipico2w) via $PIO ..."
"$PIO" run "$@"
echo "OK -> $HERE/.pio/build/rpipico2w/firmware.uf2"

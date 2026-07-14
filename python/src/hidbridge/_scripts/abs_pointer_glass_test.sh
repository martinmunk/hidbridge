#!/usr/bin/env bash
# abs_pointer_glass_test.sh -- empirical on-GLASS test of the absolute-mouse (reportID 3) and
# digitizer (reportID 4) HID reports against a real iPad, webcam-verified.
#
# THE OPEN QUESTION: iPadOS pointer support (13.4+) is documented as RELATIVE (the AssistiveTouch
# cursor). Whether iOS honours an ABSOLUTE mouse axis and/or a HID digitizer is unknown -- this
# script drives known absolute coordinates + captures both the webcam (what landed on glass) and
# btmon (proof the reports were DELIVERED), so a negative is proven, not assumed.
#
# PRECONDITIONS (arrange via 'main' first):
#   * The dongle (hci1) is FREE (no other task using it).
#   * the maintainer has FORGOTTEN the bt_hid device on the target iPad and RE-PAIRED it -- iOS caches the
#     GATT report map per bond, so reportIDs 3+4 are invisible until a fresh pair.
#   * The bt_hid daemon is running on the dongle:  sudo tools/bt_hid/bt_hid pair-mode
#   * The iPad is connected (check: sudo tools/bt_hid/bt_hid status) and UNLOCKED to the Home screen
#     (or a known screen, e.g. Settings), so a tap has a recognizable target.
#
# Usage:
#   sudo tools/bt_hid/abs_pointer_glass_test.sh [outdir]
# Env: BT_HID=path to the bt_hid launcher, WEBCAM=path to webcam_ipad.sh, HCI=hci1 (btmon adapter).
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
BT_HID="${BT_HID:-$HERE/bt_hid}"
WEBCAM="${WEBCAM:-$ROOT/tools/webcam_ipad.sh}"
HCI="${HCI:-hci1}"
OUT="${1:-$ROOT/captures/bt_hid_abs_$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$OUT"

echo "[abs-test] output dir: $OUT"
echo "[abs-test] verifying the daemon is up on the dongle ..."
"$BT_HID" status | tee "$OUT/status.txt"

# btmon HCI capture (proof reports are delivered over the air). btmon needs root + the dongle index.
HCI_IDX="${HCI#hci}"
echo "[abs-test] starting btmon on $HCI (index $HCI_IDX) ..."
btmon --index "$HCI_IDX" -w "$OUT/btmon.btsnoop" >"$OUT/btmon.log" 2>&1 &
BTMON_PID=$!
trap 'kill "$BTMON_PID" 2>/dev/null || true' EXIT
sleep 1

snap() { "$WEBCAM" snap "$OUT/$1.jpg" >/dev/null 2>&1 && echo "[abs-test] webcam -> $OUT/$1.jpg"; }

# ---- Absolute mouse (reportID 3): does the cursor JUMP to the commanded fraction? ----
# Sweep the four corners + centre. If absolute is honoured the cursor snaps to each spot; if
# ignored it drifts relatively (or not at all). A webcam snap after each move is the verdict.
for spec in "tl:0.1:0.1" "tr:0.9:0.1" "br:0.9:0.9" "bl:0.1:0.9" "mid:0.5:0.5"; do
    name="${spec%%:*}"; rest="${spec#*:}"; x="${rest%%:*}"; y="${rest#*:}"
    echo "[abs-test] move-abs $x $y  ($name)"
    "$BT_HID" move-abs "$x" "$y" || echo "[abs-test] WARN move-abs failed"
    sleep 1.2
    snap "absmouse_$name"
done

# Absolute click at a known target (adjust the fraction to whatever recognizable control is on
# screen; 0.5 0.9 aims low-centre).
echo "[abs-test] tap (abs-click) 0.5 0.9"
"$BT_HID" tap 0.5 0.9 || echo "[abs-test] WARN tap failed"
sleep 1.5
snap "absmouse_tap_result"

# ---- Digitizer (reportID 4): does a touch report register as a real tap? ----
for spec in "mid:0.5:0.5" "low:0.5:0.85"; do
    name="${spec%%:*}"; rest="${spec#*:}"; x="${rest%%:*}"; y="${rest#*:}"
    echo "[abs-test] touch $x $y  ($name)"
    "$BT_HID" touch "$x" "$y" || echo "[abs-test] WARN touch failed"
    sleep 1.5
    snap "digitizer_$name"
done

sleep 1
kill "$BTMON_PID" 2>/dev/null || true
trap - EXIT

cat <<EOF

[abs-test] DONE.
  Webcam frames : $OUT/*.jpg   (compare absmouse_tl/tr/br/bl/mid -- did the cursor snap to corners?)
  btmon capture : $OUT/btmon.btsnoop  (decode: btmon -r "$OUT/btmon.btsnoop" | less  -- shows the
                  ATT Handle Value Notifications = the reports WERE delivered, even if iOS ignored them)
  Verdict -> record in docs/bt-hid-absolute-pointer.md (positive: calibration; negative: proven-delivered).
EOF

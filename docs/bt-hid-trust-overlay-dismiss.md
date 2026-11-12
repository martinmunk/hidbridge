# Dismissing the iPadOS "Trust This Computer?" overlay over BT HID (task #15)

When Linux (wsb) re-enumerates on an iPad's USB -- a KVM move, an NCM bring-up, a `usbmuxd`
pairing attempt (this is what the mini5 rdlink work kept triggering) -- the iPad throws the modal
**"Trust This Computer?"** alert. It blocks the whole screen until answered. Task #15 is to
detect + dismiss it from Linux via the BT-HID backends.

Tool: `python/src/hidbridge/dismiss_trust.py` (pure plan builders + a HidSession runner; `--dry-run` prints
the plan). Tests: `python/tests/test_bt_hid_dismiss_trust.py` (offline + a `BT_HID_DISMISS_LIVE`
device-gated live test).

## KEY FINDING -- arrow keys DO dismiss "Trust" (the user, LIVE 2026-07-14)

**Arrow keys CAN move the highlight onto "Trust" and activate it over BT HID.** The user
live-verified this on mini5 after a KVM switch to dev-usb threw a fresh Trust prompt: a bonded BT
keyboard moved the highlight from "Don't Trust" onto "Trust" with ARROW keys and activated it.

The tool now implements this: `dismiss_trust.py --method trust` = `build_trust_arrow_plan` =
arrow toward "Trust" (Right, the right button) -> activate (Space/Return) -> optional passcode.

### THE SUBTLE HIGHLIGHT CUE -- why the earlier "impossible" conclusion was WRONG

The focus indicator on this security alert is **NOT a bold focus ring**. It is a **subtle relative
brightness shift** between the two buttons:

| button                 | unfocused        | focused                  |
|------------------------|------------------|--------------------------|
| "Don't Trust" (left)   | bright blue      | LESS blue (dims)         |
| "Trust" (right)        | plain grey       | SLIGHTLY BRIGHTER grey   |

So when focus moves onto "Trust", the LEFT button dims (bright blue -> less blue) and the RIGHT
button brightens (grey -> slightly brighter grey). There is no ring that jumps.

**How to read it on the C920 webcam:** COMPARE THE RELATIVE BRIGHTNESS OF THE TWO BUTTONS after
each arrow -- does the left dim AND the right brighten? Do NOT hunt for a focus ring. The earlier
agent (2026-07-13/14) read the webcam looking for a ring, saw no ring on "Trust", and wrongly
concluded focus stayed on "Don't Trust". Focus WAS moving under the arrows the whole time; the cue
was just too subtle to register as a ring. That agent's "HID-hardened / arrow keys cannot reach or
activate Trust" matrix is **REFUTED**.

The English (mini6, "Trust") vs Danish (mini5, "Godkend denne computer?") wording is pure
localization -- FKA behaviour is identical, so the arrow+activate path applies to both iPads.

### The working sequence

From the default focus on "Don't Trust" (left), send arrow(s) toward "Trust" (Right), then activate:

* `--arrow-direction right` (Trust is the RIGHT button), `--arrow-presses N`, `--activate-key space`.
* Default `--arrow-presses 2` for robustness: iOS FKA sometimes consumes the FIRST arrow just to
  REVEAL the current focus without moving it, and arrow focus CLAMPS at the row edges (does not
  wrap), so a second Right lands/stays on the rightmost button ("Trust") either way. Tune to 1 if a
  live run shows a single Right already lands on Trust.
* After activation a passcode-confirm sheet may follow -- the PIN from `~/.config/bt_hid/idevice.pin`
  is typed + Enter automatically (read from the 0600 file, never logged/echoed/on argv).

### Pointer path (retained, still not aimable) -- `--method trust-pointer`

The relative/absolute pointer path is kept as a scaffold for a pointer-capable rig but is NOT the
working path. A relative left click IS delivered (it clears any focus), but the iOS pointer is
invisible in the C920 view (dim, reflective/fingerprinted glass, content shimmer) so it cannot be
aimed closed-loop -- six cursor/hover detection methods all failed (temporal-variance, before/after
differential, high-pass differential, median-over-frames, alive-jiggle median hover, 30fps
consecutive-frame diff). Absolute coordinates are ignored by iPadOS (memory
`ipad-absolute-hid-ignored`). Use `--method trust` (arrow keys), not the pointer, on this rig.

## mini5 (Pico) wake blocker -- BT HID cannot even START (2026-07-14)

Trying the matrix on mini5 (Pico `HIDBRIDGE-pico` on `/dev/ttyACM0`, Danish alert) hit a
peripheral-role wall BEFORE any key could be sent: **mini5's screen was OFF (auto-locked) and it
would not (re)connect to the bonded Pico.** A BLE-HID device is the PERIPHERAL; the iPad (central)
initiates the link, so a sleeping/locked iPad that has torn down the link cannot be woken from the
peripheral side. Evidence: the Pico firmware was alive + advertising (`PONG name=HIDBRIDGE-pico
conn=0`) the whole time, and polling `conn=` for 95s showed no reconnect; the current Pico firmware
exposes only send + ping (no directed-advertise / wake command). mini6 drove fine only because it
was already awake showing its prompt. So over BT HID, mini5 is unreachable while asleep -- the
matrix above (run on the identical, language-only-different mini6 alert) stands in for it.

Trust baseline (read-only, no pairing action taken): `idevicepair list` shows a LOCAL pair record
for mini5 (`00008020-001D310211A3002E`) AND mini6 (`00008110-001644313C6B801E`), yet BOTH show a
live Trust prompt -- i.e. the on-device trust was reset and the local records are STALE; the host is
NOT currently trusted by either. `idevice_id -l` could not even see the devices at probe time.
Confirming/repairing trust is left to the USB/`idevicepair` route (reserved for that workstream).

## What the tool does today

* DETECTS the overlay (`--webcam PATH` grabs a C920 frame before/after for visual confirmation).
* Drives every HID answer path we have and returns a structured, passcode-free result:
  - `--method trust`: ARROW toward "Trust" (`--arrow-direction right`, `--arrow-presses N`) +
    activate (`--activate-key space`) + the PIN confirm from `~/.config/bt_hid/idevice.pin` (never
    logged) -- **the working path** (the user, live 2026-07-14).
  - `--method dialog` / `decline`: Tab(s) + activate (answers ORDINARY dialogs, e.g. Sidecar
    "Mistet forbindelse"; on the Trust alert the Tab-focused button is "Don't Trust").
  - `--method trust-pointer`: relative-pointer click at `--pointer-frac x,y` (+ PIN confirm) -- a
    scaffold for a pointer-capable rig; not aimable on this rig (see above).
* `--dry-run` prints the (passcode-free) plan. The passcode is read from a 0600 file and expanded to
  HID frames locally (never argv/logged).

## Rig facts reconciled here

`bt_hid.targets` DEFAULT_ENTRIES were stale (both iPads pointed at dongle `hciN`). Reconciled to the
LIVE wsb rig (probed 2026-07-13): **mini6 = ESP32 `HIDBRIDGE-esp32` on `/dev/ttyUSB1`** (CP2102),
**mini5 = Pico 2 W `HIDBRIDGE-pico` on `/dev/ttyACM0`**. Both are per-device-identity MCU backends
(no shared-MAC dongle warning; drivable simultaneously). The dongle `hci1` still holds a stale
"iPad" bond (AA:BB:CC:DD:EE:01); the live HID links are the MCUs. See `docs/testbed/inventory.yaml`.

## Alternatives (BT HID via `--method trust` is now the primary answer)

* **`--method trust` (arrow + activate) -- the working BT-HID answer for "Trust"** (the user, live
  2026-07-14). No physical tap needed. Requires the iPad AWAKE + reconnected to its bonded board
  (a sleeping iPad tears down the peripheral link -- see the wake-blocker note above).
* Remove the USB trigger (stop the `usbmuxd`/lockdown pairing attempt) so iOS auto-dismisses -- not
  "via BT HID", and risks disturbing the banked mini5 USB state.
* A physical tap (the user) -- still available, but no longer the ONLY way to answer "Trust".
* USB / `idevicepair pair` -- the lockdown-trust route (orthogonal to the on-glass dialog).

## STILL NOT live-verified on wsb + a KVM'd iPad (2026-07-14, night) -- two hard blockers

Attempted to LIVE-VERIFY `--method trust` on mini6 during the dev-usb/Ubuntu Sidecar work (mini6 on
the KVM to dev-usb, showing a fresh "Trust This Computer?" modal; the ESP32/Pico boards stay wired to
wsb and reach mini6 over BLE regardless of which host owns the USB). The verify was **BLOCKED** and
the tool's "not yet live-verified" status STANDS (the user's 2026-07-14 daytime live-verify note above
is the only positive evidence; this night session could not reproduce it). Two independent walls:

### 1. mini6 will NOT re-initiate its bonded BLE link (peripheral-side reconnect wall)

`dismiss_trust.py --device mini6 --method trust` FAILED at the connect stage: the tool correctly
refused with `'mini6' did not connect via esp32 (nimble-hog, /dev/ttyUSB1) within 20s` -- no central
ever subscribed, so it never sent a keystroke (good: it does NOT type into a void). mini6 would not
reconnect to EITHER bonded board despite being **awake, unlocked, sitting in Settings > Bluetooth,
with both boards listed under "My Devices" (= bonded)**. Exhausted, all conn=0 throughout:

  * wait (3x ~60-90s polls of the board `conn=` field),
  * ESP32 re-advertise (`A\n` -> `OK adv`), repeated,
  * ESP32 **cold hardware reset** via DTR/RTS EN-pulse (confirmed `POWERON_RESET` boot banner) +
    re-advertise -- the doc's "after a cold reset the iPad auto-reconnects in a few seconds" did NOT
    hold here,
  * the Pico (continuous BTstack advertising) -- also conn=0 the whole time,
  * re-checked AFTER the Trust modal was cleared (below) and ~15 min later -- STILL conn=0.

So the modal is NOT what suspends the reconnect; mini6 (the BLE central) simply is not re-initiating
the link to the advertising peripheral. This is the SAME class as the mini5 sleeping-iPad wake-blocker
above, but here mini6 is fully awake -- so an awake+bonded iPad is ALSO not guaranteed to reconnect.
Untried levers that need the user physically: a manual Bluetooth toggle / tapping the board in the BT
list on mini6, or a physical re-pair. Over BT HID alone, the peripheral cannot force it.

### 2. The "Trust This Computer?" modal on a KVM'd iPad is INSULATED from host-side USB disconnects

Separate finding (corrects the "cfg-cycle clears stuck modals" assumption for the **lockdown Trust
prompt**): with mini6 behind the USB-serial KVM, NONE of the host-side "unplug" tricks dismissed the
modal, because the KVM keeps mini6's USB attachment alive to the iPad regardless of the host:

  * a `bConfigurationValue` cfg-cycle (0 -> 5), incl. `WSB_PREFLIGHT_FORCE=1` -- modal persists,
  * a full USB **deauthorize** (`authorized=0`) for 18s -- modal persists,
  * a true **hub-port electrical disable** (`.../3-2.3.3-port4/disable` = 1) for 15s -- modal persists.

What DID clear it: a **KVM switch away and back** (`kvm-serial.sh 0` then `3`) makes mini6 register a
real detach/attach and dismisses the prompt -- BUT ONLY after the pairing REQUESTERS on the target
host are silenced first, else GNOME re-pairs on re-attach and the modal returns in seconds. On Ubuntu
that meant masking BOTH `usbmuxd` (`.service`+`.socket`) AND the user `gvfs-afc-volume-monitor.service`
(the GNOME afc auto-mount, which is the real re-trigger -- masking usbmuxd alone was NOT enough) +
`pkill gvfsd-afc`. With both silenced, a KVM 3->0->3 cycle cleared the modal durably (webcam-confirmed).
wsb is a safe transit port for the cycle: mini6 is trusted there (or wsb `usbmuxd` is down) so it does
not re-prompt, and the wsb supervisor can be frozen during the hop. (All masks were reverted at teardown.)

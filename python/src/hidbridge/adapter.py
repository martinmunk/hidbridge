"""bt_hid adapter selection -- identify the USB dongle, REFUSE the wsb built-in adapter.

HARD REQUIREMENT (the maintainer): this toolbox must drive the USB Bluetooth DONGLE plugged into the hub,
never the laptop's built-in adapter (he may be using the built-in for his own BT peripherals). The
classifier below distinguishes them by the two most reliable, general signals:

  1. USB `removable` sysfs attribute: the built-in radio is `fixed`; a hub-plugged dongle is
     `removable` or `unknown` (it hangs off an external hub port, not a soldered root port).
  2. USB vendor: Intel (0x8087) and the common soldered-in Broadcom (0x0a5c) IDs are built-in
     combo radios -> treated as built-in regardless of `removable`.

Any adapter that is neither `fixed` nor a known built-in vendor is a dongle candidate.

Selection policy:
  * default: auto-pick the single dongle; error out if there is only the built-in.
  * `prefer=<hciN | MAC>`: pin a specific adapter. Pinning the built-in RAISES unless
    `allow_builtin=True` (documented escape hatch), so the tool never silently grabs hci0.

The detection reads sysfs only (no bluetooth sockets) so `classify_adapter` / `select_adapter` are
pure and unit-tested offline; `list_adapters()` does the live sysfs walk.
"""

from __future__ import annotations

import os
import subprocess

# USB vendor IDs of radios that are soldered-in built-in combo cards (never a plug-in dongle).
_BUILTIN_USB_VENDORS = {"8087", "0a5c"}   # Intel, Broadcom (common laptop BT/WiFi combos)

_SYS_BT = "/sys/class/bluetooth"


class NoDongleError(RuntimeError):
    """No USB Bluetooth dongle found (only the built-in adapter is present)."""


class BuiltinRefusedError(RuntimeError):
    """The pinned adapter is the built-in; refusing unless allow_builtin=True."""


class AmbiguousDongleError(RuntimeError):
    """A dongle id matched MORE THAN ONE adapter (e.g. the shared UB500 BD address); the caller
    must disambiguate with a unique id (hciN or the USB path)."""


def classify_adapter(info: dict) -> str:
    """'builtin' or 'dongle' for one adapter-info dict (see list_adapters for the shape)."""
    vendor = (info.get("usb_vendor") or "").lower()
    if vendor in _BUILTIN_USB_VENDORS:
        return "builtin"
    if (info.get("removable") or "").lower() == "fixed":
        return "builtin"
    return "dongle"


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _usb_device_dir(hci: str) -> str:
    """Walk up from /sys/class/bluetooth/<hci>/device to the enclosing USB device dir."""
    start = os.path.realpath(os.path.join(_SYS_BT, hci, "device"))
    cur = start
    while cur and cur != "/":
        if os.path.exists(os.path.join(cur, "idVendor")):
            return cur
        cur = os.path.dirname(cur)
    return ""


def _hci_address(hci: str) -> str:
    addr = _read(os.path.join(_SYS_BT, hci, "address"))
    if not addr:
        # sysfs `address` can be empty without root; fall back to hciconfig.
        try:
            out = subprocess.run(["hciconfig", hci], capture_output=True, text=True).stdout
            for tok in out.split():
                if len(tok) == 17 and tok.count(":") == 5:
                    addr = tok
                    break
        except OSError:
            pass
    return addr.upper()


def list_adapters() -> list[dict]:
    """Enumerate all local HCI adapters with the USB attributes we classify on."""
    out: list[dict] = []
    try:
        names = sorted(os.listdir(_SYS_BT))
    except OSError:
        names = []
    for hci in names:
        if not hci.startswith("hci"):
            continue
        usb = _usb_device_dir(hci)
        info = {
            "hci": hci,
            "address": _hci_address(hci),
            "usb_vendor": _read(os.path.join(usb, "idVendor")) if usb else "",
            "usb_product": _read(os.path.join(usb, "idProduct")) if usb else "",
            "product_name": _read(os.path.join(usb, "product")) if usb else "",
            "removable": _read(os.path.join(usb, "removable")) if usb else "",
            "usb_path": usb,
        }
        info["kind"] = classify_adapter(info)
        out.append(info)
    return out


def _matches(info: dict, pin: str) -> bool:
    pin_l = pin.lower()
    return info["hci"].lower() == pin_l or (info.get("address", "").lower() == pin_l)


def select_adapter(adapters: list[dict] | None = None, prefer: str | None = None,
                    allow_builtin: bool = False) -> dict:
    """Pick the adapter to drive.

    prefer: hciN or MAC to pin. Pinning the built-in raises BuiltinRefusedError unless
            allow_builtin. With no prefer, auto-pick the sole dongle (NoDongleError if none).
    """
    if adapters is None:
        adapters = list_adapters()

    if prefer:
        pinned = next((a for a in adapters if _matches(a, prefer)), None)
        if pinned is None:
            raise NoDongleError(f"pinned adapter {prefer!r} not found among "
                                f"{[a['hci'] for a in adapters]}")
        if classify_adapter(pinned) == "builtin" and not allow_builtin:
            raise BuiltinRefusedError(
                f"refusing to use built-in adapter {pinned['hci']} ({prefer}); "
                f"pass the dongle adapter, or set allow_builtin/--force-builtin to override")
        return pinned

    dongles = [a for a in adapters if classify_adapter(a) == "dongle"]
    if not dongles:
        raise NoDongleError(
            "no USB Bluetooth dongle found (only the built-in adapter is present); "
            "plug in the dongle or pin it with BT_HID_ADAPTER=hciN")
    if len(dongles) > 1:
        # deterministic: lowest hci index; operator can pin to disambiguate.
        dongles.sort(key=lambda a: a["hci"])
    return dongles[0]


def resolve(prefer_env: str | None = None, allow_builtin: bool = False) -> dict:
    """Convenience: resolve the adapter honouring the BT_HID_ADAPTER env if prefer_env is None."""
    prefer = prefer_env if prefer_env is not None else os.environ.get("BT_HID_ADAPTER")
    return select_adapter(prefer=prefer or None, allow_builtin=allow_builtin)


# --------------------------------------------------------------------------- #
# Multi-adapter support: stable slug, duplicate-address detection, flexible id resolution
# --------------------------------------------------------------------------- #
def adapter_slug(info: dict) -> str:
    """The stable-within-a-boot key we isolate a daemon's runtime artifacts on.

    We use `hciN`: it is DISTINCT per physical adapter even when two dongles SHARE a BD address
    (the UB500 defect), and every BlueZ object is already scoped `/org/bluez/hciN`. See naming.py.
    """
    return info.get("hci", "") or "default"


def duplicate_address_groups(adapters: list[dict] | None = None) -> dict[str, list[str]]:
    """Return {BD_ADDRESS: [hciA, hciB, ...]} for every address shared by >1 present adapter.

    This is the UB500 trap: two dongles with the SAME public BD address confuse BlueZ + the kernel
    routing (both index partly on BD_ADDR), so the iPad may never see a stable HID peripheral. The
    address is NOT runtime-reprogrammable, so the only safe answer is DETECT + WARN + do not run
    both at once. An empty dict means every adapter has a unique address.
    """
    if adapters is None:
        adapters = list_adapters()
    by_addr: dict[str, list[str]] = {}
    for a in adapters:
        addr = (a.get("address") or "").upper()
        if not addr or addr == "00:00:00:00:00:00":
            continue
        by_addr.setdefault(addr, []).append(a.get("hci", ""))
    return {addr: sorted(hcis) for addr, hcis in by_addr.items() if len(hcis) > 1}


def _fuzzy_matches(info: dict, dongle_id: str) -> bool:
    """Beyond hci/MAC (see _matches): also match a USB-path substring or product-name substring."""
    d = dongle_id.lower()
    return (d in (info.get("usb_path") or "").lower()
            or d in (info.get("product_name") or "").lower())


def resolve_dongle(dongle_id: str | None = None, adapters: list[dict] | None = None,
                   allow_builtin: bool = False) -> dict:
    """Resolve a caller-supplied dongle id to exactly ONE adapter, or raise.

    `dongle_id` accepts (in priority order): hciN, a full BD address, a USB-path substring, or a
    product-name substring. None/"" auto-picks the sole dongle (select_adapter semantics).

    Raises NoDongleError (no match), AmbiguousDongleError (the id matched several adapters -- the
    classic case is targeting the SHARED UB500 address, which hits both UB500 dongles), or
    BuiltinRefusedError (matched the built-in and allow_builtin is False).
    """
    if adapters is None:
        adapters = list_adapters()
    if not dongle_id:
        return select_adapter(adapters, allow_builtin=allow_builtin)

    matched = [a for a in adapters if _matches(a, dongle_id)]
    if not matched:
        matched = [a for a in adapters if _fuzzy_matches(a, dongle_id)]
    if not matched:
        raise NoDongleError(
            f"dongle id {dongle_id!r} matched no adapter among {[a['hci'] for a in adapters]}")
    if len(matched) > 1:
        raise AmbiguousDongleError(
            f"dongle id {dongle_id!r} matched {len(matched)} adapters "
            f"{[a['hci'] for a in matched]} (shared BD address?); pin a unique id: use the hciN or "
            f"the USB path")
    picked = matched[0]
    if classify_adapter(picked) == "builtin" and not allow_builtin:
        raise BuiltinRefusedError(
            f"refusing to use built-in adapter {picked['hci']} ({dongle_id}); pass the dongle, "
            f"or set allow_builtin/--force-builtin to override")
    return picked


def preflight(adapters: list[dict] | None = None) -> dict:
    """Audit all local adapters + surface the multi-adapter hazards BEFORE a daemon starts.

    Returns a report dict:
      {"adapters": [...], "dongles": [hci,...], "builtin": [hci,...],
       "duplicate_address_groups": {addr: [hci,...]}, "warnings": [str,...], "ok": bool}
    `ok` is False iff there is a same-address collision (the routing-corrupting UB500 trap). Pure
    over the supplied adapter list so it unit-tests offline.
    """
    if adapters is None:
        adapters = list_adapters()
    dupes = duplicate_address_groups(adapters)
    warnings: list[str] = []
    for addr, hcis in sorted(dupes.items()):
        warnings.append(
            f"DUPLICATE BD ADDRESS {addr} shared by {', '.join(hcis)} -- BlueZ/kernel routing can "
            f"corrupt and the iPad may never recognise the HID dongle. Run ONLY ONE of these at a "
            f"time (the address is not runtime-reprogrammable; known TP-Link UB500 V1 defect).")
    dongles = [a["hci"] for a in adapters if classify_adapter(a) == "dongle"]
    builtin = [a["hci"] for a in adapters if classify_adapter(a) == "builtin"]
    if not dongles:
        warnings.append("no USB Bluetooth dongle present (only the built-in adapter, which is "
                        "REFUSED as a HID peripheral).")
    return {
        "adapters": adapters,
        "dongles": dongles,
        "builtin": builtin,
        "duplicate_address_groups": dupes,
        "warnings": warnings,
        "ok": not dupes,
    }


def hciconfig(hci: str, *args: str) -> str:
    """Run hciconfig scoped to a single adapter and return stdout (best-effort)."""
    cmd = ["hciconfig", hci, *args]
    return subprocess.run(cmd, capture_output=True, text=True).stdout

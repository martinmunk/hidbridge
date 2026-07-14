"""Offline tests for bt_hid.targets -- the device->backend target registry + resolver.

NO hardware: the registry is a small JSON config (friendly iOS-device name -> backend target +
bond identity) and the resolver is a pure function over target strings (backend capabilities are
read from a hardware-free LinkBackend construction). Asserts:
  * a fresh registry seeds sane defaults; add/remove/get round-trip through JSON on disk (0644),
  * config path honours BT_HID_TARGETS then XDG_CONFIG_HOME then ~/.config/bt_hid/targets.json,
  * resolve() returns the HidSession target string + bond + parsed backend family + capability,
  * the shared-MAC dongle routing-ambiguity WARNING fires when a dongle-backed device shares its
    (identity-less) dongle family with another registered device, and does NOT fire for an MCU
    (per-device-identity) backend or a lone dongle device,
  * resolving an unknown name raises a clear KeyError-style error.
"""

import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

from hidbridge import targets as T  # noqa: E402


# --------------------------------------------------------------------------- #
# config path resolution
# --------------------------------------------------------------------------- #
def test_config_path_env_override_wins(monkeypatch, tmp_path):
    p = tmp_path / "custom.json"
    monkeypatch.setenv("BT_HID_TARGETS", str(p))
    assert T.config_path() == str(p)


def test_config_path_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("BT_HID_TARGETS", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert T.config_path() == str(tmp_path / "bt_hid" / "targets.json")


def test_config_path_home_default(monkeypatch):
    monkeypatch.delenv("BT_HID_TARGETS", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert T.config_path().endswith("/.config/bt_hid/targets.json")


# --------------------------------------------------------------------------- #
# registry: defaults + persistence
# --------------------------------------------------------------------------- #
def test_missing_file_seeds_defaults(tmp_path):
    reg = T.TargetRegistry.load(str(tmp_path / "nope.json"))
    assert set(reg.names()) >= {"mini6", "mini5"}  # sane project defaults
    # a default entry has a usable target + bond
    e = reg.get("mini6")
    assert e.target and e.bond


def test_add_remove_roundtrip_on_disk(tmp_path):
    path = str(tmp_path / "targets.json")
    reg = T.TargetRegistry.load(path)
    reg.add("labpad", target="pico:/dev/ttyACM0", bond="AA:BB:CC:DD:EE:FF", note="lab unit")
    reg.save()
    # a fresh load sees it
    reg2 = T.TargetRegistry.load(path)
    e = reg2.get("labpad")
    assert e.target == "pico:/dev/ttyACM0"
    assert e.bond == "AA:BB:CC:DD:EE:FF"
    assert e.note == "lab unit"
    # remove persists too
    reg2.remove("labpad")
    reg2.save()
    reg3 = T.TargetRegistry.load(path)
    assert "labpad" not in reg3.names()


def test_saved_file_is_plain_json_0644(tmp_path):
    path = tmp_path / "targets.json"
    reg = T.TargetRegistry.load(str(path))
    reg.add("x", target="hci1", bond="My iPad")
    reg.save()
    data = json.loads(path.read_text())
    assert "x" in data
    # target map is NOT secret -> world-readable 0644 (unlike idevice.pin's 0600)
    assert (path.stat().st_mode & 0o777) == 0o644


def test_add_rejects_blank_fields(tmp_path):
    reg = T.TargetRegistry.load(str(tmp_path / "t.json"))
    with pytest.raises(ValueError):
        reg.add("", target="hci1", bond="x")
    with pytest.raises(ValueError):
        reg.add("y", target="", bond="x")
    with pytest.raises(ValueError):
        reg.add("y", target="hci1", bond="")


# --------------------------------------------------------------------------- #
# resolver
# --------------------------------------------------------------------------- #
def test_resolve_returns_target_bond_backend(tmp_path):
    reg = T.TargetRegistry(entries={
        "onlypad": T.TargetEntry("onlypad", target="hci1", bond="My iPad"),
    })
    r = reg.resolve("onlypad")
    assert r.name == "onlypad"
    assert r.target == "hci1"
    assert r.bond == "My iPad"
    assert r.backend == "dongle"
    assert r.per_device_identity is False       # a BlueZ dongle has one identity
    assert r.shared_mac_warning is None         # only one device -> no ambiguity


def test_resolve_mcu_backend_family_and_identity(tmp_path):
    reg = T.TargetRegistry(entries={
        "pad": T.TargetEntry("pad", target="pico:/dev/ttyACM0", bond="AA:BB"),
    })
    r = reg.resolve("pad")
    assert r.backend == "pico"
    assert r.per_device_identity is True        # MCU firmware picks address per bond
    assert r.shared_mac_warning is None


def test_resolve_unknown_raises(tmp_path):
    reg = T.TargetRegistry(entries={})
    with pytest.raises(KeyError):
        reg.resolve("ghost")


# --------------------------------------------------------------------------- #
# the shared-MAC routing-ambiguity warning (the core of the task)
# --------------------------------------------------------------------------- #
def test_two_dongle_devices_warn_shared_mac():
    reg = T.TargetRegistry(entries={
        "mini6": T.TargetEntry("mini6", target="hci1", bond="mini6"),
        "mini5": T.TargetEntry("mini5", target="hci2", bond="mini5"),
    })
    r = reg.resolve("mini6")
    assert r.shared_mac_warning is not None
    w = r.shared_mac_warning.lower()
    assert "mini5" in w              # names the OTHER device it collides with
    assert "dongle" in w
    # symmetric: mini5 warns about mini6
    assert "mini6" in reg.resolve("mini5").shared_mac_warning


def test_dongle_plus_mcu_does_not_warn():
    # a dongle device and an MCU device do NOT collide (the MCU has its own identity)
    reg = T.TargetRegistry(entries={
        "mini6": T.TargetEntry("mini6", target="hci1", bond="mini6"),
        "mini5": T.TargetEntry("mini5", target="pico:/dev/ttyACM0", bond="mini5"),
    })
    assert reg.resolve("mini6").shared_mac_warning is None
    assert reg.resolve("mini5").shared_mac_warning is None


def test_two_mcu_devices_do_not_warn():
    reg = T.TargetRegistry(entries={
        "a": T.TargetEntry("a", target="pico:/dev/ttyACM0", bond="a"),
        "b": T.TargetEntry("b", target="esp32:/dev/ttyUSB1", bond="b"),
    })
    assert reg.resolve("a").shared_mac_warning is None
    assert reg.resolve("b").shared_mac_warning is None


# --------------------------------------------------------------------------- #
# multi-board routes: BOTH boards can reach mini5 (task: both MCUs bonded to mini5 + switch)
# --------------------------------------------------------------------------- #
def test_defaults_give_mini5_a_route_on_both_boards(tmp_path):
    """A fresh registry must let BOTH the Pico and the ESP32 reach mini5 (both boards bonded)."""
    reg = T.TargetRegistry.load(str(tmp_path / "nope.json"))
    backends = reg.backends_for_bond("mini5")
    assert backends == {"pico", "esp32"}, f"mini5 should route on both boards, got {backends}"
    # symmetric: mini6 also reachable on both boards (so each board can switch mini5<->mini6)
    assert reg.backends_for_bond("mini6") == {"pico", "esp32"}


def test_routes_for_bond_resolves_each_board(tmp_path):
    reg = T.TargetRegistry.load(str(tmp_path / "nope.json"))
    routes = reg.routes_for_bond("mini5")
    assert len(routes) == 2
    for r in routes:
        assert r.bond == "mini5"
        assert r.backend in ("pico", "esp32")
        assert r.target and r.per_device_identity is True   # MCU boards own their identity
    # each board appears exactly once for mini5
    assert sorted(r.backend for r in routes) == ["esp32", "pico"]


def test_each_board_can_switch_mini5_and_mini6(tmp_path):
    """Per board, the registry must expose a route to mini5 AND to mini6 (the switch targets)."""
    reg = T.TargetRegistry.load(str(tmp_path / "nope.json"))
    by_backend: dict[str, set[str]] = {}
    for name in reg.names():
        e = reg.get(name)
        by_backend.setdefault(reg.resolve(name).backend, set()).add(e.bond)
    assert by_backend.get("pico") == {"mini5", "mini6"}    # Pico bonded to both -> can switch
    assert by_backend.get("esp32") == {"mini5", "mini6"}   # ESP32 bonded to both -> can switch


def test_resolve_all_reports_each():
    reg = T.TargetRegistry(entries={
        "mini6": T.TargetEntry("mini6", target="hci1", bond="mini6"),
        "mini5": T.TargetEntry("mini5", target="hci2", bond="mini5"),
    })
    allr = reg.resolve_all()
    assert set(allr) == {"mini6", "mini5"}
    assert all(r.shared_mac_warning for r in allr.values())

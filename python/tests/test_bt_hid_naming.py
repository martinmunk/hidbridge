"""Offline tests for bt_hid per-adapter naming (the multi-daemon isolation keys).

Two ble_hid daemons on two dongles must NOT collide on the control socket, the D-Bus application
path, the agent path, or the advertised name. These pure functions derive a distinct set per
adapter slug; the tests pin that they are distinct + path-safe.
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]

from hidbridge import naming as N  # noqa: E402


def test_control_sock_distinct_per_slug():
    assert N.control_sock("hci1") != N.control_sock("hci2")
    assert N.control_sock("hci1").endswith("hci1.sock")


def test_control_sock_env_override_wins():
    assert N.control_sock("hci1", env_override="/run/custom.sock") == "/run/custom.sock"


def test_app_and_agent_paths_distinct_and_nested():
    assert N.app_path("hci1") == "/org/martinmunk/bthid/hci1"
    assert N.agent_path("hci1") == "/org/martinmunk/bthid/hci1/agent"
    assert N.app_path("hci1") != N.app_path("hci2")
    assert N.agent_path("hci1") != N.agent_path("hci2")


def test_default_name_distinct_per_slug():
    assert N.default_name("hci1") == "wsb-hid-hci1"
    assert N.default_name("hci1") != N.default_name("hci2")


def test_slug_sanitized_for_dbus_and_fs():
    # A messy slug (should never happen for hciN, but be safe) yields a valid path element + file.
    assert N.app_path("hci-1:x") == "/org/martinmunk/bthid/hci_1_x"
    assert N.control_sock("hci-1:x").endswith("hci_1_x.sock")


def test_empty_slug_falls_back_to_default():
    assert N.app_path("") == "/org/martinmunk/bthid/default"

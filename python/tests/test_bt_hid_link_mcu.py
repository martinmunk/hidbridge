"""Offline tests: the MCU link backends (link_esp32 / link_pico) load + advertise per-device identity.

conn.make_link_backend("esp32:/dev/ttyUSB1") auto-loads bt_hid.link_esp32 (no hardware); the MCU
backends advertise per_device_identity as a capability the BlueZ dongle cannot offer. The full
connect-on-demand drive lifecycle now lives on the board driver -- see test_bt_hid_board.py
(McuBoard) and test_bt_hid_bridge.py (HidBridge).
"""

from hidbridge import conn as C
from hidbridge import link_esp32, link_pico


# --------------------------------------------------------------------------- #
# conn.make_link_backend auto-loads the dedicated MCU modules (not the generic fallback)
# --------------------------------------------------------------------------- #
def test_conn_loads_dedicated_esp32_backend():
    be = C.make_link_backend("esp32:/dev/ttyUSB1")
    assert isinstance(be, link_esp32.Esp32LinkBackend)
    assert be._port == "/dev/ttyUSB1"


def test_conn_loads_dedicated_pico_backend():
    be = C.make_link_backend("pico:/dev/ttyACM0")
    assert isinstance(be, link_pico.PicoLinkBackend)
    assert be._port == "/dev/ttyACM0"


def test_mcu_backends_advertise_per_device_identity_capability():
    assert C.make_link_backend("esp32:/dev/ttyUSB1").capabilities["per_device_identity"] is True
    assert C.make_link_backend("pico:/dev/ttyACM0").capabilities["per_device_identity"] is True
    # the dongle backend cannot (BlueZ: one identity per adapter)
    assert C.make_link_backend("hci0").capabilities["per_device_identity"] is False


def test_pico_connected_parse():
    assert link_pico._pong_connected("PONG name=HIDBRIDGE-pico conn=1") is True
    assert link_pico._pong_connected("PONG name=HIDBRIDGE-pico conn=0") is False
    assert link_pico._pong_connected("") is False

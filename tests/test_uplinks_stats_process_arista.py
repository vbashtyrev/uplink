"""process_one_arista / process_one_juniper via mocks."""

from unittest.mock import MagicMock, patch

import uplinks_stats as us


def test_process_one_arista_success():
    device = MagicMock()
    device.name = "R1"
    logs = []
    with patch.object(
        us,
        "get_arista_uplink_stats",
        return_value=([{"name": "Eth1", "description": "Uplink: X"}], None),
    ):
        name, data = us.process_one_arista(
            device, None, "u", "p", ".io", lambda d, m: logs.append(m)
        )
    assert name == "R1"
    assert data[0]["name"] == "Eth1"


def test_process_one_juniper_error():
    device = MagicMock()
    device.name = "MX1"
    with patch.object(us, "get_juniper_uplink_stats", return_value=(None, "ssh fail")):
        name, data = us.process_one_juniper(
            device, None, "u", "p", ".io", lambda d, m: None
        )
    assert name == "MX1"
    assert isinstance(data, dict) and "error" in data

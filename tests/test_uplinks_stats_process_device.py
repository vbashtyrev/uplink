"""process_one_device and _run_report paths."""

from unittest.mock import MagicMock, patch

import uplinks_stats as us


def test_process_one_device_netbox_and_ssh(capsys):
    device = MagicMock()
    device.name = "R1"
    device.id = 1
    device.primary_ip4 = MagicMock(address="203.0.113.1/24")
    nb = MagicMock()
    iface = MagicMock()
    iface.name = "Ethernet1"
    iface.description = "Uplink: ISP"
    nb.dcim.interfaces.filter.return_value = [iface]
    nb.ipam.ip_addresses.get.return_value = MagicMock(address="203.0.113.1/24")
    logs = []

    with patch.object(
        us,
        "get_ssh_uplinks",
        return_value=([("Ethernet1", "Uplink: ISP")], None),
    ):
        with patch.object(us, "get_device_platform_name", return_value="Arista EOS"):
            row = us.process_one_device(
                device,
                nb,
                "admin",
                "pass",
                ".example.com",
                "—",
                "—",
                lambda d, m: logs.append(m),
            )
    assert row[0] == "R1"
    assert "Ethernet1" in row[2] or "Uplink" in row[2]


def test_process_one_device_ssh_error():
    device = MagicMock()
    device.name = "R1"
    device.id = 1
    device.primary_ip4 = None
    nb = MagicMock()
    nb.dcim.interfaces.filter.return_value = []
    with patch.object(us, "get_ssh_uplinks", return_value=(None, "timeout")):
        with patch.object(us, "get_device_platform_name", return_value="Arista EOS"):
            row = us.process_one_device(
                device, nb, "u", "p", ".io", "nb", "ssh",
                lambda d, m: None,
            )
    assert "timeout" in row[3]

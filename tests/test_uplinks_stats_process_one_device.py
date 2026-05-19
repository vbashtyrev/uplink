"""process_one_device and format_cell coverage."""

from unittest.mock import MagicMock, patch

from uplinks_stats import format_cell, process_one_device


def test_format_cell():
    assert format_cell([], "none") == "none"
    assert "Eth1" in format_cell([("Eth1", "Uplink: X")], "none")


def test_process_one_device(monkeypatch):
    device = MagicMock()
    device.name = "ALA-R1"
    device.primary_ip4 = "203.0.113.1/24"

    nb_iface = MagicMock()
    nb_iface.name = "Ethernet51/1"
    nb = MagicMock()
    nb.dcim.interfaces.filter.return_value = [nb_iface]

    monkeypatch.setattr(
        "uplinks_stats.get_ssh_uplinks",
        lambda *a, **k: ([("Ethernet51/1", "Uplink: ISP")], None),
    )
    name, ip, nb_cell, ssh_cell = process_one_device(
        device,
        nb,
        "admin",
        "pass",
        ".example.com",
        "not in nb",
        "ssh fail",
        lambda d, m: None,
    )
    assert name == "ALA-R1"
    assert "Ethernet51/1" in ssh_cell


def test_process_one_device_primary_ip_int(monkeypatch):
    device = MagicMock()
    device.name = "ALA-R1"
    device.primary_ip4 = 42
    device.id = 1
    nb_iface = MagicMock()
    nb_iface.name = "Ethernet51/1"
    nb_iface.description = "Uplink: ISP"
    nb = MagicMock()
    nb.dcim.interfaces.filter.return_value = [nb_iface]
    nb.ipam.ip_addresses.get.return_value = MagicMock(address="203.0.113.1/24")
    monkeypatch.setattr(
        "uplinks_stats.get_ssh_uplinks",
        lambda *a, **k: ([], None),
    )
    monkeypatch.setattr("uplinks_stats.get_device_platform_name", lambda d, nb: "Arista EOS")
    name, ip, _, _ = process_one_device(
        device, nb, "u", "p", "", "nb", "ssh", lambda d, m: None
    )
    assert name == "ALA-R1"
    assert "203.0.113.1" in ip

"""process_one_arista / process_one_juniper with mocked get_*_uplink_stats."""

from unittest.mock import MagicMock

import uplinks_stats as us


def test_process_one_arista_success(monkeypatch):
    device = MagicMock()
    device.name = "ALA-R1"
    monkeypatch.setattr(
        us,
        "get_arista_uplink_stats",
        lambda *a, **k: ([{"name": "Eth1"}], None),
    )
    monkeypatch.setattr(us, "_resolve_ssh_host", lambda *a, **k: ("host", "user"))
    name, data = us.process_one_arista(
        device, MagicMock(), "u", "p", ".example.com", print, ssh_config=None
    )
    assert name == "ALA-R1"
    assert data[0]["name"] == "Eth1"


def test_process_one_arista_error(monkeypatch):
    device = MagicMock()
    device.name = "ALA-R1"
    monkeypatch.setattr(
        us,
        "get_arista_uplink_stats",
        lambda *a, **k: (None, "ssh failed"),
    )
    monkeypatch.setattr(us, "_resolve_ssh_host", lambda *a, **k: ("host", "user"))
    name, data = us.process_one_arista(
        device, MagicMock(), "u", "p", ".example.com", print
    )
    assert name == "ALA-R1"
    assert data["error"] == "ssh failed"


def test_process_one_juniper_success(monkeypatch):
    device = MagicMock()
    device.name = "FRN-MX-1"
    monkeypatch.setattr(
        us,
        "get_juniper_uplink_stats",
        lambda *a, **k: ([{"name": "ae5"}], None),
    )
    monkeypatch.setattr(us, "_resolve_ssh_host", lambda *a, **k: ("host", "user"))
    name, data = us.process_one_juniper(
        device, MagicMock(), "u", "p", ".example.com", print
    )
    assert name == "FRN-MX-1"
    assert data[0]["name"] == "ae5"

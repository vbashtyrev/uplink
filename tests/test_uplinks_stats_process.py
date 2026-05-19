"""uplinks_stats process_one_device_stats routing."""

from unittest.mock import MagicMock

import uplinks_stats as us


def test_process_one_device_stats_arista(monkeypatch):
    device = MagicMock()
    device.name = "dev1"
    monkeypatch.setattr(us, "get_device_platform_name", lambda d, nb: "Arista EOS")
    monkeypatch.setattr(
        us,
        "process_one_arista",
        lambda *a, **k: ("dev1", [{"name": "Eth1"}]),
    )
    monkeypatch.setattr(us, "is_arista_platform", lambda n: True)
    monkeypatch.setattr(us, "is_juniper_platform", lambda n: False)
    name, data = us.process_one_device_stats(device, MagicMock(), "u", "p", "", print)
    assert name == "dev1"
    assert data[0]["name"] == "Eth1"


def test_process_one_device_stats_juniper(monkeypatch):
    device = MagicMock()
    device.name = "mx1"
    monkeypatch.setattr(us, "get_device_platform_name", lambda d, nb: "Juniper JUNOS")
    monkeypatch.setattr(
        us,
        "process_one_juniper",
        lambda *a, **k: ("mx1", [{"name": "ae5"}]),
    )
    monkeypatch.setattr(us, "is_arista_platform", lambda n: False)
    monkeypatch.setattr(us, "is_juniper_platform", lambda n: True)
    name, data = us.process_one_device_stats(device, MagicMock(), "u", "p", "", print)
    assert name == "mx1"
    assert data[0]["name"] == "ae5"


def test_process_one_device_stats_skip_other_platform(monkeypatch):
    device = MagicMock()
    device.name = "other"
    monkeypatch.setattr(us, "get_device_platform_name", lambda d, nb: "Cisco IOS")
    monkeypatch.setattr(us, "is_arista_platform", lambda n: False)
    monkeypatch.setattr(us, "is_juniper_platform", lambda n: False)
    name, data = us.process_one_device_stats(device, MagicMock(), "u", "p", "", print)
    assert name == "other"
    assert data is None

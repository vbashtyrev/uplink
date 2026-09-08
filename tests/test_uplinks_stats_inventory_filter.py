"""uplinks_stats --inventory-file limits SSH device polling."""

import json
import sys
from unittest.mock import MagicMock

import uplinks_stats as us


def _device(name, platform):
    d = MagicMock()
    d.name = name
    d.platform = MagicMock()
    d.platform.name = platform
    return d


def _write_inventory(path, complete_devices, incomplete=None):
    path.write_text(
        json.dumps(
            {
                "complete": [{"device": name, "interface": "Eth1", "provider": "P"} for name in complete_devices],
                "incomplete": incomplete or [],
                "stats": {"complete": len(complete_devices), "incomplete": len(incomplete or [])},
            }
        ),
        encoding="utf-8",
    )


def test_main_fetch_inventory_file_limits_devices(tmp_path, monkeypatch, netbox_env, ssh_env, capsys):
    inv = tmp_path / "netbox_inventory.json"
    _write_inventory(inv, ["ALA-KZT-7280TR-1"])

    nb = MagicMock()
    dev_a = _device("ALA-KZT-7280TR-1", "Arista EOS")
    dev_b = _device("FRN-MX-1", "Juniper JUNOS")
    nb.dcim.devices.filter.return_value = [dev_a, dev_b]

    fetched = []

    def fake_process(device, *args, **kwargs):
        fetched.append(device.name)
        return device.name, [{"name": "eth1"}]

    monkeypatch.setattr("uplinks_stats.pynetbox.api", lambda url, token: nb)
    monkeypatch.setattr(us, "get_device_platform_name", lambda d, nb: d.platform.name)
    monkeypatch.setattr(us, "process_one_device_stats", fake_process)
    monkeypatch.setattr(us, "_load_ssh_config", lambda: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "uplinks_stats.py",
            "--fetch",
            "--json",
            "--platform",
            "all",
            "--inventory-file",
            str(inv),
        ],
    )

    assert us.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out["devices"].keys()) == {"ALA-KZT-7280TR-1"}
    assert fetched == ["ALA-KZT-7280TR-1"]


def test_main_fetch_without_inventory_file_unchanged(monkeypatch, netbox_env, ssh_env, capsys):
    nb = MagicMock()
    dev_a = _device("ALA-KZT-7280TR-1", "Arista EOS")
    dev_b = _device("FRN-MX-1", "Juniper JUNOS")
    nb.dcim.devices.filter.return_value = [dev_a, dev_b]

    fetched = []

    def fake_process(device, *args, **kwargs):
        fetched.append(device.name)
        return device.name, [{"name": "eth1"}]

    monkeypatch.setattr("uplinks_stats.pynetbox.api", lambda url, token: nb)
    monkeypatch.setattr(us, "get_device_platform_name", lambda d, nb: d.platform.name)
    monkeypatch.setattr(us, "process_one_device_stats", fake_process)
    monkeypatch.setattr(us, "_load_ssh_config", lambda: None)
    monkeypatch.setattr(sys, "argv", ["uplinks_stats.py", "--fetch", "--json", "--platform", "all"])

    assert us.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out["devices"].keys()) == {"ALA-KZT-7280TR-1", "FRN-MX-1"}
    assert set(fetched) == {"ALA-KZT-7280TR-1", "FRN-MX-1"}


def test_main_fetch_inventory_file_no_complete_entries(tmp_path, monkeypatch, netbox_env, ssh_env, capsys):
    inv = tmp_path / "empty_inventory.json"
    inv.write_text(json.dumps({"complete": [], "incomplete": [], "stats": {}}), encoding="utf-8")

    nb = MagicMock()
    nb.dcim.devices.filter.return_value = [_device("ALA-KZT-7280TR-1", "Arista EOS")]
    monkeypatch.setattr("uplinks_stats.pynetbox.api", lambda url, token: nb)
    monkeypatch.setattr(us, "get_device_platform_name", lambda d, nb: "Arista EOS")
    monkeypatch.setattr(us, "process_one_device_stats", lambda *a, **k: ("x", []))
    monkeypatch.setattr(us, "_load_ssh_config", lambda: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["uplinks_stats.py", "--fetch", "--json", "--inventory-file", str(inv)],
    )

    assert us.main() == 1
    assert "no complete device entries" in capsys.readouterr().err.lower()

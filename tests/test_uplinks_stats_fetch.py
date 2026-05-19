"""uplinks_stats --fetch --json and --merge-into."""

import json
import sys
from unittest.mock import MagicMock, patch

import uplinks_stats as us


def _device(name, platform):
    d = MagicMock()
    d.name = name
    d.platform = MagicMock()
    d.platform.name = platform
    return d


def test_main_fetch_json(monkeypatch, netbox_env, ssh_env, capsys):
    nb = MagicMock()
    dev_a = _device("ALA-KZT-7280TR-1", "Arista EOS")
    dev_j = _device("FRN-MX-1", "Juniper JUNOS")
    nb.dcim.devices.filter.return_value = [dev_a, dev_j]

    monkeypatch.setattr("uplinks_stats.pynetbox.api", lambda url, token: nb)
    monkeypatch.setattr(us, "get_device_platform_name", lambda d, nb: d.platform.name)
    monkeypatch.setattr(
        us,
        "process_one_device_stats",
        lambda device, *a, **k: (device.name, [{"name": "eth1"}]),
    )
    monkeypatch.setattr(us, "_load_ssh_config", lambda: None)
    monkeypatch.setattr(sys, "argv", ["uplinks_stats.py", "--fetch", "--json", "--platform", "all"])

    assert us.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert "ALA-KZT-7280TR-1" in out["devices"]
    assert "FRN-MX-1" in out["devices"]


def test_main_fetch_merge_into(tmp_path, monkeypatch, netbox_env, ssh_env, capsys):
    existing = tmp_path / "stats.json"
    existing.write_text(
        json.dumps({"devices": {"OTHER-HOST": [{"name": "eth0"}]}}),
        encoding="utf-8",
    )
    nb = MagicMock()
    nb.dcim.devices.filter.return_value = [_device("ALA-KZT-7280TR-1", "Arista EOS")]
    monkeypatch.setattr("uplinks_stats.pynetbox.api", lambda url, token: nb)
    monkeypatch.setattr(us, "get_device_platform_name", lambda d, nb: "Arista EOS")
    monkeypatch.setattr(
        us,
        "process_one_device_stats",
        lambda device, *a, **k: (device.name, [{"name": "Eth1"}]),
    )
    monkeypatch.setattr(us, "_load_ssh_config", lambda: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "uplinks_stats.py",
            "--fetch",
            "--merge-into",
            str(existing),
            "--platform",
            "arista",
        ],
    )
    assert us.main() == 0
    merged = json.loads(existing.read_text(encoding="utf-8"))
    assert "OTHER-HOST" in merged["devices"]
    assert "ALA-KZT-7280TR-1" in merged["devices"]

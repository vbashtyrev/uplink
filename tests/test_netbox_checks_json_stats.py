"""netbox_checks JSON output with hide-ok-hosts and stats."""

import json
import sys
from unittest.mock import patch

import netbox_checks as nc
from tests.mocks.netbox_full import NetBoxTestEnvironment


def test_main_json_with_diff_and_hide_ok(monkeypatch, netbox_env, tmp_path, capsys):
    stats = tmp_path / "stats.json"
    stats.write_text(
        json.dumps(
            {
                "devices": {
                    "ALA-KZT-7280TR-1": [
                        {
                            "name": "Ethernet51/1",
                            "description": "Uplink: Cogent 10G",
                            "bandwidth": 10000000000,
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    env = NetBoxTestEnvironment()
    dev = env.add_device("ALA-KZT-7280TR-1")
    dev.tag = "border"
    iface = env.add_interface(dev, "Ethernet51/1", speed=1000)
    iface.description = "old description"

    with patch.object(nc.pynetbox, "api", lambda url, token: env):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "netbox_checks.py",
                "-f",
                str(stats),
                "--description",
                "--bandwidth",
                "--json",
                "--hide-ok-hosts",
            ],
        )
        assert nc.main() == 0
    out = capsys.readouterr().out
    json_start = out.find("{")
    assert json_start >= 0, out
    data = json.loads(out[json_start:])
    assert "rows" in data
    assert "stats" in data
    assert data["stats"]["hosts_not_ok"] >= 1

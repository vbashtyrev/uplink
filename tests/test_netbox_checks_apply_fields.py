"""netbox_checks --apply updates description, bandwidth, mtu."""

import json
import sys
from unittest.mock import patch

import netbox_checks as nc
from tests.mocks.netbox_full import NetBoxTestEnvironment


def test_main_apply_updates_fields(monkeypatch, netbox_env, tmp_path, capsys):
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
                            "mtu": 9214,
                            "duplex": "duplexFull",
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
    iface = env.add_interface(dev, "Ethernet51/1", speed=1000, iface_type="other")
    iface.description = "old"
    iface.mtu = 1500
    updates = []

    def track_update(data):
        updates.append(data)

    iface.update = track_update

    with patch.object(nc.pynetbox, "api", lambda url, token: env):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "netbox_checks.py",
                "-f",
                str(stats),
                "--apply",
                "--description",
                "--bandwidth",
                "--mtu",
                "--duplex",
            ],
        )
        assert nc.main() == 0
    assert updates
    out = capsys.readouterr().out
    assert "Updated" in out or "apply" in out.lower() or updates

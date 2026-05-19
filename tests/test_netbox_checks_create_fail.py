"""netbox_checks apply create interface failure path."""

import json
import sys
from unittest.mock import patch

import netbox_checks as nc
from tests.mocks.netbox_full import NetBoxTestEnvironment


def test_apply_create_interface_error(monkeypatch, netbox_env, tmp_path, capsys):
    stats = tmp_path / "stats.json"
    stats.write_text(
        json.dumps(
            {
                "devices": {
                    "R1": [
                        {
                            "name": "Ethernet99/1",
                            "description": "Uplink: New",
                            "mediaType": "10gbase-x-sfpp",
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    env = NetBoxTestEnvironment()
    dev = env.add_device("R1")
    dev.tag = "border"
    dev.platform = type("P", (), {"name": "Arista EOS"})()

    def boom(**kwargs):
        raise RuntimeError("create denied")

    env.dcim.interfaces.create = boom
    with patch.object(nc.pynetbox, "api", lambda url, token: env):
        with patch("netbox_checks.is_juniper_platform", return_value=False):
            with patch("netbox_checks.is_arista_platform", return_value=True):
                with patch("netbox_checks.get_device_platform_name", return_value="Arista EOS"):
                    with patch.object(nc, "_get_interface_ip_addresses", return_value=[]):
                        monkeypatch.setattr(
                            sys,
                            "argv",
                            [
                                "netbox_checks.py",
                                "-f",
                                str(stats),
                                "--host",
                                "R1",
                                "--apply",
                                "--intname",
                                "--description",
                                "--mediatype",
                            ],
                        )
                        assert nc.main() == 0
    assert "Error creating" in capsys.readouterr().err

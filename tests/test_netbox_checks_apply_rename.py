"""netbox_checks --apply renames interface when intname differs."""

import json
import sys
from unittest.mock import patch

import netbox_checks as nc
from tests.mocks.netbox_full import NetBoxTestEnvironment


def test_apply_renames_interface_name(monkeypatch, netbox_env, tmp_path):
    stats = tmp_path / "stats.json"
    stats.write_text(
        json.dumps(
            {
                "devices": {
                    "R1": [
                        {
                            "name": "Ethernet51/1",
                            "description": "Uplink: Cogent",
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
    env.add_interface(dev, "eth51/1", iface_type="10gbase-x-sfpp")

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
                            ],
                        )
                        assert nc.main() == 0
    renamed = env.dcim.interfaces.filter(device_id=dev.id, name="Ethernet51/1")
    assert list(renamed)

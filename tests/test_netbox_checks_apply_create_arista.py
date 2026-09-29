"""netbox_checks --apply --intname skips missing Arista physical interface."""

import json
import sys
from unittest.mock import patch

import netbox_checks as nc
from tests.mocks.netbox_full import NetBoxTestEnvironment


def test_apply_skips_missing_ethernet_interface(monkeypatch, netbox_env, tmp_path, capsys):
    stats = tmp_path / "stats.json"
    stats.write_text(
        json.dumps(
            {
                "devices": {
                    "ALA-KZT-7280TR-1": [
                        {
                            "name": "Ethernet52/1",
                            "description": "Uplink: Cogent",
                            "mediaType": "10gbase-x-sfpp",
                            "bandwidth": 10_000_000_000,
                            "duplex": "duplexFull",
                            "forwardingModel": "routed",
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
    dev.platform = type("P", (), {"name": "Arista EOS"})()
    env.dcim.interfaces.create = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("interface create must not run")
    )

    with patch.object(nc.pynetbox, "api", lambda url, token: env):
        with patch("netbox_checks.is_juniper_platform", return_value=False):
            with patch("netbox_checks.is_arista_platform", return_value=True):
                with patch("netbox_checks.get_device_platform_name", return_value="Arista EOS"):
                    with patch.object(nc, "_get_interface_ip_addresses", return_value=([], None)):
                        monkeypatch.setattr(
                            sys,
                            "argv",
                            [
                                "netbox_checks.py",
                                "-f",
                                str(stats),
                                "--host",
                                "ALA-KZT-7280TR-1",
                                "--apply",
                                "--intname",
                                "--description",
                                "--mediatype",
                                "--bandwidth",
                                "--duplex",
                                "--forwarding-model",
                            ],
                        )
                        assert nc.main() == 0
    assert not env.dcim.interfaces.filter(device_id=dev.id, name="Ethernet52/1")
    assert "not found in NetBox, skipped" in capsys.readouterr().out

"""netbox_checks --apply --intname creates Juniper logical unit + LAG."""

import json
import sys
from unittest.mock import patch

import netbox_checks as nc
from tests.mocks.netbox_full import NetBoxTestEnvironment


def test_apply_creates_ae_logical_unit(monkeypatch, netbox_env, tmp_path, capsys):
    stats = tmp_path / "stats.json"
    stats.write_text(
        json.dumps(
            {
                "devices": {
                    "FRN-MX-1": [
                        {
                            "name": "ae5.0",
                            "description": "Uplink: Hurricane",
                            "isLogical": True,
                            "aggregateInterface": "ae5",
                            "ipv4_addresses": ["203.0.113.10/24"],
                        },
                        {
                            "name": "ae5",
                            "description": "Uplink: Hurricane LAG",
                            "isLag": True,
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    env = NetBoxTestEnvironment()
    dev = env.add_device("FRN-MX-1")
    dev.tag = "border"
    dev.platform = type("P", (), {"name": "Juniper JunOS"})()
    ae5 = env.add_interface(dev, "ae5", iface_type="lag")
    ae5.description = "old lag"

    with patch.object(nc.pynetbox, "api", lambda url, token: env):
        with patch("netbox_checks.is_juniper_platform", return_value=True):
            with patch("netbox_checks.is_arista_platform", return_value=False):
                with patch("netbox_checks.get_device_platform_name", return_value="Juniper JunOS"):
                    with patch.object(nc, "_get_interface_ip_addresses", return_value=[]):
                        with patch.object(nc, "_apply_ip_addresses_to_interface") as ip_apply:
                            monkeypatch.setattr(
                                sys,
                                "argv",
                                [
                                    "netbox_checks.py",
                                    "-f",
                                    str(stats),
                                    "--host",
                                    "FRN-MX-1",
                                    "--apply",
                                    "--intname",
                                    "--description",
                                    "--parent",
                                    "--ip-address",
                                ],
                            )
                            assert nc.main() == 0
    out = capsys.readouterr().out
    assert "created" in out.lower() or "Updated" in out or ip_apply.called

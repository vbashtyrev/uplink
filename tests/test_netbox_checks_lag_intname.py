"""netbox_checks: intname create, lag/parent, ip-address apply."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import netbox_checks as nc

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _juniper_stats_file(tmp_path):
    stats = {
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
                    "name": "et-0/0/1",
                    "description": "Uplink: Hurricane member",
                    "aggregateInterface": "ae5",
                },
                {
                    "name": "ae5",
                    "description": "Uplink: Hurricane LAG",
                    "isLag": True,
                },
            ],
        },
    }
    p = tmp_path / "juniper_stats.json"
    p.write_text(json.dumps(stats), encoding="utf-8")
    return p


def test_main_apply_lag_parent_intname(monkeypatch, netbox_env, tmp_path, capsys):
    ae5 = MagicMock()
    ae5.id = 50
    ae5.name = "ae5"
    member = MagicMock()
    member.id = 51
    member.name = "et-0/0/1"
    member.update = MagicMock()
    logical = MagicMock()
    logical.id = 52
    logical.name = "ae5.0"
    logical.update = MagicMock()

    device = MagicMock()
    device.id = 1
    device.name = "FRN-MX-1"
    device.platform = MagicMock()
    device.platform.name = "Juniper JunOS"

    nb = MagicMock()
    nb.dcim.devices.filter.return_value = [device]
    nb.dcim.interfaces.filter.return_value = [ae5, member, logical]
    nb.dcim.interfaces.create = MagicMock(return_value=member)

    with patch("netbox_checks.pynetbox.api", return_value=nb):
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
                                    str(_juniper_stats_file(tmp_path)),
                                    "--host",
                                    "FRN-MX-1",
                                    "--lag",
                                    "--parent",
                                    "--intname",
                                    "--apply",
                                ],
                            )
                            nc.main()
                            assert member.update.called or logical.update.called

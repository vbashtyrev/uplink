"""netbox_checks --apply updates existing interface (description, mtu, lag, parent)."""

import json
import sys
from unittest.mock import patch

import netbox_checks as nc
from tests.mocks.netbox_full import NetBoxTestEnvironment


def test_apply_updates_existing_interface(monkeypatch, netbox_env, tmp_path, capsys):
    stats = tmp_path / "stats.json"
    stats.write_text(
        json.dumps(
            {
                "devices": {
                    "FRN-MX-1": [
                        {
                            "name": "ae5",
                            "description": "Uplink: Hurricane LAG",
                            "isLag": True,
                            "bandwidth": 10_000_000_000,
                            "duplex": "full",
                        },
                        {
                            "name": "et-0/0/1",
                            "description": "Uplink: Hurricane member",
                            "aggregateInterface": "ae5",
                            "bandwidth": 10_000_000_000,
                        },
                        {
                            "name": "ae5.0",
                            "description": "Uplink: Hurricane",
                            "isLogical": True,
                            "aggregateInterface": "ae5",
                            "forwardingModel": "routed",
                            "mtu": 9192,
                            "txPower": -2.5,
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
    ae5.description = "old lag desc"
    ae5.speed = 1000
    member = env.add_interface(dev, "et-0/0/1", iface_type="1000base-x")
    member.lag = None
    unit = env.add_interface(dev, "ae5.0", iface_type="virtual")
    unit.description = "old unit"
    unit.parent = None
    unit.mtu = 1500

    with patch.object(nc.pynetbox, "api", lambda url, token: env):
        with patch("netbox_checks.is_juniper_platform", return_value=True):
            with patch("netbox_checks.is_arista_platform", return_value=False):
                with patch("netbox_checks.get_device_platform_name", return_value="Juniper JunOS"):
                    with patch.object(nc, "_get_interface_ip_addresses", return_value=[]):
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
                                "--bandwidth",
                                "--duplex",
                                "--mtu",
                                "--tx-power",
                                "--forwarding-model",
                                "--lag",
                                "--parent",
                            ],
                        )
                        assert nc.main() == 0
    assert ae5.description == "Uplink: Hurricane LAG"
    assert unit.description == "Uplink: Hurricane"
    assert unit.mtu == 9192

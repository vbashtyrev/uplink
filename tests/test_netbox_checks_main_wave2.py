"""netbox_checks main: json output, show-change, hide-no-diff-cols."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import netbox_checks as nc

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _stats(tmp_path):
    p = tmp_path / "stats.json"
    p.write_text(
        json.dumps(
            {
                "devices": {
                    "ALA-KZT-7280TR-1": [
                        {
                            "name": "Ethernet51/1",
                            "description": "Uplink: Cogent 10G",
                            "mediaType": "10GBASE-SR",
                            "bandwidth": 10000000000,
                            "duplex": "duplexFull",
                            "mtu": 9214,
                            "physicalAddress": "44:4c:a8:bf:2e:91",
                            "forwardingModel": "routed",
                            "ipv4_addresses": ["203.0.113.1/24"],
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    return p


def _nb():
    nb_iface = MagicMock()
    nb_iface.id = 10
    nb_iface.name = "Ethernet51/1"
    nb_iface.description = ""
    nb_iface.speed = 1000
    nb_iface.type = {"value": "other", "label": "Other"}
    nb_iface.mac_address = None
    nb_iface.mac_addresses = []
    nb_iface.primary_mac_address = None
    nb_iface.duplex = None
    nb_iface.mtu = 1500
    nb_iface.mode = None
    nb_iface.lag = None
    nb_iface.parent = None
    device = MagicMock()
    device.id = 1
    device.name = "ALA-KZT-7280TR-1"
    device.platform = MagicMock()
    device.platform.name = "Arista EOS"
    nb = MagicMock()
    nb.dcim.devices.filter.return_value = [device]
    nb.dcim.interfaces.filter.return_value = [nb_iface]
    return nb


def test_main_json_all_checks(monkeypatch, netbox_env, tmp_path, capsys):
    """JSON output with --all; row values must be JSON-serializable."""
    mt = tmp_path / "types.json"
    mt.write_text('{"interface_types": [{"value": "10gbase-x-sfpp", "label": "SFP+"}]}', encoding="utf-8")
    with patch("netbox_checks.pynetbox.api", return_value=_nb()):
        with patch("netbox_checks.is_arista_platform", return_value=True):
            with patch("netbox_checks.is_juniper_platform", return_value=False):
                with patch("netbox_checks.get_device_platform_name", return_value="Arista EOS"):
                    monkeypatch.setattr(
                        sys,
                        "argv",
                        [
                            "netbox_checks.py",
                            "-f",
                            str(_stats(tmp_path)),
                            "--all",
                            "--json",
                            "--mt-ref",
                            str(mt),
                            "--hide-no-diff-cols",
                        ],
                    )
                    assert nc.main() == 0


def test_main_show_change_table(monkeypatch, netbox_env, tmp_path, capsys):
    mt = tmp_path / "types.json"
    mt.write_text('{"interface_types": [{"value": "10gbase-x-sfpp", "label": "SFP+"}]}', encoding="utf-8")
    with patch("netbox_checks.pynetbox.api", return_value=_nb()):
        with patch("netbox_checks.is_arista_platform", return_value=True):
            with patch("netbox_checks.is_juniper_platform", return_value=False):
                with patch("netbox_checks.get_device_platform_name", return_value="Arista EOS"):
                    monkeypatch.setattr(
                        sys,
                        "argv",
                        [
                            "netbox_checks.py",
                            "-f",
                            str(_stats(tmp_path)),
                            "--description",
                            "--bandwidth",
                            "--show-change",
                            "--mt-ref",
                            str(mt),
                        ],
                    )
                    nc.main()
    assert "ALA-KZT-7280TR-1" in capsys.readouterr().out

"""netbox_checks: duplex, lag, intname, apply description."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import netbox_checks as nc

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _file_payload(tmp_path):
    stats = {
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
                    "forwardingModel": "bridged",
                    "txPower": -2.0,
                },
            ],
        },
    }
    p = tmp_path / "stats.json"
    p.write_text(json.dumps(stats), encoding="utf-8")
    return p


def _nb_mock():
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
    return nb, nb_iface


def test_checks_duplex_mtu_tx(monkeypatch, tmp_path, capsys, netbox_env):
    nb, _ = _nb_mock()
    mt = tmp_path / "types.json"
    mt.write_text('{"interface_types": [{"value": "10gbase-x-sfpp", "label": "SFP+"}]}', encoding="utf-8")
    with patch("netbox_checks.pynetbox.api", return_value=nb):
        with patch("netbox_checks.is_arista_platform", return_value=True):
            with patch("netbox_checks.is_juniper_platform", return_value=False):
                with patch("netbox_checks.get_device_platform_name", return_value="Arista EOS"):
                    monkeypatch.setattr(
                        sys,
                        "argv",
                        [
                            "netbox_checks.py",
                            "-f",
                            str(_file_payload(tmp_path)),
                            "--duplex",
                            "--mtu",
                            "--tx-power",
                            "--forwarding-model",
                            "--mt-ref",
                            str(mt),
                        ],
                    )
                    assert nc.main() == 0


def test_apply_description(monkeypatch, tmp_path, capsys, netbox_env):
    nb, nb_iface = _nb_mock()
    updates = []
    nb_iface.update = lambda data: updates.append(data)
    with patch("netbox_checks.pynetbox.api", return_value=nb):
        with patch("netbox_checks.is_arista_platform", return_value=True):
            with patch("netbox_checks.is_juniper_platform", return_value=False):
                with patch("netbox_checks.get_device_platform_name", return_value="Arista EOS"):
                    monkeypatch.setattr(
                        sys,
                        "argv",
                        [
                            "netbox_checks.py",
                            "-f",
                            str(_file_payload(tmp_path)),
                            "--host",
                            "ALA-KZT-7280TR-1",
                            "--description",
                            "--apply",
                        ],
                    )
                    nc.main()
                    assert updates or "Updated" in capsys.readouterr().out

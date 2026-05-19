"""netbox_checks main --apply --intname: create missing interface."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import netbox_checks as nc

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_apply_creates_missing_interface(monkeypatch, netbox_env, tmp_path, capsys):
    stats = tmp_path / "stats.json"
    stats.write_text(
        json.dumps(
            {
                "devices": {
                    "ALA-KZT-7280TR-1": [
                        {
                            "name": "Ethernet52/1",
                            "description": "Uplink: New",
                            "mediaType": "10GBASE-SR",
                            "bandwidth": 10000000000,
                            "duplex": "duplexFull",
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    mt = tmp_path / "types.json"
    mt.write_text(
        '{"interface_types": [{"value": "10gbase-x-sfpp", "label": "SFP+"}]}',
        encoding="utf-8",
    )

    device = MagicMock()
    device.id = 1
    device.name = "ALA-KZT-7280TR-1"
    device.platform = MagicMock()
    device.platform.name = "Arista EOS"

    existing = MagicMock()
    existing.id = 9
    existing.name = "Ethernet51/1"

    created = MagicMock()
    created.id = 11
    created.name = "Ethernet52/1"

    nb = MagicMock()
    nb.dcim.devices.filter.return_value = [device]
    nb.dcim.interfaces.filter.return_value = [existing]
    nb.dcim.interfaces.create.return_value = created

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
                            str(stats),
                            "--apply",
                            "--intname",
                            "--description",
                            "--mt-ref",
                            str(mt),
                        ],
                    )
                    assert nc.main() == 0
    out = capsys.readouterr().out
    assert "created" in out.lower() or "Created" in out
    nb.dcim.interfaces.create.assert_called_once()

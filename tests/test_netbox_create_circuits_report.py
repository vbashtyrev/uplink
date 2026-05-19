"""netbox_create_circuits: virtual_to_physical, errors, report sections."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from netbox_create_circuits import main
from tests.mocks.netbox_full import NetBoxTestEnvironment

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_virtual_iface_and_errors(monkeypatch, netbox_env, tmp_path, capsys):
    env = NetBoxTestEnvironment()
    env.seed_for_create_circuits()
    dev = env.dcim.devices._items[0]
    dev.tag = "border"
    dev.site = 1
    env.add_interface(dev, "ae5", iface_type="lag")

    cr = tmp_path / "commit_rates.json"
    cr.write_text(
        json.dumps(
            {
                "FRN-MX-1": {
                    "ae5.0": {
                        "provider": "Hurricane",
                        "circuit_id": "CKT-MX-1",
                        "commit_rate_gbps": 10,
                    },
                    "Ethernet51/1": {
                        "provider": "Cogent",
                        "circuit_id": "",
                    },
                },
                "ALA-KZT-7280TR-1": {
                    "Ethernet99/1": {
                        "provider": "Cogent",
                        "circuit_id": "CKT-MISSING-IF",
                        "commit_rate_gbps": 10,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    dry = tmp_path / "dry.json"
    dry.write_text(
        json.dumps(
            {
                "devices": {
                    "FRN-MX-1": [
                        {
                            "name": "ae5.0",
                            "physicalInterface": "ae5",
                            "description": "Uplink: Hurricane",
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "netbox_create_circuits._patch_interface_mark_connected",
        lambda *a, **k: None,
    )
    with patch("netbox_create_circuits.pynetbox.api", lambda url, token: env):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "netbox_create_circuits.py",
                "-f",
                str(cr),
                "-d",
                str(dry),
            ],
        )
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "errors" in captured.out.lower() or "empty circuit_id" in captured.err

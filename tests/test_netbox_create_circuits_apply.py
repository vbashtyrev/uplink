"""netbox_create_circuits.main apply path (mocked NetBox)."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from netbox_create_circuits import main
from tests.mocks.netbox_full import NetBoxTestEnvironment

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_main_apply_circuit(monkeypatch, netbox_env, tmp_path, capsys):
    env = NetBoxTestEnvironment()
    env.seed_for_create_circuits()
    dev = env.dcim.devices._items[0]
    dev.tag = "border"
    dev.site = 1

    cr = tmp_path / "commit_rates.json"
    cr.write_text(
        json.dumps(
            {
                "ALA-KZT-7280TR-1": {
                    "Ethernet51/1": {
                        "provider": "Cogent",
                        "circuit_id": "CKT-ALA-1",
                        "commit_rate_gbps": 10,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "netbox_create_circuits._patch_interface_mark_connected",
        lambda nb, iid, val: None,
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
                str(FIXTURES / "dry_ssh_minimal.json"),
            ],
        )
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0

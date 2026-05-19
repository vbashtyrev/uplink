"""Tests for netbox_create_circuits.main() and create_termination_and_cable."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from netbox_create_circuits import create_termination_and_cable, main
from tests.mocks.netbox_full import NetBoxTestEnvironment

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_create_termination_and_cable_new(monkeypatch):
    env = NetBoxTestEnvironment()
    dev = env.add_device("ALA-KZT-7280TR-1")
    dev.site = 1
    iface = env.add_interface(dev, "Ethernet51/1")
    prov, _ = env._ensure_provider("Cogent")
    ct = env.circuits.circuit_types.create(name="Internet", slug="internet")
    circuit = env.circuits.circuits.create(
        cid="CKT-1",
        provider=prov.id,
        type=ct.id,
        commit_rate=10_000_000,
    )
    report = {"deleted_cables": [], "disabled_mark_connected": [], "created_cables": []}
    monkeypatch.setattr(
        "netbox_create_circuits._patch_interface_mark_connected",
        lambda nb, iid, val: None,
    )
    term, err = create_termination_and_cable(env, circuit, dev, iface, report=report)
    assert err is None
    assert term is not None
    assert report["created_cables"]


def test_main_dry_run(monkeypatch, netbox_env, tmp_path, capsys):
    env = NetBoxTestEnvironment()
    env.seed_for_create_circuits()
    cr = tmp_path / "commit_rates.json"
    cr.write_text(
        json.dumps(
            {
                "ALA-KZT-7280TR-1": {
                    "Ethernet51/1": {
                        "provider": "Cogent",
                        "circuit_id": "CKT-1",
                        "commit_rate_gbps": 10,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    dev = env.dcim.devices._items[0]
    dev.tag = "border"
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
                "--dry-run",
            ],
        )
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0

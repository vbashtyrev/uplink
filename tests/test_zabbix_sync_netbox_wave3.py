"""zabbix_sync_commit_rate NetBox inventory helpers (wave 3)."""

import sys
from unittest.mock import MagicMock

import pytest

from tests.mocks.netbox_api import build_netbox_for_commit_rates
from zabbix_sync_commit_rate import (
    fetch_uplink_inventory_report,
    load_burst_pairs,
    load_dry_ssh,
)


def test_load_burst_pairs_from_inventory():
    report = {
        "complete": [
            {
                "device": "H",
                "interface": "Eth1",
                "billing_model": "Burst",
                "provider": "Cogent",
                "circuit_id": "CKT-1",
            }
        ],
        "incomplete": [],
        "stats": {"complete": 1},
    }
    pairs = load_burst_pairs(inventory_report=report)
    assert ("H", "Eth1") in pairs


def test_load_dry_ssh_missing(tmp_path):
    assert load_dry_ssh(str(tmp_path / "nope.json")) is None


def test_fetch_inventory_auth_exit(monkeypatch):
    nb = MagicMock()
    nb.circuits.providers.all.side_effect = Exception("403 token expired")
    monkeypatch.setattr(sys, "exit", lambda code=0: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit) as exc:
        fetch_uplink_inventory_report(nb, tag="t", debug=True)
    assert exc.value.code == 1


def test_sync_inventory_file_skips_live_relations_collect(monkeypatch, zabbix_env, tmp_path):
    """--inventory-file without embedded relations must not live-fetch NetBox."""
    import json
    import sys

    import zabbix_sync_commit_rate as mod
    from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker

    inv = tmp_path / "inventory.json"
    inv.write_text(
        json.dumps(
            {
                "complete": [
                    {
                        "device": "ALA-KZT-7280TR-1",
                        "interface": "Ethernet51/1",
                        "provider": "Cogent",
                        "commit_rate_kbps": 10_000_000,
                    }
                ],
                "incomplete": [],
                "stats": {"complete": 1, "providers": 1},
                "provider_slo_percent": {"Cogent": 99.9},
                "provider_limits_gbps": {"Cogent": 10.0},
                "provider_slo_read": "ok",
                "provider_limits_read": "ok",
            }
        ),
        encoding="utf-8",
    )

    live_collect = MagicMock(side_effect=AssertionError("live relations collect must not run"))
    monkeypatch.setattr(mod, "collect_netbox_interface_relations", live_collect)
    build_standard_zabbix_mocker(
        hosts=[{"hostid": "101", "host": "ALA-KZT-7280TR-1", "name": "ALA-KZT-7280TR-1"}],
    ).activate(monkeypatch)
    monkeypatch.setattr(mod, "validate_zabbix_token", lambda *a, **k: True)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zabbix_sync_commit_rate.py",
            "--inventory-file",
            str(inv),
            "--dry-run",
            "--no-util-triggers",
        ],
    )
    mod.main()
    live_collect.assert_not_called()


def test_fetch_inventory_auth_on_devices_filter_exit(monkeypatch):
    nb = build_netbox_for_commit_rates()
    nb.dcim.devices.filter = lambda **kw: (_ for _ in ()).throw(Exception("403 forbidden"))
    monkeypatch.setattr(sys, "exit", lambda code=0: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit) as exc:
        fetch_uplink_inventory_report(nb, tag="border", debug=True)
    assert exc.value.code == 1

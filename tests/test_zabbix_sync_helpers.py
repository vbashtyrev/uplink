"""Tests for zabbix_sync_commit_rate.py helpers."""

import json
from pathlib import Path

from zabbix_sync_commit_rate import (
    interfaces_by_host_from_dry_ssh,
    is_physical_uplink_iface,
    load_burst_pairs,
)


def test_is_physical_uplink_iface():
    assert is_physical_uplink_iface({"name": "Ethernet51/1"}) is True
    assert is_physical_uplink_iface({"name": "ae5.0", "isLogical": True}) is False
    assert is_physical_uplink_iface({"name": "ae5", "isLag": True}) is False
    assert is_physical_uplink_iface({"name": "ae10"}) is False


def test_interfaces_by_host_from_dry_ssh_physical_only():
    devices = {
        "r1": [
            {"name": "Ethernet1"},
            {"name": "ae5.0", "isLogical": True},
            {"name": "ae5", "isLag": True},
        ],
    }
    all_ifaces = interfaces_by_host_from_dry_ssh(devices, physical_only=False)
    phys = interfaces_by_host_from_dry_ssh(devices, physical_only=True)
    assert all_ifaces["r1"] == ["Ethernet1", "ae5.0", "ae5"]
    assert phys["r1"] == ["Ethernet1"]


def test_load_burst_pairs(tmp_path: Path):
    path = tmp_path / "commit_rates.json"
    path.write_text(
        json.dumps(
            {
                "_provider_limits": {"P": 10},
                "host1": {
                    "Eth1": {"billing_model": "Burst", "circuit_id": "P-ALA-1"},
                    "Eth2": {"billing_model": "Flat"},
                },
            }
        ),
        encoding="utf-8",
    )
    pairs = load_burst_pairs(str(path))
    assert pairs == {("host1", "Eth1")}

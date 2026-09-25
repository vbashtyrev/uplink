"""load_burst_metadata and load_burst_pairs from inventory report."""

import json

from zabbix_sync_commit_rate import load_burst_metadata, load_burst_pairs


def _burst_inventory():
    return {
        "complete": [
            {
                "device": "H1",
                "interface": "Eth1",
                "provider": "Cogent",
                "circuit_id": "CKT-1",
                "billing_model": "Burst",
            },
            {
                "device": "H1",
                "interface": "Eth2",
                "provider": "Cogent",
                "billing_model": "Commit",
            },
        ],
        "incomplete": [],
        "stats": {"complete": 2},
    }


def test_load_burst_metadata_from_inventory():
    meta = load_burst_metadata(inventory_report=_burst_inventory())
    assert meta[("H1", "Eth1")] == {"provider": "Cogent", "circuit_id": "CKT-1"}
    pairs = load_burst_pairs(inventory_report=_burst_inventory())
    assert ("H1", "Eth1") in pairs
    assert ("H1", "Eth2") not in pairs


def test_load_burst_metadata_empty_without_inventory():
    assert load_burst_metadata(inventory_report=None) == {}
    assert load_burst_pairs(inventory_report=None) == set()

"""load_burst_metadata and related helpers."""

import json

from zabbix_sync_commit_rate import load_burst_metadata, load_burst_pairs


def test_load_burst_metadata(tmp_path):
    p = tmp_path / "cr.json"
    p.write_text(
        json.dumps(
            {
                "H1": {
                    "Eth1": {
                        "billing_model": "Burst",
                        "provider": "Cogent",
                        "circuit_id": "CKT-1",
                    },
                    "Eth2": {"billing_model": "Commit"},
                },
                "_meta": {},
            }
        ),
        encoding="utf-8",
    )
    meta = load_burst_metadata(str(p))
    assert meta[("H1", "Eth1")] == {"provider": "Cogent", "circuit_id": "CKT-1"}
    pairs = load_burst_pairs(str(p))
    assert ("H1", "Eth1") in pairs
    assert ("H1", "Eth2") not in pairs

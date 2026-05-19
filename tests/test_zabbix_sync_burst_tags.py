"""Burst trigger tag helpers (no API)."""

from zabbix_sync_commit_rate import (
    burst_link_trigger_tags_no_sla,
    burst_sla_breach_trigger_tags,
    load_burst_metadata,
)


def test_burst_link_trigger_tags():
    tags = burst_link_trigger_tags_no_sla("Cogent", "Cogent-ALA-1")
    assert {"tag": "provider", "value": "Cogent"} in tags
    assert {"tag": "circuit", "value": "Cogent-ALA-1"} in tags
    assert {"tag": "billing", "value": "burst"} in tags
    assert not any(t.get("tag") == "sla" for t in tags)


def test_burst_sla_breach_includes_sla_tag():
    tags = burst_sla_breach_trigger_tags("Cogent", "Cogent-ALA-1")
    assert {"tag": "sla", "value": "true"} in tags


def test_load_burst_metadata(tmp_path):
    path = tmp_path / "cr.json"
    path.write_text(
        """
        {
          "host1": {
            "Eth1": {
              "billing_model": "Burst",
              "provider": "Cogent",
              "circuit_id": "Cogent-ALA-1"
            },
            "Eth2": {"billing_model": "Flat", "provider": "X", "circuit_id": "X-1"}
          }
        }
        """,
        encoding="utf-8",
    )
    meta = load_burst_metadata(str(path))
    assert meta[("host1", "Eth1")] == {"provider": "Cogent", "circuit_id": "Cogent-ALA-1"}
    assert ("host1", "Eth2") not in meta

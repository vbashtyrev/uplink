"""Tests for grafana_uplinks_graph.build_edges deduplication."""

from grafana_uplinks_graph import build_edges


def test_build_edges_prefers_logical_with_items():
    devices = {
        "host1": [
            {"name": "ae5", "description": "Uplink: ISP", "isLag": True},
            {"name": "ae5.0", "description": "Uplink: ISP", "isLogical": True},
        ],
    }
    items = {
        ("host1", "ae5.0"): {"itemid_in": "111", "itemid_out": "222"},
    }
    edges = build_edges(devices, {"host1": "42"}, items, {"Uplink: ISP": "ISP"})
    assert len(edges) == 1
    assert edges[0][2] == "ae5.0"
    assert edges[0][4] == "111"

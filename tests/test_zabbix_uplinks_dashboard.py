"""Tests for zabbix_uplinks_dashboard.py helpers."""

from generate_commit_rates import is_uplink
from zabbix_uplinks_dashboard import (
    _build_edges,
    _item_pattern_escape,
    _location_from_hostname,
    _make_graph_widget,
)


def test_location_from_hostname():
    assert _location_from_hostname("ALA-KZT-7280TR-1") == "ALA"
    assert _location_from_hostname("single") == "single"


def test_item_pattern_escape():
    assert _item_pattern_escape("Eth[1]*") == "Eth\\[1\\]\\\\*"


def test_build_edges_skips_non_uplink():
    devices = {
        "h1": [
            {"name": "Eth1", "description": "Uplink: ISP"},
            {"name": "Eth2", "description": "management"},
        ],
    }
    edges = _build_edges(devices, {"h1": "1"}, {}, {"Uplink: ISP": "ISP"})
    assert len(edges) == 1
    assert edges[0][2] == "Eth1"


def test_make_graph_widget():
    w = _make_graph_widget(0, "host1", "Eth1", "ISP", "111", "222", x=0, y=0)
    assert w is not None
    assert w["type"] == "svggraph"
    assert "host1" in w["name"]
    field_names = [f["name"] for f in w["fields"]]
    assert "ds.0.hosts.0" in field_names


def test_make_graph_widget_no_items():
    assert _make_graph_widget(0, "h", "e", "ISP", "", "", x=0, y=0) is None


def test_is_uplink_reexport():
    assert is_uplink({"description": "Uplink: test"}) is True

"""zabbix_map layout and map helper coverage."""

from zabbix_map import (
    _compute_layout,
    _iface_from_trigger_desc,
    _normalize_interface_name,
    _normalize_provider_name,
    _selement_hostid,
)


def test_compute_layout_multi_isp():
    edges = [
        ("h1", "1", "eth1", "ISP-A", "i1", "o1", "ki", "ko", "desc1"),
        ("h2", "2", "eth2", "ISP-A", "i2", "o2", "ki", "ko", "desc2"),
        ("h3", "3", "eth3", "ISP-B", "i3", "o3", "ki", "ko", "desc3"),
    ]
    host_pos, isp_pos, w, h = _compute_layout(edges, 800, 600)
    assert "1" in host_pos
    assert "ISP-A" in isp_pos
    assert "ISP-B" in isp_pos
    assert w > 0 and h > 0


def test_trigger_desc_and_selement():
    assert _iface_from_trigger_desc("Interface Eth1: 90%") == "Eth1"
    assert _iface_from_trigger_desc("bad") is None
    el = {"elements": [{"hostid": "42"}]}
    assert _selement_hostid(el) == "42"
    assert _normalize_interface_name("Eth1/1") == "eth1/1"
    assert _normalize_provider_name("A-B_C") == "abc"

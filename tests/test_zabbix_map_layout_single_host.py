"""zabbix_map _compute_layout: single-host provider placement edge cases."""

from zabbix_map import _compute_layout


def test_compute_layout_two_hosts_same_isp():
    edges = [
        ("h1", "101", "eth1", "ISP-A", "i1", "o1", "ki", "ko", "d1"),
        ("h2", "102", "eth2", "ISP-A", "i2", "o2", "ki", "ko", "d2"),
    ]
    host_pos, isp_pos, w, h = _compute_layout(edges, 1400, 900)
    assert "101" in host_pos
    assert "102" in host_pos
    assert "ISP-A" in isp_pos


def test_compute_layout_single_host_isp():
    edges = [
        ("only-host", "201", "eth0", "Lonely-ISP", "i1", "o1", "ki", "ko", "d"),
    ]
    host_pos, isp_pos, w, h = _compute_layout(edges, 1200, 800)
    assert "201" in host_pos
    assert "Lonely-ISP" in isp_pos

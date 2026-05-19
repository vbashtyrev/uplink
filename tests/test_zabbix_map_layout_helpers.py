"""zabbix_map layout helpers: placement and collision."""

from zabbix_map import (
    _compute_layout,
    _is_free,
    _occupied_positions,
    _place_single_host_provider,
)


def test_occupied_and_is_free():
    host_pos = {"h1": (100, 100)}
    isp_pos = {"ISP": (300, 100)}
    occ = _occupied_positions(host_pos, isp_pos)
    assert (100, 100) in occ
    assert _is_free(500, 100, occ, 50) is True
    assert _is_free(110, 100, occ, 50) is False


def test_place_single_host_provider():
    host_pos = {"h1": (200, 200)}
    isp_pos = {}
    xy = _place_single_host_provider(200, 200, host_pos, isp_pos)
    assert isinstance(xy, tuple)
    assert len(xy) == 2


def test_compute_layout_single_host_per_isp():
    edges = [
        ("h1", "1", "eth1", "ISP-A", "i1", "o1", "ki", "ko", "d"),
        ("h2", "2", "eth2", "ISP-B", "i2", "o2", "ki", "ko", "d"),
    ]
    host_pos, isp_pos, w, h = _compute_layout(edges, 1200, 800)
    assert "1" in host_pos
    assert "ISP-A" in isp_pos

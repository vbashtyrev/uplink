"""Regression tests for zabbix_map layout: Piter-IX MSK and shared-host providers."""

from zabbix_map import (
    LAYOUT_MIN_DISTANCE,
    MAP_HEIGHT,
    MAP_WIDTH,
    SELEMENT_HEIGHT,
    SELEMENT_WIDTH,
    _compute_layout,
)


def _assert_layout_within_bounds(host_pos, isp_pos, width, height):
    for name, (x, y) in {**host_pos, **isp_pos}.items():
        assert x >= 0, f"{name} x={x} is negative"
        assert y >= 0, f"{name} y={y} is negative"
        assert x + SELEMENT_WIDTH <= width, f"{name} exceeds map width ({x}+{SELEMENT_WIDTH}>{width})"
        assert y + SELEMENT_HEIGHT <= height, f"{name} exceeds map height ({y}+{SELEMENT_HEIGHT}>{height})"


def _assert_no_collisions(host_pos, isp_pos):
    positions = list(host_pos.values()) + list(isp_pos.values())
    for i, (x1, y1) in enumerate(positions):
        for x2, y2 in positions[i + 1 :]:
            dist_sq = (x1 - x2) ** 2 + (y1 - y2) ** 2
            assert dist_sq >= LAYOUT_MIN_DISTANCE ** 2, (
                f"elements too close: ({x1},{y1}) vs ({x2},{y2}), dist={dist_sq ** 0.5:.1f}"
            )


def test_compute_layout_two_piter_msk_hosts():
    """Both MSK Piter-IX uplinks must fit on the map without overlap."""
    edges = [
        ("MSK-M9-MX204-1", "101", "et-0/0/2", "Piter-IX", "i1", "o1", "ki", "ko", "d1"),
        ("MSK-M9-MX204-2", "102", "et-0/0/2", "Piter-IX", "i2", "o2", "ki", "ko", "d2"),
    ]
    host_pos, isp_pos, width, height = _compute_layout(edges, MAP_WIDTH, MAP_HEIGHT)

    assert "101" in host_pos
    assert "102" in host_pos
    assert "Piter-IX" in isp_pos
    _assert_layout_within_bounds(host_pos, isp_pos, width, height)
    _assert_no_collisions(host_pos, isp_pos)


def test_compute_layout_host_already_at_other_provider():
    """Single-host ISP must not be placed outside map when host is in another block."""
    edges = [
        ("MSK-M9-MX204-1", "101", "et-0/0/2", "Piter-IX", "i1", "o1", "ki", "ko", "d1"),
        ("MSK-M9-MX204-1", "101", "et-0/0/3", "Other-ISP", "i3", "o3", "ki", "ko", "d3"),
        ("MSK-M9-MX204-2", "102", "et-0/0/2", "Piter-IX", "i2", "o2", "ki", "ko", "d2"),
    ]
    host_pos, isp_pos, width, height = _compute_layout(edges, MAP_WIDTH, MAP_HEIGHT)

    assert "Other-ISP" in isp_pos
    assert isp_pos["Other-ISP"][0] >= 0
    _assert_layout_within_bounds(host_pos, isp_pos, width, height)
    _assert_no_collisions(host_pos, isp_pos)


def test_compute_layout_msk_multi_provider_with_orphan_isps():
    """Orphan single-host providers near already-placed Piter hosts stay in bounds."""
    edges = [
        ("MSK-M9-MX204-1", "101", "et-0/0/2", "Piter-IX", "i1", "o1", "ki", "ko", "d1"),
        ("MSK-M9-MX204-2", "102", "et-0/0/2", "Piter-IX", "i2", "o2", "ki", "ko", "d2"),
        ("MSK-M9-MX204-1", "101", "ae5.0", "Cogent", "i3", "o3", "ki", "ko", "d3"),
        ("MSK-M9-MX204-2", "102", "ae5.0", "Cogent", "i4", "o4", "ki", "ko", "d4"),
        ("MSK-M9-MX204-1", "101", "et-0/0/1", "Hurricane", "i5", "o5", "ki", "ko", "d5"),
        ("MSK-M9-MX204-2", "102", "et-0/0/1", "RETN", "i6", "o6", "ki", "ko", "d6"),
    ]
    host_pos, isp_pos, width, height = _compute_layout(edges, MAP_WIDTH, MAP_HEIGHT)

    assert len(host_pos) == 2
    assert len(isp_pos) == 4
    _assert_layout_within_bounds(host_pos, isp_pos, width, height)
    _assert_no_collisions(host_pos, isp_pos)

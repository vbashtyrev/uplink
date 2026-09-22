"""Regression tests for zabbix_map layout: Piter-IX MSK and shared-host providers."""

from zabbix_map import (
    LAYOUT_ELEMENT_GAP,
    MAP_HEIGHT,
    MAP_WIDTH,
    SELEMENT_HEIGHT,
    SELEMENT_WIDTH,
    _bounds_overlap,
    _compute_layout,
    _element_bounds,
    _inflated_bounds,
)


def _assert_layout_within_bounds(host_pos, isp_pos, width, height):
    for name, (x, y) in {**host_pos, **isp_pos}.items():
        assert x >= 0, f"{name} x={x} is negative"
        assert y >= 0, f"{name} y={y} is negative"
        assert x + SELEMENT_WIDTH <= width, f"{name} exceeds map width ({x}+{SELEMENT_WIDTH}>{width})"
        assert y + SELEMENT_HEIGHT <= height, f"{name} exceeds map height ({y}+{SELEMENT_HEIGHT}>{height})"


def _assert_no_rect_collisions(host_pos, isp_pos, gap=LAYOUT_ELEMENT_GAP):
    positions = list(host_pos.items()) + list(isp_pos.items())
    for i, (name1, (x1, y1)) in enumerate(positions):
        for name2, (x2, y2) in positions[i + 1 :]:
            inflated1 = _inflated_bounds(x1, y1, gap)
            rect2 = _element_bounds(x2, y2)
            assert not _bounds_overlap(inflated1, rect2), (
                f"{name1} at ({x1},{y1}) overlaps {name2} at ({x2},{y2}) "
                f"(gap={gap}px)"
            )


def test_compute_layout_adjacent_multi_host_isps_no_overlap():
    """Two adjacent multi-host ISP blocks must not overlap at map width 1200."""
    edges = [
        ("h1", "1", "eth1", "ISP-A", "i1", "o1", "ki", "ko", "d1"),
        ("h2", "2", "eth2", "ISP-A", "i2", "o2", "ki", "ko", "d2"),
        ("h3", "3", "eth3", "ISP-B", "i3", "o3", "ki", "ko", "d3"),
        ("h4", "4", "eth4", "ISP-B", "i4", "o4", "ki", "ko", "d4"),
    ]
    host_pos, isp_pos, width, height = _compute_layout(edges, MAP_WIDTH, MAP_HEIGHT)

    assert len(host_pos) == 4
    assert "ISP-A" in isp_pos
    assert "ISP-B" in isp_pos
    _assert_layout_within_bounds(host_pos, isp_pos, width, height)
    _assert_no_rect_collisions(host_pos, isp_pos)


def test_compute_layout_adjacent_single_host_isps_block_gap():
    """Two adjacent single-host ISP blocks keep at least 40px rectangle gap."""
    edges = [
        ("h1", "1", "eth1", "ISP-A", "i1", "o1", "ki", "ko", "d1"),
        ("h2", "2", "eth2", "ISP-B", "i2", "o2", "ki", "ko", "d2"),
    ]
    host_pos, isp_pos, width, height = _compute_layout(edges, MAP_WIDTH, MAP_HEIGHT)

    _assert_layout_within_bounds(host_pos, isp_pos, width, height)
    _assert_no_rect_collisions(host_pos, isp_pos)

    positions = {**host_pos, **isp_pos}
    rects = [(name, _element_bounds(x, y)) for name, (x, y) in positions.items()]
    isp_a_rects = [r for n, r in rects if n in ("ISP-A", "1")]
    isp_b_rects = [r for n, r in rects if n in ("ISP-B", "2")]
    a_right = max(r[2] for r in isp_a_rects)
    b_left = min(r[0] for r in isp_b_rects)
    assert b_left - a_right >= LAYOUT_ELEMENT_GAP, (
        f"gap between ISP-A and ISP-B blocks is {b_left - a_right}px, expected >= {LAYOUT_ELEMENT_GAP}"
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
    _assert_no_rect_collisions(host_pos, isp_pos)


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
    _assert_no_rect_collisions(host_pos, isp_pos)


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
    _assert_no_rect_collisions(host_pos, isp_pos)


def test_compute_layout_multi_host_orphan_provider_no_overlap():
    """Multi-host ISP whose hosts are already placed must not overlap existing rectangles."""
    edges = [
        ("h1", "1", "eth1", "Main-ISP", "i1", "o1", "ki", "ko", "d1"),
        ("h2", "2", "eth2", "Main-ISP", "i2", "o2", "ki", "ko", "d2"),
        ("h1", "1", "eth3", "Shared-ISP", "i3", "o3", "ki", "ko", "d3"),
        ("h2", "2", "eth4", "Shared-ISP", "i4", "o4", "ki", "ko", "d4"),
    ]
    host_pos, isp_pos, width, height = _compute_layout(edges, MAP_WIDTH, MAP_HEIGHT)

    assert "Shared-ISP" in isp_pos
    assert len(host_pos) == 2
    _assert_no_rect_collisions(host_pos, isp_pos)


def test_compute_layout_many_orphans_on_small_map_no_overlap():
    """Many orphan providers on a small nominal map expand layout without overlap."""
    edges = [("h1", "1", "eth1", "Main-ISP", "i1", "o1", "ki", "ko", "d1")]
    for i in range(12):
        edges.append(
            ("h1", "1", f"eth{i + 2}", f"Orphan-{i}", f"i{i}", f"o{i}", "ki", "ko", f"d{i}"),
        )
    host_pos, isp_pos, width, height = _compute_layout(edges, 400, 400)

    assert len(isp_pos) == 13
    _assert_no_rect_collisions(host_pos, isp_pos)
    assert width > 400 or height > 400


def test_compute_layout_shared_host_provider_no_host_overlap():
    """Regression: orphan provider for shared host must not overlap host rectangle."""
    edges = [
        ("MSK-M9-MX204-1", "101", "et-0/0/2", "Piter-IX", "i1", "o1", "ki", "ko", "d1"),
        ("MSK-M9-MX204-2", "102", "et-0/0/2", "Piter-IX", "i2", "o2", "ki", "ko", "d2"),
        ("MSK-M9-MX204-2", "102", "ae5.0", "Cogent", "i4", "o4", "ki", "ko", "d4"),
    ]
    host_pos, isp_pos, width, height = _compute_layout(edges, MAP_WIDTH, MAP_HEIGHT)

    hx, hy = host_pos["102"]
    px, py = isp_pos["Cogent"]
    assert not _bounds_overlap(
        _inflated_bounds(px, py, LAYOUT_ELEMENT_GAP),
        _element_bounds(hx, hy),
    ), f"Cogent provider ({px},{py}) overlaps host 102 ({hx},{hy})"
    _assert_layout_within_bounds(host_pos, isp_pos, width, height)
    _assert_no_rect_collisions(host_pos, isp_pos)

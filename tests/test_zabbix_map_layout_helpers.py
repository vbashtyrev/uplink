"""zabbix_map layout helpers: placement and collision."""

from zabbix_map import (
    LAYOUT_ELEMENT_GAP,
    SELEMENT_WIDTH,
    _bounds_overlap,
    _compute_layout,
    _element_bounds,
    _find_free_layout_position,
    _inflated_bounds,
    _is_free,
    _occupied_positions,
    _place_single_host_provider,
)


def test_occupied_and_is_free():
    host_pos = {"h1": (100, 100)}
    isp_pos = {"ISP": (300, 100)}
    occ = _occupied_positions(host_pos, isp_pos)
    assert (100, 100) in occ
    assert _is_free(540, 100, occ) is True
    assert _is_free(110, 100, occ) is False
    # 50px gap to host right edge only: host ends at 300, candidate starts at 350
    host_only = [(100, 100)]
    assert _is_free(350, 100, host_only, gap=50) is True
    assert _is_free(340, 100, host_only, gap=50) is False
    assert _is_free(100 + SELEMENT_WIDTH + LAYOUT_ELEMENT_GAP, 100, host_only) is True


def test_place_single_host_provider():
    host_pos = {"h1": (200, 200)}
    isp_pos = {}
    xy = _place_single_host_provider(200, 200, host_pos, isp_pos)
    assert isinstance(xy, tuple)
    assert len(xy) == 2
    assert _is_free(xy[0], xy[1], _occupied_positions(host_pos, isp_pos))


def test_place_single_host_provider_dense_grid_no_overlap():
    """Dense occupancy around host: placement must expand, never overlap."""
    step = SELEMENT_WIDTH + LAYOUT_ELEMENT_GAP
    hx, hy = 500, 500
    host_pos = {"h1": (hx, hy)}
    isp_pos = {
        f"p{dx}_{dy}": (hx + dx * step, hy + dy * step)
        for dx in range(-4, 5)
        for dy in range(-4, 5)
        if not (dx == 0 and dy == 0)
    }
    xy = _place_single_host_provider(hx, hy, host_pos, isp_pos)
    occ = _occupied_positions(host_pos, isp_pos)
    assert _is_free(xy[0], xy[1], occ)
    inflated = _inflated_bounds(xy[0], xy[1], LAYOUT_ELEMENT_GAP)
    for ox, oy in occ:
        assert not _bounds_overlap(inflated, _element_bounds(ox, oy))


def test_find_free_layout_position_never_returns_overlapping_fallback():
    """Even on a tiny nominal map, search expands until a collision-free slot exists."""
    step = SELEMENT_WIDTH + LAYOUT_ELEMENT_GAP
    near_x, near_y = 100, 100
    host_pos = {"h1": (near_x, near_y)}
    isp_pos = {
        "blocker": (near_x + step, near_y),
        "blocker2": (near_x - step, near_y),
        "blocker3": (near_x, near_y + step),
        "blocker4": (near_x, near_y - step),
    }
    xy = _find_free_layout_position(near_x, near_y, host_pos, isp_pos, map_width=300, map_height=300)
    occ = _occupied_positions(host_pos, isp_pos)
    assert _is_free(xy[0], xy[1], occ)
    assert xy != (near_x - step, near_y) or _is_free(near_x - step, near_y, occ)


def test_compute_layout_single_host_per_isp():
    edges = [
        ("h1", "1", "eth1", "ISP-A", "i1", "o1", "ki", "ko", "d"),
        ("h2", "2", "eth2", "ISP-B", "i2", "o2", "ki", "ko", "d"),
    ]
    host_pos, isp_pos, w, h = _compute_layout(edges, 1200, 800)
    assert "1" in host_pos
    assert "ISP-A" in isp_pos

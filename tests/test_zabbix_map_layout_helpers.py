"""zabbix_map layout helpers: placement and collision."""

from zabbix_map import (
    LAYOUT_ELEMENT_GAP,
    SELEMENT_WIDTH,
    _bounds_overlap,
    _compute_layout_validated,
    _element_bounds,
    _find_layout_collisions,
    _inflated_bounds,
    _is_free,
    _layout_visible_boxes,
    _occupied_positions,
    _validate_edges_layout,
)


def test_occupied_and_is_free():
    host_pos = {"h1": (100, 100)}
    isp_pos = {"ISP": (300, 100)}
    occ = _occupied_positions(host_pos, isp_pos)
    assert (100, 100) in occ
    assert _is_free(540, 100, occ) is True
    assert _is_free(110, 100, occ) is False
    host_only = [(100, 100)]
    assert _is_free(350, 100, host_only, gap=50) is True
    assert _is_free(340, 100, host_only, gap=50) is False
    assert _is_free(100 + SELEMENT_WIDTH + LAYOUT_ELEMENT_GAP, 100, host_only) is True


def test_orphan_provider_validated_without_icon_overlap():
    edges = [
        ("h1", "1", "eth1", "Main-ISP", "i1", "o1", "ki", "ko", "d1"),
        ("h1", "1", "eth2", "Orphan-ISP", "i2", "o2", "ki", "ko", "d2"),
    ]
    host_pos, isp_pos, w, h = _compute_layout_validated(edges, 1200, 800)
    assert "Orphan-ISP" in isp_pos
    assert _validate_edges_layout(edges, host_pos, isp_pos) == []


def test_find_layout_collisions_never_returns_overlapping_boxes():
    boxes = [
        {"id": "a-icon", "kind": "icon", "owner_id": "a", "bounds": (100, 100, 300, 300)},
        {"id": "b-icon", "kind": "icon", "owner_id": "b", "bounds": (500, 100, 700, 300)},
    ]
    assert _find_layout_collisions(boxes, gap=LAYOUT_ELEMENT_GAP) == []


def test_compute_layout_single_host_per_isp():
    edges = [
        ("h1", "1", "eth1", "ISP-A", "i1", "o1", "ki", "ko", "d"),
        ("h2", "2", "eth2", "ISP-B", "i2", "o2", "ki", "ko", "d"),
    ]
    host_pos, isp_pos, w, h = _compute_layout_validated(edges, 1200, 800)
    assert "1" in host_pos
    assert "ISP-A" in isp_pos
    assert _validate_edges_layout(edges, host_pos, isp_pos) == []

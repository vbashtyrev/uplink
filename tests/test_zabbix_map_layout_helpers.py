"""zabbix_map layout helpers: placement and collision."""

from zabbix_map import (
    LAYOUT_ELEMENT_GAP,
    RENDER_ICON_INSET,
    RENDER_ICON_SIZE,
    SELEMENT_HEIGHT,
    SELEMENT_WIDTH,
    _bounds_overlap,
    _compute_layout_validated,
    _element_bounds,
    _element_render_icon_bounds,
    _find_layout_collisions,
    _inflated_bounds,
    _layout_visible_boxes,
    _render_icon_inflated_bounds,
    _validate_edges_layout,
    _validate_layout_segment_collisions,
    _build_layout_model_from_edges,
)


def test_orphan_provider_validated_without_icon_overlap():
    edges = [
        ("h1", "1", "eth1", "Main-ISP", "i1", "o1", "ki", "ko", "d1"),
        ("h1", "1", "eth2", "Orphan-ISP", "i2", "o2", "ki", "ko", "d2"),
    ]
    host_pos, isp_pos, w, h = _compute_layout_validated(edges, 1200, 800)
    assert "Orphan-ISP" in isp_pos
    assert _validate_edges_layout(edges, host_pos, isp_pos) == []
    links = _build_layout_model_from_edges(edges, host_pos, isp_pos)[1]
    assert _validate_layout_segment_collisions(host_pos, isp_pos, links) == []


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


def test_icon_bounds_and_inflated_bounds_helpers():
    rect = _element_bounds(100, 100)
    assert rect == (100, 100, 100 + SELEMENT_WIDTH, 100 + SELEMENT_HEIGHT)
    inflated = _inflated_bounds(100, 100, 10)
    assert _bounds_overlap(inflated, rect)
    render = _element_render_icon_bounds(100, 100)
    assert render == (
        100 + RENDER_ICON_INSET,
        100 + RENDER_ICON_INSET,
        100 + RENDER_ICON_INSET + RENDER_ICON_SIZE,
        100 + RENDER_ICON_INSET + RENDER_ICON_SIZE,
    )
    render_inflated = _render_icon_inflated_bounds(100, 100, 10)
    assert _bounds_overlap(render_inflated, render)

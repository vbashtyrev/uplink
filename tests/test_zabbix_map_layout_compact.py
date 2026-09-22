"""Compact provider-block layout: representative inventory and AABB sizing."""

from zabbix_map import (
    LAYOUT_ELEMENT_GAP,
    LAYOUT_MARGIN,
    MAP_HEIGHT,
    MAP_WIDTH,
    SELEMENT_HEIGHT,
    SELEMENT_WIDTH,
    _bounds_overlap,
    _compute_layout,
    _element_bounds,
    _inflated_bounds,
    _layout_block_width,
    _layout_required_dimensions,
    _layout_step,
)


def _edge(hostname, hostid, iface, isp, suffix=""):
    tag = suffix or hostid
    return (hostname, hostid, iface, isp, f"i{tag}", f"o{tag}", "ki", "ko", f"d{tag}")


def _representative_inventory_edges():
    """18 edges, 9 providers, 15 hosts (production-like counts)."""
    edges = []
    for hostid in ("101", "102", "103", "104"):
        edges.append(_edge(f"h-{hostid}", hostid, "eth-c", "Cogent", hostid))
    for hostid in ("105", "106", "107", "108"):
        edges.append(_edge(f"h-{hostid}", hostid, "eth-h", "Hurricane", hostid))
    for hostid in ("109", "110"):
        edges.append(_edge(f"h-{hostid}", hostid, "eth-p", "Piter-IX", hostid))
    for hostid in ("111", "112"):
        edges.append(_edge(f"h-{hostid}", hostid, "eth-k", "KZT", hostid))
    for hostid in ("113", "114"):
        edges.append(_edge(f"h-{hostid}", hostid, "eth-i", "InfiniVAN", hostid))
    edges.append(_edge("h-101", "101", "eth-r", "RETN", "retn"))
    edges.append(_edge("h-105", "105", "eth-f", "Fiord", "fiord"))
    edges.append(_edge("h-115", "115", "eth-e", "Ertelecom", "ert"))
    edges.append(_edge("h-102", "102", "eth-l", "Level3", "lvl"))
    return edges


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


def _multi_host_providers_on_same_shelf(isp_pos, min_count=2):
    """Return provider names grouped by provider-row y coordinate."""
    by_y = {}
    for name, (x, y) in isp_pos.items():
        by_y.setdefault(y, []).append(name)
    shelves = [names for names in by_y.values() if len(names) >= min_count]
    return shelves


def _assert_multi_host_block_geometry(isp_name, host_ids, isp_pos, host_pos):
    """Provider at block origin; hosts in 2-column grid below."""
    step = _layout_step()
    bx, by = isp_pos[isp_name]
    assert isp_pos[isp_name] == (bx, by)
    expected = [(0, step), (step, step)]
    if len(host_ids) > 2:
        expected.extend([(0, step + step), (step, step + step)])
    actual = sorted(
        ((host_pos[hid][0] - bx, host_pos[hid][1] - by) for hid in host_ids),
        key=lambda t: (t[1], t[0]),
    )
    assert actual == expected[: len(host_ids)], (
        f"{isp_name} hosts scattered: got {actual}, expected {expected[: len(host_ids)]}"
    )


def _assert_shelf_gaps(isp_pos, host_pos, min_gap=LAYOUT_ELEMENT_GAP):
    """Vertical gap between provider shelves is at least min_gap."""
    shelf_tops = sorted({y for y, _x in isp_pos.values()})
    for top_a, top_b in zip(shelf_tops, shelf_tops[1:]):
        bottom_a = max(
            y + SELEMENT_HEIGHT
            for name, (x, y) in {**host_pos, **isp_pos}.items()
            if y == top_a or (name in isp_pos and isp_pos[name][1] == top_a)
        )
        assert top_b - bottom_a >= min_gap, (
            f"shelf gap {top_b - bottom_a}px between y={top_a} and y={top_b}, expected >= {min_gap}"
        )


def test_compute_layout_adjacent_multi_host_blocks_keep_geometry():
    edges = [
        ("h1", "1", "eth1", "ISP-A", "i1", "o1", "ki", "ko", "d1"),
        ("h2", "2", "eth2", "ISP-A", "i2", "o2", "ki", "ko", "d2"),
        ("h3", "3", "eth3", "ISP-B", "i3", "o3", "ki", "ko", "d3"),
        ("h4", "4", "eth4", "ISP-B", "i4", "o4", "ki", "ko", "d4"),
    ]
    host_pos, isp_pos, width, height = _compute_layout(edges, MAP_WIDTH, MAP_HEIGHT)

    _assert_multi_host_block_geometry("ISP-A", ["1", "2"], isp_pos, host_pos)
    _assert_multi_host_block_geometry("ISP-B", ["3", "4"], isp_pos, host_pos)
    assert isp_pos["ISP-A"][1] == isp_pos["ISP-B"][1]
    _assert_no_rect_collisions(host_pos, isp_pos)


def test_compute_layout_wrapped_multi_host_blocks_keep_geometry():
    """Forced wrap must preserve block geometry and shelf gap."""
    # Three 4-host blocks exceed one shelf at width 1200.
    edges = []
    for isp_idx, isp in enumerate(("ISP-A", "ISP-B", "ISP-C")):
        for host_idx in range(4):
            hid = str(isp_idx * 10 + host_idx + 1)
            edges.append(_edge(f"h-{hid}", hid, "eth0", isp, hid))
    host_pos, isp_pos, width, height = _compute_layout(edges, 1200, MAP_HEIGHT)

    _assert_multi_host_block_geometry("ISP-A", ["1", "2", "3", "4"], isp_pos, host_pos)
    _assert_multi_host_block_geometry("ISP-B", ["11", "12", "13", "14"], isp_pos, host_pos)
    _assert_multi_host_block_geometry("ISP-C", ["21", "22", "23", "24"], isp_pos, host_pos)
    assert isp_pos["ISP-A"][1] == isp_pos["ISP-B"][1]
    assert isp_pos["ISP-C"][1] > isp_pos["ISP-A"][1]
    _assert_shelf_gaps(isp_pos, host_pos)
    _assert_no_rect_collisions(host_pos, isp_pos)


def test_compute_layout_representative_inventory_compact_at_1460():
    edges = _representative_inventory_edges()
    host_pos, isp_pos, width, height = _compute_layout(edges, MAP_WIDTH, MAP_HEIGHT)

    assert len(host_pos) == 15
    assert len(isp_pos) == 9
    _assert_layout_within_bounds(host_pos, isp_pos, width, height)
    _assert_no_rect_collisions(host_pos, isp_pos)
    assert height <= 1300, f"expected compact height at {MAP_WIDTH}, got {height}"

    shared_shelves = _multi_host_providers_on_same_shelf(isp_pos, min_count=2)
    assert shared_shelves, "expected at least two multi-host provider blocks on one shelf"
    assert isp_pos["Cogent"][1] == isp_pos["Hurricane"][1]
    _assert_multi_host_block_geometry("KZT", ["111", "112"], isp_pos, host_pos)
    _assert_multi_host_block_geometry("InfiniVAN", ["113", "114"], isp_pos, host_pos)
    _assert_shelf_gaps(isp_pos, host_pos)


def test_compute_layout_representative_inventory_compact_at_1200():
    edges = _representative_inventory_edges()
    host_pos, isp_pos, width, height = _compute_layout(edges, 1200, MAP_HEIGHT)

    assert len(host_pos) == 15
    assert len(isp_pos) == 9
    _assert_layout_within_bounds(host_pos, isp_pos, width, height)
    _assert_no_rect_collisions(host_pos, isp_pos)
    assert height <= 1800, f"expected bounded height at 1200, got {height}"
    _assert_multi_host_block_geometry("KZT", ["111", "112"], isp_pos, host_pos)
    _assert_multi_host_block_geometry("InfiniVAN", ["113", "114"], isp_pos, host_pos)


def test_compute_layout_orphan_does_not_reserve_shelf():
    """Orphan provider must not consume a full block slot on the shelf."""
    edges = [
        ("h1", "1", "eth1", "Main-ISP", "i1", "o1", "ki", "ko", "d1"),
        ("h1", "1", "eth2", "Orphan-ISP", "i2", "o2", "ki", "ko", "d2"),
        ("h2", "2", "eth3", "Next-ISP", "i3", "o3", "ki", "ko", "d3"),
    ]
    host_pos, isp_pos, width, height = _compute_layout(edges, MAP_WIDTH, MAP_HEIGHT)

    assert len(host_pos) == 2
    assert len(isp_pos) == 3
    _assert_no_rect_collisions(host_pos, isp_pos)
    assert isp_pos["Main-ISP"][1] == isp_pos["Next-ISP"][1]
    assert isp_pos["Next-ISP"][0] == LAYOUT_MARGIN + _layout_block_width() + LAYOUT_ELEMENT_GAP


def test_compute_layout_required_dimensions_follow_aabb():
    edges = _representative_inventory_edges()
    host_pos, isp_pos, width, height = _compute_layout(edges, MAP_WIDTH, MAP_HEIGHT)
    expected_w, expected_h = _layout_required_dimensions(host_pos, isp_pos)

    assert width == expected_w
    assert height == expected_h

    max_right = max(x + SELEMENT_WIDTH for x, y in list(host_pos.values()) + list(isp_pos.values()))
    max_bottom = max(y + SELEMENT_HEIGHT for x, y in list(host_pos.values()) + list(isp_pos.values()))
    assert width == max_right + LAYOUT_MARGIN + LAYOUT_ELEMENT_GAP
    assert height == max_bottom + LAYOUT_MARGIN + LAYOUT_ELEMENT_GAP

"""Compact provider-block layout: inventory model, label-aware validation."""

from zabbix_map import (
    ELEMENT_TYPE_HOST,
    ELEMENT_TYPE_IMAGE,
    LABEL_LOCATION_BOTTOM,
    LABEL_LOCATION_TOP,
    LAYOUT_ELEMENT_GAP,
    LAYOUT_HOST_LABEL_LOCATION,
    LAYOUT_MARGIN,
    LAYOUT_PROVIDER_LABEL_LOCATION,
    MAP_HEIGHT,
    MAP_WIDTH,
    SELEMENT_HEIGHT,
    SELEMENT_WIDTH,
    _assign_layout_label_locations,
    _bounds_overlap,
    _build_layout_model_from_edges,
    _build_link_label,
    _compute_layout_validated,
    _element_bounds,
    _find_layout_collisions,
    _inflated_bounds,
    _layout_block_width,
    _layout_host_row_step,
    _layout_host_row_offset,
    _layout_icon_step,
    _layout_provider_label_corridor,
    _layout_single_host_horizontal_offset,
    _layout_required_dimensions,
    _layout_visible_boxes,
    _layout_positions_from_model,
    _validate_edges_layout,
    _validate_layout_model,
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


def _assert_no_visible_collisions(edges, host_pos, isp_pos):
    collisions = _validate_edges_layout(edges, host_pos, isp_pos)
    assert collisions == [], (
        "label-aware layout collisions: {}".format(
            ", ".join("{}↔{}".format(c["box_a"], c["box_b"]) for c in collisions[:8]),
        )
    )


def _assert_explicit_label_locations(selements, host_label_locations, isp_label_locations):
    for el in selements:
        etype = int(el.get("elementtype", 0))
        if etype == ELEMENT_TYPE_HOST:
            hid = str(el.get("hostid"))
            assert el.get("label_location") == host_label_locations[hid]
            assert el.get("label_location") == LABEL_LOCATION_BOTTOM
            assert el.get("label_location") != -1
        elif etype == ELEMENT_TYPE_IMAGE:
            isp = el.get("label")
            assert el.get("label_location") == isp_label_locations[isp]
            assert el.get("label_location") == LABEL_LOCATION_TOP
            assert el.get("label_location") != -1


def _multi_host_providers_on_same_shelf(isp_pos, min_count=2):
    by_y = {}
    for name, (x, y) in isp_pos.items():
        by_y.setdefault(y, []).append(name)
    return [names for names in by_y.values() if len(names) >= min_count]


def _assert_multi_host_block_geometry(isp_name, host_ids, isp_pos, host_pos):
    """Provider and hosts stay in the block grid (2 columns below provider)."""
    col_step = _layout_icon_step()
    host_y = _layout_host_row_offset() - _layout_provider_label_corridor()
    bx, by = isp_pos[isp_name]
    expected = [(0, host_y), (col_step, host_y)]
    if len(host_ids) > 2:
        row_step = _layout_host_row_step()
        expected.extend([(0, host_y + row_step), (col_step, host_y + row_step)])
    actual = sorted(
        ((host_pos[hid][0] - bx, host_pos[hid][1] - by) for hid in host_ids),
        key=lambda t: (t[1], t[0]),
    )
    assert actual == expected[: len(host_ids)], (
        f"{isp_name} hosts scattered: got {actual}, expected {expected[: len(host_ids)]}"
    )


def test_compute_layout_adjacent_multi_host_blocks_keep_geometry():
    edges = [
        ("h1", "1", "eth1", "ISP-A", "i1", "o1", "ki", "ko", "d1"),
        ("h2", "2", "eth2", "ISP-A", "i2", "o2", "ki", "ko", "d2"),
        ("h3", "3", "eth3", "ISP-B", "i3", "o3", "ki", "ko", "d3"),
        ("h4", "4", "eth4", "ISP-B", "i4", "o4", "ki", "ko", "d4"),
    ]
    host_pos, isp_pos, width, height = _compute_layout_validated(edges, MAP_WIDTH, MAP_HEIGHT)

    _assert_multi_host_block_geometry("ISP-A", ["1", "2"], isp_pos, host_pos)
    _assert_multi_host_block_geometry("ISP-B", ["3", "4"], isp_pos, host_pos)
    assert isp_pos["ISP-A"][1] == isp_pos["ISP-B"][1]
    _assert_no_rect_collisions(host_pos, isp_pos)
    _assert_no_visible_collisions(edges, host_pos, isp_pos)


def test_compute_layout_wrapped_multi_host_blocks_keep_geometry():
    edges = []
    for isp_idx, isp in enumerate(("ISP-A", "ISP-B", "ISP-C")):
        for host_idx in range(4):
            hid = str(isp_idx * 10 + host_idx + 1)
            edges.append(_edge(f"h-{hid}", hid, "eth0", isp, hid))
    host_pos, isp_pos, width, height = _compute_layout_validated(edges, 1200, MAP_HEIGHT)

    _assert_multi_host_block_geometry("ISP-A", ["1", "2", "3", "4"], isp_pos, host_pos)
    _assert_multi_host_block_geometry("ISP-B", ["11", "12", "13", "14"], isp_pos, host_pos)
    _assert_multi_host_block_geometry("ISP-C", ["21", "22", "23", "24"], isp_pos, host_pos)
    assert isp_pos["ISP-A"][1] == isp_pos["ISP-B"][1]
    assert isp_pos["ISP-C"][1] > isp_pos["ISP-A"][1]
    _assert_no_visible_collisions(edges, host_pos, isp_pos)


def test_compute_layout_representative_inventory_label_safe_at_1460():
    edges = _representative_inventory_edges()
    host_pos, isp_pos, width, height = _compute_layout_validated(edges, MAP_WIDTH, MAP_HEIGHT)

    assert len(host_pos) == 15
    assert len(isp_pos) == 9
    _assert_layout_within_bounds(host_pos, isp_pos, width, height)
    _assert_no_rect_collisions(host_pos, isp_pos)
    _assert_no_visible_collisions(edges, host_pos, isp_pos)
    assert width <= 1800, f"expected width <= 1800 at {MAP_WIDTH}, got {width}"
    assert height <= 1850, f"expected height <= 1850 at {MAP_WIDTH}, got {height}"

    shared_shelves = _multi_host_providers_on_same_shelf(isp_pos, min_count=2)
    assert shared_shelves, "expected at least two multi-host provider blocks on one shelf"
    _assert_multi_host_block_geometry("KZT", ["111", "112"], isp_pos, host_pos)
    _assert_multi_host_block_geometry("InfiniVAN", ["113", "114"], isp_pos, host_pos)

    selements, links, host_locs, isp_locs = _build_layout_model_from_edges(edges, host_pos, isp_pos)
    assert len(selements) == 24
    assert len(links) == 18
    _assert_explicit_label_locations(selements, host_locs, isp_locs)
    assert all(loc == LABEL_LOCATION_BOTTOM for loc in host_locs.values())
    assert all(loc == LABEL_LOCATION_TOP for loc in isp_locs.values())


def test_compute_layout_representative_inventory_label_safe_at_1200():
    edges = _representative_inventory_edges()
    host_pos, isp_pos, width, height = _compute_layout_validated(edges, 1200, MAP_HEIGHT)

    assert len(host_pos) == 15
    assert len(isp_pos) == 9
    _assert_layout_within_bounds(host_pos, isp_pos, width, height)
    _assert_no_visible_collisions(edges, host_pos, isp_pos)
    assert height <= 2100, f"expected bounded height at 1200, got {height}"


def test_compute_layout_orphan_does_not_reserve_shelf():
    edges = [
        ("h1", "1", "eth1", "Main-ISP", "i1", "o1", "ki", "ko", "d1"),
        ("h1", "1", "eth2", "Orphan-ISP", "i2", "o2", "ki", "ko", "d2"),
        ("h2", "2", "eth3", "Next-ISP", "i3", "o3", "ki", "ko", "d3"),
    ]
    host_pos, isp_pos, width, height = _compute_layout_validated(edges, MAP_WIDTH, MAP_HEIGHT)

    assert len(host_pos) == 2
    assert len(isp_pos) == 3
    _assert_no_visible_collisions(edges, host_pos, isp_pos)
    main_w = _layout_single_host_horizontal_offset() + SELEMENT_WIDTH
    assert isp_pos["Next-ISP"][0] == LAYOUT_MARGIN + main_w + LAYOUT_ELEMENT_GAP


def test_compute_layout_required_dimensions_follow_visible_aabb():
    edges = _representative_inventory_edges()
    host_pos, isp_pos, width, height = _compute_layout_validated(edges, MAP_WIDTH, MAP_HEIGHT)
    expected_w, expected_h = _layout_required_dimensions(host_pos, isp_pos, edges=edges)

    assert width >= expected_w
    assert height >= expected_h


def test_build_link_label_preserves_traffic_macros():
    label = _build_link_label(
        "MSK-M9-MX204-1",
        "et-0/0/2",
        'net.if.in["et-0/0/2"]',
        'net.if.out["et-0/0/2"]',
    )
    assert label.splitlines() == [
        "et-0/0/2",
        'In: {?last(/MSK-M9-MX204-1/net.if.in["et-0/0/2"])}',
        'Out: {?last(/MSK-M9-MX204-1/net.if.out["et-0/0/2"])}',
    ]


def test_validate_layout_model_detects_deliberate_collision():
    selements = [
        {
            "elementtype": ELEMENT_TYPE_HOST,
            "hostid": "1",
            "x": 100,
            "y": 100,
            "label": "host-a",
            "label_location": LABEL_LOCATION_BOTTOM,
        },
        {
            "elementtype": ELEMENT_TYPE_IMAGE,
            "label": "ISP",
            "x": 120,
            "y": 120,
            "label_location": LABEL_LOCATION_TOP,
        },
    ]
    links = [
        {
            "id": "link-0",
            "selementid1": "host-1",
            "selementid2": "isp-ISP",
            "label": _build_link_label("host-a", "eth0", "ki", "ko"),
        },
    ]
    collisions = _validate_layout_model(selements, links)
    assert collisions
    kinds = {(c["kind_a"], c["kind_b"]) for c in collisions}
    assert ("icon", "icon") in kinds or ("element_label", "icon") in kinds


def test_layout_visible_boxes_include_icons_labels_and_links():
    edges = _representative_inventory_edges()
    host_pos, isp_pos, _, _ = _compute_layout_validated(edges, MAP_WIDTH, MAP_HEIGHT)
    selements, links, _, _ = _build_layout_model_from_edges(edges, host_pos, isp_pos)
    positions = _layout_positions_from_model(selements)
    boxes = _layout_visible_boxes(selements, links, positions)
    kinds = {b["kind"] for b in boxes}
    assert kinds == {"icon", "element_label", "link_label"}
    assert len([b for b in boxes if b["kind"] == "link_label"]) == 18


def test_find_layout_collisions_skips_same_owner_icon_and_label():
    boxes = [
        {"id": "host-1-icon", "kind": "icon", "owner_id": "1", "bounds": (0, 0, 10, 10)},
        {"id": "host-1-label", "kind": "element_label", "owner_id": "1", "bounds": (0, 10, 10, 20)},
        {"id": "host-2-icon", "kind": "icon", "owner_id": "2", "bounds": (5, 5, 15, 15)},
    ]
    collisions = _find_layout_collisions(boxes, gap=0, skip_same_owner=True)
    assert len(collisions) == 2
    pairs = {(c["box_a"], c["box_b"]) for c in collisions}
    assert ("host-1-icon", "host-2-icon") in pairs
    assert ("host-1-label", "host-2-icon") in pairs


def test_shared_host_piter_beeline_ertelecom_cases():
    piter_edges = [
        ("MSK-M9-MX204-1", "101", "et-0/0/2", "Piter-IX", "i1", "o1", "ki", "ko", "d1"),
        ("MSK-M9-MX204-2", "102", "et-0/0/2", "Piter-IX", "i2", "o2", "ki", "ko", "d2"),
        ("MSK-M9-MX204-1", "101", "ae5.0", "Beeline", "i3", "o3", "ki", "ko", "d3"),
        ("MSK-M9-MX204-2", "102", "et-0/0/3", "Ertelecom", "i4", "o4", "ki", "ko", "d4"),
    ]
    host_pos, isp_pos, width, height = _compute_layout_validated(piter_edges, MAP_WIDTH, MAP_HEIGHT)
    _assert_no_visible_collisions(piter_edges, host_pos, isp_pos)
    selements, links, host_locs, isp_locs = _build_layout_model_from_edges(
        piter_edges, host_pos, isp_pos,
    )
    _assert_explicit_label_locations(selements, host_locs, isp_locs)
    assert {el["label"] for el in selements if el.get("elementtype") == ELEMENT_TYPE_IMAGE} == {
        "Piter-IX", "Beeline", "Ertelecom",
    }

"""Compact provider-block layout: inventory model, label-aware validation."""

import os
from pathlib import Path

import pytest

from tests.mocks.inventory_scope import dry_ssh_minimal_inventory_context
from tests.mocks.map_state import MapStateTracker
from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker
from uplinks.data import load_devices_json
from uplinks.netbox.inventory import load_inventory_report
from zabbix_map import (
    ELEMENT_TYPE_HOST,
    ELEMENT_TYPE_IMAGE,
    LABEL_LOCATION_BOTTOM,
    LABEL_LOCATION_TOP,
    LAYOUT_CANVAS_PADDING,
    LAYOUT_ELEMENT_GAP,
    LAYOUT_HOST_LABEL_LOCATION,
    LAYOUT_LABEL_GAP,
    LAYOUT_LATTICE_ROW_STEP,
    LAYOUT_PACK_FINE_Y_RADIUS,
    LAYOUT_PACK_FINE_Y_STEP,
    LAYOUT_MARGIN,
    LAYOUT_MAX_HEIGHT,
    LAYOUT_MAX_WIDTH,
    LAYOUT_PROVIDER_LABEL_LOCATION,
    LAYOUT_VERTICAL_LINK_SCALES,
    LINK_LABEL_PREVIEW_HEIGHT_PX,
    LINK_LABEL_PREVIEW_WIDTH_PX,
    LayoutValidationError,
    MAP_HEIGHT,
    MAP_WIDTH,
    SELEMENT_HEIGHT,
    SELEMENT_WIDTH,
    update_uplinks_map,
    _assign_layout_label_locations,
    _bounds_overlap,
    _build_layout_model_from_edges,
    _build_link_label,
    _compute_layout_validated,
    _element_bounds,
    _element_icon_center,
    _element_render_icon_bounds,
    _find_layout_collisions,
    _find_layout_editor_collisions,
    _inflated_bounds,
    _render_icon_inflated_bounds,
    RENDER_ICON_SIZE,
    _layout_block_width,
    _layout_render_extent,
    _layout_column_step,
    _layout_connected_components,
    _layout_content_aabb,
    _layout_host_label_corridor,
    _layout_host_row_offset,
    _layout_host_row_offset_for,
    _layout_host_row_step,
    _layout_host_row_step_for,
    _layout_icon_step,
    _layout_link_corridor_for,
    _layout_provider_below_host_row,
    _layout_provider_label_corridor,
    _layout_single_host_horizontal_offset,
    _link_label_box,
    _layout_required_dimensions,
    _layout_skip_link_label_endpoint_paint,
    _layout_skip_render_paint_overlap,
    _layout_visible_boxes,
    _layout_positions_from_model,
    LAYOUT_STAR_ROW_STEP,
    _validate_edges_layout,
    _validate_layout_model,
    _validate_layout_segment_collisions,
)


def _edge(hostname, hostid, iface, isp, suffix=""):
    tag = suffix or hostid
    return (hostname, hostid, iface, isp, f"i{tag}", f"o{tag}", "ki", "ko", f"d{tag}")


def _netbox_inventory_edges():
    """18 edges / 9 providers / 15 hosts from sanitized netbox_inventory.json."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    inventory_path = os.path.join(repo_root, "netbox_inventory.json")
    report = load_inventory_report(inventory_path)
    complete = report.get("complete") or []
    host_ids = {
        device: str(1000 + idx)
        for idx, device in enumerate(sorted({row["device"] for row in complete}))
    }
    edges = []
    for row in sorted(complete, key=lambda item: (item["device"], item["provider"], item["interface"])):
        hostname = row["device"]
        iface = row["interface"]
        edges.append((
            hostname,
            host_ids[hostname],
            iface,
            row["provider"],
            "",
            "",
            'net.if.in["{}"]'.format(iface),
            'net.if.out["{}"]'.format(iface),
            "",
        ))
    return edges


def _representative_inventory_edges():
    """16 edges, 7 providers, 15 hosts (production-like counts)."""
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
    edges.append(_edge("h-105", "105", "eth-f", "Fiord", "fiord"))
    edges.append(_edge("h-115", "115", "eth-e", "Ertelecom", "ert"))
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
            inflated1 = _render_icon_inflated_bounds(x1, y1, gap)
            rect2 = _element_render_icon_bounds(x2, y2)
            assert not _bounds_overlap(inflated1, rect2), (
                f"{name1} render icon at ({x1},{y1}) overlaps {name2} at ({x2},{y2}) "
                f"(gap={gap}px)"
            )


def _assert_no_segment_collisions(edges, host_pos, isp_pos):
    links = _build_layout_model_from_edges(edges, host_pos, isp_pos)[1]
    collisions = _validate_layout_segment_collisions(host_pos, isp_pos, links)
    assert collisions == [], (
        "link segment through foreign icon: {}".format(
            ", ".join(
                "{}→{}".format(c["box_a"], c["box_b"]) for c in collisions[:8]
            ),
        )
    )


def _assert_no_visible_collisions(edges, host_pos, isp_pos):
    collisions = _validate_edges_layout(edges, host_pos, isp_pos)
    assert collisions == [], (
        "gap=0 visible layout collisions: {}".format(
            ", ".join("{}↔{}".format(c["box_a"], c["box_b"]) for c in collisions[:8]),
        )
    )
    boxes = _layout_boxes(edges, host_pos, isp_pos)
    raw_collisions = [
        collision
        for collision in _find_layout_collisions(
            boxes, gap=0, skip_render_paint=True,
        )
        if "link_label" not in {collision["kind_a"], collision["kind_b"]}
    ]
    assert raw_collisions == [], (
        "raw visible collisions (gap=0): {}".format(
            ", ".join("{}↔{}".format(c["box_a"], c["box_b"]) for c in raw_collisions[:8]),
        )
    )
    _assert_no_segment_collisions(edges, host_pos, isp_pos)


def _layout_boxes(edges, host_pos, isp_pos):
    selements, links, _, _ = _build_layout_model_from_edges(edges, host_pos, isp_pos)
    positions = _layout_positions_from_model(selements)
    return _layout_visible_boxes(selements, links, positions)


def _assert_no_render_collisions(edges, host_pos, isp_pos):
    """Fail-closed render check: foreign icon/link-label overlaps only."""
    boxes = _layout_boxes(edges, host_pos, isp_pos)
    collisions = _find_layout_collisions(boxes, gap=0, skip_render_paint=True)
    foreign = [
        c for c in collisions
        if "link_label" not in {c["kind_a"], c["kind_b"]}
    ]
    assert foreign == [], (
        "render layout collisions: {}".format(
            ", ".join("{}↔{}".format(c["box_a"], c["box_b"]) for c in foreign[:8]),
        )
    )


def _assert_all_link_label_heights(boxes):
    for box in boxes:
        if box["kind"] != "link_label":
            continue
        width = box["bounds"][2] - box["bounds"][0]
        height = box["bounds"][3] - box["bounds"][1]
        assert abs(width - LINK_LABEL_PREVIEW_WIDTH_PX) < 0.5, (
            f"{box['id']} link label width {width} != {LINK_LABEL_PREVIEW_WIDTH_PX}"
        )
        assert abs(height - LINK_LABEL_PREVIEW_HEIGHT_PX) < 0.5, (
            f"{box['id']} link label height {height} != {LINK_LABEL_PREVIEW_HEIGHT_PX}"
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


def _layout_provider_y_bands(isp_pos):
    """Group providers by Y band (within label gap)."""
    bands = []
    for name, (_x, y) in sorted(isp_pos.items(), key=lambda item: (item[1][1], item[0])):
        if bands and abs(y - bands[-1][0]) <= LAYOUT_LABEL_GAP:
            bands[-1][1].append(name)
        else:
            bands.append((y, [name]))
    return bands


def _assert_multi_provider_band_y(edges, isp_pos):
    """3-provider shared-router tile shares or follows the lower filler band."""
    filler_ys = []
    multi_band_y = None
    for component in _layout_connected_components(edges):
        if len(component["providers"]) >= 3:
            multi_band_y = min(isp_pos[provider][1] for provider in component["providers"])
        elif len(component["hosts"]) <= 2:
            filler_ys.extend(isp_pos[provider][1] for provider in component["providers"])
    assert multi_band_y is not None, "expected a 3-provider component"
    if filler_ys:
        assert multi_band_y >= max(filler_ys), (
            f"3-provider band y={multi_band_y} should not precede filler ys={sorted(filler_ys)}"
        )


def _assert_dynamic_canvas_dimensions(edges, host_pos, isp_pos, width, height):
    """Canvas exceeds occupied bounds by configured reserve; matches required dimensions."""
    selements, links, _, _ = _build_layout_model_from_edges(edges, host_pos, isp_pos)
    positions = _layout_positions_from_model(selements)
    _min_x, _min_y, max_x, max_y = _layout_content_aabb(selements, links, positions)
    expected_w, expected_h = _layout_required_dimensions(host_pos, isp_pos, edges=edges)
    assert width == expected_w
    assert height == expected_h
    assert width > max_x
    assert height > max_y
    assert width >= max_x + LAYOUT_CANVAS_PADDING
    assert height >= max_y + LAYOUT_CANVAS_PADDING


def _assert_hurricane_fiord_provider_axis(isp_pos):
    """Hurricane primary and Fiord secondary share the same provider column."""
    hurricane = isp_pos["Hurricane"]
    fiord_name = next(
        name for name in isp_pos
        if name != "Hurricane" and "fiord" in name.lower()
    )
    fiord = isp_pos[fiord_name]
    assert hurricane[0] == fiord[0], (
        f"Hurricane x={hurricane[0]} and {fiord_name} x={fiord[0]} should share an axis"
    )
    assert hurricane[1] < fiord[1], (
        f"Hurricane y={hurricane[1]} should sit above {fiord_name} y={fiord[1]}"
    )


def _assert_hole_filling_tile_bands(edges, isp_pos):
    """
    Structural mosaic bands (topology-driven, not provider names):
    two large top-band stars, 2-host fillers below, 3-provider mid band last.
    """
    components = _layout_connected_components(edges)
    top_band_y = min(y for _x, y in isp_pos.values())
    top_large = []
    pair_fillers = []
    multi_band = []
    for component in components:
        providers = component["providers"]
        provider_count = len(providers)
        host_count = len(component["hosts"])
        ys = [isp_pos[provider][1] for provider in providers]
        min_y = min(ys)
        if provider_count >= 3:
            multi_band.append(min_y)
        elif provider_count == 2 or host_count >= 4:
            top_large.append(min_y)
        elif host_count == 2:
            pair_fillers.append(min_y)
    assert len(top_large) >= 2, "expected two large components in the top band"
    for y in top_large:
        assert abs(y - top_band_y) <= LAYOUT_LABEL_GAP, (
            f"large component at y={y} not in top band (top={top_band_y})"
        )
    assert pair_fillers, "expected 2-host filler tiles below the top band"
    row_step = LAYOUT_LATTICE_ROW_STEP
    for y in pair_fillers:
        assert y > top_band_y + row_step * 0.5, (
            f"2-host filler at y={y} should sit below top band, not on a new shelf row"
        )
    if multi_band:
        assert max(multi_band) >= min(pair_fillers), (
            "3-provider shared-router component should land in the mid band"
        )


def _assert_multi_host_block_geometry(isp_name, host_ids, isp_pos, host_pos):
    """Two-host star: provider centered between hosts on one horizontal row."""
    col_step = _layout_column_step()
    block_x = min(host_pos[hid][0] for hid in host_ids)
    host_y = host_pos[host_ids[0]][1]
    assert host_pos[host_ids[0]][1] == host_pos[host_ids[1]][1]
    assert isp_pos[isp_name][0] - block_x == col_step
    assert abs(isp_pos[isp_name][1] - host_y) <= LAYOUT_LABEL_GAP
    assert host_pos[host_ids[0]][0] == block_x
    assert host_pos[host_ids[1]][0] == block_x + 2 * col_step


def _assert_shared_host_between_providers(host_id, provider_a, provider_b, host_pos, isp_pos):
    """Shared router sits between its two linked providers."""
    hx, hy = host_pos[str(host_id)]
    ax, ay = isp_pos[provider_a]
    bx, by = isp_pos[provider_b]
    host_cx, host_cy = _element_icon_center(hx, hy)
    a_cx, a_cy = _element_icon_center(ax, ay)
    b_cx, b_cy = _element_icon_center(bx, by)
    on_segment = (
        min(a_cx, b_cx) - LAYOUT_LABEL_GAP <= host_cx <= max(a_cx, b_cx) + LAYOUT_LABEL_GAP
        and min(a_cy, b_cy) - LAYOUT_LABEL_GAP <= host_cy <= max(a_cy, b_cy) + LAYOUT_LABEL_GAP
    )
    assert on_segment, (
        f"shared host {host_id} at ({hx},{hy}) not between {provider_a} and {provider_b}"
    )


def _assert_provider_central_to_exclusive_hosts(provider, host_ids, isp_pos, host_pos):
    """Provider render-icon center stays inside the host render-icon bounding box."""
    if not host_ids:
        return
    px, py = isp_pos[provider]
    provider_cx, provider_cy = _element_icon_center(px, py)
    host_bounds = [_element_render_icon_bounds(host_pos[hid][0], host_pos[hid][1]) for hid in host_ids]
    min_x = min(rect[0] for rect in host_bounds)
    max_x = max(rect[2] for rect in host_bounds)
    min_y = min(rect[1] for rect in host_bounds)
    max_y = max(rect[3] for rect in host_bounds)
    assert min_x - LAYOUT_LABEL_GAP <= provider_cx <= max_x + LAYOUT_LABEL_GAP, (
        f"{provider} x-center {provider_cx} outside host span [{min_x}, {max_x}]"
    )
    assert min_y - LAYOUT_LABEL_GAP <= provider_cy <= max_y + LAYOUT_LABEL_GAP, (
        f"{provider} y-center {provider_cy} outside host span [{min_y}, {max_y}]"
    )


def _assert_all_link_labels_include_traffic(edges, host_pos, isp_pos):
    selements, links, _, _ = _build_layout_model_from_edges(edges, host_pos, isp_pos)
    expected_edges = [edge for edge in edges if edge[3]]
    assert len(links) == len(expected_edges)
    edge_by_key = {
        (str(edge[1]), edge[3], edge[2]): edge
        for edge in expected_edges
    }
    for link in links:
        host_sid = str(link.get("selementid1", ""))
        isp_sid = str(link.get("selementid2", ""))
        hostid = host_sid.replace("host-", "", 1)
        isp = isp_sid.replace("isp-", "", 1)
        edge = edge_by_key[(hostid, isp, link["label"].splitlines()[0])]
        hostname, _hostid, iface_name, _isp, _in, _out, key_in, key_out, _desc = edge
        lines = link["label"].splitlines()
        assert lines[0] == iface_name
        if key_in:
            assert any(line.startswith("In: {?last(/") for line in lines[1:])
        if key_out:
            assert any(line.startswith("Out: {?last(/") for line in lines[1:])
        assert hostname in link["label"]


def _assert_component_count(edges, expected):
    components = _layout_connected_components(edges)
    assert len(components) == expected, (
        f"expected {expected} connected components, got {len(components)}"
    )


def test_vertical_geometry_compact_halves_reserved_corridor():
    """Compact vertical helpers halve link-corridor reservation; full scale stays for fallback."""
    full_offset = _layout_host_row_offset_for(1.0)
    compact_offset = _layout_host_row_offset()
    full_step = _layout_host_row_step_for(1.0)
    compact_step = _layout_host_row_step()
    provider_y = _layout_provider_label_corridor()
    full_dy = full_offset - provider_y
    compact_dy = compact_offset - provider_y
    assert LAYOUT_VERTICAL_LINK_SCALES[0] == 0.5
    assert compact_dy == _layout_render_extent() + LAYOUT_LABEL_GAP
    assert compact_dy <= full_dy * 0.75, (
        f"compact provider-host dy {compact_dy}px should be materially below full {full_dy}px"
    )
    assert compact_step == (
        _layout_render_extent()
        + _layout_host_label_corridor()
        + _layout_link_corridor_for(LAYOUT_VERTICAL_LINK_SCALES[0])
        + LAYOUT_LABEL_GAP
    )
    assert compact_step <= full_step * 0.9
    assert _layout_column_step() == 200


def _assert_render_geometry_spacing(edges, host_pos, isp_pos):
    """Layout helpers must use 64x64 render steps, not the legacy 200px editor slot."""
    from zabbix_map import _layout_component_tile, _layout_normalize_tile

    row_step = _layout_host_row_step()
    assert row_step < 280, f"row step {row_step}px still reflects old 314px slot model"
    assert _layout_icon_step() == RENDER_ICON_SIZE + LAYOUT_ELEMENT_GAP
    old_row_offset = (
        _layout_provider_label_corridor()
        + SELEMENT_HEIGHT
        + LAYOUT_LABEL_GAP
        + LINK_LABEL_PREVIEW_HEIGHT_PX
        + LAYOUT_LABEL_GAP
    )
    assert _layout_host_row_offset() < old_row_offset, (
        "host row offset must be tighter than the old editor-slot vertical stride"
    )
    for component in _layout_connected_components(edges):
        if len(component["hosts"]) != 2 or len(component["providers"]) != 1:
            continue
        local_host, local_isp = _layout_component_tile(component)
        norm_host, norm_isp, _tile_w, _tile_h = _layout_normalize_tile(
            local_host, local_isp, component["edges"],
        )
        provider = component["providers"][0]
        host_ids = sorted(component["hosts"])
        row_delta = abs(norm_host[host_ids[1]][1] - norm_host[host_ids[0]][1])
        col_delta = abs(norm_host[host_ids[1]][0] - norm_host[host_ids[0]][0])
        assert row_delta <= LAYOUT_LABEL_GAP, f"{provider} two-host block should be one render row"
        assert col_delta == 2 * _layout_column_step(), (
            f"{provider} host column span should be 2*col_step render geometry, got {col_delta}"
        )
        assert norm_isp[provider][0] - min(norm_host[hid][0] for hid in host_ids) == _layout_column_step()


def _assert_multi_host_grid_geometry(isp_name, host_ids, isp_pos, host_pos):
    """Four-host star: provider centered between top routers; two below."""
    col_step = _layout_column_step()
    star_step = LAYOUT_STAR_ROW_STEP
    block_x = min(host_pos[hid][0] for hid in host_ids)
    block_y = min(host_pos[hid][1] for hid in host_ids)
    rel_isp = (isp_pos[isp_name][0] - block_x, isp_pos[isp_name][1] - block_y)
    assert rel_isp == (col_step, 0), (
        f"{isp_name} provider not centered on top host row"
    )
    ordered = sorted(host_ids, key=lambda hid: (host_pos[hid][1], host_pos[hid][0]))
    expected = [
        (0, 0),
        (2 * col_step, 0),
        (0, star_step),
        (2 * col_step, star_step),
    ]
    actual = [
        (host_pos[hid][0] - block_x, host_pos[hid][1] - block_y)
        for hid in ordered
    ]
    assert actual == expected, (
        f"{isp_name} grid hosts scattered: got {actual}, expected {expected}"
    )


def _compact_top_provider_four_host_positions(host_entries, isp_name):
    """Legacy compact top-provider 4-host star (causes sibling label/link overlaps)."""
    provider_y = _layout_provider_label_corridor()
    col_step = _layout_column_step()
    # Historical compact row gap from map-35 (206-38); too tight once paint skips are fixed.
    bot_y = provider_y + 168
    host_pos = {}
    for idx, (_hostname, hostid) in enumerate(host_entries[:2]):
        host_pos[str(hostid)] = (idx * 2 * col_step, provider_y)
    for idx, (_hostname, hostid) in enumerate(host_entries[2:4]):
        host_pos[str(hostid)] = (idx * 2 * col_step, bot_y)
    isp_pos = {isp_name: (col_step, provider_y)}
    return host_pos, isp_pos


def _foreign_element_link_collisions(boxes, gap=0, skip_render_paint=False):
    """element_label vs link_label pairs where the label owner is not a link endpoint."""
    collisions = _find_layout_collisions(
        boxes, gap=gap, skip_render_paint=skip_render_paint,
    )
    by_id = {box["id"]: box for box in boxes}
    foreign = []
    for collision in collisions:
        kinds = {collision["kind_a"], collision["kind_b"]}
        if kinds != {"element_label", "link_label"}:
            continue
        element_box = (
            by_id[collision["box_a"]]
            if by_id[collision["box_a"]]["kind"] == "element_label"
            else by_id[collision["box_b"]]
        )
        link_box = (
            by_id[collision["box_a"]]
            if by_id[collision["box_a"]]["kind"] == "link_label"
            else by_id[collision["box_b"]]
        )
        owner = element_box.get("owner_id")
        endpoint_ids = link_box.get("endpoint_ids") or ()
        is_endpoint = any(
            owner and endpoint_id in (f"host-{owner}", f"isp-{owner}")
            for endpoint_id in endpoint_ids
        )
        if not is_endpoint:
            foreign.append(collision)
    return foreign


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
    # Full 220px labels widen tiles; blocks may pack side-by-side or stack in one column.
    if isp_pos["ISP-A"][1] == isp_pos["ISP-B"][1]:
        assert isp_pos["ISP-A"][0] != isp_pos["ISP-B"][0]
    else:
        assert isp_pos["ISP-A"][0] == isp_pos["ISP-B"][0]
    _assert_no_rect_collisions(host_pos, isp_pos)
    _assert_no_visible_collisions(edges, host_pos, isp_pos)


def test_compute_layout_wrapped_multi_host_blocks_keep_geometry():
    edges = []
    for isp_idx, isp in enumerate(("ISP-A", "ISP-B", "ISP-C")):
        for host_idx in range(4):
            hid = str(isp_idx * 10 + host_idx + 1)
            edges.append(_edge(f"h-{hid}", hid, "eth0", isp, hid))
    host_pos, isp_pos, width, height = _compute_layout_validated(edges, 1200, MAP_HEIGHT)

    _assert_multi_host_grid_geometry("ISP-A", ["1", "2", "3", "4"], isp_pos, host_pos)
    _assert_multi_host_grid_geometry("ISP-B", ["11", "12", "13", "14"], isp_pos, host_pos)
    _assert_multi_host_grid_geometry("ISP-C", ["21", "22", "23", "24"], isp_pos, host_pos)
    assert _layout_block_width(False, 4) == 2 * _layout_column_step() + _layout_render_extent()
    assert isp_pos["ISP-C"][1] >= isp_pos["ISP-A"][1]
    _assert_no_visible_collisions(edges, host_pos, isp_pos)


def test_compute_layout_representative_inventory_has_dynamic_canvas():
    edges = _representative_inventory_edges()
    host_pos, isp_pos, width, height = _compute_layout_validated(edges, MAP_WIDTH, MAP_HEIGHT)

    assert len(host_pos) == 15
    assert len(isp_pos) == 7
    _assert_layout_within_bounds(host_pos, isp_pos, width, height)
    _assert_no_rect_collisions(host_pos, isp_pos)
    _assert_no_visible_collisions(edges, host_pos, isp_pos)
    assert width <= LAYOUT_MAX_WIDTH, f"expected width <= {LAYOUT_MAX_WIDTH} at {MAP_WIDTH}, got {width}"
    assert height <= LAYOUT_MAX_HEIGHT, f"expected height <= {LAYOUT_MAX_HEIGHT} at {MAP_WIDTH}, got {height}"
    assert height <= 2600, f"expected representative height <= {LAYOUT_MAX_HEIGHT} at {MAP_WIDTH}, got {height}"

    _assert_hole_filling_tile_bands(edges, isp_pos)
    _assert_component_count(edges, 6)
    _assert_hurricane_fiord_provider_axis(isp_pos)
    _assert_multi_host_block_geometry("KZT", ["111", "112"], isp_pos, host_pos)
    _assert_multi_host_block_geometry("InfiniVAN", ["113", "114"], isp_pos, host_pos)
    _assert_provider_central_to_exclusive_hosts(
        "Cogent", ["101", "102", "103", "104"], isp_pos, host_pos,
    )
    _assert_all_link_labels_include_traffic(edges, host_pos, isp_pos)

    selements, links, host_locs, isp_locs = _build_layout_model_from_edges(edges, host_pos, isp_pos)
    assert len(selements) == 22
    assert len(links) == 16
    _assert_explicit_label_locations(selements, host_locs, isp_locs)
    assert all(loc == LABEL_LOCATION_BOTTOM for loc in host_locs.values())
    assert all(loc == LABEL_LOCATION_TOP for loc in isp_locs.values())


def test_compute_layout_representative_inventory_label_safe_at_1200():
    edges = _representative_inventory_edges()
    host_pos, isp_pos, width, height = _compute_layout_validated(edges, 1200, MAP_HEIGHT)

    assert len(host_pos) == 15
    assert len(isp_pos) == 7
    _assert_layout_within_bounds(host_pos, isp_pos, width, height)
    _assert_no_visible_collisions(edges, host_pos, isp_pos)
    assert width <= LAYOUT_MAX_WIDTH, f"expected width <= {LAYOUT_MAX_WIDTH} at 1200, got {width}"
    assert height <= LAYOUT_MAX_HEIGHT, f"expected height <= {LAYOUT_MAX_HEIGHT} at 1200, got {height}"


def test_compute_layout_validated_never_returns_canvas_above_layout_max():
    edges = _representative_inventory_edges()
    for map_width in (800, 1200, MAP_WIDTH):
        host_pos, isp_pos, width, height = _compute_layout_validated(edges, map_width, MAP_HEIGHT)
        assert width <= LAYOUT_MAX_WIDTH, f"width {width} exceeds {LAYOUT_MAX_WIDTH} at map_width={map_width}"
        assert height <= LAYOUT_MAX_HEIGHT, f"height {height} exceeds {LAYOUT_MAX_HEIGHT} at map_width={map_width}"
        _assert_no_visible_collisions(edges, host_pos, isp_pos)


def test_compute_layout_shared_provider_component_tile():
    edges = [
        ("h1", "1", "eth1", "Main-ISP", "i1", "o1", "ki", "ko", "d1"),
        ("h1", "1", "eth2", "Secondary-ISP", "i2", "o2", "ki", "ko", "d2"),
        ("h2", "2", "eth3", "Next-ISP", "i3", "o3", "ki", "ko", "d3"),
    ]
    host_pos, isp_pos, width, height = _compute_layout_validated(edges, MAP_WIDTH, MAP_HEIGHT)

    assert len(host_pos) == 2
    assert len(isp_pos) == 3
    _assert_component_count(edges, 2)
    _assert_no_visible_collisions(edges, host_pos, isp_pos)
    _assert_shared_host_between_providers("1", "Main-ISP", "Secondary-ISP", host_pos, isp_pos)
    assert isp_pos["Secondary-ISP"][1] == host_pos["1"][1]
    _assert_dynamic_canvas_dimensions(edges, host_pos, isp_pos, width, height)


def test_compute_layout_required_dimensions_follow_visible_aabb():
    edges = _representative_inventory_edges()
    host_pos, isp_pos, width, height = _compute_layout_validated(edges, MAP_WIDTH, MAP_HEIGHT)
    _assert_dynamic_canvas_dimensions(edges, host_pos, isp_pos, width, height)


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
    assert len([b for b in boxes if b["kind"] == "link_label"]) == 16


def test_layout_skip_render_paint_overlap_only_endpoint_icons():
    """Paint-order skip is limited to link_label vs its own endpoint render icon."""
    endpoint_icon = {
        "id": "host-1-icon",
        "kind": "icon",
        "owner_id": "1",
        "bounds": (100, 100, 164, 164),
    }
    endpoint_label = {
        "id": "host-1-label",
        "kind": "element_label",
        "owner_id": "1",
        "bounds": (100, 170, 220, 198),
    }
    foreign_label = {
        "id": "host-2-label",
        "kind": "element_label",
        "owner_id": "2",
        "bounds": (110, 165, 230, 193),
    }
    link_box = {
        "id": "link-0",
        "kind": "link_label",
        "owner_id": None,
        "endpoint_ids": ("host-1", "isp-A"),
        "bounds": (120, 150, 340, 204),
    }
    assert _layout_skip_render_paint_overlap(endpoint_icon, link_box)
    assert _layout_skip_render_paint_overlap(link_box, endpoint_icon)
    assert not _layout_skip_render_paint_overlap(endpoint_label, link_box)
    assert not _layout_skip_render_paint_overlap(link_box, endpoint_label)
    assert not _layout_skip_render_paint_overlap(foreign_label, link_box)
    assert not _layout_skip_link_label_endpoint_paint(foreign_label, link_box)


def test_find_layout_collisions_rejects_star_sibling_host_labels_over_traffic_labels():
    """Top-row host labels must block bottom-row traffic labels on compact stars."""
    edges = _netbox_inventory_edges()
    components = _layout_connected_components(edges)
    cogent = next(comp for comp in components if comp["providers"] == ["Cogent"])
    hurricane = next(
        comp for comp in components
        if set(comp["providers"]) == {"Fiord / PING-WIN", "Hurricane"}
    )

    def host_entries(component):
        seen = set()
        entries = []
        for edge in component["edges"]:
            hid = str(edge[1])
            if hid in seen:
                continue
            seen.add(hid)
            entries.append((edge[0], hid))
        return sorted(entries, key=lambda item: item[1])

    fixtures = [
        (cogent, "Cogent", {"host-1002-label", "host-1004-label"}),
        (hurricane, "Hurricane", {"host-1003-label", "host-1005-label"}),
    ]
    foreign_labels = set()
    for component, isp_name, expected_labels in fixtures:
        host_pos, isp_pos = _compact_top_provider_four_host_positions(
            host_entries(component), isp_name,
        )
        selements, links, _, _ = _build_layout_model_from_edges(
            component["edges"], host_pos, isp_pos,
        )
        boxes = _layout_visible_boxes(
            selements, links, _layout_positions_from_model(selements),
        )
        foreign = _foreign_element_link_collisions(
            boxes, gap=LAYOUT_LABEL_GAP, skip_render_paint=True,
        )
        assert foreign, f"expected foreign host-label/link-label collisions for {isp_name}"
        labels = {
            element_id
            for collision in foreign
            for element_id in (collision["box_a"], collision["box_b"])
            if element_id.endswith("-label")
        }
        assert expected_labels <= labels
        foreign_labels.update(labels)

    assert foreign_labels == {
        "host-1002-label",
        "host-1003-label",
        "host-1004-label",
        "host-1005-label",
    }


def test_find_layout_collisions_reports_provider_label_link_overlap():
    """Provider top labels must collide with their own link labels when overlapping."""
    boxes = [
        {
            "id": "isp-Orphan-label",
            "kind": "element_label",
            "owner_id": "Orphan",
            "bounds": (100, 180, 200, 208),
        },
        {
            "id": "link-0",
            "kind": "link_label",
            "owner_id": None,
            "endpoint_ids": ("host-1", "isp-Orphan"),
            "bounds": (110, 170, 190, 224),
        },
    ]
    collisions = _find_layout_collisions(boxes, gap=0)
    assert collisions, "expected provider label vs downward link label overlap"
    pairs = {(c["box_a"], c["box_b"]) for c in collisions}
    assert ("isp-Orphan-label", "link-0") in pairs or ("link-0", "isp-Orphan-label") in pairs


def test_find_layout_collisions_reports_same_provider_fan_overlap():
    """Same-provider fan link labels must not be silently skipped."""
    boxes = [
        {
            "id": "link-a",
            "kind": "link_label",
            "owner_id": None,
            "endpoint_ids": ("host-1", "isp-A"),
            "bounds": (100, 100, 320, 154),
        },
        {
            "id": "link-b",
            "kind": "link_label",
            "owner_id": None,
            "endpoint_ids": ("host-2", "isp-A"),
            "bounds": (110, 105, 330, 159),
        },
    ]
    collisions = _find_layout_collisions(boxes, gap=0)
    assert collisions, "expected overlap between same-provider fan link labels"
    pairs = {(c["box_a"], c["box_b"]) for c in collisions}
    assert ("link-a", "link-b") in pairs or ("link-b", "link-a") in pairs


def test_shared_host_parallel_links_report_collisions_when_overlapping():
    """Shared-host orphan uplink labels crossing another provider link must collide."""
    edges = [
        ("h1", "1", "eth1", "Main-ISP", "i1", "o1", "ki", "ko", "d1"),
        ("h1", "1", "eth2", "Orphan-ISP", "i2", "o2", "ki", "ko", "d2"),
    ]
    host_pos = {"1": (100, 400)}
    isp_pos = {"Main-ISP": (100, 100), "Orphan-ISP": (150, 250)}
    selements, links, _, _ = _build_layout_model_from_edges(edges, host_pos, isp_pos)
    positions = _layout_positions_from_model(selements)
    boxes = _layout_visible_boxes(selements, links, positions)
    collisions = _find_layout_collisions(boxes, gap=0)
    assert collisions, "expected collision when orphan link box crosses main link label"
    involved = {c["box_a"] for c in collisions} | {c["box_b"] for c in collisions}
    assert any("Orphan-ISP" in box_id or "link-" in box_id for box_id in involved)


def test_find_layout_collisions_gap_zero_vs_label_gap():
    """gap=0 checks raw bounds; gap=LAYOUT_LABEL_GAP adds 8px expansion per non-icon side."""
    boxes = [
        {"id": "label-a", "kind": "element_label", "owner_id": "1", "bounds": (0, 0, 10, 10)},
        {"id": "label-b", "kind": "element_label", "owner_id": "2", "bounds": (20, 0, 30, 10)},
    ]
    assert _find_layout_collisions(boxes, gap=0) == []
    assert _find_layout_collisions(boxes, gap=LAYOUT_LABEL_GAP)


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


def test_link_label_box_centered_on_segment_midpoint():
    """Regression: link labels stay centered on the segment, never parked off-line."""
    host_pos = (100, 200)
    isp_pos = (300, 200)
    box = _link_label_box(
        host_pos,
        isp_pos,
        "eth0\nIn: x\nOut: y",
        "link-narrow-row",
        endpoint_ids=("host-1", "isp-A"),
    )
    width = box["bounds"][2] - box["bounds"][0]
    height = box["bounds"][3] - box["bounds"][1]
    assert width == LINK_LABEL_PREVIEW_WIDTH_PX
    assert height == LINK_LABEL_PREVIEW_HEIGHT_PX

    host_cx, host_cy = _element_icon_center(host_pos[0], host_pos[1])
    isp_cx, isp_cy = _element_icon_center(isp_pos[0], isp_pos[1])
    seg_mid_x = (host_cx + isp_cx) / 2.0
    seg_mid_y = (host_cy + isp_cy) / 2.0
    label_cx = (box["bounds"][0] + box["bounds"][2]) / 2.0
    label_cy = (box["bounds"][1] + box["bounds"][3]) / 2.0
    assert abs(label_cx - seg_mid_x) < 0.5
    assert abs(label_cy - seg_mid_y) < 0.5
    host_icon_bottom = _element_render_icon_bounds(host_pos[0], host_pos[1])[3]
    assert label_cy < host_icon_bottom + _layout_host_label_corridor() + 10, (
        "label must not be parked below the same-row link corridor"
    )

    foreign = {
        "id": "foreign-label",
        "kind": "element_label",
        "owner_id": "foreign",
        "bounds": (
            label_cx - 20,
            label_cy - 20,
            label_cx + 20,
            label_cy + 20,
        ),
    }
    collisions = _find_layout_collisions([box, foreign], gap=0)
    assert collisions, "expected collision at centered midpoint, not off-line parking"
    assert any("link-narrow-row" in (c["box_a"], c["box_b"]) for c in collisions)


def test_editor_slots_may_overlap_while_render_icons_validate_clean():
    """Editor 200x200 slots can overlap; 64x64 render icons drive fail-closed checks."""
    provider_y = _layout_provider_label_corridor()
    # Editor slots overlap (200px tall) while render icons stay separated.
    host_y = provider_y + 150
    host_pos = {"1": (0, host_y), "2": (_layout_column_step(), host_y)}
    isp_pos = {"ISP-A": (0, provider_y), "ISP-B": (_layout_column_step(), provider_y)}
    edges = [
        ("h1", "1", "eth1", "ISP-A", "i1", "o1", "ki", "ko", "d1"),
        ("h2", "2", "eth2", "ISP-B", "i2", "o2", "ki", "ko", "d2"),
    ]
    editor_collisions = _find_layout_editor_collisions(host_pos, isp_pos, gap=0)
    assert editor_collisions, "expected overlapping editor slots in this fixture"
    selements, links, _, _ = _build_layout_model_from_edges(edges, host_pos, isp_pos)
    boxes = _layout_visible_boxes(selements, links, _layout_positions_from_model(selements))
    for gap in (0, LAYOUT_LABEL_GAP):
        assert [
            collision
            for collision in _find_layout_collisions(boxes, gap=gap)
            if "link_label" not in {collision["kind_a"], collision["kind_b"]}
        ] == []
    assert _validate_edges_layout(edges, host_pos, isp_pos) == []
    _assert_no_rect_collisions(host_pos, isp_pos)
    link_boxes = [box for box in boxes if box["kind"] == "link_label"]
    assert link_boxes
    for box in link_boxes:
        width = box["bounds"][2] - box["bounds"][0]
        height = box["bounds"][3] - box["bounds"][1]
        assert width == LINK_LABEL_PREVIEW_WIDTH_PX
        assert height == LINK_LABEL_PREVIEW_HEIGHT_PX


def test_column_step_matches_compact_view_geometry():
    """Column spacing follows the compact visible-icon geometry."""
    col_step = _layout_column_step()
    assert col_step == 200
    assert col_step > RENDER_ICON_SIZE + LAYOUT_ELEMENT_GAP
    assert _layout_icon_step() == RENDER_ICON_SIZE + LAYOUT_ELEMENT_GAP


def test_compute_layout_netbox_inventory_live_topology():
    edges = _netbox_inventory_edges()
    assert len(edges) == 18
    assert len({edge[3] for edge in edges}) == 9
    assert len({edge[1] for edge in edges}) == 15
    _assert_component_count(edges, 6)

    host_pos, isp_pos, width, height = _compute_layout_validated(edges, MAP_WIDTH, MAP_HEIGHT)
    _assert_layout_within_bounds(host_pos, isp_pos, width, height)
    _assert_no_visible_collisions(edges, host_pos, isp_pos)
    _assert_no_render_collisions(edges, host_pos, isp_pos)
    _assert_all_link_labels_include_traffic(edges, host_pos, isp_pos)
    assert LAYOUT_ELEMENT_GAP == 8
    assert width <= LAYOUT_MAX_WIDTH
    assert height <= LAYOUT_MAX_HEIGHT
    assert width * height < 1696 * 2060, (
        f"expected area materially below old 1696x2060 shelf, got {width}x{height}"
    )
    assert LAYOUT_STAR_ROW_STEP < 222, (
        f"star row step {LAYOUT_STAR_ROW_STEP}px should be more compact than legacy 222px"
    )
    _assert_dynamic_canvas_dimensions(edges, host_pos, isp_pos, width, height)
    _assert_hurricane_fiord_provider_axis(isp_pos)
    row_step = _layout_host_row_step()
    icon_step = _layout_icon_step()
    assert icon_step == 72
    assert row_step < 280, f"row step must use render geometry, not old 314px slot step (got {row_step})"
    assert row_step == (
        _layout_render_extent()
        + _layout_host_label_corridor()
        + _layout_link_corridor_for(LAYOUT_VERTICAL_LINK_SCALES[0])
        + LAYOUT_LABEL_GAP
    )
    editor_collisions = _find_layout_editor_collisions(host_pos, isp_pos, gap=0)
    render_boxes = _layout_boxes(edges, host_pos, isp_pos)
    render_collisions = [
        collision
        for collision in _find_layout_collisions(
            render_boxes, gap=0, skip_render_paint=True,
        )
        if "link_label" not in {collision["kind_a"], collision["kind_b"]}
    ]
    assert render_collisions == []
    skipped_pairs = [
        (box_a["id"], box_a["kind"], box_b["id"], box_b["kind"])
        for index, box_a in enumerate(render_boxes)
        for box_b in render_boxes[index + 1 :]
        if _layout_skip_render_paint_overlap(box_a, box_b)
    ]
    assert skipped_pairs
    assert all(
        kinds == {"icon", "link_label"}
        for _id_a, kind_a, _id_b, kind_b in skipped_pairs
        for kinds in [{kind_a, kind_b}]
    )
    assert editor_collisions, "editor-slot overlaps are diagnostic-only for dense tiles"

    exclusive_cogent = sorted(
        str(edge[1]) for edge in edges
        if edge[3] == "Cogent"
        and len({row[3] for row in edges if str(row[1]) == str(edge[1])}) == 1
    )
    _assert_provider_central_to_exclusive_hosts("Cogent", exclusive_cogent, isp_pos, host_pos)
    _assert_provider_central_to_exclusive_hosts(
        "Hurricane",
        sorted(
            {
                str(edge[1]) for edge in edges
                if edge[3] == "Hurricane" and edge[0] != "WAW-EQX-7280QR-2"
            },
        ),
        isp_pos,
        host_pos,
    )

    host_id_by_name = {edge[0]: str(edge[1]) for edge in edges}
    for host_name, provider_name in (
        ("MSK-M9-MX204-1", "Beeline"),
        ("MSK-M9-MX204-2", "Ertelecom"),
    ):
        host_id = host_id_by_name[host_name]
        assert isp_pos[provider_name][1] > host_pos[host_id][1]
        assert min(pos[0] for pos in host_pos.values()) <= isp_pos[provider_name][0]
        assert isp_pos[provider_name][0] <= max(pos[0] for pos in host_pos.values())
    waw_x, waw_y = host_pos[host_id_by_name["WAW-EQX-7280QR-2"]]
    fiord_x, fiord_y = isp_pos["Fiord / PING-WIN"]
    assert waw_y == fiord_y
    assert waw_x > fiord_x

    selements, links, host_locs, isp_locs = _build_layout_model_from_edges(edges, host_pos, isp_pos)
    assert len(links) == 18
    assert len([el for el in selements if int(el.get("elementtype", 0)) == ELEMENT_TYPE_IMAGE]) == 9
    assert len([el for el in selements if int(el.get("elementtype", 0)) == ELEMENT_TYPE_HOST]) == 15
    _assert_explicit_label_locations(selements, host_locs, isp_locs)
    assert {el["label"] for el in selements if el.get("elementtype") == ELEMENT_TYPE_IMAGE} == {
        "365 Data Centers",
        "Beeline",
        "Cogent",
        "Ertelecom",
        "Fiord / PING-WIN",
        "Hurricane",
        "InfiniVAN",
        "KZT",
        "Piter-IX",
    }

    boxes = _layout_boxes(edges, host_pos, isp_pos)
    _assert_all_link_label_heights(boxes)
    assert [
        collision
        for collision in _find_layout_collisions(
            boxes, gap=0, skip_render_paint=True,
        )
        if "link_label" not in {collision["kind_a"], collision["kind_b"]}
    ] == []
    _assert_no_segment_collisions(edges, host_pos, isp_pos)
    for link in links:
        lines = link["label"].splitlines()
        assert len(lines) == 3, f"link label must stay 3 lines: {lines!r}"
        assert any(line.startswith("In: {?last(/") for line in lines[1:])
        assert any(line.startswith("Out: {?last(/") for line in lines[1:])
    _assert_multi_provider_band_y(edges, isp_pos)
    _assert_hole_filling_tile_bands(edges, isp_pos)
    _assert_render_geometry_spacing(edges, host_pos, isp_pos)


def test_layout_pack_fine_y_finds_first_valid_row_near_coarse_failure():
    """Fine-Y packing lands the 3-provider tile in the lowest valid mid-band row."""
    edges = _netbox_inventory_edges()
    host_pos, isp_pos, width, height = _compute_layout_validated(edges, MAP_WIDTH, MAP_HEIGHT)

    assert LAYOUT_PACK_FINE_Y_RADIUS == 16
    assert LAYOUT_PACK_FINE_Y_STEP == 2
    _assert_dynamic_canvas_dimensions(edges, host_pos, isp_pos, width, height)
    _assert_multi_provider_band_y(edges, isp_pos)
    _assert_no_render_collisions(edges, host_pos, isp_pos)
    _assert_no_visible_collisions(edges, host_pos, isp_pos)
    _assert_no_segment_collisions(edges, host_pos, isp_pos)
    selements, links, _, _ = _build_layout_model_from_edges(edges, host_pos, isp_pos)
    assert len(links) == 18


def test_compute_layout_hurricane_shared_waw_fills_center_slot():
    edges = [
        row for row in _netbox_inventory_edges()
        if row[3] in ("Hurricane", "Fiord / PING-WIN")
    ]
    host_pos, isp_pos, width, height = _compute_layout_validated(edges, MAP_WIDTH, MAP_HEIGHT)
    _assert_component_count(edges, 1)
    _assert_no_visible_collisions(edges, host_pos, isp_pos)
    host_id_by_name = {edge[0]: str(edge[1]) for edge in edges}
    waw_id = host_id_by_name["WAW-EQX-7280QR-2"]
    mia_id = host_id_by_name["MIA-EQX-7280QR-2"]
    top_y = min(y for _x, y in host_pos.values())
    top_hosts = sorted(
        [hid for hid in host_pos if host_pos[hid][1] == top_y],
        key=lambda hid: host_pos[hid][0],
    )
    assert host_pos[waw_id][0] == host_pos[mia_id][0]
    assert host_pos[waw_id][1] > host_pos[mia_id][1]
    assert isp_pos["Hurricane"][0] == (host_pos[top_hosts[0]][0] + host_pos[top_hosts[1]][0]) // 2
    _assert_hurricane_fiord_provider_axis(isp_pos)
    assert host_pos[waw_id][1] > host_pos[mia_id][1]


def test_shared_host_piter_beeline_ertelecom_cases():
    piter_edges = [
        ("MSK-M9-MX204-1", "101", "et-0/0/2", "Piter-IX", "i1", "o1", "ki", "ko", "d1"),
        ("MSK-M9-MX204-2", "102", "et-0/0/2", "Piter-IX", "i2", "o2", "ki", "ko", "d2"),
        ("MSK-M9-MX204-1", "101", "ae5.0", "Beeline", "i3", "o3", "ki", "ko", "d3"),
        ("MSK-M9-MX204-2", "102", "et-0/0/3", "Ertelecom", "i4", "o4", "ki", "ko", "d4"),
    ]
    host_pos, isp_pos, width, height = _compute_layout_validated(piter_edges, MAP_WIDTH, MAP_HEIGHT)
    _assert_component_count(piter_edges, 1)
    _assert_no_visible_collisions(piter_edges, host_pos, isp_pos)
    _assert_no_render_collisions(piter_edges, host_pos, isp_pos)
    _assert_all_link_labels_include_traffic(piter_edges, host_pos, isp_pos)
    assert isp_pos["Beeline"][1] > host_pos["101"][1]
    assert isp_pos["Ertelecom"][1] > host_pos["102"][1]
    assert min(host_pos["101"][0], host_pos["102"][0]) < isp_pos["Beeline"][0]
    assert isp_pos["Beeline"][0] < max(host_pos["101"][0], host_pos["102"][0])
    assert min(host_pos["101"][0], host_pos["102"][0]) < isp_pos["Ertelecom"][0]
    assert isp_pos["Ertelecom"][0] < max(host_pos["101"][0], host_pos["102"][0])
    selements, links, host_locs, isp_locs = _build_layout_model_from_edges(
        piter_edges, host_pos, isp_pos,
    )
    assert len(links) == 4
    _assert_explicit_label_locations(selements, host_locs, isp_locs)
    assert {el["label"] for el in selements if el.get("elementtype") == ELEMENT_TYPE_IMAGE} == {
        "Piter-IX", "Beeline", "Ertelecom",
    }


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _single_edge():
    return [_edge("h1", "1", "eth1", "ISP-A")]


def test_compute_layout_validated_rejects_collisions_without_repacking(monkeypatch):
    fake_collision = [{"box_a": "host-1", "box_b": "link-1"}]
    calls = {"n": 0}

    def always_collide(*args, **kwargs):
        calls["n"] += 1
        return list(fake_collision)

    monkeypatch.setattr("zabbix_map._validate_edges_layout", always_collide)

    with pytest.raises(LayoutValidationError):
        _compute_layout_validated(_single_edge(), LAYOUT_MAX_WIDTH, MAP_HEIGHT)

    assert calls["n"] <= 2


def test_compute_layout_validated_propagates_packing_failure(monkeypatch):
    calls = {"n": 0}

    def fail_pack(edges, width, height):
        calls["n"] += 1
        raise LayoutValidationError("layout: cannot pack component tile for ['ISP-A']")

    monkeypatch.setattr("zabbix_map._compute_layout", fail_pack)

    with pytest.raises(LayoutValidationError, match=r"cannot pack component tile"):
        _compute_layout_validated(_single_edge(), LAYOUT_MAX_WIDTH, MAP_HEIGHT)

    assert calls["n"] == 1


def test_compute_layout_validated_rejects_oversized_canvas(monkeypatch):
    monkeypatch.setattr(
        "zabbix_map._compute_layout",
        lambda *args: ({"1": (0, 0)}, {"ISP-A": (200, 0)}, LAYOUT_MAX_WIDTH + 1, MAP_HEIGHT),
    )
    monkeypatch.setattr("zabbix_map._validate_edges_layout", lambda *args, **kwargs: [])

    with pytest.raises(LayoutValidationError, match=r"exceeds maximum size"):
        _compute_layout_validated(_single_edge(), MAP_WIDTH, MAP_HEIGHT)


def test_compute_layout_validated_rejects_persistent_collisions(monkeypatch):
    calls = {"n": 0}

    def always_collide(*args, **kwargs):
        calls["n"] += 1
        return [{"box_a": "a", "box_b": "b"}]

    monkeypatch.setattr("zabbix_map._validate_edges_layout", always_collide)

    with pytest.raises(LayoutValidationError):
        _compute_layout_validated(_single_edge(), MAP_WIDTH, MAP_HEIGHT)

    assert calls["n"] <= 2


def test_update_uplinks_map_skips_map_update_when_layout_validation_stuck(monkeypatch, zabbix_env):
    data, err = load_devices_json(str(FIXTURES / "dry_ssh_minimal.json"))
    assert err is None
    devices = data["devices"]
    host_id = {"ALA-KZT-7280TR-1": "101", "FRN-MX-1": "102"}
    items = {
        ("ALA-KZT-7280TR-1", "ethernet51/1"): {
            "itemid_in": "1001",
            "itemid_out": "1002",
            "bits_in": 'net.if.in["Ethernet51/1"]',
            "bits_out": 'net.if.out["Ethernet51/1"]',
        },
        ("FRN-MX-1", "ae5.0"): {
            "itemid_in": "2001",
            "itemid_out": "2002",
            "bits_in": "net.if.in[ae5]",
            "bits_out": "net.if.out[ae5]",
        },
    }
    desc = {"Uplink: Cogent 10G": "Cogent", "Uplink: Hurricane": "Hurricane"}

    map_tracker = MapStateTracker(sysmapid="55")
    mocker = (
        build_standard_zabbix_mocker()
        .on("map.get", map_tracker.map_get)
        .on("map.update", map_tracker.map_update)
        .on("map.create", map_tracker.map_create)
    )
    mocker.activate(monkeypatch)

    monkeypatch.setattr(
        "zabbix_map._validate_edges_layout",
        lambda *args, **kwargs: [{"box_a": "a", "box_b": "b"}],
    )

    err, sysmapid = update_uplinks_map(
        "https://z.example/api_jsonrpc.php",
        "token",
        devices,
        host_id,
        items,
        desc,
        debug=False,
        device_iface_to_provider=dry_ssh_minimal_inventory_context()["device_iface_to_provider"],
    )

    assert sysmapid is None
    assert err is not None
    assert "layout validation failed" in err
    assert not map_tracker.updates
    assert not map_tracker.creates

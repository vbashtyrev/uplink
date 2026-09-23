#!/usr/bin/env python3
"""Build or update a Zabbix map for uplinks (hosts, providers, links) based on dry-ssh.json and Zabbix API."""

import argparse
import json
import math
import os
import re
import sys

from env_urls import load_env_file_if_present
from uplinks.data import (
    DEFAULT_INPUT,
    DESCRIPTION_MAP_FILE,
    GENERATE_DESCRIPTION_MAP_REQUIRES_LEGACY_MSG,
    MAP_LEGACY_UTILITY_REQUIRES_LEGACY_MSG,
    load_description_map,
    load_devices_json,
    resolve_uplink_cli_input,
)
from uplinks.netbox.inventory import (
    arm_netbox_incomplete_guard,
    is_uplink_iface,
    load_inventory_report,
    load_uplink_provider_context,
    resolve_provider_name_for_iface,
    scoped_devices_from_inventory_report,
)
from uplinks.zabbix.client import (
    BITS_RECEIVED_NAME,
    BITS_SENT_NAME,
    ZABBIX_CACHE_FILE,
    _get_zabbix_url_token,
    _interface_from_item_name,
    _interface_from_key,
    _normalize_interface_name,
    fetch_zabbix_hosts_and_items,
    load_zabbix_cache,
    save_zabbix_cache,
    validate_zabbix_token,
    zabbix_request,
)

load_env_file_if_present()


from uplinks_config import (
    LINK_COLOR_HIGH,
    LINK_COLOR_WARN,
    MAP_ICON_CLOUD,
    MAP_ICON_HOST,
    MAP_NAME,
    TRIGGER_DESC_90_SUFFIX,
    TRIGGER_DESC_100_SUFFIX,
    TRIGGER_DESC_SEARCH,
    UPLINKS_AGGREGATE_HOST_PREFIX,
)

LEGACY_TRIGGER_DESC_90_SUFFIX = "High bandwidth ({}%)".format(90)
LEGACY_TRIGGER_DESC_100_SUFFIX = "High bandwidth (threshold line)"
# Zabbix 7: 0 line, 2 bold, 3 dotted, 4 dashed (value 1 is not allowed in the API → Wrong fields for map link).
LINK_DRAWTYPE_BOLD = 2


def _api_map_id(value):
    """Integer ID for map link/selement fields (Zabbix 7 API)."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


# PERM_READ = 1, PERM_READ_WRITE = 2 (Zabbix constants for map sharing)
MAP_SHARE_PERMISSION = 2

def _get_map_user_groups(url, token, debug=False):
    """
    List of user groups for map sharing: userGroups = [{usrgrpid, permission}, ...].
    In Zabbix 7, map.create may require userGroups when permissions checking is enabled. Not yet in use.
    Return (list, None) or (None, error_msg).
    """
    result, err = zabbix_request(
        url, token, "user.get",
        {"output": ["userid"], "selectUsrgrps": ["usrgrpid"], "limit": 1},
        debug=debug,
    )
    if err or not result:
        return None, err or "user.get: empty response"
    usrgrps = result[0].get("usrgrps") or []
    user_groups = []
    for g in usrgrps:
        u = g.get("usrgrpid")
        if u is not None:
            user_groups.append({"usrgrpid": int(u), "permission": MAP_SHARE_PERMISSION})
    if not user_groups:
        return None, "user has no groups (usrgrps); add the user to a group in Zabbix"
    return user_groups, None


def _normalize_provider_name(name):
    """
    Normalize the provider name for stable matching:
    We ignore case and delimiters (space/hyphen/underscore/signs).
    Example: "ER-Telecom", "Er telecom" -> "ertelecom".
    """
    if not name:
        return ""
    return re.sub(r"[^a-z0-9]+", "", str(name).strip().lower())


def get_provider_aggregate_triggers(url, token, providers, debug=False):
    """
    Get triggerid of aggregate triggers 90%/100% by providers.

    Triggers are created by zabbix_provider_aggregate.py on the “Uplinks {Provider}” hosts:
    - priority=1 (Information) — 90% of the limit;
    - priority=2 (Warning) — 100% of the limit.

    Return: dict provider_name -> (triggerid_warn, triggerid_high).
    """
    providers = [p for p in (providers or []) if p]
    if not providers:
        return {}
    # Find aggregate hosts: first by technical host (host), then by visible name (name),
    # because for some providers (Fiord / PING-WIN) host and name may differ.
    host_names = [UPLINKS_AGGREGATE_HOST_PREFIX + p for p in providers]
    hostid_by_provider = {}

    # host.get by host field
    result, err = zabbix_request(
        url,
        token,
        "host.get",
        {
            "output": ["hostid", "host", "name"],
            "filter": {"host": host_names},
        },
        debug=debug,
    )
    if err:
        return {}
    provider_key_to_name = {_normalize_provider_name(p): p for p in providers}

    def _match_provider_from_host_fields(host_value, name_value):
        for candidate in (host_value or "", name_value or ""):
            c = str(candidate).strip()
            if not c:
                continue
            # Exact match "Uplinks <Provider>"
            for p in providers:
                wanted = UPLINKS_AGGREGATE_HOST_PREFIX + p
                if c == wanted:
                    return p
            # Normalized suffix after prefix match
            if c.startswith(UPLINKS_AGGREGATE_HOST_PREFIX):
                suffix = c[len(UPLINKS_AGGREGATE_HOST_PREFIX):]
                p = provider_key_to_name.get(_normalize_provider_name(suffix))
                if p:
                    return p
        return None

    for h in result or []:
        host = h.get("host") or ""
        name = h.get("name") or ""
        matched = _match_provider_from_host_fields(host, name)
        if matched:
            hostid_by_provider[matched] = str(h.get("hostid"))

    # For missing ones - try by visible name (name)
    missing = [p for p in providers if p not in hostid_by_provider]
    if missing:
        names_filter = [UPLINKS_AGGREGATE_HOST_PREFIX + p for p in missing]
        result2, err2 = zabbix_request(
            url,
            token,
            "host.get",
            {
                "output": ["hostid", "host", "name"],
                "filter": {"name": names_filter},
            },
            debug=debug,
        )
        if not err2:
            for h in result2 or []:
                host = h.get("host") or ""
                name = h.get("name") or ""
                matched = _match_provider_from_host_fields(host, name)
                if matched and matched in missing:
                    hostid_by_provider[matched] = str(h.get("hostid"))
    if not hostid_by_provider:
        return {}
    hostids = list({hid for hid in hostid_by_provider.values() if hid})
    trig_res, err = zabbix_request(
        url,
        token,
        "trigger.get",
        {
            "hostids": hostids,
            "output": ["triggerid", "description", "priority"],
            "selectHosts": ["hostid"],
        },
        debug=debug,
    )
    if err or not trig_res:
        return {}
    triggers_by_provider = {p: {"warn": None, "high": None} for p in providers}
    # Priorities are set in zabbix_provider_aggregate: 1=Info (90%), 2=Warning (100%).
    hostid_to_provider = {hid: p for p, hid in hostid_by_provider.items()}
    for t in trig_res:
        hosts = t.get("hosts") or []
        hostid = None
        if hosts and isinstance(hosts[0], dict):
            hostid = str(hosts[0].get("hostid") or "")
        if not hostid or hostid not in hostid_to_provider:
            continue
        provider = hostid_to_provider[hostid]
        prio = int(t.get("priority", 0))
        tid = t.get("triggerid")
        if not tid:
            continue
        if prio == 1:
            triggers_by_provider.setdefault(provider, {})["warn"] = tid
        elif prio == 2:
            triggers_by_provider.setdefault(provider, {})["high"] = tid
    out = {}
    for p, ids in triggers_by_provider.items():
        out[p] = (ids.get("warn"), ids.get("high"))
    return out


def _iface_from_trigger_desc(desc):
    """Extract iface from 'Interface <iface>: ...'."""
    s = (desc or "").strip()
    if not s.startswith("Interface "):
        return None
    rest = s[len("Interface "):]
    if ":" not in rest:
        return None
    return rest.split(":", 1)[0].strip() or None


def get_link_commit_triggers(url, token, hostids, debug=False):
    """Return dict (hostid, iface) -> (warn_triggerid, high_triggerid)."""
    if not hostids:
        return {}
    res, err = zabbix_request(
        url,
        token,
        "trigger.get",
        {
            "output": ["triggerid", "description"],
            "hostids": [str(h) for h in hostids],
            "selectHosts": ["hostid"],
            "search": {"description": "Interface "},
        },
        debug=debug,
    )
    if err:
        return {}
    out = {}
    for t in (res or []):
        desc = (t.get("description") or "").strip()
        iface = _iface_from_trigger_desc(desc)
        hosts = t.get("hosts") or []
        if not iface or not hosts:
            continue
        hostid = str(hosts[0].get("hostid") or "")
        if not hostid:
            continue
        key = (hostid, _normalize_interface_name(iface))
        warn_id, high_id = out.get(key, (None, None))
        tid = t.get("triggerid")
        if not tid:
            continue
        if desc.endswith(TRIGGER_DESC_90_SUFFIX) or desc.endswith(LEGACY_TRIGGER_DESC_90_SUFFIX):
            warn_id = tid
        if desc.endswith(TRIGGER_DESC_100_SUFFIX) or desc.endswith(LEGACY_TRIGGER_DESC_100_SUFFIX):
            high_id = tid
        out[key] = (warn_id, high_id)
    return out


MAP_WIDTH = 1460
MAP_HEIGHT = 800
LAYOUT_MAX_WIDTH = 1800
LAYOUT_MAX_HEIGHT = 2600
ELEMENT_TYPE_HOST = 0
# In API: 0=host, 4=image (picture with caption)
ELEMENT_TYPE_IMAGE = 4

# Icon placement slot: Zabbix map (x,y) is the upper-left of the element area; icons render inside it.
LAYOUT_MARGIN = 30
LAYOUT_ELEMENT_GAP = 8
# Dynamic empty space between the occupied bounds and each canvas edge.
LAYOUT_CANVAS_PADDING = 100
SELEMENT_HEIGHT = 200
SELEMENT_WIDTH = 200
# Zabbix map icons Router_symbol_(64) and Cloud_(64) render at 64x64 inside the slot.
RENDER_ICON_SIZE = 64
RENDER_ICON_INSET = (SELEMENT_WIDTH - RENDER_ICON_SIZE) / 2.0

# Zabbix map label_location: 0 bottom, 1 left, 2 right, 3 top, -1 map default.
LABEL_LOCATION_BOTTOM = 0
LABEL_LOCATION_LEFT = 1
LABEL_LOCATION_RIGHT = 2
LABEL_LOCATION_TOP = 3
LABEL_LOCATION_DEFAULT = -1
LAYOUT_PROVIDER_LABEL_LOCATION = LABEL_LOCATION_TOP
LAYOUT_HOST_LABEL_LOCATION = LABEL_LOCATION_BOTTOM

# Conservative visible-label preview estimates (exact Zabbix font metrics are not exposed via API).
LABEL_CHAR_WIDTH_PX = 9
LABEL_LINE_HEIGHT_PX = 16
LABEL_PADDING_PX = 6
LINK_LABEL_LINE_HEIGHT_PX = 14
# Preview cap for 3-line link labels (iface + In + Out); not 56 chars * 8 px.
LINK_LABEL_PREVIEW_WIDTH_PX = 220
LINK_LABEL_PREVIEW_HEIGHT_PX = 54
LAYOUT_LABEL_GAP = 8
# Horizontal component spacing observed on the compact view-mode map.
LAYOUT_COLUMN_STEP = 200
# Dense lattice for component packing; the reference map uses 100px row bands.
LAYOUT_LATTICE_ROW_STEP = 100
# In-tile star host-row spacing observed on the compact view-mode map.
LAYOUT_STAR_ROW_STEP = 100
# Fail-closed cap on provider slot backtracking for dense single-host fans.
LAYOUT_SLOT_SEARCH_MAX_CANDIDATES = 256
# Bounded lattice scan for hole-filling tile packing (deterministic, no infinite loops).
LAYOUT_PACK_MAX_GRID_COLS = 12
LAYOUT_PACK_MAX_GRID_ROWS = 12
# Bounded fine-Y refinement when a coarse lattice row fails validation.
LAYOUT_PACK_FINE_Y_RADIUS = 16
LAYOUT_PACK_FINE_Y_STEP = 2
# Bounded vertical link-corridor scales (tightest first); full 220x54 labels stay centered.
LAYOUT_VERTICAL_LINK_SCALES = (0.5, 1.0)


class LayoutValidationError(RuntimeError):
    """Raised when the offline layout model still detects collisions."""


def _layout_int(value):
    """Round layout scalar to integer for Zabbix map API payloads."""
    return int(round(value))


def _layout_int_pos(pos):
    return (_layout_int(pos[0]), _layout_int(pos[1]))


def _layout_normalize_positions(host_pos, isp_pos):
    """Normalize icon coordinates to integers while preserving geometry."""
    return (
        {hid: _layout_int_pos(pos) for hid, pos in host_pos.items()},
        {isp: _layout_int_pos(pos) for isp, pos in isp_pos.items()},
    )


def _layout_render_extent():
    """Editor-slot origin to render-icon far edge (icons are centered in the slot)."""
    return RENDER_ICON_INSET + RENDER_ICON_SIZE


def _layout_icon_step():
    """Horizontal/vertical distance between adjacent editor-slot upper-left corners."""
    return RENDER_ICON_SIZE + LAYOUT_ELEMENT_GAP


def _layout_column_step():
    """Horizontal component spacing for visible icons and topology templates."""
    return max(_layout_icon_step(), LAYOUT_COLUMN_STEP)


def _layout_star_row_step():
    """Vertical spacing between star-pattern host rows inside a component tile."""
    return LAYOUT_STAR_ROW_STEP


def _layout_provider_label_corridor():
    """Space reserved above provider icons for a one-line top label."""
    return LABEL_LINE_HEIGHT_PX + 2 * LABEL_PADDING_PX + LAYOUT_LABEL_GAP


def _layout_host_label_corridor():
    """Space reserved below host icons for a one-line bottom label."""
    return LABEL_LINE_HEIGHT_PX + 2 * LABEL_PADDING_PX + LAYOUT_LABEL_GAP


def _layout_vertical_link_height(link_scale=1.0):
    """Scaled link-label corridor height; full preview stays 54px at link_scale=1."""
    if link_scale >= 1.0:
        return LINK_LABEL_PREVIEW_HEIGHT_PX
    return max(LAYOUT_LABEL_GAP, _layout_int(LINK_LABEL_PREVIEW_HEIGHT_PX * link_scale))


def _layout_link_corridor_for(link_scale=1.0):
    """Vertical gap between provider and host icon rows for a 3-line link label."""
    return _layout_vertical_link_height(link_scale) + 2 * LAYOUT_LABEL_GAP


def _layout_link_corridor():
    """Default compact vertical link corridor (tightest scale)."""
    return _layout_link_corridor_for(LAYOUT_VERTICAL_LINK_SCALES[0])


def _layout_provider_below_host_row_for(host_ry, link_scale=1.0):
    """
    Relative Y for a provider icon directly below a host row.
    Compact scale keeps render/label clearance; full scale reserves link corridor.
    """
    base = (
        host_ry
        + _layout_render_extent()
        + _layout_host_label_corridor()
        + LAYOUT_LABEL_GAP
    )
    if link_scale >= 1.0:
        return base + LINK_LABEL_PREVIEW_HEIGHT_PX + LAYOUT_LABEL_GAP + _layout_provider_label_corridor()
    return base + _layout_provider_label_corridor()


def _layout_provider_below_host_row(host_ry):
    """Default compact provider-below-host row offset."""
    return _layout_provider_below_host_row_for(host_ry, LAYOUT_VERTICAL_LINK_SCALES[0])


def _layout_provider_below_shared_host_for(host_ry, link_scale=1.0):
    """
    Tight provider Y below a same-column shared host: clears the host bottom label
    and the centered vertical link-label preview without extra shelf padding.
    """
    base = (
        host_ry
        + _layout_render_extent()
        + _layout_host_label_corridor()
        + LINK_LABEL_PREVIEW_HEIGHT_PX
    )
    if link_scale >= 1.0:
        return base + LAYOUT_LABEL_GAP + _layout_provider_label_corridor()
    return base + LAYOUT_LABEL_GAP


def _layout_host_row_offset_for(link_scale=1.0):
    """
    Y offset from block top to the host icon row.
    Compact scale keeps render-icon clearance only; full scale reserves link corridor.
    """
    provider_y = _layout_provider_label_corridor()
    base = provider_y + _layout_render_extent() + LAYOUT_LABEL_GAP
    if link_scale >= 1.0:
        return base + LINK_LABEL_PREVIEW_HEIGHT_PX + LAYOUT_LABEL_GAP
    return base


def _layout_host_row_offset():
    """Default compact host-row Y offset from block top."""
    return _layout_host_row_offset_for(LAYOUT_VERTICAL_LINK_SCALES[0])


def _layout_host_row_step_for(link_scale=1.0):
    """Y distance between host editor-slot rows (render icon + labels + link corridor)."""
    return (
        _layout_render_extent()
        + _layout_host_label_corridor()
        + _layout_link_corridor_for(link_scale)
        + LAYOUT_LABEL_GAP
    )


def _layout_host_row_step():
    """Default compact Y distance between host rows."""
    return _layout_host_row_step_for(LAYOUT_VERTICAL_LINK_SCALES[0])


def _layout_component_row_ys(link_scale=1.0):
    """Deterministic host/provider row anchors for one vertical link scale."""
    row_offset = _layout_host_row_offset_for(link_scale)
    row_step = _layout_host_row_step_for(link_scale)
    return {
        "link_scale": link_scale,
        "provider_y": _layout_provider_label_corridor(),
        "row_offset": row_offset,
        "row_step": row_step,
        "row1_y": row_offset,
        "row2_y": row_offset + row_step,
        "row3_y": row_offset + 2 * row_step,
        "row4_y": row_offset + 3 * row_step,
    }


def _layout_multi_host_grid(num_placed):
    """Column/row packing for provider-centered tiles (3 columns for cross layouts)."""
    if num_placed <= 1:
        return 1, 1
    if num_placed == 2:
        return 3, 1
    if num_placed == 3:
        return 3, 2
    return 3, 2


def _layout_single_host_horizontal_offset(provider_name=None):
    """
    X offset between provider and host editor slots in a same-row single-host block.
    Must clear the provider top label (render-icon model) with LAYOUT_LABEL_GAP clearance.
    """
    icon_step = _layout_icon_step()
    if not provider_name:
        return icon_step
    provider_cx = RENDER_ICON_INSET + RENDER_ICON_SIZE / 2.0
    label_width, _ = _label_text_metrics(provider_name)
    min_for_label = (
        provider_cx
        + label_width / 2.0
        + LAYOUT_LABEL_GAP * 2
        + RENDER_ICON_INSET
    )
    return max(icon_step, _layout_int(min_for_label))


def _layout_block_width(single_host=False, num_placed=1, primary_isp=None, hosts_to_place=None, edges=None):
    """Content width of one provider-centered block (3-column cross when multi-host)."""
    if single_host:
        return _layout_single_host_horizontal_offset(primary_isp) + _layout_render_extent()
    if num_placed <= 1:
        return _layout_render_extent()
    col_step = _layout_column_step()
    cols, _rows = _layout_multi_host_grid(num_placed)
    return (cols - 1) * col_step + _layout_render_extent()


def _layout_block_height(single_host, num_placed, primary_isp=None, hosts_to_place=None, edges=None, link_scale=1.0):
    """Reserved vertical extent of a placed provider-centered block."""
    if num_placed <= 0:
        return 0
    provider_y = _layout_provider_label_corridor()
    if single_host:
        link_tail = (
            LAYOUT_LABEL_GAP + LINK_LABEL_PREVIEW_HEIGHT_PX
            if link_scale >= 1.0
            else LAYOUT_LABEL_GAP
        )
        return (
            provider_y
            + _layout_render_extent()
            + _layout_host_label_corridor()
            + link_tail
        )
    row_offset = _layout_host_row_offset_for(link_scale)
    row_step = _layout_host_row_step_for(link_scale)
    star_step = _layout_star_row_step()
    if num_placed == 2:
        return provider_y + _layout_render_extent() + _layout_host_label_corridor()
    if num_placed == 3:
        return provider_y + star_step + _layout_render_extent() + _layout_host_label_corridor()
    return provider_y + star_step + _layout_render_extent() + _layout_host_label_corridor()


def _layout_block_positions(single_host, hosts_to_place, primary_isp=None, edges=None, link_scale=1.0):
    """Relative (rx, ry) positions for a provider-centered star block."""
    provider_y = _layout_provider_label_corridor()
    col_step = _layout_column_step()
    if single_host:
        (_hostname, hostid) = hosts_to_place[0]
        return [
            ("isp", None, 0, provider_y),
            ("host", str(hostid), _layout_single_host_horizontal_offset(primary_isp), provider_y),
        ]

    num_placed = len(hosts_to_place)
    star_step = _layout_star_row_step()
    if num_placed == 2:
        positions = [("isp", None, col_step, provider_y)]
        (_h0, hid0), (_h1, hid1) = hosts_to_place
        positions.extend([
            ("host", str(hid0), 0, provider_y),
            ("host", str(hid1), 2 * col_step, provider_y),
        ])
        return positions
    if num_placed == 3:
        bot_y = provider_y + star_step
        positions = [("isp", None, col_step, provider_y)]
        for idx, (_hostname, hostid) in enumerate(hosts_to_place[:2]):
            positions.append(("host", str(hostid), idx * 2 * col_step, provider_y))
        positions.append(("host", str(hosts_to_place[2][1]), col_step, bot_y))
        return positions
    bot_y = provider_y + star_step
    positions = [("isp", None, col_step, provider_y)]
    corner_cols = (0, 2 * col_step)
    for idx, (_hostname, hostid) in enumerate(hosts_to_place[:4]):
        positions.append((
            "host",
            str(hostid),
            corner_cols[idx % 2],
            provider_y if idx < 2 else bot_y,
        ))
    return positions


def _layout_graph_indices(edges):
    """Build host/provider adjacency maps from edge list."""
    host_to_providers = {}
    provider_to_hosts = {}
    host_names = {}
    for hostname, hostid, _iface, isp, *_rest in edges:
        if not isp:
            continue
        hid = str(hostid)
        host_names[hid] = hostname
        host_to_providers.setdefault(hid, set()).add(isp)
        provider_to_hosts.setdefault(isp, set()).add(hid)
    return host_to_providers, provider_to_hosts, host_names


def _layout_connected_components(edges):
    """Partition edges into connected provider-router components."""
    host_to_providers, provider_to_hosts, _host_names = _layout_graph_indices(edges)
    parent = {}

    def _find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def _union(left, right):
        root_left = _find(left)
        root_right = _find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    nodes = set()
    for hid, providers in host_to_providers.items():
        nodes.add(("host", hid))
        for isp in providers:
            nodes.add(("provider", isp))
    for node in nodes:
        parent[node] = node
    for hid, providers in host_to_providers.items():
        host_node = ("host", hid)
        for isp in providers:
            _union(host_node, ("provider", isp))

    groups = {}
    for node in nodes:
        groups.setdefault(_find(node), set()).add(node)

    components = []
    for members in sorted(groups.values(), key=lambda group: (-len(group), sorted(group)[0])):
        providers = sorted(isp for kind, isp in members if kind == "provider")
        hosts = sorted(hid for kind, hid in members if kind == "host")
        comp_edges = [
            edge for edge in edges
            if str(edge[1]) in hosts and edge[3] in providers
        ]
        components.append({
            "providers": providers,
            "hosts": hosts,
            "edges": comp_edges,
            "host_to_providers": {
                hid: host_to_providers.get(hid, set()) & set(providers)
                for hid in hosts
            },
            "provider_to_hosts": {
                isp: provider_to_hosts.get(isp, set()) & set(hosts)
                for isp in providers
            },
        })
    return components


def _layout_component_star(component, link_scale=1.0):
    """Single-provider star tile: provider central, exclusive hosts in a grid below."""
    provider = component["providers"][0]
    host_pos = {}
    isp_pos = {}
    host_entries = []
    for hid in component["hosts"]:
        hostname = next(edge[0] for edge in component["edges"] if str(edge[1]) == hid)
        host_entries.append((hostname, hid))
    single_host = len(host_entries) == 1
    positions = _layout_block_positions(
        single_host, host_entries, primary_isp=provider, edges=component["edges"],
        link_scale=link_scale,
    )
    for kind, key, rel_x, rel_y in positions:
        if kind == "isp":
            isp_pos[provider] = (rel_x, rel_y)
        else:
            host_pos[key] = (rel_x, rel_y)
    return host_pos, isp_pos


def _layout_providers_by_exclusive_hosts(providers, host_to_providers):
    """Primary provider first: highest hub score, then stable name order."""
    def _hub_score(isp):
        return sum(
            len(provider_set)
            for _hid, provider_set in host_to_providers.items()
            if isp in provider_set
        )
    return sorted(providers, key=lambda isp: (-_hub_score(isp), isp))


def _layout_component_pair(component, link_scale=1.0):
    """
    Two-provider tile with a 3-column provider-centered grid.
    Hurricane shape: [L0] [P_left] [L1] / [L2] [shared] [P_right].
    """
    provider_left, provider_right = _layout_providers_by_exclusive_hosts(
        component["providers"], component["host_to_providers"],
    )
    host_to_providers = component["host_to_providers"]
    col_step = _layout_column_step()
    rows = _layout_component_row_ys(link_scale)
    provider_y = rows["provider_y"]
    row1_y = rows["row1_y"]
    row2_y = rows["row2_y"]
    row_step = rows["row_step"]
    host_pos = {}
    isp_pos = {}

    shared_hosts = sorted(
        hid for hid, providers in host_to_providers.items()
        if provider_left in providers and provider_right in providers
    )
    left_only = sorted(
        hid for hid, providers in host_to_providers.items()
        if providers == {provider_left}
    )
    right_only = sorted(
        hid for hid, providers in host_to_providers.items()
        if providers == {provider_right}
    )

    if shared_hosts and not left_only and not right_only:
        if len(shared_hosts) == 2:
            host_pos[shared_hosts[0]] = (0, row1_y)
            host_pos[shared_hosts[1]] = (2 * col_step, row1_y)
            isp_pos[provider_left] = (col_step, provider_y)
            isp_pos[provider_right] = (col_step, row2_y)
            return host_pos, isp_pos
        host_entries = []
        for hid in shared_hosts:
            hostname = next(
                edge[0] for edge in component["edges"] if str(edge[1]) == hid
            )
            host_entries.append((hostname, hid))
        positions = _layout_block_positions(
            len(host_entries) == 1,
            host_entries,
            primary_isp=provider_left,
            edges=component["edges"],
            link_scale=link_scale,
        )
        for kind, key, rel_x, rel_y in positions:
            if kind == "host":
                host_pos[key] = (rel_x, rel_y)
            else:
                isp_pos[provider_left] = (rel_x, rel_y)
        anchor_host = shared_hosts[0]
        anchor_x, anchor_y = host_pos[anchor_host]
        isp_pos[provider_right] = (anchor_x + 2 * col_step, anchor_y)
        return host_pos, isp_pos

    if len(left_only) >= 3 and shared_hosts:
        top_y = provider_y
        bot_y = top_y + _layout_star_row_step()
        mid_x = col_step
        isp_pos[provider_left] = (mid_x, top_y)
        isp_pos[provider_right] = (mid_x, bot_y)
        host_pos[left_only[0]] = (0, top_y)
        host_pos[left_only[1]] = (2 * col_step, top_y)
        host_pos[left_only[2]] = (0, bot_y)
        host_pos[shared_hosts[0]] = (2 * col_step, bot_y)
        return host_pos, isp_pos

    if len(left_only) == 1 and len(shared_hosts) == 1 and not right_only:
        host_pos[left_only[0]] = (0, row1_y)
        host_pos[shared_hosts[0]] = (2 * col_step, row1_y)
        isp_pos[provider_left] = (col_step, provider_y)
        isp_pos[provider_right] = (3 * col_step, row1_y)
        return host_pos, isp_pos

    isp_pos[provider_left] = (col_step, row1_y)
    if len(left_only) >= 1:
        host_pos[left_only[0]] = (0, row1_y)
    if len(left_only) >= 2:
        host_pos[left_only[1]] = (2 * col_step, row1_y)

    shared_y = row2_y if len(left_only) >= 2 else row1_y
    for idx, hid in enumerate(shared_hosts):
        shared_col = col_step if len(left_only) >= 2 else (idx + 1) * col_step
        host_pos[hid] = (shared_col, shared_y)

    if right_only:
        right_y = row2_y if shared_hosts or len(left_only) >= 2 else row1_y
        for idx, hid in enumerate(right_only):
            host_pos[hid] = ((idx + 2) * col_step, right_y)
        isp_pos[provider_right] = (2 * col_step, right_y)
    elif shared_hosts:
        isp_pos[provider_right] = (2 * col_step, shared_y)
    else:
        isp_pos[provider_right] = (2 * col_step, row1_y)
    return host_pos, isp_pos


def _layout_component_pair_single_host(component):
    """Two-provider tile with one shared host: providers flank the host."""
    provider_primary, provider_secondary = _layout_providers_by_exclusive_hosts(
        component["providers"], component["host_to_providers"],
    )
    host_id = component["hosts"][0]
    provider_y = _layout_provider_label_corridor()
    col_step = _layout_column_step()
    host_pos = {host_id: (col_step, provider_y)}
    isp_pos = {
        provider_primary: (0, provider_y),
        provider_secondary: (2 * col_step, provider_y),
    }
    return host_pos, isp_pos


def _layout_component_is_dedicated_secondary_pattern(component):
    """True when each secondary provider links to one shared primary host."""
    providers = _layout_providers_by_exclusive_hosts(
        component["providers"], component["host_to_providers"],
    )
    if len(providers) < 2:
        return False
    primary = providers[0]
    host_to_providers = component["host_to_providers"]
    for isp in providers[1:]:
        anchor_hosts = [
            hid for hid, provider_set in host_to_providers.items()
            if isp in provider_set
        ]
        if len(anchor_hosts) != 1:
            return False
        if primary not in host_to_providers[anchor_hosts[0]]:
            return False
    return True


def _layout_component_dedicated_secondary(component, link_scale=1.0):
    """
    Shared hosts flanking a centered primary provider; dedicated secondaries below
    their anchor host. Target: [MSK1] [Piter] [MSK2] / [Beeline] [Ertelecom].
    """
    providers = _layout_providers_by_exclusive_hosts(
        component["providers"], component["host_to_providers"],
    )
    primary = providers[0]
    host_to_providers = component["host_to_providers"]
    secondary_anchors = {}
    for isp in providers[1:]:
        anchor = next(
            hid for hid, provider_set in host_to_providers.items()
            if isp in provider_set
        )
        secondary_anchors[isp] = anchor

    # Put the first secondary anchor on the right and the second on the left;
    # this keeps each secondary label inside its anchor pair without crossing
    # the opposite host label.
    shared_hosts = list(reversed(sorted(set(secondary_anchors.values()))))
    exclusive_hosts = sorted(
        hid for hid in component["hosts"] if hid not in shared_hosts
    )
    col_step = _layout_column_step()
    rows = _layout_component_row_ys(link_scale)
    provider_y = rows["provider_y"]
    row1_y = rows["row1_y"]
    row2_y = rows["row2_y"]
    row3_y = rows["row3_y"]
    host_pos = {}
    isp_pos = {}

    if exclusive_hosts:
        host_pos[shared_hosts[0]] = (col_step, row1_y)
        host_pos[shared_hosts[1]] = (2 * col_step, row1_y)
        isp_pos[primary] = (col_step, row2_y)
        for isp, anchor in sorted(secondary_anchors.items()):
            if anchor == shared_hosts[0]:
                isp_pos[isp] = (0, row1_y)
            else:
                isp_pos[isp] = (3 * col_step, row1_y)
        host_pos[exclusive_hosts[0]] = (0, row2_y)
        if len(exclusive_hosts) > 1:
            host_pos[exclusive_hosts[1]] = (2 * col_step, row2_y)
        return host_pos, isp_pos

    row_y = provider_y
    secondary_y = row_y + LAYOUT_STAR_ROW_STEP
    host_pos[shared_hosts[0]] = (0, row_y)
    host_pos[shared_hosts[1]] = (2 * col_step, row_y)
    isp_pos[primary] = (col_step, row_y)
    secondary_inset = (3 * col_step) // 4
    for isp, anchor in sorted(secondary_anchors.items()):
        if anchor == shared_hosts[0]:
            isp_pos[isp] = (secondary_inset, secondary_y)
        else:
            isp_pos[isp] = (2 * col_step - secondary_inset, secondary_y)

    return host_pos, isp_pos


def _layout_provider_slot_pool(col_step, provider_y, row1_y, row2_y, row3_y, row4_y=None):
    """Candidate upper-left slots for provider icons inside a component tile."""
    rows = [provider_y, row1_y, row2_y, row3_y]
    if row4_y is not None:
        rows.append(row4_y)
    pool = []
    for row_y in rows:
        for col in (0, col_step, 2 * col_step, 3 * col_step):
            pool.append((col, row_y))
    return pool


def _layout_assign_providers_to_slots(host_pos, isp_pos, providers, slot_pool, edges):
    """Backtracking slot assignment validated by the full offline layout model."""
    used = set(host_pos.values()) | set(isp_pos.values())
    free_slots = [slot for slot in slot_pool if slot not in used]
    search_state = {"candidates": 0}

    def _search(idx, trial_isp):
        if idx >= len(providers):
            return True
        isp = providers[idx]
        for slot in free_slots:
            if slot in used:
                continue
            search_state["candidates"] += 1
            if search_state["candidates"] > LAYOUT_SLOT_SEARCH_MAX_CANDIDATES:
                return False
            trial_isp[isp] = slot
            if _validate_edges_layout(edges, host_pos, trial_isp) != []:
                del trial_isp[isp]
                continue
            used.add(slot)
            if _search(idx + 1, trial_isp):
                return True
            used.remove(slot)
            del trial_isp[isp]
        return False

    trial_isp = dict(isp_pos)
    if not _search(0, trial_isp):
        return None
    return trial_isp


def _layout_component_general_multi(component, link_scale=1.0):
    """Deterministic multi-provider layout for two shared hosts."""
    providers = _layout_providers_by_exclusive_hosts(
        component["providers"], component["host_to_providers"],
    )
    primary = providers[0]
    hosts = sorted(component["hosts"])
    col_step = _layout_column_step()
    rows = _layout_component_row_ys(link_scale)
    provider_y = rows["provider_y"]
    row1_y = rows["row1_y"]
    row2_y = rows["row2_y"]
    row3_y = rows["row3_y"]
    row4_y = rows["row4_y"]
    host_pos = {hosts[0]: (0, row1_y), hosts[1]: (2 * col_step, row1_y)}
    isp_pos = {primary: (col_step, provider_y)}
    slot_pool = _layout_provider_slot_pool(
        col_step, provider_y, row1_y, row2_y, row3_y, row4_y,
    )
    assigned = _layout_assign_providers_to_slots(
        host_pos, isp_pos, providers[1:], slot_pool, component["edges"],
    )
    if assigned is None:
        raise LayoutValidationError(
            "layout: cannot place providers {!r} in multi-host component".format(
                component["providers"],
            )
        )
    return host_pos, assigned


def _layout_component_providers_around_host(component, link_scale=1.0):
    """One host with multiple providers: host centered, providers on adjacent slots."""
    host_id = component["hosts"][0]
    providers = _layout_providers_by_exclusive_hosts(
        component["providers"], component["host_to_providers"],
    )
    col_step = _layout_column_step()
    provider_y = _layout_provider_label_corridor()
    host_pos = {host_id: (col_step, provider_y)}
    below_y = (
        _layout_provider_below_host_row_for(provider_y, link_scale)
        + _layout_link_corridor_for(link_scale)
    )
    row_step = _layout_host_row_step_for(link_scale)
    slot_pool = [(0, provider_y), (2 * col_step, provider_y)]
    for row in range(6):
        row_y = below_y + row * row_step
        slot_pool.extend([(0, row_y), (col_step, row_y), (2 * col_step, row_y)])
    assigned = _layout_assign_providers_to_slots(
        host_pos, {}, providers, slot_pool, component["edges"],
    )
    if assigned is None:
        raise LayoutValidationError(
            "layout: cannot place providers around host {!r}".format(host_id)
        )
    return host_pos, assigned


def _layout_component_tile_for_scale(component, link_scale=1.0):
    """Build local coordinates for one connected component tile at one vertical scale."""
    providers = component["providers"]
    hosts = component["hosts"]
    if len(providers) == 1:
        return _layout_component_star(component, link_scale=link_scale)
    if len(hosts) == 1 and len(providers) == 2:
        return _layout_component_pair_single_host(component)
    if len(hosts) == 1 and len(providers) >= 2:
        return _layout_component_providers_around_host(component, link_scale=link_scale)
    if len(providers) == 2:
        return _layout_component_pair(component, link_scale=link_scale)
    if _layout_component_is_dedicated_secondary_pattern(component):
        host_pos, isp_pos = _layout_component_dedicated_secondary(
            component, link_scale=link_scale,
        )
        if _validate_edges_layout(component["edges"], host_pos, isp_pos) != []:
            raise LayoutValidationError(
                "layout: dedicated-secondary component failed validation for {!r}".format(
                    component["providers"],
                )
            )
        return host_pos, isp_pos
    if len(providers) >= 3 and len(hosts) >= 2:
        return _layout_component_general_multi(component, link_scale=link_scale)
    raise LayoutValidationError(
        "layout: unsupported component topology providers={!r} hosts={!r}".format(
            providers, hosts,
        )
    )


def _layout_component_tile(component):
    """Build local coordinates; pick the tightest safe vertical link scale per topology."""
    last_error = None
    for link_scale in LAYOUT_VERTICAL_LINK_SCALES:
        try:
            host_pos, isp_pos = _layout_component_tile_for_scale(component, link_scale)
        except LayoutValidationError as exc:
            last_error = exc
            continue
        if _validate_edges_layout(component["edges"], host_pos, isp_pos) != []:
            continue
        return host_pos, isp_pos
    if last_error is not None:
        raise last_error
    raise LayoutValidationError(
        "layout: no safe vertical link scale for component providers={!r} hosts={!r}".format(
            component["providers"], component["hosts"],
        )
    )


def _layout_content_aabb(selements, links, positions_by_id):
    """
    Axis-aligned bounds for tile sizing: render icons, labels, link labels,
    plus editor slots so API (x,y) upper-left corners stay on-canvas.
    """
    boxes = _layout_visible_boxes(selements, links, positions_by_id)
    min_x = min(box["bounds"][0] for box in boxes)
    min_y = min(box["bounds"][1] for box in boxes)
    max_x = max(box["bounds"][2] for box in boxes)
    max_y = max(box["bounds"][3] for box in boxes)
    for x, y in positions_by_id.values():
        left, top, right, bottom = _element_bounds(x, y)
        min_x = min(min_x, left)
        min_y = min(min_y, top)
        max_x = max(max_x, right)
        max_y = max(max_y, bottom)
    return min_x, min_y, max_x, max_y


def _layout_normalize_tile(host_pos, isp_pos, edges):
    """Shift tile to origin and return visible AABB size including label-gap padding."""
    if not host_pos and not isp_pos:
        return {}, {}, 0, 0
    all_positions = list(host_pos.values()) + list(isp_pos.values())
    min_x = min(x for x, _y in all_positions)
    min_y = min(y for x, y in all_positions)
    norm_host = {hid: (x - min_x, y - min_y) for hid, (x, y) in host_pos.items()}
    norm_isp = {isp: (x - min_x, y - min_y) for isp, (x, y) in isp_pos.items()}
    probe_host = {
        hid: (x + LAYOUT_MARGIN, y + LAYOUT_MARGIN)
        for hid, (x, y) in norm_host.items()
    }
    probe_isp = {
        isp: (x + LAYOUT_MARGIN, y + LAYOUT_MARGIN)
        for isp, (x, y) in norm_isp.items()
    }
    selements, links, _, _ = _build_layout_model_from_edges(edges, probe_host, probe_isp)
    positions = _layout_positions_from_model(selements)
    pad = LAYOUT_LABEL_GAP
    min_box_x, min_box_y, max_box_x, max_box_y = _layout_content_aabb(
        selements, links, positions,
    )
    min_box_x -= pad
    min_box_y -= pad
    max_box_x += pad
    max_box_y += pad
    origin_x = min_box_x - LAYOUT_MARGIN
    origin_y = min_box_y - LAYOUT_MARGIN
    norm_host = {
        hid: (x - origin_x, y - origin_y)
        for hid, (x, y) in norm_host.items()
    }
    norm_isp = {
        isp: (x - origin_x, y - origin_y)
        for isp, (x, y) in norm_isp.items()
    }
    tile_w = max_box_x - min_box_x
    tile_h = max_box_y - min_box_y
    return norm_host, norm_isp, tile_w, tile_h


def _layout_merge_component_positions(host_pos, isp_pos, tile, origin_x, origin_y):
    """Apply one tile at an absolute origin."""
    for hid, (rel_x, rel_y) in tile["host_pos"].items():
        host_pos[hid] = (origin_x + rel_x, origin_y + rel_y)
    for isp_name, (rel_x, rel_y) in tile["isp_pos"].items():
        isp_pos[isp_name] = (origin_x + rel_x, origin_y + rel_y)


def _layout_tile_icon_footprint(host_pos, isp_pos):
    """Axis-aligned icon footprint (upper-left origins through render extent)."""
    extent = _layout_render_extent()
    positions = list(host_pos.values()) + list(isp_pos.values())
    if not positions:
        return 0, 0, 0, 0
    min_x = min(x for x, _y in positions)
    min_y = min(y for x, y in positions)
    max_x = max(x for x, _y in positions) + extent
    max_y = max(y for x, y in positions) + extent
    return min_x, min_y, max_x, max_y


def _layout_tile_lattice_span(host_pos, isp_pos, col_step, row_step):
    """How many lattice cells a tile occupies (at least 1x1)."""
    min_x, min_y, max_x, max_y = _layout_tile_icon_footprint(host_pos, isp_pos)
    cols = max(1, math.ceil((max_x - min_x) / col_step))
    rows = max(1, math.ceil((max_y - min_y) / row_step))
    return cols, rows, min_x, min_y


def _layout_lattice_cells_for_positions(host_pos, isp_pos, col_step, row_step, margin):
    """Map absolute icon positions to occupied lattice grid cells."""
    cells = set()
    extent = _layout_render_extent()
    for x, y in list(host_pos.values()) + list(isp_pos.values()):
        left = x
        top = y
        right = x + extent
        bottom = y + extent
        col0 = max(0, int(math.floor((left - margin) / col_step)))
        col1 = max(0, int(math.floor((right - margin) / col_step)))
        row0 = max(0, int(math.floor((top - margin) / row_step)))
        row1 = max(0, int(math.floor((bottom - margin) / row_step)))
        for col in range(col0, col1 + 1):
            for row in range(row0, row1 + 1):
                cells.add((col, row))
    return cells


def _layout_tile_pack_priority(tile):
    """
    Pack order for dense lattice hole-filling (topology-driven, not by name).
    Two-provider stars and large single-provider stars first; dedicated-secondary last.
    """
    host_count = len(tile["host_pos"])
    provider_count = len(tile["isp_pos"])
    if provider_count >= 3:
        tier = 0
    elif provider_count == 2:
        tier = 3
    elif host_count >= 4:
        tier = 2
    else:
        tier = 1
    return (tier, host_count, provider_count, tile["providers"])


def _layout_pack_tile_class(tile):
    """Topology class for lattice band/hole scoring (not provider names)."""
    host_count = len(tile["host_pos"])
    provider_count = len(tile["isp_pos"])
    if provider_count >= 3:
        return "multi"
    if host_count >= 4 or provider_count == 2:
        return "top"
    if host_count == 2:
        return "pair"
    return "single"


def _layout_pack_tile_min_row(tile):
    """Minimum lattice row band for a tile class."""
    return {
        "top": 0,
        "pair": 1,
        "single": 2,
        "multi": 2,
    }[_layout_pack_tile_class(tile)]


def _layout_pack_content_bbox(host_pos, isp_pos):
    """Axis-aligned content bounds for placed icons (render extent included)."""
    positions = list(host_pos.values()) + list(isp_pos.values())
    if not positions:
        return None
    extent = _layout_render_extent()
    xs = [x for x, _y in positions]
    ys = [y for x, y in positions]
    return (min(xs), min(ys), max(xs) + extent, max(ys) + extent)


def _layout_pack_filler_column_band(grid_col, side):
    """Left/right column bands for hole-filling 2-host and single-host tiles."""
    if side == 0:
        return grid_col <= 1
    return grid_col >= 3


def _layout_pack_filler_side(tile_class, filler_index):
    """
    Alternate filler sides by placement order: pair tiles left then right;
    single-host tiles prefer the right column (under large top-band stars).
    """
    if tile_class == "pair":
        return filler_index % 2
    return 1


def _layout_pack_candidate_score(
    tile,
    origin_x,
    origin_y,
    grid_col,
    host_pos,
    isp_pos,
    filler_state,
    edges,
    pack_width,
):
    """
    Score one lattice origin. Lower is better. Returns None when invalid.
    Placement must pass the full offline render model (fail-closed).
    """
    trial_host = dict(host_pos)
    trial_isp = dict(isp_pos)
    _layout_merge_component_positions(
        trial_host, trial_isp, tile, origin_x, origin_y,
    )
    if _validate_edges_layout(edges, trial_host, trial_isp) != []:
        return None
    current = _layout_pack_content_bbox(host_pos, isp_pos)
    new_bounds = _layout_pack_content_bbox(trial_host, trial_isp)
    if new_bounds[3] > LAYOUT_MAX_HEIGHT - LAYOUT_MARGIN:
        return None
    width_overflow = max(0, new_bounds[2] - (pack_width - LAYOUT_MARGIN))
    tile_class = _layout_pack_tile_class(tile)
    if current is None:
        return (origin_y, origin_x)
    top_y = current[1]
    ext_h = max(0, new_bounds[3] - current[3])
    ext_w = max(0, new_bounds[2] - current[2])
    positions = list(tile["host_pos"].values()) + list(tile["isp_pos"].values())
    tile_min_y = origin_y + min(y for _x, y in positions)
    tile_max_y = origin_y + max(y for _x, y in positions) + _layout_render_extent()
    tile_min_x = origin_x + min(x for x, _y in positions)
    tile_max_x = origin_x + max(x for x, _y in positions) + _layout_render_extent()
    inside_current = (
        tile_min_y < current[3]
        and tile_max_y > current[1]
        and tile_min_x < current[2]
        and tile_max_x > current[0]
    )
    if tile_class == "top":
        row_penalty = max(0, origin_y - top_y) * 1000
        return (width_overflow, row_penalty, ext_h, ext_w, origin_y, origin_x)
    if tile_class in ("pair", "single"):
        filler_key = "pair" if tile_class == "pair" else "single"
        side = _layout_pack_filler_side(
            tile_class, filler_state[filler_key],
        )
        if not _layout_pack_filler_column_band(grid_col, side):
            return None
        return (
            width_overflow,
            0 if inside_current else 1,
            origin_y,
            ext_h,
            ext_w,
            origin_x,
        )
    return (width_overflow, ext_h, ext_w, origin_y, origin_x)


def _layout_pack_fine_y_deltas():
    """Deterministic fine-Y offsets around a coarse lattice row (±radius, step)."""
    radius = LAYOUT_PACK_FINE_Y_RADIUS
    step = LAYOUT_PACK_FINE_Y_STEP
    deltas = [0]
    offset = step
    while offset <= radius:
        deltas.extend((-offset, offset))
        offset += step
    return deltas


def _layout_pack_component_tiles(tiles, map_width, edges):
    """
    Pack component tiles on a dense lattice with hole-filling. Each tile scans
    bounded lattice origins row-major; candidates inside the current content
    band are preferred before extending height/width. A placement is accepted
    only when the full offline model reports zero render collisions.
    """
    host_pos = {}
    isp_pos = {}
    col_step = _layout_column_step()
    row_step = LAYOUT_LATTICE_ROW_STEP
    margin = LAYOUT_MARGIN
    max_cols = min(
        LAYOUT_PACK_MAX_GRID_COLS,
        max(1, (map_width - margin + col_step - 1) // col_step),
    )
    max_rows = min(
        LAYOUT_PACK_MAX_GRID_ROWS,
        max(1, (LAYOUT_MAX_HEIGHT - margin + row_step - 1) // row_step),
    )
    unplaced = sorted(
        tiles,
        key=lambda tile: (
            -_layout_tile_pack_priority(tile)[0],
            -tile["height"],
            -tile["width"],
            tile["providers"],
        ),
    )
    filler_state = {"pair": 0, "single": 0}

    for tile in unplaced:
        _cols, _rows, min_x, min_y = _layout_tile_lattice_span(
            tile["host_pos"], tile["isp_pos"], col_step, row_step,
        )
        min_row = _layout_pack_tile_min_row(tile)
        best = None
        for grid_row in range(max_rows):
            if grid_row < min_row:
                continue
            coarse_y = margin + grid_row * row_step - min_y
            for grid_col in range(max_cols):
                origin_x = margin + grid_col * col_step - min_x
                if origin_x < 0 or coarse_y < 0:
                    continue
                coarse_score = _layout_pack_candidate_score(
                    tile,
                    origin_x,
                    coarse_y,
                    grid_col,
                    host_pos,
                    isp_pos,
                    filler_state,
                    edges,
                    map_width,
                )
                if coarse_score is not None:
                    if best is None or coarse_score < best[0]:
                        best = (coarse_score, origin_x, coarse_y)
                    continue
                for dy in _layout_pack_fine_y_deltas():
                    if dy == 0:
                        continue
                    origin_y = coarse_y + dy
                    if origin_y < 0:
                        continue
                    score = _layout_pack_candidate_score(
                        tile,
                        origin_x,
                        origin_y,
                        grid_col,
                        host_pos,
                        isp_pos,
                        filler_state,
                        edges,
                        map_width,
                    )
                    if score is None:
                        continue
                    if best is None or score < best[0]:
                        best = (score, origin_x, origin_y)
        if best is None:
            raise LayoutValidationError(
                "layout: cannot pack component tile for {!r}".format(
                    sorted(tile["isp_pos"].keys()),
                )
            )
        _origin_x, _origin_y = best[1], best[2]
        _layout_merge_component_positions(
            host_pos, isp_pos, tile, _origin_x, _origin_y,
        )
        tile_class = _layout_pack_tile_class(tile)
        if tile_class == "pair":
            filler_state["pair"] += 1
        elif tile_class == "single":
            filler_state["single"] += 1
    return host_pos, isp_pos


def _element_icon_center(x, y):
    """Center of the 64x64 render icon inside the editor slot."""
    return _bounds_center(_element_render_icon_bounds(x, y))


def _label_text_metrics(text, char_width=LABEL_CHAR_WIDTH_PX, line_height=LABEL_LINE_HEIGHT_PX):
    """Conservative (width, height) for an element label string."""
    lines = (text or "").split("\n") or [""]
    width = max(len(line) for line in lines) * char_width + 2 * LABEL_PADDING_PX
    height = len(lines) * line_height + 2 * LABEL_PADDING_PX
    return width, height


def _element_label_box(x, y, label, label_location, box_id, owner_id=None):
    """Conservative visible element-label rectangle adjacent to the render icon."""
    width, height = _label_text_metrics(label)
    icon_left, icon_top, icon_right, icon_bottom = _element_render_icon_bounds(x, y)
    cx, cy = _bounds_center((icon_left, icon_top, icon_right, icon_bottom))
    loc = int(label_location)
    if loc == LABEL_LOCATION_TOP:
        bounds = (
            cx - width / 2.0,
            icon_top - height - LAYOUT_LABEL_GAP,
            cx + width / 2.0,
            icon_top - LAYOUT_LABEL_GAP,
        )
    elif loc == LABEL_LOCATION_LEFT:
        bounds = (
            icon_left - width - LAYOUT_LABEL_GAP,
            cy - height / 2.0,
            icon_left - LAYOUT_LABEL_GAP,
            cy + height / 2.0,
        )
    elif loc == LABEL_LOCATION_RIGHT:
        bounds = (
            icon_right + LAYOUT_LABEL_GAP,
            cy - height / 2.0,
            icon_right + LAYOUT_LABEL_GAP + width,
            cy + height / 2.0,
        )
    else:
        bounds = (
            cx - width / 2.0,
            icon_bottom + LAYOUT_LABEL_GAP,
            cx + width / 2.0,
            icon_bottom + LAYOUT_LABEL_GAP + height,
        )
    return {"id": box_id, "kind": "element_label", "owner_id": owner_id, "bounds": bounds}


def _element_icon_box(x, y, box_id, owner_id=None):
    return {
        "id": box_id,
        "kind": "icon",
        "owner_id": owner_id,
        "bounds": _element_render_icon_bounds(x, y),
    }


def _link_endpoint_positions(pos1, pos2, endpoint_ids):
    """Return (host_pos, isp_pos) upper-left tuples from link endpoints."""
    sid1, sid2 = endpoint_ids
    if str(sid1).startswith("host-"):
        return pos1, pos2
    return pos2, pos1


def _link_label_box(pos1, pos2, label, box_id, endpoint_ids=None):
    """
    Conservative link-label rectangle centered on the segment midpoint.
    Zabbix renders link labels on the line; there is no API position for them.
    The full LINK_LABEL_PREVIEW_WIDTH_PX x LINK_LABEL_PREVIEW_HEIGHT_PX box is never
    relocated off-line or shrunk — layout must reject overcrowded candidates instead.
    """
    endpoint_ids = tuple(endpoint_ids or ())
    x1, y1 = pos1
    x2, y2 = pos2
    cx1, cy1 = _bounds_center(_element_render_icon_bounds(x1, y1))
    cx2, cy2 = _bounds_center(_element_render_icon_bounds(x2, y2))
    mx = (cx1 + cx2) / 2.0
    my = (cy1 + cy2) / 2.0
    width = LINK_LABEL_PREVIEW_WIDTH_PX
    height = LINK_LABEL_PREVIEW_HEIGHT_PX
    return {
        "id": box_id,
        "kind": "link_label",
        "owner_id": None,
        "endpoint_ids": endpoint_ids,
        "bounds": (mx - width / 2.0, my - height / 2.0, mx + width / 2.0, my + height / 2.0),
    }


def _build_link_label(hostname, iface_name, key_in="", key_out=""):
    """Link label text (interface + In/Out macros) without calling Zabbix API."""
    label_parts = [iface_name or "—"]
    if key_in or key_out:
        if key_in:
            label_parts.append("In: {?last(/" + hostname + "/" + key_in + ")}")
        if key_out:
            label_parts.append("Out: {?last(/" + hostname + "/" + key_out + ")}")
    return "\n".join(label_parts)


def _missing_edge_selements(edges, host_to_selement, isp_to_selement):
    """Return expected edges that lack a host and/or provider selement on the map."""
    missing = []
    for hostname, hostid, _iface, isp, *_rest in edges:
        host_sid = host_to_selement.get(str(hostid))
        isp_sid = isp_to_selement.get(isp) if isp else None
        if host_sid and isp_sid:
            continue
        missing_parts = []
        if not host_sid:
            missing_parts.append("host")
        if not isp_sid:
            missing_parts.append("provider")
        missing.append({
            "hostname": hostname,
            "hostid": str(hostid),
            "isp": isp or "",
            "missing": ", ".join(missing_parts),
        })
    return missing


def _format_missing_edge_selements(missing):
    return "; ".join(
        "{hostname} (hostid {hostid}) -> {isp} (missing {missing})".format(**entry)
        for entry in missing
    )


def _assign_layout_label_locations(edges, host_pos, isp_pos):
    """Explicit label_location per hostid and provider for offline validation."""
    host_label_locations = {
        str(hostid): LAYOUT_HOST_LABEL_LOCATION
        for hostid in host_pos
    }
    isp_label_locations = {
        isp: LAYOUT_PROVIDER_LABEL_LOCATION
        for isp in isp_pos
    }
    return host_label_locations, isp_label_locations


def _build_layout_model_from_edges(edges, host_pos, isp_pos, host_label_locations=None, isp_label_locations=None):
    """Offline selement/link model for layout validation (no Zabbix API)."""
    if host_label_locations is None or isp_label_locations is None:
        host_label_locations, isp_label_locations = _assign_layout_label_locations(
            edges, host_pos, isp_pos,
        )

    host_names = {}
    for hostname, hostid, _iface, _isp, _in, _out, _ki, _ko, _desc in edges:
        host_names.setdefault(str(hostid), hostname)

    selements = []
    for hostid, (x, y) in sorted(host_pos.items(), key=lambda item: (item[1][1], item[1][0])):
        selements.append({
            "elementtype": ELEMENT_TYPE_HOST,
            "hostid": hostid,
            "x": x,
            "y": y,
            "label": host_names.get(hostid, hostid),
            "label_location": host_label_locations.get(hostid, LAYOUT_HOST_LABEL_LOCATION),
        })
    for isp, (x, y) in sorted(isp_pos.items(), key=lambda item: (item[1][1], item[1][0])):
        selements.append({
            "elementtype": ELEMENT_TYPE_IMAGE,
            "label": isp,
            "x": x,
            "y": y,
            "label_location": isp_label_locations.get(isp, LAYOUT_PROVIDER_LABEL_LOCATION),
        })

    hostid_to_sid = {str(el["hostid"]): "host-{}".format(el["hostid"]) for el in selements if el.get("hostid")}
    isp_to_sid = {
        el["label"]: "isp-{}".format(el["label"])
        for el in selements
        if int(el.get("elementtype", 0)) == ELEMENT_TYPE_IMAGE
    }

    links = []
    for idx, (hostname, hostid, iface_name, isp, _in, _out, key_in, key_out, _desc) in enumerate(edges):
        if not isp:
            continue
        sid1 = hostid_to_sid.get(str(hostid))
        sid2 = isp_to_sid.get(isp)
        if not sid1 or not sid2:
            continue
        links.append({
            "id": "link-{}".format(idx),
            "selementid1": sid1,
            "selementid2": sid2,
            "label": _build_link_label(hostname, iface_name, key_in, key_out),
        })
    return selements, links, host_label_locations, isp_label_locations


def _layout_positions_from_model(selements):
    """Map selement id -> (x, y) upper-left icon coordinates."""
    positions = {}
    for el in selements:
        etype = int(el.get("elementtype", 0))
        if etype == ELEMENT_TYPE_HOST:
            box_id = "host-{}".format(el.get("hostid"))
        else:
            box_id = "isp-{}".format(el.get("label"))
        positions[box_id] = (el["x"], el["y"])
    return positions


def _layout_visible_boxes(selements, links, positions_by_id):
    """Build icon, element-label, and link-label boxes for collision checks."""
    boxes = []
    for el in selements:
        etype = int(el.get("elementtype", 0))
        if etype == ELEMENT_TYPE_HOST:
            owner = str(el.get("hostid", ""))
            box_id = "host-{}".format(owner)
        else:
            owner = el.get("label", "")
            box_id = "isp-{}".format(owner)
        pos = positions_by_id.get(box_id)
        if pos is None:
            continue
        x, y = pos
        boxes.append(_element_icon_box(x, y, "{}-icon".format(box_id), owner_id=owner))
        label = el.get("label", "")
        if label:
            boxes.append(_element_label_box(
                x,
                y,
                label,
                el.get("label_location", LAYOUT_HOST_LABEL_LOCATION),
                "{}-label".format(box_id),
                owner_id=owner,
            ))

    for link in links:
        sid1 = str(link.get("selementid1", ""))
        sid2 = str(link.get("selementid2", ""))
        pos1 = positions_by_id.get(sid1)
        pos2 = positions_by_id.get(sid2)
        if pos1 is None or pos2 is None:
            continue
        label = link.get("label", "")
        if not label:
            continue
        boxes.append(_link_label_box(
            pos1, pos2, label, link.get("id", "link"), endpoint_ids=(sid1, sid2),
        ))
    return boxes


def _link_label_endpoint_owner(link_box, other_box):
    """True when other_box is an icon/label owned by a link_label endpoint."""
    if link_box.get("kind") != "link_label":
        return False
    owner = other_box.get("owner_id")
    if not owner:
        return False
    for sid in link_box.get("endpoint_ids") or ():
        if sid in (f"host-{owner}", f"isp-{owner}"):
            return True
    return False


def _layout_collision_gap(box_a, box_b, gap=LAYOUT_LABEL_GAP):
    """
    Outward expansion applied to each box before overlap test.

    Icon-icon pairs use LAYOUT_ELEMENT_GAP/2 per side (8px total clearance).
    Link-label vs its endpoint icon/label uses 0 (intentional adjacency).
    All other pairs use gap (default LAYOUT_LABEL_GAP=8 per side).
    """
    if box_a.get("kind") == "icon" and box_b.get("kind") == "icon":
        return LAYOUT_ELEMENT_GAP / 2.0
    if _link_label_endpoint_owner(box_a, box_b) or _link_label_endpoint_owner(box_b, box_a):
        return 0
    return gap


def _layout_skip_link_label_endpoint_paint(box_a, box_b):
    """
    Allow link labels to overlap their endpoint render icon only.
    Zabbix paints icons above link labels in view mode; endpoint text labels
    remain blocking unless future paint-order evidence says otherwise.
    """
    if not (
        _link_label_endpoint_owner(box_a, box_b)
        or _link_label_endpoint_owner(box_b, box_a)
    ):
        return False
    other = box_b if box_a.get("kind") == "link_label" else box_a
    return other.get("kind") == "icon"


def _layout_skip_render_paint_overlap(box_a, box_b):
    """
    View-mode paint-order overlaps excluded from fail-closed layout validation.
    Only link_label vs its own endpoint render icon; foreign element_label and
    link_label pairs stay blocking.
    """
    return _layout_skip_link_label_endpoint_paint(box_a, box_b)


def _find_layout_collisions(
    boxes,
    gap=LAYOUT_LABEL_GAP,
    skip_same_owner=True,
    skip_render_paint=False,
):
    """
    Return collision pairs among visible boxes (conservative axis-aligned overlap).

    Each box is expanded outward by pair_gap from _layout_collision_gap before testing.
    gap=0 checks raw bounds; gap=LAYOUT_LABEL_GAP enforces 8px label clearance per side.
    skip_render_paint=True omits link_label vs endpoint render-icon paint overlaps.
    """
    collisions = []
    for i, box_a in enumerate(boxes):
        for box_b in boxes[i + 1 :]:
            if skip_same_owner and box_a.get("owner_id") and box_a["owner_id"] == box_b.get("owner_id"):
                continue
            if skip_render_paint and _layout_skip_render_paint_overlap(box_a, box_b):
                continue
            pair_gap = _layout_collision_gap(box_a, box_b, gap=gap)
            ax1, ay1, ax2, ay2 = box_a["bounds"]
            ax1 -= pair_gap
            ay1 -= pair_gap
            ax2 += pair_gap
            ay2 += pair_gap
            bx1, by1, bx2, by2 = box_b["bounds"]
            bx1 -= pair_gap
            by1 -= pair_gap
            bx2 += pair_gap
            by2 += pair_gap
            if ax1 < bx2 and bx1 < ax2 and ay1 < by2 and by1 < ay2:
                collisions.append({
                    "box_a": box_a["id"],
                    "box_b": box_b["id"],
                    "kind_a": box_a["kind"],
                    "kind_b": box_b["kind"],
                })
    return collisions


def _validate_layout_model(selements, links, gap=0):
    positions = _layout_positions_from_model(selements)
    boxes = _layout_visible_boxes(selements, links, positions)
    collisions = _find_layout_collisions(boxes, gap=gap, skip_render_paint=True)
    # Zabbix draws link labels along the line; their conservative preview box
    # is diagnostic only. Icons, element labels, and line geometry remain
    # blocking checks below.
    return [
        collision
        for collision in collisions
        if "link_label" not in {collision["kind_a"], collision["kind_b"]}
    ]


def _layout_segment_point_on_rect(px, py, rect, tolerance=LAYOUT_LABEL_GAP):
    left, top, right, bottom = rect
    return (
        left - tolerance <= px <= right + tolerance
        and top - tolerance <= py <= bottom + tolerance
    )


def _layout_segment_intersects_rect(p1, p2, rect, tolerance=LAYOUT_LABEL_GAP):
    """True when open segment p1->p2 intersects rect (endpoints excluded by caller)."""
    x1, y1 = p1
    x2, y2 = p2
    left, top, right, bottom = rect
    left -= tolerance
    top -= tolerance
    right += tolerance
    bottom += tolerance
    if _layout_segment_point_on_rect(x1, y1, (left, top, right, bottom), tolerance=0):
        return True
    if _layout_segment_point_on_rect(x2, y2, (left, top, right, bottom), tolerance=0):
        return True
    dx = x2 - x1
    dy = y2 - y1
    steps = max(abs(dx), abs(dy), 1.0)
    count = int(math.ceil(steps)) + 1
    for idx in range(1, count):
        t = idx / float(count)
        px = x1 + dx * t
        py = y1 + dy * t
        if left <= px <= right and top <= py <= bottom:
            return True
    return False


def _layout_icon_rects_from_positions(host_pos, isp_pos):
    """Map element id -> render-icon rectangle for segment validation."""
    rects = {}
    for hostid, (x, y) in host_pos.items():
        rects["host-{}".format(hostid)] = _element_render_icon_bounds(x, y)
    for isp, (x, y) in isp_pos.items():
        rects["isp-{}".format(isp)] = _element_render_icon_bounds(x, y)
    return rects


def _validate_layout_segment_collisions(host_pos, isp_pos, links):
    """Reject center-to-center link segments that pass through foreign icon rectangles."""
    icon_rects = _layout_icon_rects_from_positions(host_pos, isp_pos)
    icon_centers = {
        sid: _bounds_center(rect)
        for sid, rect in icon_rects.items()
    }
    collisions = []
    for link in links:
        sid1 = str(link.get("selementid1", ""))
        sid2 = str(link.get("selementid2", ""))
        center1 = icon_centers.get(sid1)
        center2 = icon_centers.get(sid2)
        if center1 is None or center2 is None:
            continue
        for sid, rect in icon_rects.items():
            if sid in (sid1, sid2):
                continue
            if _layout_segment_intersects_rect(center1, center2, rect):
                collisions.append({
                    "box_a": link.get("id", "link"),
                    "box_b": "{}-icon".format(sid),
                    "kind_a": "link_segment",
                    "kind_b": "icon",
                })
    return collisions


def _edges_with_placed_endpoints(edges, host_pos, isp_pos):
    """Edges whose host and provider both have layout positions."""
    return [
        edge for edge in edges
        if str(edge[1]) in host_pos and edge[3] in isp_pos
    ]


def _validate_edge_positions(edges, host_pos, isp_pos):
    """Fail closed when any edge endpoint lacks a layout position."""
    missing = []
    for hostname, hostid, _iface, isp, *_rest in edges:
        if str(hostid) not in host_pos:
            missing.append("host {} ({})".format(hostid, hostname))
        if isp and isp not in isp_pos:
            missing.append("provider {} (host {})".format(isp, hostname))
    if missing:
        return [{
            "box_a": "edge",
            "box_b": missing[0],
            "kind_a": "missing_position",
            "kind_b": "endpoint",
        }]
    return []


def _validate_edges_layout(edges, host_pos, isp_pos, require_all=False):
    if require_all:
        missing = _validate_edge_positions(edges, host_pos, isp_pos)
        if missing:
            return missing
    placed_edges = _edges_with_placed_endpoints(edges, host_pos, isp_pos)
    if not placed_edges:
        return []
    selements, links, _, _ = _build_layout_model_from_edges(placed_edges, host_pos, isp_pos)
    collisions = _validate_layout_model(selements, links)
    if collisions:
        return collisions
    return _validate_layout_segment_collisions(host_pos, isp_pos, links)


def _layout_required_dimensions(host_pos, isp_pos, edges=None):
    """
    Map size from visible/editor occupied bounds plus a small dynamic edge padding.
    Maximum-size enforcement happens after this calculation so oversized
    content fails closed.
    """
    max_right = LAYOUT_MARGIN
    max_bottom = LAYOUT_MARGIN
    if edges is not None and host_pos and isp_pos:
        selements, links, _, _ = _build_layout_model_from_edges(edges, host_pos, isp_pos)
        positions = _layout_positions_from_model(selements)
        if positions:
            _min_x, _min_y, visible_right, visible_bottom = _layout_content_aabb(
                selements, links, positions,
            )
            max_right = max(max_right, visible_right)
            max_bottom = max(max_bottom, visible_bottom)
    for x, y in list(host_pos.values()) + list(isp_pos.values()):
        left, top, right, bottom = _element_bounds(x, y)
        max_right = max(max_right, right)
        max_bottom = max(max_bottom, bottom)
    return (
        _layout_int(max_right + LAYOUT_CANVAS_PADDING),
        _layout_int(max_bottom + LAYOUT_CANVAS_PADDING),
    )


def _selement_hostid(el):
    """From a map element of type "host", extract hostid (string). The API can return elementid or elements[0].hostid."""
    eid = el.get("elementid")
    if eid is None or eid == "":
        elems = el.get("elements") or []
        if elems and isinstance(elems[0], dict):
            eid = elems[0].get("hostid")
    if eid is not None and str(eid) != "":
        return str(eid)
    return None


def _element_bounds(x, y):
    """Upper-left (x, y) editor slot rectangle: (left, top, right, bottom)."""
    return (x, y, x + SELEMENT_WIDTH, y + SELEMENT_HEIGHT)


def _element_render_icon_bounds(x, y):
    """64x64 render-icon rectangle centered inside the editor slot."""
    inset = RENDER_ICON_INSET
    return (
        x + inset,
        y + inset,
        x + inset + RENDER_ICON_SIZE,
        y + inset + RENDER_ICON_SIZE,
    )


def _bounds_center(rect):
    left, top, right, bottom = rect
    return ((left + right) / 2.0, (top + bottom) / 2.0)


def _inflated_bounds(x, y, gap):
    """Editor slot rectangle expanded by gap on all sides."""
    return (x - gap, y - gap, x + SELEMENT_WIDTH + gap, y + SELEMENT_HEIGHT + gap)


def _render_icon_inflated_bounds(x, y, gap):
    """Render-icon rectangle expanded by gap on all sides."""
    left, top, right, bottom = _element_render_icon_bounds(x, y)
    return (left - gap, top - gap, right + gap, bottom + gap)


def _layout_editor_slot_boxes(host_pos, isp_pos):
    """Editor-only 200x200 slot boxes for diagnostics (not fail-closed)."""
    boxes = []
    for hostid, (x, y) in host_pos.items():
        boxes.append({
            "id": "host-{}-editor".format(hostid),
            "kind": "editor_slot",
            "owner_id": str(hostid),
            "bounds": _element_bounds(x, y),
        })
    for isp, (x, y) in isp_pos.items():
        boxes.append({
            "id": "isp-{}-editor".format(isp),
            "kind": "editor_slot",
            "owner_id": isp,
            "bounds": _element_bounds(x, y),
        })
    return boxes


def _find_layout_editor_collisions(host_pos, isp_pos, gap=0):
    """Diagnostic editor-slot overlaps; never used for fail-closed layout updates."""
    return _find_layout_collisions(
        _layout_editor_slot_boxes(host_pos, isp_pos),
        gap=gap,
        skip_same_owner=False,
    )


def _bounds_overlap(a, b):
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    return ax1 < bx2 and bx1 < ax2 and ay1 < by2 and by1 < ay2


def _layout_validation_failure_detail(last_error, last_collisions):
    if last_error is not None:
        return str(last_error)
    return "layout model collisions remain ({}): {}".format(
        len(last_collisions),
        ", ".join("{}↔{}".format(c["box_a"], c["box_b"]) for c in last_collisions[:5]),
    )


def _compute_layout(edges, map_width, map_height):
    """
    Deterministic component/tile layout: providers are central nodes inside each
    connected component, shared routers sit between linked providers, then tiles
    are packed into a dense grid with offline visible-box validation.
    """
    components = _layout_connected_components(edges)
    tiles = []
    for component in components:
        local_host, local_isp = _layout_component_tile(component)
        norm_host, norm_isp, tile_w, tile_h = _layout_normalize_tile(
            local_host, local_isp, component["edges"],
        )
        tiles.append({
            "host_pos": norm_host,
            "isp_pos": norm_isp,
            "width": tile_w,
            "height": tile_h,
            "edges": component["edges"],
            "providers": component["providers"],
        })
    tiles.sort(
        key=lambda tile: (-tile["height"], -tile["width"], tile["providers"]),
    )

    host_pos, isp_pos = _layout_pack_component_tiles(tiles, map_width, edges)
    required_width, required_height = _layout_required_dimensions(host_pos, isp_pos, edges=edges)
    return host_pos, isp_pos, required_width, required_height


def _compute_layout_validated(edges, map_width=MAP_WIDTH, map_height=MAP_HEIGHT):
    """
    Compute layout and validate with the offline visible-box model.
    The component packer is deterministic, so one validated pass is sufficient.
    Re-running the complete packer for every width only repeats the same expensive
    topology search and can make an update appear hung.
    """
    try:
        host_pos, isp_pos, required_width, required_height = _compute_layout(
            edges, map_width, map_height,
        )
    except LayoutValidationError:
        raise
    collisions = _validate_edges_layout(edges, host_pos, isp_pos, require_all=True)
    if collisions:
        raise LayoutValidationError(
            "layout validation failed ({}): {}".format(
                len(collisions),
                _layout_validation_failure_detail(None, collisions),
            )
        )
    final_width = required_width
    final_height = required_height
    if final_width > LAYOUT_MAX_WIDTH or final_height > LAYOUT_MAX_HEIGHT:
        raise LayoutValidationError(
            "layout exceeds maximum size: {}x{} (max {}x{})".format(
                final_width,
                final_height,
                LAYOUT_MAX_WIDTH,
                LAYOUT_MAX_HEIGHT,
            )
        )
    host_pos, isp_pos = _layout_normalize_positions(host_pos, isp_pos)
    return host_pos, isp_pos, _layout_int(final_width), _layout_int(final_height)


def ensure_map_exists(url, token, debug=False, width=None, height=None):
    """Create a [test] uplinks map if it doesn't already exist. Return (sysmapid or None, err).
    width/height — when creating a map; if not specified - MAP_WIDTH/MAP_HEIGHT."""
    existing, err = zabbix_request(url, token, "map.get", {
        "filter": {"name": MAP_NAME},
        "output": ["sysmapid"],
    }, debug=debug)
    if err:
        return None, err
    if existing:
        return existing[0]["sysmapid"], None
    w = _layout_int(width if width is not None else MAP_WIDTH)
    h = _layout_int(height if height is not None else MAP_HEIGHT)
    # In Zabbix 7, with permission checking enabled, the map can require userGroups (see _get_map_user_groups)
    result, err = zabbix_request(url, token, "map.create", {
        "name": MAP_NAME,
        "width": w,
        "height": h,
        "label_type": 0,
        "label_type_image": 0,
    }, debug=debug)
    if err:
        return None, err
    return result["sysmapids"][0], None


def update_uplinks_map(
    url,
    token,
    devices,
    host_id_by_name,
    items_by_host_iface,
    desc_to_name,
    debug=False,
    prune_obsolete=True,
    device_iface_to_provider=None,
    inventory_scoped=True,
):
    """
    Update the map: hosts, providers (image), links.

    When prune_obsolete=True (default for full update): elements are removed from the map
    which are not in the current data (hosts and provider clouds not from dry-ssh, other types of elements).
    With --host, prune_obsolete=False is passed to the CLI - we do not touch the other hosts on the map.
    Disable cleanup for a full run: --keep-obsolete-map-elements.
    """
    # Edges for links. If there are logical interfaces (ae5, ae5.0, et-0/0/3) per uplink
    # leave one edge on (host, ISP): priority - interface with Zabbix items, then logical (ae5.0).
    # edges_raw: + has_items, is_logical, is_aggregate, description. The final edge is 9 fields (without these three flags).
    edges_raw = []
    for hostname in sorted(devices.keys()):
        hostid = host_id_by_name.get(hostname)
        if not hostid:
            continue
        for iface in devices[hostname]:
            if not is_uplink_iface(
                iface,
                hostname=hostname,
                device_iface_to_provider=device_iface_to_provider,
                inventory_scoped=inventory_scoped,
            ):
                continue
            iface_name = iface.get("name", "")
            description = iface.get("description", "")
            isp = resolve_provider_name_for_iface(
                hostname,
                iface,
                desc_to_name,
                device_iface_to_provider=device_iface_to_provider,
            )
            key_norm = _normalize_interface_name(iface_name)
            rec = items_by_host_iface.get((hostname, key_norm), {})
            itemid_in = rec.get("itemid_in") or ""
            itemid_out = rec.get("itemid_out") or ""
            key_in = rec.get("bits_in") or ""
            key_out = rec.get("bits_out") or ""
            has_items = bool(itemid_in or itemid_out)
            is_logical = bool(iface.get("isLogical"))
            is_aggregate = bool(iface.get("isLag"))
            edges_raw.append((hostname, str(hostid), iface_name, isp, itemid_in, itemid_out, key_in, key_out,
                              has_items, is_logical, is_aggregate, description))

    # One edge on (hostname, hostid, isp): priority - has_items, then is_logical, then not aggregate
    def _edge_priority(e):
        _, _, _, _, _, _, _, _, has_items, is_logical, is_aggregate, _ = e
        return (has_items, is_logical, not is_aggregate)

    seen_key = {}
    for e in edges_raw:
        key = (e[0], e[1], e[3])  # hostname, hostid, isp
        if key not in seen_key or _edge_priority(e) > _edge_priority(seen_key[key]):
            seen_key[key] = e
    # edge: (hostname, hostid, iface_name, isp, itemid_in, itemid_out, key_in, key_out, description)
    edges = [(e[0], e[1], e[2], e[3], e[4], e[5], e[6], e[7], e[11]) for e in sorted(seen_key.values(), key=lambda x: (x[0], x[3], x[2]))]

    unique_hosts = []  # (hostname, hostid)
    seen_hosts = set()
    for hostname, hostid, _if, _isp, _in, _out, _ki, _ko, _desc in edges:
        if (hostname, hostid) not in seen_hosts:
            seen_hosts.add((hostname, hostid))
            unique_hosts.append((hostname, hostid))

    if not unique_hosts:
        return "no data for card (no host with uplink)", None

    # Base URL of the web interface (without api_jsonrpc.php) for links to charts
    base_url = url.replace("/api_jsonrpc.php", "").rstrip("/")
    if not base_url.endswith("/"):
        base_url += "/"
    # For each host - links to the “Bits received” graph by interface; signature: provider name and Bits received
    host_to_urls = {}
    for hostname, hostid, iface_name, isp, itemid_in, itemid_out, key_in, key_out, description in edges:
        if not itemid_in:
            continue
        link_name = "{} Bits received".format(isp or "Uplink").strip()
        link_url = "{}history.php?action=showgraph&itemids[]={}&from=now-1d&to=now".format(base_url, itemid_in)
        host_to_urls.setdefault(str(hostid), []).append({"name": link_name, "url": link_url})

    unique_isps = []
    seen_isp = set()
    for _hn, _hid, _if, isp, _in, _out, _ki, _ko, _desc in edges:
        if isp and isp not in seen_isp:
            seen_isp.add(isp)
            unique_isps.append(isp)

    # Positions by providers: from left to right, provider with max. connections - first
    try:
        host_pos, isp_pos, required_width, required_height = _compute_layout_validated(
            edges, MAP_WIDTH, MAP_HEIGHT,
        )
    except LayoutValidationError as exc:
        return "layout validation failed: {}".format(exc), None
    map_width = _layout_int(required_width)
    map_height = _layout_int(required_height)
    layout_collisions = _validate_edges_layout(edges, host_pos, isp_pos, require_all=True)
    if layout_collisions:
        return "layout validation failed: {} collision(s) before map update".format(
            len(layout_collisions),
        ), None
    host_label_locations, isp_label_locations = _assign_layout_label_locations(
        edges, host_pos, isp_pos,
    )

    # Get a map (create an empty one if not)
    existing, err = zabbix_request(url, token, "map.get", {
        "filter": {"name": MAP_NAME},
        "output": ["sysmapid"],
        "selectSelements": "extend",
        "selectLinks": "extend",
    }, debug=debug)
    if err:
        return "map.get: {}".format(err), None
    if not existing:
        sysmapid, err = ensure_map_exists(url, token, debug=debug, width=map_width, height=map_height)
        if err:
            return "map.create: {}".format(err), None
        existing = [{"sysmapid": sysmapid, "selements": [], "links": []}]

    sysmapid = existing[0]["sysmapid"]
    old_selements_raw = existing[0].get("selements", [])

    wanted_host_ids = {str(hid) for _, hid in unique_hosts}
    wanted_isp_labels = set(unique_isps)

    # One element for hostid and one for provider (label), so as not to be duplicated during repeated updates.
    # selementid_to_canonical: to replace deleted duplicates in links with the left selementid
    old_selements = []
    old_by_eid = {}
    old_by_image_label = {}
    selementid_to_canonical = {} # removed selementid -> canonical (kept)
    pruned_selements = 0
    for el in old_selements_raw:
        etype = int(el.get("elementtype", 0))
        sid = el.get("selementid")
        if etype == ELEMENT_TYPE_IMAGE:
            label = el.get("label", "")
            if prune_obsolete and label not in wanted_isp_labels:
                pruned_selements += 1
                continue
            key_img = (ELEMENT_TYPE_IMAGE, label)
            if key_img in old_by_image_label:
                selementid_to_canonical[str(sid)] = str(old_by_image_label[key_img])
                continue
            old_by_image_label[key_img] = sid
        else:
            if prune_obsolete and etype != ELEMENT_TYPE_HOST:
                pruned_selements += 1
                continue
            eid = _selement_hostid(el)
            if prune_obsolete and etype == ELEMENT_TYPE_HOST:
                if not eid or eid not in wanted_host_ids:
                    pruned_selements += 1
                    continue
            if eid is not None:
                if eid in old_by_eid:
                    selementid_to_canonical[str(sid)] = str(old_by_eid[eid])
                    continue
                old_by_eid[eid] = sid
        old_selements.append(el)
    if prune_obsolete and pruned_selements:
        print("Map: deleted obsolete elements (not in current data): {}".format(pruned_selements), file=sys.stderr)

    # Add only those elements that are not yet on the map; we take positions from layout
    new_selements = []
    for hostname, hostid in unique_hosts:
        if str(hostid) in old_by_eid:
            continue
        try:
            eid = int(hostid)
        except (TypeError, ValueError):
            eid = hostid
        pos = host_pos.get(str(hostid))
        if pos is None:
            return "layout validation failed: missing position for host {!r} (hostid {})".format(
                hostname, hostid,
            ), None
        x, y = pos
        new_selements.append({
            "elementtype": ELEMENT_TYPE_HOST,
            "elementid": eid,
            "hostid": eid,
            "elements": [{"hostid": str(eid)}],
            "x": x,
            "y": y,
            "label": hostname,
            "label_location": host_label_locations.get(str(hostid), LAYOUT_HOST_LABEL_LOCATION),
            "iconid_off": MAP_ICON_HOST,
            "urls": host_to_urls.get(str(hostid), []),
        })
    for isp in unique_isps:
        if (ELEMENT_TYPE_IMAGE, isp) in old_by_image_label:
            continue
        pos = isp_pos.get(isp)
        if pos is None:
            return "layout validation failed: missing position for provider {!r}".format(
                isp,
            ), None
        x, y = pos
        new_selements.append({
            "elementtype": ELEMENT_TYPE_IMAGE,
            "elementid": 0,
            "elements": [],
            "label": isp,
            "label_location": isp_label_locations.get(isp, LAYOUT_PROVIDER_LABEL_LOCATION),
            "x": x,
            "y": y,
            "iconid_off": MAP_ICON_CLOUD,
        })

    selements_merged = list(old_selements) + new_selements

    # Apply layout and graph links to all elements (old and new)
    for el in selements_merged:
        etype = int(el.get("elementtype", 0))
        if etype == ELEMENT_TYPE_HOST:
            eid = _selement_hostid(el)
            if eid is not None:
                pos = host_pos.get(eid)
                if pos is not None:
                    el["x"], el["y"] = pos
                el["label_location"] = host_label_locations.get(
                    eid, LAYOUT_HOST_LABEL_LOCATION,
                )
                el["urls"] = host_to_urls.get(eid, [])
        elif etype == ELEMENT_TYPE_IMAGE:
            label = el.get("label", "")
            if label in isp_pos:
                el["x"], el["y"] = isp_pos[label]
                el["label_location"] = isp_label_locations.get(
                    label, LAYOUT_PROVIDER_LABEL_LOCATION,
                )

    # Removed/merged selements are still listed in the old map links → Zabbix with map.update(selements):
    # "Link selementid1 points to a nonexistent map selement." First we remove all links.
    need_clear_links = pruned_selements > 0 or len(old_selements) < len(old_selements_raw)
    map_sid = _api_map_id(sysmapid)
    if need_clear_links:
        n_clear = len(existing[0].get("links") or [])
        print(
            "Warning: clearing {} map link(s) after element merge/collapse".format(n_clear),
            file=sys.stderr,
        )
        _, err_clear = zabbix_request(
            url, token, "map.update", {"sysmapid": map_sid, "links": []}, debug=debug
        )
        if err_clear:
            return "map.update (clear links before selements): {}".format(err_clear), sysmapid

    result, err = zabbix_request(url, token, "map.update", {
        "sysmapid": map_sid,
        "width": map_width,
        "height": map_height,
        "label_type": 0,
        "label_type_image": 0,
        "selements": selements_merged,
    }, debug=debug)
    if err:
        return "map.update (selements): {}".format(err), sysmapid

    # Get selementid for building links
    result, err = zabbix_request(url, token, "map.get", {
        "sysmapids": [sysmapid],
        "output": ["sysmapid"],
        "selectSelements": "extend",
        "selectLinks": "extend",
    }, debug=debug)
    if err or not result:
        return "map.get: {}".format(err or "map not found"), sysmapid
    elem_list = result[0].get("selements", [])
    links_existing = result[0].get("links", [])
    host_to_selement = {}
    isp_to_selement = {}
    for el in elem_list:
        sid = str(el.get("selementid", ""))
        if not sid:
            continue
        etype = int(el.get("elementtype", 0))
        if etype == ELEMENT_TYPE_HOST:
            eid = _selement_hostid(el)
            if eid is not None:
                host_to_selement[eid] = sid
        elif etype == ELEMENT_TYPE_IMAGE:
            isp_to_selement[el.get("label", "")] = sid

    # Triggers 90%/100% for coloring links: now we use aggregate provider triggers
    # from "Uplinks {Provider}" hosts (created by zabbix_provider_aggregate.py).
    providers_for_triggers = [isp for isp in unique_isps if isp]
    trigger_ids_by_provider = get_provider_aggregate_triggers(
        url, token, providers_for_triggers, debug=debug
    )
    hostids_for_links = sorted(set(str(e[1]) for e in edges if e[1] is not None))
    trigger_ids_by_link = get_link_commit_triggers(url, token, hostids_for_links, debug=debug)

    missing_selements = _missing_edge_selements(edges, host_to_selement, isp_to_selement)
    if missing_selements:
        return "map links: missing selements for {} edge(s): {}".format(
            len(missing_selements),
            _format_missing_edge_selements(missing_selements),
        ), sysmapid

    new_links = []
    our_link_pairs = set()
    for hostname, hostid, iface_name, isp, itemid_in, itemid_out, key_in, key_out, _desc in edges:
        sid1 = host_to_selement[str(hostid)]
        sid2 = isp_to_selement[isp]
        our_link_pairs.add(frozenset((str(sid1), str(sid2))))
        # New link: do not pass linkid (read-only in the API; linkid:0 gives Wrong fields for map link).
        link = {
            "selementid1": _api_map_id(sid1),
            "selementid2": _api_map_id(sid2),
            "label": _build_link_label(hostname, iface_name, key_in, key_out),
        }
        # Bind triggers to a link with priority:
        # 1) per-link commit (100/90), 2) provider aggregate (100/90).
        trigger_warn, trigger_high = trigger_ids_by_provider.get(isp or "", (None, None))
        link_warn, link_high = trigger_ids_by_link.get(
            (str(hostid), _normalize_interface_name(iface_name)),
            (None, None),
        )
        linktriggers = []

        def _append_trigger(tid, color, bold=False):
            if not tid:
                return
            if any(str(x.get("triggerid")) == str(tid) for x in linktriggers):
                return
            entry = {"triggerid": _api_map_id(tid), "color": color}
            if bold:
                entry["drawtype"] = LINK_DRAWTYPE_BOLD
            linktriggers.append(entry)

        _append_trigger(link_high, LINK_COLOR_HIGH, bold=True)
        _append_trigger(trigger_high, LINK_COLOR_HIGH, bold=False)
        _append_trigger(link_warn, LINK_COLOR_WARN, bold=False)
        _append_trigger(trigger_warn, LINK_COLOR_WARN, bold=False)
        if linktriggers:
            link["linktriggers"] = linktriggers
        new_links.append(link)

    def _link_entry_from_existing(l):
        s1 = str(l.get("selementid1", ""))
        s2 = str(l.get("selementid2", ""))
        s1 = selementid_to_canonical.get(s1, s1)
        s2 = selementid_to_canonical.get(s2, s2)
        label = str(l.get("label") or "")
        if l.get("linkid"):
            entry = {
                "linkid": int(l["linkid"]),
                "selementid1": _api_map_id(s1),
                "selementid2": _api_map_id(s2),
                "label": label,
            }
            lt_list = l.get("linktriggers") or []
            if lt_list:
                entry["linktriggers"] = [
                    {"triggerid": _api_map_id(lt.get("triggerid")), "color": lt.get("color", LINK_COLOR_HIGH)}
                    for lt in lt_list
                    if lt.get("triggerid")
                ]
        else:
            entry = {
                "selementid1": _api_map_id(s1),
                "selementid2": _api_map_id(s2),
                "label": label,
            }
        return entry

    # Existing links: keep unrelated links, but replace every current edge
    # regardless of which endpoint Zabbix returned as selementid1.
    links_merged = []
    for l in links_existing:
        s1 = str(l.get("selementid1", ""))
        s2 = str(l.get("selementid2", ""))
        if frozenset((
            selementid_to_canonical.get(s1, s1),
            selementid_to_canonical.get(s2, s2),
        )) in our_link_pairs:
            continue
        links_merged.append(_link_entry_from_existing(l))
    links_merged.extend(new_links)
    # Ensure that each link has a label key (string) so that there are no gaps in the JSON.
    for link in links_merged:
        if "label" not in link:
            link["label"] = ""
        link["label"] = str(link.get("label") or "")

    # Zabbix 7: each object in links must have sysmapid, otherwise map.update → Wrong fields for map link.
    map_sysmapid = _api_map_id(sysmapid)
    for link in links_merged:
        link["sysmapid"] = map_sysmapid

    if debug or new_links:
        print("Links: existing {}, new {}, total {}".format(
            len(links_existing), len(new_links), len(links_merged)), file=sys.stderr)
    if not new_links and edges:
        want_hosts = sorted(set(e[1] for e in edges))
        want_isps = sorted(set(e[3] for e in edges if e[3]))
        print("Links have not been created. We are looking for hostid: {!r}, isp: {!r}. On the map hostid: {!r}, isp: {!r}".format(
            want_hosts[:15], want_isps[:15], sorted(host_to_selement.keys())[:15], sorted(isp_to_selement.keys())[:15]), file=sys.stderr)

    # Update map links
    result, err = zabbix_request(url, token, "map.update", {
        "sysmapid": map_sysmapid,
        "links": links_merged,
    }, debug=debug)
    if err:
        return "map.update (links): {}".format(err), sysmapid

    return None, sysmapid


def main():
    parser = argparse.ArgumentParser(
        description="Data for Zabbix map. Default: hostname, interface, description, ISP."
    )
    parser.add_argument(
        "-f", "--file",
        default=None,
        metavar="FILE",
        help="Legacy JSON with devices (dry-ssh.json)",
    )
    parser.add_argument(
        "--inventory-file",
        default=None,
        metavar="FILE",
        help="Scoped inventory JSON from netbox_uplinks_inventory.py --json",
    )
    parser.add_argument(
        "-m", "--description-map",
        default=DESCRIPTION_MAP_FILE,
        metavar="FILE",
        help="Map file description -> name (default {})".format(DESCRIPTION_MAP_FILE),
    )
    parser.add_argument(
        "--zabbix",
        action="store_true",
        help="Query Zabbix API: find hosts and items Bits received/sent (for map or table)",
    )
    parser.add_argument(
        "--print-table",
        action="store_true",
        help="Output the table to the console (hostname, interface, description, ISP; with --zabbix - hostid and items keys)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Output debugging information when working with Zabbix API",
    )
    parser.add_argument(
        "--create-map",
        action="store_true",
        help="Only create a [test] uplinks map if it doesn't exist yet (empty)",
    )
    parser.add_argument(
        "--update-map",
        action="store_true",
        help="Update map: hosts, providers, links; with --host - only the specified host and its links",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Do not use Zabbix cache, request data again (default cache in {})".format(ZABBIX_CACHE_FILE),
    )
    parser.add_argument(
        "--host",
        metavar="HOSTNAME",
        help="Work only with the specified host (name from devices)",
    )
    parser.add_argument(
        "--keep-obsolete-map-elements",
        action="store_true",
        help="When --update-map, do not remove hosts/providers from the map that are not in the current JSON (old behavior)",
    )
    parser.add_argument(
        "--legacy-dry-ssh",
        action="store_true",
        help="Use legacy dry-ssh.json input (-f/--file) instead of --inventory-file",
    )
    parser.add_argument(
        "--legacy-provider-filter",
        action="store_true",
        help="Use description-based uplink filter instead of NetBox circuit scope (legacy)",
    )
    parser.add_argument(
        "--export-map",
        metavar="SYSMAPID",
        help="Output JSON maps from the API (sysmapid) for comparison with a manual map; ZABBIX_URL and ZABBIX_TOKEN are needed",
    )
    parser.add_argument(
        "--generate-description-map",
        action="store_true",
        help="Collect all descriptions from the devices file and output the JSON template (description -> description). "
             "Save to description_to_name.json and edit: reduce options to one name (eg Beeline 5, Uplink: Beeline 5 -> Beeline)",
    )
    args = parser.parse_args()

    # Legacy utility: build description_to_name template from explicit dry-ssh.json
    if args.generate_description_map:
        if not args.legacy_dry_ssh or not args.file:
            print(GENERATE_DESCRIPTION_MAP_REQUIRES_LEGACY_MSG, file=sys.stderr)
            sys.exit(1)
        input_mode, input_path, input_err = resolve_uplink_cli_input(
            inventory_file=args.inventory_file,
            dry_ssh_file=args.file,
            legacy_dry_ssh=args.legacy_dry_ssh,
        )
        if input_err:
            print(input_err, file=sys.stderr)
            sys.exit(1)
        if input_mode != "legacy_dry_ssh":
            print(GENERATE_DESCRIPTION_MAP_REQUIRES_LEGACY_MSG, file=sys.stderr)
            sys.exit(1)
        data, err = load_devices_json(input_path)
        if err:
            print(err, file=sys.stderr)
            sys.exit(1)
        descriptions = set()
        for host_ifaces in data.get("devices", {}).values():
            for iface in host_ifaces:
                d = (iface.get("description") or "").strip()
                if d:
                    descriptions.add(d)
        existing = {}
        if "-m" in sys.argv or "--description-map" in sys.argv:
            existing = load_description_map(args.description_map)
        # We save the existing mappings, new description -> as is (then edit)
        out = dict(existing)
        for d in sorted(descriptions):
            if d not in out:
                out[d] = d
        print(json.dumps(out, indent=2, ensure_ascii=False))
        sys.exit(0)

    # Legacy utility: export map JSON from Zabbix API only
    if args.export_map:
        if not args.legacy_dry_ssh:
            print(MAP_LEGACY_UTILITY_REQUIRES_LEGACY_MSG, file=sys.stderr)
            sys.exit(1)
        url, token = _get_zabbix_url_token()
        if not url:
            print("For --export-map, set ZABBIX_URL and ZABBIX_TOKEN", file=sys.stderr)
            sys.exit(1)
        result, err = zabbix_request(url, token, "map.get", {
            "sysmapids": [args.export_map],
            "output": "extend",
            "selectSelements": "extend",
            "selectLinks": "extend",
        }, debug=args.debug)
        if err or not result:
            print(err or "card not found", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        sys.exit(0)

    # Legacy utility: create an empty map shell without inventory data
    if args.create_map and not args.update_map and not args.zabbix and not args.print_table:
        if not args.legacy_dry_ssh:
            print(MAP_LEGACY_UTILITY_REQUIRES_LEGACY_MSG, file=sys.stderr)
            sys.exit(1)
        url, token = _get_zabbix_url_token()
        if not url:
            print("Set ZABBIX_URL and ZABBIX_TOKEN", file=sys.stderr)
            sys.exit(1)
        sysmapid, err = ensure_map_exists(url, token, debug=args.debug)
        if err:
            print(err, file=sys.stderr)
            sys.exit(1)
        print("Map created (or already exists): sysmapid={}".format(sysmapid), file=sys.stderr)
        sys.exit(0)

    input_mode, input_path, input_err = resolve_uplink_cli_input(
        inventory_file=args.inventory_file,
        dry_ssh_file=args.file,
        legacy_dry_ssh=args.legacy_dry_ssh,
    )
    if input_err:
        print(input_err, file=sys.stderr)
        sys.exit(1)

    desc_to_name = {}
    inventory_report = None
    inv_ctx = None
    if input_mode == "inventory":
        try:
            inventory_report = load_inventory_report(input_path)
        except (OSError, json.JSONDecodeError) as e:
            print("failed to read inventory file {}: {}".format(input_path, e), file=sys.stderr)
            sys.exit(1)
        inv_ctx = load_uplink_provider_context(inventory_report=inventory_report, debug=args.debug)
        expanded_map = (inv_ctx or {}).get("device_iface_to_provider") or {}
        devices = scoped_devices_from_inventory_report(inventory_report, expanded_map)
        if not devices:
            print("No devices in scoped inventory", file=sys.stderr)
            sys.exit(1)
    else:
        desc_to_name = load_description_map(args.description_map)
        data, err = load_devices_json(input_path)
        if err:
            print(err, file=sys.stderr)
            sys.exit(1)
        devices = data["devices"]
    if args.host:
        if args.host not in devices:
            print("Host {!r} not found in devices. Available: {}".format(
                args.host, ", ".join(sorted(devices.keys()))), file=sys.stderr)
            sys.exit(1)
        devices = {args.host: devices[args.host]}

    # By default (without --update-map and --print-table) we create a map if it does not already exist.
    default_create_map = not args.update_map and not args.print_table

    use_zabbix = args.zabbix or args.create_map or args.update_map or default_create_map
    device_iface_to_provider = {}
    inventory_scoped = not args.legacy_provider_filter
    inventory_read_error = False
    if not args.legacy_provider_filter:
        if inv_ctx is None and (use_zabbix or args.print_table):
            inv_ctx = load_uplink_provider_context(
                devices,
                debug=args.debug,
                inventory_report=inventory_report,
            )
        if inv_ctx is not None:
            device_iface_to_provider = inv_ctx.get("device_iface_to_provider") or {}
            inventory_read_error = bool(inv_ctx.get("read_error"))
            if inventory_read_error:
                arm_netbox_incomplete_guard(inv_ctx.get("stats"))

    items_by_host_iface = {}
    if use_zabbix:
        url, token = _get_zabbix_url_token()
        if not url:
            print("For --zabbix, --create-map and --update-map set ZABBIX_URL and ZABBIX_TOKEN", file=sys.stderr)
            sys.exit(1)
        hostnames = set(devices.keys())
        cache_base = args.inventory_file or args.file
        cache_path = os.path.join(
            os.path.dirname(os.path.abspath(cache_base)) if cache_base else ".",
            ZABBIX_CACHE_FILE,
        )
        host_id_by_name = None
        items_by_host_iface = None
        if not args.no_cache:
            cached_host, cached_items = load_zabbix_cache(cache_path)
            if cached_host is not None and cached_items is not None and set(cached_host.keys()) >= hostnames:
                host_id_by_name = {k: cached_host[k] for k in hostnames if k in cached_host}
                items_by_host_iface = {(h, i): rec for (h, i), rec in cached_items.items() if h in host_id_by_name}
                if args.debug:
                    print("DEBUG: data loaded from cache {}".format(cache_path), file=sys.stderr)
        if host_id_by_name is None or items_by_host_iface is None:
            host_id_by_name, items_by_host_iface, err = fetch_zabbix_hosts_and_items(
                url, token, hostnames, debug=args.debug
            )
            if err:
                print(err, file=sys.stderr)
                sys.exit(1)
            if not args.no_cache:
                save_zabbix_cache(cache_path, host_id_by_name, items_by_host_iface)
                if args.debug:
                    print("DEBUG: cache saved in {}".format(cache_path), file=sys.stderr)
    else:
        host_id_by_name = {}

    # Table to output to the console (--print-table, or on incomplete inventory before exit)
    print_table = args.print_table or inventory_read_error
    rows = []
    if print_table:
        header = ("hostname", "interface", "description", "ISP")
        if use_zabbix:
            header = ("hostname", "hostid", "interface", "description", "ISP", "key Bits received", "key Bits sent")
        rows.append(header)
        lookup_debug_count = 0
        for hostname in sorted(devices.keys()):
            interfaces = devices[hostname]
            for iface in interfaces:
                iface_name = iface.get("name", "")
                description = iface.get("description", "")
                isp = resolve_provider_name_for_iface(
                    hostname,
                    iface,
                    desc_to_name,
                    device_iface_to_provider=device_iface_to_provider,
                )
                row = (hostname, iface_name, description, isp)
                if use_zabbix:
                    hostid = str(host_id_by_name.get(hostname, ""))
                    key_norm = _normalize_interface_name(iface_name)
                    rec = items_by_host_iface.get((hostname, key_norm), {})
                    if args.debug and lookup_debug_count < 8:
                        found = bool(rec.get("bits_in") or rec.get("bits_out"))
                        print("DEBUG lookup: hostname={!r} iface_name={!r} key_norm={!r} found={}".format(
                            hostname, iface_name, key_norm, found), file=sys.stderr)
                        lookup_debug_count += 1
                    row = (hostname, hostid, iface_name, description, isp, rec.get("bits_in", ""), rec.get("bits_out", ""))
                rows.append(row)

    # Map update on demand
    if args.update_map:
        if inventory_read_error:
            print(
                "NetBox data is incomplete; map not updated (a map built from partial data would drop links)",
                file=sys.stderr,
            )
        else:
            err_msg, sysmapid = update_uplinks_map(
                url,
                token,
                devices,
                host_id_by_name,
                items_by_host_iface,
                desc_to_name,
                debug=args.debug,
                prune_obsolete=(not args.host) and (not args.keep_obsolete_map_elements),
                device_iface_to_provider=device_iface_to_provider,
                inventory_scoped=inventory_scoped,
            )
            if err_msg:
                print(err_msg, file=sys.stderr)
                sys.exit(1)
            print("Map updated: sysmapid={}".format(sysmapid), file=sys.stderr)
    # Default behavior: create a map with elements if it doesn't already exist
    elif default_create_map and use_zabbix:
        # Check if there is already a card with the same name
        existing, err = zabbix_request(
            url, token, "map.get",
            {"filter": {"name": MAP_NAME}, "output": ["sysmapid"]},
            debug=args.debug,
        )
        if err:
            print("map.get: {}".format(err), file=sys.stderr)
            sys.exit(1)
        if existing:
            sysmapid = existing[0].get("sysmapid")
            print("Map already exists: name={!r}, sysmapid={}. Use --update-map to update.".format(
                MAP_NAME, sysmapid), file=sys.stderr)
        elif inventory_read_error:
            print(
                "NetBox data is incomplete; map not created",
                file=sys.stderr,
            )
        else:
            err_msg, sysmapid = update_uplinks_map(
                url,
                token,
                devices,
                host_id_by_name,
                items_by_host_iface,
                desc_to_name,
                debug=args.debug,
                device_iface_to_provider=device_iface_to_provider,
                inventory_scoped=inventory_scoped,
            )
            if err_msg:
                print(err_msg, file=sys.stderr)
                sys.exit(1)
            print("Map created: name={!r}, sysmapid={}".format(MAP_NAME, sysmapid), file=sys.stderr)

    if print_table and rows:
        num_cols = len(rows[0])
        widths = [max(len(str(rows[i][c])) for i in range(len(rows))) for c in range(num_cols)]
        pad = "  "
        for row in rows:
            print(pad.join(str(row[c]).ljust(widths[c]) for c in range(num_cols)))

    if inventory_read_error:
        print(
            "NetBox inventory is incomplete; map run marked as failed",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()

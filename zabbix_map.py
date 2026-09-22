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
LAYOUT_MAX_HEIGHT = 1600
ELEMENT_TYPE_HOST = 0
# In API: 0=host, 4=image (picture with caption)
ELEMENT_TYPE_IMAGE = 4

# Icon placement slot: Zabbix map (x,y) is the upper-left of the element area; icons render inside it.
LAYOUT_MARGIN = 30
LAYOUT_HOST_COLUMNS = 2
LAYOUT_ELEMENT_GAP = 40
SELEMENT_HEIGHT = 200
SELEMENT_WIDTH = 200

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


class LayoutValidationError(RuntimeError):
    """Raised when the offline layout model still detects collisions."""


def _layout_icon_step():
    """Horizontal/vertical distance between adjacent icon upper-left corners."""
    return SELEMENT_WIDTH + LAYOUT_ELEMENT_GAP


def _layout_step():
    """Backward-compatible alias for icon step."""
    return _layout_icon_step()


def _layout_provider_label_corridor():
    """Space reserved above provider icons for a one-line top label."""
    return LABEL_LINE_HEIGHT_PX + 2 * LABEL_PADDING_PX + LAYOUT_LABEL_GAP


def _layout_host_label_corridor():
    """Space reserved below host icons for a one-line bottom label."""
    return LABEL_LINE_HEIGHT_PX + 2 * LABEL_PADDING_PX + LAYOUT_LABEL_GAP


def _layout_link_corridor():
    """Vertical gap between provider and host icon rows for a 3-line link label."""
    return LINK_LABEL_PREVIEW_HEIGHT_PX + 2 * LAYOUT_LABEL_GAP


def _layout_host_row_offset():
    """
    Y offset from block top to the first host icon row.
    Sized so a preview link label centered on icon centers clears both icons.
    """
    provider_y = _layout_provider_label_corridor()
    return provider_y + SELEMENT_HEIGHT + LINK_LABEL_PREVIEW_HEIGHT_PX + 2 * LAYOUT_LABEL_GAP


def _layout_host_row_step():
    """Y distance between host icon rows inside a multi-host block."""
    return SELEMENT_HEIGHT + _layout_host_label_corridor() + LAYOUT_ELEMENT_GAP


def _layout_single_host_horizontal_offset():
    """X offset between provider and host icons in a single-host block."""
    return SELEMENT_WIDTH + max(LAYOUT_ELEMENT_GAP, LINK_LABEL_PREVIEW_WIDTH_PX // 2 + LAYOUT_LABEL_GAP)


def _layout_block_width(single_host=False, num_placed=1):
    """Content width of one provider block icon slots."""
    if single_host:
        return _layout_single_host_horizontal_offset() + SELEMENT_WIDTH
    if num_placed <= 1:
        return SELEMENT_WIDTH
    return (LAYOUT_HOST_COLUMNS - 1) * _layout_icon_step() + SELEMENT_WIDTH


def _layout_block_height(single_host, num_placed):
    """Reserved vertical extent of a placed provider block (icons + label/link corridors)."""
    if num_placed <= 0:
        return 0
    if single_host:
        return _layout_provider_label_corridor() + SELEMENT_HEIGHT + _layout_host_label_corridor()
    host_rows = math.ceil(num_placed / LAYOUT_HOST_COLUMNS)
    return (
        _layout_host_row_offset()
        + (host_rows - 1) * _layout_host_row_step()
        + SELEMENT_HEIGHT
        + _layout_host_label_corridor()
    )


def _layout_block_positions(single_host, hosts_to_place):
    """Relative (rx, ry) icon positions for provider and hosts inside one block."""
    col_step = _layout_icon_step()
    provider_y = _layout_provider_label_corridor()
    positions = [("isp", None, 0, provider_y)]
    if single_host:
        (_hostname, hostid) = hosts_to_place[0]
        positions.append((
            "host",
            str(hostid),
            _layout_single_host_horizontal_offset(),
            provider_y,
        ))
    else:
        row_offset = _layout_host_row_offset()
        row_step = _layout_host_row_step()
        for idx, (_hostname, hostid) in enumerate(hosts_to_place):
            row, subcol = divmod(idx, LAYOUT_HOST_COLUMNS)
            positions.append((
                "host",
                str(hostid),
                subcol * col_step,
                row_offset + row * row_step,
            ))
    return positions


def _element_icon_center(x, y):
    return (x + SELEMENT_WIDTH / 2.0, y + SELEMENT_HEIGHT / 2.0)


def _label_text_metrics(text, char_width=LABEL_CHAR_WIDTH_PX, line_height=LABEL_LINE_HEIGHT_PX):
    """Conservative (width, height) for an element label string."""
    lines = (text or "").split("\n") or [""]
    width = max(len(line) for line in lines) * char_width + 2 * LABEL_PADDING_PX
    height = len(lines) * line_height + 2 * LABEL_PADDING_PX
    return width, height


def _element_label_box(x, y, label, label_location, box_id, owner_id=None):
    """Conservative visible element-label rectangle adjacent to an icon slot."""
    width, height = _label_text_metrics(label)
    cx, cy = _element_icon_center(x, y)
    loc = int(label_location)
    if loc == LABEL_LOCATION_TOP:
        bounds = (cx - width / 2.0, y - height, cx + width / 2.0, y)
    elif loc == LABEL_LOCATION_LEFT:
        bounds = (x - width, cy - height / 2.0, x, cy + height / 2.0)
    elif loc == LABEL_LOCATION_RIGHT:
        bounds = (
            x + SELEMENT_WIDTH,
            cy - height / 2.0,
            x + SELEMENT_WIDTH + width,
            cy + height / 2.0,
        )
    else:
        bounds = (
            cx - width / 2.0,
            y + SELEMENT_HEIGHT,
            cx + width / 2.0,
            y + SELEMENT_HEIGHT + height,
        )
    return {"id": box_id, "kind": "element_label", "owner_id": owner_id, "bounds": bounds}


def _element_icon_box(x, y, box_id, owner_id=None):
    return {"id": box_id, "kind": "icon", "owner_id": owner_id, "bounds": _element_bounds(x, y)}


def _link_label_box(pos1, pos2, label, box_id, endpoint_ids=None):
    """Conservative link-label rectangle centered on icon centers (preview-sized, not char-derived width)."""
    cx1, cy1 = _element_icon_center(*pos1)
    cx2, cy2 = _element_icon_center(*pos2)
    mx = (cx1 + cx2) / 2.0
    my = (cy1 + cy2) / 2.0
    width = LINK_LABEL_PREVIEW_WIDTH_PX
    height = LINK_LABEL_PREVIEW_HEIGHT_PX
    return {
        "id": box_id,
        "kind": "link_label",
        "owner_id": None,
        "endpoint_ids": tuple(endpoint_ids or ()),
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


def _links_share_provider_endpoint(box_a, box_b):
    """True when two link labels fan out from the same provider element."""
    end_a = set(box_a.get("endpoint_ids") or ())
    end_b = set(box_b.get("endpoint_ids") or ())
    if not end_a or not end_b:
        return False
    shared = end_a & end_b
    return any(str(endpoint).startswith("isp-") for endpoint in shared)


def _links_share_host_endpoint(box_a, box_b):
    """True when two link labels fan out from the same host element."""
    end_a = set(box_a.get("endpoint_ids") or ())
    end_b = set(box_b.get("endpoint_ids") or ())
    if not end_a or not end_b:
        return False
    shared = end_a & end_b
    return any(str(endpoint).startswith("host-") for endpoint in shared)


def _box_selement_id(box):
    """Map a visible box back to its offline selement id (host-101, isp-Cogent)."""
    box_id = str(box.get("id", ""))
    for suffix in ("-icon", "-label"):
        if box_id.endswith(suffix):
            return box_id[: -len(suffix)]
    return box_id


def _link_label_touches_endpoint(link_box, other_box):
    """Link labels are anchored to two endpoints; skip collisions with those elements."""
    if link_box.get("kind") != "link_label":
        return False
    endpoints = set(link_box.get("endpoint_ids") or ())
    if not endpoints:
        return False
    return _box_selement_id(other_box) in endpoints


def _link_label_same_provider_fan(link_box, other_box, isp_fan_hosts, link_isp_by_id):
    """
    Multiple uplinks to one provider share a fan; preview boxes may cross within the fan.
    Still checked against unrelated providers/hosts.
    """
    if link_box.get("kind") != "link_label" or other_box["kind"] not in ("icon", "element_label"):
        return False
    isp = link_isp_by_id.get(link_box.get("id", ""))
    if not isp or not isp_fan_hosts:
        return False
    return _box_selement_id(other_box) in isp_fan_hosts.get(isp, set())


def _find_layout_collisions(
    boxes,
    gap=LAYOUT_LABEL_GAP,
    skip_same_owner=True,
    isp_fan_hosts=None,
    link_isp_by_id=None,
    host_providers=None,
):
    """Return collision pairs among visible boxes (conservative axis-aligned overlap)."""
    isp_fan_hosts = isp_fan_hosts or {}
    link_isp_by_id = link_isp_by_id or {}
    host_providers = host_providers or {}
    boxes_by_id = {box["id"]: box for box in boxes}
    collisions = []
    for i, box_a in enumerate(boxes):
        ax1, ay1, ax2, ay2 = box_a["bounds"]
        ax1 -= gap
        ay1 -= gap
        ax2 += gap
        ay2 += gap
        for box_b in boxes[i + 1 :]:
            if skip_same_owner and box_a.get("owner_id") and box_a["owner_id"] == box_b.get("owner_id"):
                continue
            if box_a["kind"] == "link_label" and box_b["kind"] == "link_label":
                if _links_share_provider_endpoint(box_a, box_b) or _links_share_host_endpoint(box_a, box_b):
                    continue
            if _link_label_touches_endpoint(box_a, box_b) or _link_label_touches_endpoint(box_b, box_a):
                continue
            if _link_label_same_provider_fan(box_a, box_b, isp_fan_hosts, link_isp_by_id):
                continue
            if _link_label_same_provider_fan(box_b, box_a, isp_fan_hosts, link_isp_by_id):
                continue
            if _shared_host_uplink_collision(box_a, box_b, host_providers, link_isp_by_id):
                continue
            if _shared_host_uplink_collision(box_b, box_a, host_providers, link_isp_by_id):
                continue
            bx1, by1, bx2, by2 = box_b["bounds"]
            if ax1 < bx2 and bx1 < ax2 and ay1 < by2 and by1 < ay2:
                collisions.append({
                    "box_a": box_a["id"],
                    "box_b": box_b["id"],
                    "kind_a": box_a["kind"],
                    "kind_b": box_b["kind"],
                })
    return collisions


def _link_isp_by_id(links, selements):
    """Map offline link id -> provider label."""
    sid_to_isp = {
        "isp-{}".format(el.get("label")): el.get("label")
        for el in selements
        if int(el.get("elementtype", 0)) == ELEMENT_TYPE_IMAGE
    }
    out = {}
    for link in links:
        isp_sid = str(link.get("selementid2", ""))
        if isp_sid in sid_to_isp:
            out[link.get("id", "")] = sid_to_isp[isp_sid]
    return out


def _isp_fan_host_ids(edges):
    """Hosts that share a provider uplink fan (multiple links to the same ISP)."""
    fan = {}
    for _hostname, hostid, _iface, isp, *_rest in edges:
        if isp:
            fan.setdefault(isp, set()).add("host-{}".format(hostid))
    return fan


def _host_provider_names(edges):
    """Map hostid -> set of provider names attached to that host."""
    out = {}
    for _hostname, hostid, _iface, isp, *_rest in edges:
        if isp:
            out.setdefault(str(hostid), set()).add(isp)
    return out


def _shared_host_uplink_collision(link_box, isp_box, host_providers, link_isp_by_id):
    """
    Shared-host orphan uplink: another provider's link preview may cross the orphan slot.
    Skip only when both the link and orphan ISP belong to the same host.
    """
    if link_box.get("kind") != "link_label" or isp_box.get("kind") not in ("icon", "element_label"):
        return False
    isp_box_id = str(isp_box.get("id", ""))
    if not isp_box_id.startswith("isp-"):
        return False
    orphan_isp = isp_box_id.split("-", 1)[1].rsplit("-", 1)[0]
    link_isp = link_isp_by_id.get(link_box.get("id", ""))
    if not link_isp or link_isp == orphan_isp:
        return False
    endpoints = set(link_box.get("endpoint_ids") or ())
    host_endpoints = [ep for ep in endpoints if str(ep).startswith("host-")]
    if len(host_endpoints) != 1:
        return False
    hostid = str(host_endpoints[0]).replace("host-", "", 1)
    providers = host_providers.get(hostid, set())
    return orphan_isp in providers and link_isp in providers


def _validate_layout_model(
    selements,
    links,
    gap=LAYOUT_LABEL_GAP,
    isp_fan_hosts=None,
    link_isp_by_id=None,
    host_providers=None,
):
    positions = _layout_positions_from_model(selements)
    boxes = _layout_visible_boxes(selements, links, positions)
    return _find_layout_collisions(
        boxes,
        gap=gap,
        isp_fan_hosts=isp_fan_hosts,
        link_isp_by_id=link_isp_by_id,
        host_providers=host_providers,
    )


def _validate_edges_layout(edges, host_pos, isp_pos):
    selements, links, _, _ = _build_layout_model_from_edges(edges, host_pos, isp_pos)
    return _validate_layout_model(
        selements,
        links,
        isp_fan_hosts=_isp_fan_host_ids(edges),
        link_isp_by_id=_link_isp_by_id(links, selements),
        host_providers=_host_provider_names(edges),
    )


def _layout_required_dimensions(host_pos, isp_pos, edges=None):
    """Map size from visible-box AABB plus margin and outer gap."""
    margin = LAYOUT_MARGIN
    gap = LAYOUT_ELEMENT_GAP
    if edges is not None and host_pos and isp_pos:
        selements, links, _, _ = _build_layout_model_from_edges(edges, host_pos, isp_pos)
        positions = _layout_positions_from_model(selements)
        boxes = _layout_visible_boxes(selements, links, positions)
        if boxes:
            max_right = max(box["bounds"][2] for box in boxes)
            max_bottom = max(box["bounds"][3] for box in boxes)
            return max_right + margin + gap, max_bottom + margin + gap
    max_right = margin
    max_bottom = margin
    for x, y in list(host_pos.values()) + list(isp_pos.values()):
        max_right = max(max_right, x + SELEMENT_WIDTH)
        max_bottom = max(max_bottom, y + SELEMENT_HEIGHT)
    return max_right + margin + gap, max_bottom + margin + gap


def _apply_block_positions(block_x, block_y, isp, positions, host_pos, isp_pos):
    for kind, key, rel_x, rel_y in positions:
        abs_x = block_x + rel_x
        abs_y = block_y + rel_y
        if kind == "isp":
            isp_pos[isp] = (abs_x, abs_y)
        else:
            host_pos[key] = (abs_x, abs_y)


def _block_layout_valid(edges, isp, block_x, block_y, positions, host_pos, isp_pos):
    trial_host = dict(host_pos)
    trial_isp = dict(isp_pos)
    _apply_block_positions(block_x, block_y, isp, positions, trial_host, trial_isp)
    return _validate_edges_layout(edges, trial_host, trial_isp) == []


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


def _occupied_positions(host_pos, isp_pos, exclude_xy=None):
    """List of occupied coordinates (x, y) for collision checking. exclude_xy - ignore this point."""
    out = []
    for v in host_pos.values():
        if exclude_xy is None or v != exclude_xy:
            out.append(v)
    for v in isp_pos.values():
        if exclude_xy is None or v != exclude_xy:
            out.append(v)
    return out


def _element_bounds(x, y):
    """Upper-left (x, y) element rectangle: (left, top, right, bottom)."""
    return (x, y, x + SELEMENT_WIDTH, y + SELEMENT_HEIGHT)


def _inflated_bounds(x, y, gap):
    """Rectangle expanded by gap on all sides for collision checks."""
    return (x - gap, y - gap, x + SELEMENT_WIDTH + gap, y + SELEMENT_HEIGHT + gap)


def _bounds_overlap(a, b):
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    return ax1 < bx2 and bx1 < ax2 and ay1 < by2 and by1 < ay2


def _is_free(cx, cy, occupied, gap=LAYOUT_ELEMENT_GAP):
    """True if element at (cx, cy) has at least gap to every occupied element rectangle."""
    candidate = _inflated_bounds(cx, cy, gap)
    for (ox, oy) in occupied:
        if _bounds_overlap(candidate, _element_bounds(ox, oy)):
            return False
    return True


def _validate_orphan_isp(edges, isp, host_pos, isp_pos, anchor_hostid=None):
    """Validate only boxes/links belonging to one orphan provider."""
    selements, links, _, _ = _build_layout_model_from_edges(edges, host_pos, isp_pos)
    positions = _layout_positions_from_model(selements)
    boxes = _layout_visible_boxes(selements, links, positions)
    collisions = _find_layout_collisions(
        boxes,
        isp_fan_hosts=_isp_fan_host_ids(edges),
        link_isp_by_id=_link_isp_by_id(links, selements),
        host_providers=_host_provider_names(edges),
    )
    orphan_sid = "isp-{}".format(isp)
    orphan_link_ids = {
        link.get("id")
        for link in links
        if str(link.get("selementid2", "")) == orphan_sid
    }
    orphan_boxes = {
        box["id"]
        for box in boxes
        if str(box.get("id", "")).startswith(orphan_sid)
    }
    orphan_boxes.update(orphan_link_ids)
    return [
        collision
        for collision in collisions
        if collision["box_a"] in orphan_boxes or collision["box_b"] in orphan_boxes
    ]


def _orphan_provider_candidates(hx, hy):
    """Deterministic nearby icon-slot candidates for orphan providers."""
    step = _layout_icon_step()
    above = _layout_provider_label_corridor() + LAYOUT_LABEL_GAP
    candidates = []
    seen = set()

    def _add(cx, cy):
        cx = int(round(cx))
        cy = int(round(cy))
        key = (cx, cy)
        if key not in seen and cx >= LAYOUT_MARGIN and cy >= LAYOUT_MARGIN:
            seen.add(key)
            candidates.append(key)

    for cx, cy in (
        (hx + step, hy),
        (hx - step, hy),
        (hx, hy - step),
        (hx + step, hy - above),
        (hx - step, hy - above),
        (hx, hy + step),
        (hx + step + step, hy),
        (hx - step - step, hy),
        (hx, hy),
    ):
        _add(cx, cy)
    for dx, dy in (
        (-1, 0), (1, 0), (0, -1), (0, 1),
        (-1, -1), (1, -1), (-1, 1), (1, 1),
    ):
        _add(hx + dx * step, hy + dy * step)
    for mult in range(2, 12):
        _add(hx - mult * step, hy)
        _add(hx + mult * step, hy)
        _add(hx, hy - mult * step)
        _add(hx, hy + mult * step)
    return candidates


def _place_orphan_provider(edges, isp, anchor_hostid, host_pos, isp_pos, map_width, map_height):
    """Place orphan provider deterministically using the offline visible-box model."""
    hx, hy = host_pos.get(str(anchor_hostid), (LAYOUT_MARGIN, LAYOUT_MARGIN))
    step = _layout_icon_step()
    max_x = min(LAYOUT_MAX_WIDTH, max(map_width, LAYOUT_MAX_WIDTH)) - LAYOUT_MARGIN - SELEMENT_WIDTH
    max_y = min(LAYOUT_MAX_HEIGHT, max(map_height, LAYOUT_MAX_HEIGHT)) - LAYOUT_MARGIN - SELEMENT_HEIGHT

    candidates = list(_orphan_provider_candidates(hx, hy))
    seen = set(candidates)
    for radius in range(1, 16):
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                if max(abs(dx), abs(dy)) != radius:
                    continue
                cx = int(round(hx + dx * step))
                cy = int(round(hy + dy * step))
                if cx < LAYOUT_MARGIN or cy < LAYOUT_MARGIN:
                    continue
                key = (cx, cy)
                if key not in seen:
                    seen.add(key)
                    candidates.append(key)

    max_col = max(1, (max_x - LAYOUT_MARGIN) // step + 1)
    max_row = max(1, (max_y - LAYOUT_MARGIN) // step + 1)
    for row in range(max_row):
        cy = LAYOUT_MARGIN + row * step
        for col in range(max_col):
            cx = LAYOUT_MARGIN + col * step
            key = (cx, cy)
            if key not in seen:
                seen.add(key)
                candidates.append(key)

    for cx, cy in candidates:
        if cx > max_x or cy > max_y:
            continue
        trial_isp = dict(isp_pos)
        trial_isp[isp] = (cx, cy)
        if _validate_orphan_isp(edges, isp, host_pos, trial_isp, anchor_hostid=anchor_hostid) == []:
            return (cx, cy)
    raise LayoutValidationError("layout: no collision-free orphan slot for {!r}".format(isp))


def _compute_layout(edges, map_width, map_height):
    """
    Using the edges, calculate the positions of hosts and providers.
    Providers in descending order of number of connections; blocks from left to right; if there is not enough space, move to the next line.
    One host at the provider: provider and host side by side with rectangle gap.
    Return: (host_pos, isp_pos, required_width, required_height).
    """
    isp_to_hosts = {}
    for hostname, hostid, _if, isp, _in, _out, _ki, _ko, _desc in edges:
        if not isp:
            continue
        if isp not in isp_to_hosts:
            isp_to_hosts[isp] = set()
        isp_to_hosts[isp].add((hostname, hostid))

    isps_sorted = sorted(isp_to_hosts.keys(), key=lambda i: -len(isp_to_hosts[i]))

    host_pos = {}
    isp_pos = {}
    placed_hosts = set()

    block_x = LAYOUT_MARGIN
    block_y = LAYOUT_MARGIN
    row_max_height = 0
    max_x = map_width - LAYOUT_MARGIN
    max_row_width = 0 # max. width across all rows (for the final card size)

    gap = LAYOUT_ELEMENT_GAP
    orphan_isps = []

    def _wrap_shelf():
        nonlocal block_x, block_y, row_max_height, max_row_width
        max_row_width = max(max_row_width, block_x + LAYOUT_MARGIN)
        block_x = LAYOUT_MARGIN
        block_y += row_max_height + gap
        row_max_height = 0

    for isp in isps_sorted:
        hosts_in_block = sorted(isp_to_hosts[isp], key=lambda t: (t[0], t[1]))
        # One host per provider = by the number of connections to the ISP, not by the number placed in this block
        single_host = len(isp_to_hosts[isp]) == 1

        hosts_to_place = [
            (hostname, hostid)
            for (hostname, hostid) in hosts_in_block
            if hostid not in placed_hosts
        ]

        if not hosts_to_place:
            orphan_isps.append(isp)
            continue

        num_placed = len(hosts_to_place)
        block_width = _layout_block_width(single_host, num_placed)
        block_height = _layout_block_height(single_host, num_placed)
        positions = _layout_block_positions(single_host, hosts_to_place)

        if block_x + block_width > max_x and block_x > LAYOUT_MARGIN:
            _wrap_shelf()

        attempts = 0
        while not _block_layout_valid(edges, isp, block_x, block_y, positions, host_pos, isp_pos):
            if block_x + block_width + gap + block_width <= max_x + LAYOUT_MARGIN:
                block_x += block_width + gap
            else:
                _wrap_shelf()
            attempts += 1
            if attempts > 256:
                raise LayoutValidationError("layout: cannot place block for {!r}".format(isp))

        _apply_block_positions(block_x, block_y, isp, positions, host_pos, isp_pos)
        for _hostname, hostid in hosts_to_place:
            placed_hosts.add(hostid)

        row_max_height = max(row_max_height, block_height)
        max_row_width = max(max_row_width, block_x + block_width)
        block_x += block_width + gap

    for isp in orphan_isps:
        hosts_in_block = sorted(isp_to_hosts[isp], key=lambda t: (t[0], t[1]))
        (_, anchor_hostid) = next(iter(hosts_in_block))
        isp_pos[isp] = _place_orphan_provider(
            edges, isp, anchor_hostid, host_pos, isp_pos, map_width, map_height,
        )

    required_width, required_height = _layout_required_dimensions(host_pos, isp_pos, edges=edges)
    return host_pos, isp_pos, required_width, required_height


def _compute_layout_validated(edges, map_width=MAP_WIDTH, map_height=MAP_HEIGHT):
    """
    Compute layout and validate with the offline visible-box model.
    Grows canvas within LAYOUT_MAX_* bounds; raises LayoutValidationError on failure.
    """
    grow_step = _layout_icon_step()
    width = map_width
    height = map_height
    last_collisions = []
    while width <= LAYOUT_MAX_WIDTH and height <= LAYOUT_MAX_HEIGHT:
        host_pos, isp_pos, required_width, required_height = _compute_layout(edges, width, height)
        collisions = _validate_edges_layout(edges, host_pos, isp_pos)
        if not collisions:
            return (
                host_pos,
                isp_pos,
                max(width, required_width),
                max(height, required_height),
            )
        last_collisions = collisions
        next_width = max(width + grow_step, required_width, map_width)
        next_height = max(height + grow_step, required_height, map_height)
        if next_width == width and next_height == height:
            break
        width = min(next_width, LAYOUT_MAX_WIDTH)
        height = min(next_height, LAYOUT_MAX_HEIGHT)
    raise LayoutValidationError(
        "layout model collisions remain ({}): {}".format(
            len(last_collisions),
            ", ".join("{}↔{}".format(c["box_a"], c["box_b"]) for c in last_collisions[:5]),
        )
    )


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
    w = width if width is not None else MAP_WIDTH
    h = height if height is not None else MAP_HEIGHT
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
    map_width = max(MAP_WIDTH, required_width)
    map_height = max(MAP_HEIGHT, required_height)
    layout_collisions = _validate_edges_layout(edges, host_pos, isp_pos)
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
        x, y = host_pos.get(str(hostid), (LAYOUT_MARGIN, LAYOUT_MARGIN))
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
        x, y = isp_pos.get(isp, (map_width - 250, LAYOUT_MARGIN))
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

    new_links = []
    our_host_sids = set()
    for hostname, hostid, iface_name, isp, itemid_in, itemid_out, key_in, key_out, _desc in edges:
        sid1 = host_to_selement.get(str(hostid))
        sid2 = isp_to_selement.get(isp) if isp else None
        if not sid1 or not sid2:
            if debug or (not new_links and not our_host_sids):
                print("DEBUG link skip: hostid={!r} isp={!r} sid1={} sid2={} (host_ids on the map: {!r}, isp labels: {!r})".format(
                    hostid, isp, sid1, sid2, list(host_to_selement.keys())[:10], list(isp_to_selement.keys())[:10]), file=sys.stderr)
            continue
        our_host_sids.add(sid1)
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

    # Existing links: only those that are not from our hosts; replace deleted duplicates with the canonical selementid
    our_host_sids_str = {str(s) for s in our_host_sids}
    links_merged = []
    for l in links_existing:
        s1 = str(l.get("selementid1", ""))
        if s1 in our_host_sids_str:
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

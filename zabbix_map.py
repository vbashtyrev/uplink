#!/usr/bin/env python3
"""Build or update a Zabbix map for uplinks (hosts, providers, links) based on dry-ssh.json and Zabbix API."""

import argparse
import json
import math
import os
import re
import sys

from env_urls import load_env_file_if_present
from generate_commit_rates import is_uplink
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


DEFAULT_INPUT = "dry-ssh.json"
DESCRIPTION_MAP_FILE = "description_to_name.json"

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


def load_devices_json(path):
    """Load JSON with the key devices. Return (data, None) or (None, error_msg).
    Format: devices[hostname] = [{"name": "...", "description": "...", ...}, ...].
    The file may contain logical interfaces (Juniper: ae5, ae5.0, et-0/0/3); optional fields:
    isLogical, isLag, physicalInterface, aggregateInterface, logicalInterface - used when selecting
    one edge per (host, ISP) for the map."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, "file not found: {}".format(path)
    except json.JSONDecodeError as e:
        return None, "JSON error: {}".format(e)
    if "devices" not in data:
        return None, "the file does not contain the 'devices' key"
    return data, None


def load_description_map(path):
    """Load mapping description -> display name. Empty dict if file does not exist."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


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


MAP_WIDTH = 1200
MAP_HEIGHT = 800
ELEMENT_TYPE_HOST = 0
# In API: 0=host, 4=image (picture with caption)
ELEMENT_TYPE_IMAGE = 4

# Arrangement: blocks by providers from left to right; map border is 30, hosts are no closer than 160 from the provider horizontally, vertical step is 100, between hosts horizontally is 180
LAYOUT_MARGIN = 30
LAYOUT_BLOCK_WIDTH = 500
LAYOUT_ISP_Y_OFFSET = 50
LAYOUT_MIN_HOST_TO_PROVIDER = 160 # horizontal minimum from provider to host
LAYOUT_HOST_HORIZONTAL_GAP = 180 # horizontal between hosts (two columns)
LAYOUT_HOST_Y_OFFSET = 100 # vertical: first row of hosts under the provider
LAYOUT_HOST_STEP_Y = 100 # vertical step between rows of hosts
LAYOUT_HOST_COLUMNS = 2
# Minimum distance between the centers of elements (so as not to overlap)
LAYOUT_MIN_DISTANCE = 80
# Size of the element on the Zabbix map (x,y - upper left corner); needed to calculate the map height
SELEMENT_HEIGHT = 200
SELEMENT_WIDTH = 200


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


def _is_free(cx, cy, occupied, min_dist):
    """True if (cx, cy) is not closer than min_dist to any occupied point."""
    for (ox, oy) in occupied:
        if (cx - ox) ** 2 + (cy - oy) ** 2 < min_dist * min_dist:
            return False
    return True


def _place_single_host_provider(hx, hy, host_pos, isp_pos):
    """
    Find a free position for a provider with one host (the host is already in another block).
    Order: left, right, bottom, top, between (closest left/right).
    Return (x, y) or (hx ​​- 170, hy) if everything is busy.
    """
    occupied = _occupied_positions(host_pos, isp_pos)
    min_d = LAYOUT_MIN_DISTANCE
    candidates = [
        (hx - 170, hy), # left
        (hx + 170, hy), # right
        (hx, hy + 100), # from below
        (hx, hy - 100), # on top
        (hx - 85, hy), # between (closer to the left)
        (hx + 85, hy), # between (closer to the right)
    ]
    for (cx, cy) in candidates:
        if _is_free(cx, cy, occupied, min_d):
            return (cx, cy)
    return (hx - 170, hy)


def _compute_layout(edges, map_width, map_height):
    """
    Using the edges, calculate the positions of hosts and providers.
    Providers in descending order of number of connections; blocks from left to right; if there is not enough space, move to the next line.
    One host at the provider: provider and host on the side (same rules ±170), at the same height.
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

    for isp in isps_sorted:
        if block_x + LAYOUT_BLOCK_WIDTH > max_x and block_x > LAYOUT_MARGIN:
            max_row_width = max(max_row_width, block_x + LAYOUT_MARGIN)
            block_x = LAYOUT_MARGIN
            block_y += row_max_height
            row_max_height = 0

        provider_x = block_x + LAYOUT_BLOCK_WIDTH // 2
        hosts_in_block = sorted(isp_to_hosts[isp], key=lambda t: (t[0], t[1]))
        # One host per provider = by the number of connections to the ISP, not by the number placed in this block
        single_host = len(isp_to_hosts[isp]) == 1
        host_y_row0 = block_y + LAYOUT_ISP_Y_OFFSET + LAYOUT_HOST_Y_OFFSET

        num_placed = 0
        for (hostname, hostid) in hosts_in_block:
            if hostid in placed_hosts:
                continue
            placed_hosts.add(hostid)
            row, subcol = divmod(num_placed, LAYOUT_HOST_COLUMNS)
            if single_host:
                # Provider and host on the side: the same ±170, provider on the left, host on the right
                isp_pos[isp] = (provider_x - 170, host_y_row0)
                x = provider_x + 170
                y = host_y_row0
            else:
                offset_x = 170 if subcol == 1 else -170
                x = provider_x + offset_x
                y = host_y_row0 + row * LAYOUT_HOST_STEP_Y
                if num_placed == 0:
                    isp_pos[isp] = (provider_x, block_y + LAYOUT_ISP_Y_OFFSET)
            host_pos[str(hostid)] = (x, y)
            num_placed += 1

        if num_placed == 0:
            if single_host:
                # Provider with one host: the host is already in another block - select a free position nearby
                (_, only_hostid) = next(iter(hosts_in_block))
                hx, hy = host_pos.get(str(only_hostid), (provider_x - 170, host_y_row0))
                isp_pos[isp] = _place_single_host_provider(hx, hy, host_pos, isp_pos)
                continue
            else:
                isp_pos[isp] = (provider_x, block_y + LAYOUT_ISP_Y_OFFSET)

        host_rows = math.ceil(num_placed / LAYOUT_HOST_COLUMNS) if num_placed else 0
        if single_host:
            block_height = LAYOUT_ISP_Y_OFFSET + LAYOUT_HOST_Y_OFFSET
        else:
            block_height = LAYOUT_ISP_Y_OFFSET + LAYOUT_HOST_Y_OFFSET + host_rows * LAYOUT_HOST_STEP_Y
        row_max_height = max(row_max_height, block_height)

        block_x += LAYOUT_BLOCK_WIDTH

    # Take into account the size of the element: in the API (x,y) - the upper left corner, element SELEMENT_WIDTH x SELEMENT_HEIGHT
    required_width = max(block_x + LAYOUT_MARGIN, max_row_width) + SELEMENT_WIDTH
    required_height = block_y + row_max_height + LAYOUT_MARGIN + SELEMENT_HEIGHT
    return host_pos, isp_pos, required_width, required_height


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
    url, token, devices, host_id_by_name, items_by_host_iface, desc_to_name, debug=False, prune_obsolete=True
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
            if not is_uplink(iface):
                continue
            iface_name = iface.get("name", "")
            description = iface.get("description", "")
            isp = desc_to_name.get(description, description)
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
    host_pos, isp_pos, required_width, required_height = _compute_layout(edges, MAP_WIDTH, MAP_HEIGHT)
    map_width = max(MAP_WIDTH, required_width)
    map_height = max(MAP_HEIGHT, required_height)

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
            "label_location": -1,
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
                el["urls"] = host_to_urls.get(eid, [])
        elif etype == ELEMENT_TYPE_IMAGE:
            label = el.get("label", "")
            if label in isp_pos:
                el["x"], el["y"] = isp_pos[label]

    # Removed/merged selements are still listed in the old map links → Zabbix with map.update(selements):
    # "Link selementid1 points to a nonexistent map selement." First we remove all links.
    need_clear_links = pruned_selements > 0 or len(old_selements) < len(old_selements_raw)
    map_sid = _api_map_id(sysmapid)
    if need_clear_links:
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
        # Signature: interface + In/Out lines with macros {?last(/hostname/key)}
        label_parts = [iface_name or "—"]
        if key_in or key_out:
            if key_in:
                label_parts.append("In: {?last(/" + hostname + "/" + key_in + ")}")
            if key_out:
                label_parts.append("Out: {?last(/" + hostname + "/" + key_out + ")}")
        # New link: do not pass linkid (read-only in the API; linkid:0 gives Wrong fields for map link).
        link = {
            "selementid1": _api_map_id(sid1),
            "selementid2": _api_map_id(sid2),
            "label": "\n".join(label_parts),
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

    # Existing links: only those that are not from our hosts; replace deleted duplicates with the canonical selementid
    our_host_sids_str = {str(s) for s in our_host_sids}
    links_merged = []
    for l in links_existing:
        s1 = str(l.get("selementid1", ""))
        if s1 in our_host_sids_str:
            continue
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
            # Save trigger bindings to the link when updating
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
        links_merged.append(entry)
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
        default=DEFAULT_INPUT,
        metavar="FILE",
        help="Path to JSON with devices (default {})".format(DEFAULT_INPUT),
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

    # Generating the description_to_name template: collect all descriptions from the file
    if args.generate_description_map:
        data, err = load_devices_json(args.file)
        if err:
            print(err, file=sys.stderr)
            sys.exit(1)
        descriptions = set()
        for host_ifaces in data.get("devices", {}).values():
            for iface in host_ifaces:
                d = (iface.get("description") or "").strip()
                if d:
                    descriptions.add(d)
        existing = load_description_map(args.description_map)
        # We save the existing mappings, new description -> as is (then edit)
        out = dict(existing)
        for d in sorted(descriptions):
            if d not in out:
                out[d] = d
        print(json.dumps(out, indent=2, ensure_ascii=False))
        sys.exit(0)

    # Export mode: map.get and JSON output only
    if args.export_map:
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

    # Only create a map - do not load data, do not display a table
    if args.create_map and not args.update_map and not args.zabbix and not args.print_table:
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

    data, err = load_devices_json(args.file)
    if err:
        print(err, file=sys.stderr)
        sys.exit(1)

    desc_to_name = load_description_map(args.description_map)
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
    items_by_host_iface = {}
    if use_zabbix:
        url, token = _get_zabbix_url_token()
        if not url:
            print("For --zabbix, --create-map and --update-map set ZABBIX_URL and ZABBIX_TOKEN", file=sys.stderr)
            sys.exit(1)
        hostnames = set(devices.keys())
        cache_path = os.path.join(os.path.dirname(os.path.abspath(args.file)) if args.file else ".", ZABBIX_CACHE_FILE)
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

    # Table to output to the console (only with --print-table)
    rows = []
    if args.print_table:
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
                isp = desc_to_name.get(description, description)
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
        prune_map = (not args.host) and (not args.keep_obsolete_map_elements)
        err_msg, sysmapid = update_uplinks_map(
            url,
            token,
            devices,
            host_id_by_name,
            items_by_host_iface,
            desc_to_name,
            debug=args.debug,
            prune_obsolete=prune_map,
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
        else:
            err_msg, sysmapid = update_uplinks_map(
                url,
                token,
                devices,
                host_id_by_name,
                items_by_host_iface,
                desc_to_name,
                debug=args.debug,
                prune_obsolete=True,
            )
            if err_msg:
                print(err_msg, file=sys.stderr)
                sys.exit(1)
            print("Map created: name={!r}, sysmapid={}".format(MAP_NAME, sysmapid), file=sys.stderr)

    # Print table only when requested
    if args.print_table and rows:
        num_cols = len(rows[0])
        widths = [max(len(str(rows[i][c])) for i in range(len(rows))) for c in range(num_cols)]
        pad = "  "
        for row in rows:
            print(pad.join(str(row[c]).ljust(widths[c]) for c in range(num_cols)))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Generate data (and optional dashboard) for Grafana Node graph view of uplinks."""

import argparse
import json
import os
import re
import sys

from env_urls import load_env_file_if_present
from zabbix_map import (
    DEFAULT_INPUT,
    DESCRIPTION_MAP_FILE,
    ZABBIX_CACHE_FILE,
    load_devices_json,
    load_description_map,
    load_zabbix_cache,
    save_zabbix_cache,
    fetch_zabbix_hosts_and_items,
    _normalize_interface_name,
    _get_zabbix_url_token,
)

load_env_file_if_present()


def _isp_id(isp):
    """Return unique provider node id for Node graph."""
    if not isp:
        return "isp_"
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", isp.strip())
    return "isp_{}".format(safe or "unknown")


def _host_id(hostid):
    return "host_{}".format(hostid)


def build_edges(devices, host_id_by_name, items_by_host_iface, desc_to_name):
    """Build deduplicated edge list per (host, provider), similar to zabbix_map."""
    edges_raw = []
    for hostname in sorted(devices.keys()):
        # Fallback: without --zabbix hostids are unknown, but graph can still be built by hostname.
        hostid = host_id_by_name.get(hostname) or hostname
        for iface in devices[hostname]:
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
            edges_raw.append((
                hostname, str(hostid), iface_name, isp, itemid_in, itemid_out, key_in, key_out,
                has_items, is_logical, is_aggregate, description,
            ))

    def _edge_priority(e):
        _, _, _, _, _, _, _, _, has_items, is_logical, is_aggregate, _ = e
        return (has_items, is_logical, not is_aggregate)

    seen_key = {}
    for e in edges_raw:
        key = (e[0], e[1], e[3])
        if key not in seen_key or _edge_priority(e) > _edge_priority(seen_key[key]):
            seen_key[key] = e
    return [
        (e[0], e[1], e[2], e[3], e[4], e[5], e[6], e[7], e[11])
        for e in sorted(seen_key.values(), key=lambda x: (x[0], x[3], x[2]))
    ]


def _csv_escape(val):
    """Escape value for CSV (quotes, commas, newlines)."""
    s = "" if val is None else str(val)
    if "," in s or "\n" in s or '"' in s:
        return '"' + s.replace('"', '""') + '"'
    return s


def _graph_to_inline_csv(graph):
    """Convert graph (nodes, edges) to two CSV strings for Infinity inline mode."""
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    #
    rows_n = []
    for n in nodes:
        rows_n.append(",".join([_csv_escape(n.get("id", "")), _csv_escape(n.get("title", ""))]))
    nodes_csv = "id,title\n" + "\n".join(rows_n)
    #
    edge_cols = ["id", "source", "target", "detail__hostname", "detail__iface", "detail__isp", "detail__itemid_in", "detail__itemid_out"]
    rows_e = []
    for e in edges:
        row = [
            _csv_escape(e.get("id", "")),
            _csv_escape(e.get("source", "")),
            _csv_escape(e.get("target", "")),
            _csv_escape(e.get("detail__hostname", "")),
            _csv_escape(e.get("detail__iface", "")),
            _csv_escape(e.get("detail__isp", "")),
            _csv_escape(e.get("detail__itemid_in", "")),
            _csv_escape(e.get("detail__itemid_out", "")),
        ]
        rows_e.append(",".join(row))
    edges_csv = ",".join(edge_cols) + "\n" + "\n".join(rows_e)
    return nodes_csv, edges_csv


def _get_grafana_env():
    """Return (base_url, api_key) from GRAFANA_URL and GRAFANA_API_KEY/TOKEN."""
    url = os.environ.get("GRAFANA_URL", "").strip().rstrip("/")
    key = os.environ.get("GRAFANA_API_KEY") or os.environ.get("GRAFANA_TOKEN", "")
    return url, (key or "").strip()


def _grafana_push_dashboard(grafana_url, api_key, graph, dashboard_uid, dashboard_title, folder_uid, infinity_uid, debug=False):
    """Create or update Grafana dashboard with a single Node graph panel from inline CSV."""
    try:
        import requests
    except ImportError:
        return "requests module required for --grafana-api (pip install requests)"
    if not grafana_url or not api_key:
        return "Set GRAFANA_URL and GRAFANA_API_KEY (or GRAFANA_TOKEN)"
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer {}".format(api_key),
    }
    dash_id = None
    version = 1
    try:
        r = requests.get(
            "{}/api/dashboards/uid/{}".format(grafana_url, dashboard_uid),
            headers=headers,
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            dash = data.get("dashboard") or {}
            dash_id = dash.get("id")
            version = (dash.get("version") or 0) + 1
            if debug:
                print("Grafana: found dashboard id={} version->{}".format(dash_id, version), file=sys.stderr)
    except requests.RequestException:
        pass

    nodes_csv, edges_csv = _graph_to_inline_csv(graph)
    ds_uid = infinity_uid or "infinity"
    nodes_columns = [
        {"selector": "id", "text": "id", "type": "string"},
        {"selector": "title", "text": "title", "type": "string"},
    ]
    edges_columns = [
        {"selector": "id", "text": "id", "type": "string"},
        {"selector": "source", "text": "source", "type": "string"},
        {"selector": "target", "text": "target", "type": "string"},
        {"selector": "detail__hostname", "text": "detail__hostname", "type": "string"},
        {"selector": "detail__iface", "text": "detail__iface", "type": "string"},
        {"selector": "detail__isp", "text": "detail__isp", "type": "string"},
        {"selector": "detail__itemid_in", "text": "detail__itemid_in", "type": "string"},
        {"selector": "detail__itemid_out", "text": "detail__itemid_out", "type": "string"},
    ]
    targets = [
        {
            "refId": "A",
            "datasource": {"type": "grafana-infinity-datasource", "uid": ds_uid},
            "type": "csv",
            "source": "inline",
            "root_selector": "",
            "data": nodes_csv,
            "format": "node-graph-nodes",
            "columns": nodes_columns,
            "filters": [],
        },
        {
            "refId": "B",
            "datasource": {"type": "grafana-infinity-datasource", "uid": ds_uid},
            "type": "csv",
            "source": "inline",
            "root_selector": "",
            "data": edges_csv,
            "format": "node-graph-edges",
            "columns": edges_columns,
            "filters": [],
        },
    ]
    panel = {
        "id": 1,
        "type": "nodeGraph",
        "title": "Uplinks",
        "gridPos": {"x": 0, "y": 0, "w": 24, "h": 12},
        "datasource": {"type": "grafana-infinity-datasource", "uid": ds_uid},
        "targets": targets,
    }
    dashboard = {
        "title": dashboard_title,
        "uid": dashboard_uid,
        "schemaVersion": 36,
        "version": version,
        "panels": [panel],
    }
    if dash_id is not None:
        dashboard["id"] = dash_id
    payload = {"dashboard": dashboard, "overwrite": True}
    if folder_uid:
        payload["folderUid"] = folder_uid
    url = "{}/api/dashboards/db".format(grafana_url)
    if debug:
        print("Grafana API: POST {}".format(url), file=sys.stderr)
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        if debug:
            print("Grafana: status={}, uid={}".format(r.status_code, data.get("uid")), file=sys.stderr)
        return None
    except requests.RequestException as e:
        body = ""
        if hasattr(e, "response") and e.response is not None and e.response.text:
            body = e.response.text[:500]
        return "Grafana API: {} ({})".format(e, body)


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    parser = argparse.ArgumentParser(
        description="Build Grafana Node graph data for uplinks (hosts, providers, links) from Zabbix.",
    )
    parser.add_argument("-f", "--file", default=DEFAULT_INPUT, help="Path to dry-ssh.json")
    parser.add_argument("-m", "--description-map", default=DESCRIPTION_MAP_FILE, help="Path to description_to_name.json")
    parser.add_argument(
        "--zabbix",
        action="store_true",
        help="Query Zabbix API for hosts/items (In/Out values come from Zabbix datasource in Grafana)",
    )
    parser.add_argument("-o", "--output", metavar="FILE", help="Output JSON file (default: stdout)")
    parser.add_argument(
        "--grafana-api",
        action="store_true",
        help="Create/update Grafana dashboard via API (requires GRAFANA_URL and GRAFANA_API_KEY/TOKEN)",
    )
    parser.add_argument("--dashboard-uid", default="uplinks", help="Dashboard UID (default: uplinks)")
    parser.add_argument("--dashboard-title", default="Uplinks", help="Dashboard title")
    parser.add_argument("--folder-uid", default="", help="Grafana folder UID (empty = General)")
    parser.add_argument(
        "--infinity-uid",
        default="",
        help="Infinity datasource UID (default: GRAFANA_INFINITY_UID or 'infinity')",
    )
    parser.add_argument("--no-cache", action="store_true", help="Do not use local Zabbix cache file")
    parser.add_argument("--debug", action="store_true", help="Debug output")
    args = parser.parse_args()

    data, err = load_devices_json(args.file)
    if err:
        print(err, file=sys.stderr)
        sys.exit(1)
    devices = data["devices"]
    desc_to_name = load_description_map(args.description_map)

    host_id_by_name = {}
    items_by_host_iface = {}
    url = None
    token = None
    if args.zabbix:
        url, token = _get_zabbix_url_token()
        if not url:
            print("Set ZABBIX_URL and ZABBIX_TOKEN", file=sys.stderr)
            sys.exit(1)
        hostnames = set(devices.keys())
        cache_path = os.path.join(
            os.path.dirname(os.path.abspath(args.file)) if args.file else ".",
            ZABBIX_CACHE_FILE,
        )
        if not args.no_cache:
            cached_h, cached_i = load_zabbix_cache(cache_path)
            if cached_h is not None and cached_i is not None and set(cached_h.keys()) >= hostnames:
                host_id_by_name = {k: cached_h[k] for k in hostnames if k in cached_h}
                items_by_host_iface = {(h, i): rec for (h, i), rec in cached_i.items() if h in host_id_by_name}
        if not host_id_by_name or not items_by_host_iface:
            host_id_by_name, items_by_host_iface, err = fetch_zabbix_hosts_and_items(
                url, token, hostnames, debug=args.debug
            )
            if err:
                print(err, file=sys.stderr)
                sys.exit(1)
            if not args.no_cache:
                save_zabbix_cache(cache_path, host_id_by_name, items_by_host_iface)

    edges = build_edges(devices, host_id_by_name, items_by_host_iface, desc_to_name)
    if not edges:
        print("No graph edges (empty devices or no Zabbix hosts/items)", file=sys.stderr)
        sys.exit(1)

    node_ids = set()
    nodes = []
    for hostname, hostid, _if, isp, _in, _out, _ki, _ko, _desc in edges:
        hid = _host_id(hostid)
        if hid not in node_ids:
            node_ids.add(hid)
            nodes.append({"id": hid, "title": hostname})
        iid = _isp_id(isp)
        if iid not in node_ids:
            node_ids.add(iid)
            nodes.append({"id": iid, "title": isp or "—"})

    #
    edges_out = []
    for i, (hostname, hostid, iface_name, isp, itemid_in, itemid_out, key_in, key_out, description) in enumerate(edges):
        edge_obj = {
            "id": "edge_{}".format(i + 1),
            "source": _host_id(hostid),
            "target": _isp_id(isp),
            "detail__hostname": hostname,
            "detail__iface": iface_name,
            "detail__isp": isp or "",
        }
        if itemid_in:
            edge_obj["detail__itemid_in"] = itemid_in
        if itemid_out:
            edge_obj["detail__itemid_out"] = itemid_out
        edges_out.append(edge_obj)

    out = {"nodes": nodes, "edges": edges_out}
    json_str = json.dumps(out, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(json_str)
        if args.debug:
            print("Wrote {} (nodes: {}, edges: {})".format(args.output, len(nodes), len(edges_out)), file=sys.stderr)
    else:
        print(json_str)

    if args.grafana_api:
        grafana_url, api_key = _get_grafana_env()
        err = _grafana_push_dashboard(
            grafana_url,
            api_key,
            out,
            dashboard_uid=args.dashboard_uid,
            dashboard_title=args.dashboard_title,
            folder_uid=args.folder_uid or None,
            infinity_uid=args.infinity_uid or os.environ.get("GRAFANA_INFINITY_UID", "").strip() or None,
            debug=args.debug,
        )
        if err:
            print(err, file=sys.stderr)
            sys.exit(1)
        if not args.output:
            print("Dashboard created/updated: {} (uid={})".format(args.dashboard_title, args.dashboard_uid), file=sys.stderr)


if __name__ == "__main__":
    main()

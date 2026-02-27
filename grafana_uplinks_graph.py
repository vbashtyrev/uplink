#!/usr/bin/env python3
"""
Генерация JSON для панели Node graph в Grafana: узлы (хосты, провайдеры) и рёбра (линки).
Топология и дедупликация — как в zabbix_map. In/Out на рёбрах берутся в Grafana из datasource Zabbix.
Опционально: создание/обновление дашборда с панелью Node graph через Grafana API.
"""

import argparse
import json
import os
import re
import sys

# Топология и Zabbix — общая логика с картой Zabbix
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


def _isp_id(isp):
    """Уникальный id узла-провайдера для Node graph (без спецсимволов)."""
    if not isp:
        return "isp_"
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", isp.strip())
    return "isp_{}".format(safe or "unknown")


def _host_id(hostid):
    return "host_{}".format(hostid)


def build_edges(devices, host_id_by_name, items_by_host_iface, desc_to_name):
    """
    Построить список рёбер (дедупликация по host, ISP), как в update_uplinks_map.
    Возврат: список кортежей (hostname, hostid, iface_name, isp, itemid_in, itemid_out, key_in, key_out, description).
    """
    edges_raw = []
    for hostname in sorted(devices.keys()):
        hostid = host_id_by_name.get(hostname)
        if not hostid:
            continue
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
    """Экранировать значение для CSV (кавычки при запятой/переводе/кавычке)."""
    s = "" if val is None else str(val)
    if "," in s or "\n" in s or '"' in s:
        return '"' + s.replace('"', '""') + '"'
    return s


def _graph_to_inline_csv(graph):
    """
    Преобразовать graph (nodes, edges) в два CSV-стринга для Infinity inline.
    В документации Infinity Node graph рабочий пример — именно CSV, не JSON.
    """
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    # Узлы: id, title (обязательные для Node graph)
    rows_n = []
    for n in nodes:
        rows_n.append(",".join([_csv_escape(n.get("id", "")), _csv_escape(n.get("title", ""))]))
    nodes_csv = "id,title\n" + "\n".join(rows_n)
    # Рёбра: id, source, target (обязательные), detail__*
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
    """Возврат (base_url, api_key) из GRAFANA_URL и GRAFANA_API_KEY или GRAFANA_TOKEN. URL без завершающего /."""
    url = os.environ.get("GRAFANA_URL", "").strip().rstrip("/")
    key = os.environ.get("GRAFANA_API_KEY") or os.environ.get("GRAFANA_TOKEN", "")
    return url, (key or "").strip()


def _grafana_push_dashboard(grafana_url, api_key, graph, dashboard_uid, dashboard_title, folder_uid, infinity_uid, debug=False):
    """
    Создать или обновить дашборд в Grafana с одной панелью Node graph.
    Данные встраиваются как inline CSV (формат из документации Infinity для Node graph).
    """
    try:
        import requests
    except ImportError:
        return "для --grafana-api нужен модуль requests (pip install requests)"
    if not grafana_url or not api_key:
        return "задайте GRAFANA_URL и GRAFANA_API_KEY (или GRAFANA_TOKEN)"
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
                print("Grafana: найден дашборд id={} version->{}".format(dash_id, version), file=sys.stderr)
    except requests.RequestException:
        pass

    nodes_csv, edges_csv = _graph_to_inline_csv(graph)
    ds_uid = infinity_uid or "infinity"
    # Infinity Node graph: type csv, source inline, format + явные columns (без них плагин может не отдать фреймы)
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
    # Подгрузить .env при наличии (pip install python-dotenv)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    parser = argparse.ArgumentParser(
        description="JSON для Grafana Node graph: узлы (хосты, провайдеры) и рёбра (линки), данные из Zabbix."
    )
    parser.add_argument("-f", "--file", default=DEFAULT_INPUT, help="Путь к dry-ssh.json")
    parser.add_argument("-m", "--description-map", default=DESCRIPTION_MAP_FILE, help="Файл description_to_name.json")
    parser.add_argument("--zabbix", action="store_true", help="Запросить Zabbix: хосты и items (In/Out в Grafana — из datasource Zabbix)")
    parser.add_argument("-o", "--output", metavar="FILE", help="Файл для JSON (по умолчанию stdout)")
    parser.add_argument("--grafana-api", action="store_true", help="Создать/обновить дашборд с Node graph через Grafana API (нужны GRAFANA_URL, GRAFANA_API_KEY)")
    parser.add_argument("--dashboard-uid", default="uplinks", help="UID дашборда (по умолчанию uplinks)")
    parser.add_argument("--dashboard-title", default="Uplinks", help="Название дашборда")
    parser.add_argument("--folder-uid", default="", help="UID папки в Grafana (пусто — общая папка)")
    parser.add_argument("--infinity-uid", default="", help="UID datasource Infinity (по умолчанию из GRAFANA_INFINITY_UID или infinity)")
    parser.add_argument("--no-cache", action="store_true", help="Не использовать кэш Zabbix")
    parser.add_argument("--debug", action="store_true", help="Отладочный вывод")
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
            print("Задайте ZABBIX_URL и ZABBIX_TOKEN", file=sys.stderr)
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
        print("Нет рёбер для графа (нет хостов в Zabbix или пустой devices)", file=sys.stderr)
        sys.exit(1)

    # Узлы: хосты + провайдеры
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

    # Рёбра: id, source, target; detail__* для Data link и для запросов к Zabbix в Grafana (In/Out — из Zabbix DS)
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
            print("Записано: {} (узлов: {}, рёбер: {})".format(args.output, len(nodes), len(edges_out)), file=sys.stderr)
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
            print("Дашборд создан/обновлён: {} (uid={})".format(args.dashboard_title, args.dashboard_uid), file=sys.stderr)


if __name__ == "__main__":
    main()

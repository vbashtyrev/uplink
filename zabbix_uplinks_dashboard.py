#!/usr/bin/env python3
"""
Создание или обновление дашборда в Zabbix с виджетами-графиками по uplink-интерфейсам.
Данные из dry-ssh.json; itemid In/Out — из Zabbix API (та же логика и кэш, что в zabbix_map).
Каждый виджет — график входящего и исходящего трафика по одному интерфейсу (Bits received / Bits sent).
"""

import argparse
import json
import os
import sys

from zabbix_map import (
    DEFAULT_INPUT,
    DESCRIPTION_MAP_FILE,
    ZABBIX_CACHE_FILE,
    load_devices_json,
    load_description_map,
    load_zabbix_cache,
    save_zabbix_cache,
    fetch_zabbix_hosts_and_items,
    zabbix_request,
    _normalize_interface_name,
    _get_zabbix_url_token,
)


def _build_edges(devices, host_id_by_name, items_by_host_iface, desc_to_name):
    """Одно ребро на (host, ISP), приоритет как в zabbix_map. Возврат списка (hostname, hostid, iface_name, isp, itemid_in, itemid_out, ...)."""
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
            has_items = bool(itemid_in or itemid_out)
            is_logical = bool(iface.get("isLogical"))
            is_aggregate = bool(iface.get("isLag"))
            edges_raw.append((
                hostname, str(hostid), iface_name, isp, itemid_in, itemid_out,
                has_items, is_logical, is_aggregate,
            ))

    def _edge_priority(e):
        _, _, _, _, _, _, has_items, is_logical, is_aggregate = e
        return (has_items, is_logical, not is_aggregate)

    seen = {}
    for e in edges_raw:
        key = (e[0], e[1], e[3])
        if key not in seen or _edge_priority(e) > _edge_priority(seen[key]):
            seen[key] = e
    return sorted(seen.values(), key=lambda x: (x[0], x[3], x[2]))


def _item_pattern_escape(name):
    """Экранировать спецсимволы для паттерна Zabbix (* — wildcard, остальное буквально)."""
    for char in ["*", "?", "\\", "[", "]"]:
        name = name.replace(char, "\\" + char)
    return name


def _make_graph_widget(index, hostname, iface_name, isp, itemid_in, itemid_out, x, y, width=18, height=5, show_threshold=True):
    """Виджет svggraph: Item patterns (host + шаблон по интерфейсу), data_set_label даёт короткие подписи «Bits received»/«Bits sent».
    При show_threshold включается Simple triggers — линия порога рисуется пунктиром (простой триггер создаётся zabbix_sync_commit_rate.py)."""
    ref = "W{:04d}".format(index)[:5]
    title = "{} - {} ({})".format(hostname, iface_name, isp or "—").strip()
    fields = [
        {"type": 1, "name": "reference", "value": ref},
        {"type": 0, "name": "legend_statistic", "value": 1},
        {"type": 0, "name": "legend_lines", "value": 2},
    ]
    if show_threshold:
        fields.append({"type": 0, "name": "simple_triggers", "value": 1})
    colors = ["1A7F37", "E02F44"]
    iface_escaped = _item_pattern_escape(iface_name)
    num_ds = 0
    if itemid_in:
        fields.extend([
            {"type": 0, "name": "ds.0.dataset_type", "value": 1},
            {"type": 1, "name": "ds.0.hosts.0", "value": hostname},
            {"type": 1, "name": "ds.0.items.0", "value": "*{}*Bits received*".format(iface_escaped)},
            {"type": 1, "name": "ds.0.color", "value": colors[0]},
            {"type": 1, "name": "ds.0.data_set_label", "value": "Bits received"},
            {"type": 0, "name": "ds.0.width", "value": 2},
            {"type": 0, "name": "ds.0.transparency", "value": 5},
            {"type": 0, "name": "ds.0.fill", "value": 3},
        ])
        num_ds += 1
    if itemid_out:
        ds_idx = num_ds
        fields.extend([
            {"type": 0, "name": "ds.{}.dataset_type".format(ds_idx), "value": 1},
            {"type": 1, "name": "ds.{}.hosts.0".format(ds_idx), "value": hostname},
            {"type": 1, "name": "ds.{}.items.0".format(ds_idx), "value": "*{}*Bits sent*".format(iface_escaped)},
            {"type": 1, "name": "ds.{}.color".format(ds_idx), "value": colors[1]},
            {"type": 1, "name": "ds.{}.data_set_label".format(ds_idx), "value": "Bits sent"},
            {"type": 0, "name": "ds.{}.width".format(ds_idx), "value": 2},
            {"type": 0, "name": "ds.{}.transparency".format(ds_idx), "value": 5},
            {"type": 0, "name": "ds.{}.fill".format(ds_idx), "value": 3},
        ])
        num_ds += 1
    # Линия порога рисуется через Simple trigger (простой триггер max(bits_in,5m)>{$IF.UTIL.MAX:"..."} создаётся zabbix_sync_commit_rate.py)
    if not itemid_in and not itemid_out:
        return None
    return {
        "type": "svggraph",
        "name": title,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "view_mode": 0,
        "fields": fields,
    }


def _location_from_hostname(hostname):
    """Локация из hostname: только первые буквы до первого дефиса (DFW-DR-7280QR-1 -> DFW, ALA-KZT-7280TR-1 -> ALA)."""
    parts = hostname.split("-")
    if parts and parts[0]:
        return parts[0]
    return hostname or "other"


def create_or_update_dashboard(url, token, edges, dashboard_name, debug=False, show_threshold=True):
    """Создать или обновить дашборд. Одна строка — одна локация; графики локации делят ширину строки. X в пределах 0–71."""
    widgets = []
    widget_h = 5
    row_max_width = 72
    by_location = {}
    for edge in edges:
        hostname = edge[0]
        loc = _location_from_hostname(hostname)
        by_location.setdefault(loc, []).append(edge)
    for loc in sorted(by_location.keys()):
        by_location[loc] = sorted(by_location[loc], key=lambda e: (e[0], e[3], e[2]))
    widget_index = 0
    for row_idx, loc in enumerate(sorted(by_location.keys())):
        loc_edges = by_location[loc]
        n = len(loc_edges)
        if n == 0:
            continue
        y = row_idx * widget_h
        width = row_max_width // n
        for col, edge in enumerate(loc_edges):
            hostname, _hid, iface_name, isp, itemid_in, itemid_out = edge[:6]
            x = col * width
            if x >= row_max_width:
                x = row_max_width - width
            wg = _make_graph_widget(
                widget_index, hostname, iface_name, isp, itemid_in, itemid_out,
                x, y, width=width, height=widget_h, show_threshold=show_threshold,
            )
            if wg:
                widgets.append(wg)
                widget_index += 1

    if not widgets:
        return None, "нет ни одного интерфейса с item In/Out в Zabbix"

    page = {"widgets": widgets}
    existing, err = zabbix_request(url, token, "dashboard.get", {
        "filter": {"name": dashboard_name},
        "output": ["dashboardid", "name"],
        "selectPages": "extend",
    }, debug=debug)
    if err:
        return None, "dashboard.get: {}".format(err)

    if existing:
        dashboardid = existing[0]["dashboardid"]
        result, err = zabbix_request(url, token, "dashboard.update", {
            "dashboardid": dashboardid,
            "name": dashboard_name,
            "pages": [page],
        }, debug=debug)
        if err:
            return None, "dashboard.update: {}".format(err)
        if debug:
            print("Дашборд обновлён: {} (id={}, виджетов: {})".format(dashboard_name, dashboardid, len(widgets)), file=sys.stderr)
        return dashboardid, None
    else:
        result, err = zabbix_request(url, token, "dashboard.create", {
            "name": dashboard_name,
            "display_period": 30,
            "auto_start": 1,
            "pages": [page],
        }, debug=debug)
        if err:
            return None, "dashboard.create: {}".format(err)
        dashboardid = result["dashboardids"][0]
        if debug:
            print("Дашборд создан: {} (id={}, виджетов: {})".format(dashboard_name, dashboardid, len(widgets)), file=sys.stderr)
        return dashboardid, None


def create_dashboard_by_location(url, token, edges, dashboard_name, debug=False, show_threshold=True):
    """Создать/обновить дашборд с одной страницей на локацию (те же графики, разбиты по вкладкам)."""
    widget_h = 5
    row_max_width = 72
    by_location = {}
    for edge in edges:
        hostname = edge[0]
        loc = _location_from_hostname(hostname)
        by_location.setdefault(loc, []).append(edge)
    for loc in sorted(by_location.keys()):
        by_location[loc] = sorted(by_location[loc], key=lambda e: (e[0], e[3], e[2]))

    pages = []
    widget_index = 0
    for loc in sorted(by_location.keys()):
        loc_edges = by_location[loc]
        if not loc_edges:
            continue
        page_widgets = []
        for row_idx, edge in enumerate(loc_edges):
            hostname, _hid, iface_name, isp, itemid_in, itemid_out = edge[:6]
            y = row_idx * widget_h
            wg = _make_graph_widget(
                widget_index, hostname, iface_name, isp, itemid_in, itemid_out,
                0, y, width=row_max_width, height=widget_h, show_threshold=show_threshold,
            )
            if wg:
                page_widgets.append(wg)
                widget_index += 1
        if page_widgets:
            pages.append({"name": loc, "widgets": page_widgets})

    if not pages:
        return None, "нет ни одного интерфейса с item In/Out в Zabbix"

    existing, err = zabbix_request(url, token, "dashboard.get", {
        "filter": {"name": dashboard_name},
        "output": ["dashboardid", "name"],
        "selectPages": "extend",
    }, debug=debug)
    if err:
        return None, "dashboard.get: {}".format(err)

    total_widgets = sum(len(p["widgets"]) for p in pages)
    if existing:
        dashboardid = existing[0]["dashboardid"]
        result, err = zabbix_request(url, token, "dashboard.update", {
            "dashboardid": dashboardid,
            "name": dashboard_name,
            "display_period": 10,
            "pages": pages,
        }, debug=debug)
        if err:
            return None, "dashboard.update: {}".format(err)
        if debug:
            print("Дашборд (по локациям) обновлён: {} (id={}, страниц: {}, виджетов: {})".format(
                dashboard_name, dashboardid, len(pages), total_widgets), file=sys.stderr)
        return dashboardid, None
    else:
        result, err = zabbix_request(url, token, "dashboard.create", {
            "name": dashboard_name,
            "display_period": 10,
            "auto_start": 1,
            "pages": pages,
        }, debug=debug)
        if err:
            return None, "dashboard.create: {}".format(err)
        dashboardid = result["dashboardids"][0]
        if debug:
            print("Дашборд (по локациям) создан: {} (id={}, страниц: {}, виджетов: {})".format(
                dashboard_name, dashboardid, len(pages), total_widgets), file=sys.stderr)
        return dashboardid, None


def main():
    parser = argparse.ArgumentParser(
        description="Создать/обновить дашборд Zabbix с графиками In/Out по uplink из dry-ssh.json.",
    )
    parser.add_argument("-f", "--file", default=DEFAULT_INPUT, help="Путь к dry-ssh.json")
    parser.add_argument("-m", "--description-map", default=DESCRIPTION_MAP_FILE, help="Файл description_to_name.json")
    parser.add_argument("--dashboard-name", default="Uplinks", help="Название основного дашборда в Zabbix")
    parser.add_argument("--dashboard-by-location", default="Uplinks (по локациям)", metavar="NAME",
                        help="Создать второй дашборд с графиками по страницам (одна страница = одна локация). Пустая строка — не создавать")
    parser.add_argument("--no-cache", action="store_true", help="Не использовать кэш Zabbix")
    parser.add_argument("--no-show-threshold", action="store_true",
                        help="Не рисовать пороги триггеров (Simple triggers) на графиках")
    parser.add_argument("--debug", action="store_true", help="Отладочный вывод")
    args = parser.parse_args()
    show_threshold = not args.no_show_threshold

    data, err = load_devices_json(args.file)
    if err:
        print(err, file=sys.stderr)
        sys.exit(1)
    devices = data["devices"]
    desc_to_name = load_description_map(args.description_map)

    url, token = _get_zabbix_url_token()
    if not url:
        print("Задайте ZABBIX_URL и ZABBIX_TOKEN", file=sys.stderr)
        sys.exit(1)

    hostnames = set(devices.keys())
    cache_path = os.path.join(
        os.path.dirname(os.path.abspath(args.file)) if args.file else ".",
        ZABBIX_CACHE_FILE,
    )
    host_id_by_name = {}
    items_by_host_iface = {}
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

    edges = _build_edges(devices, host_id_by_name, items_by_host_iface, desc_to_name)
    if not edges:
        print("Нет данных для дашборда (нет хостов в Zabbix или uplink без items)", file=sys.stderr)
        sys.exit(1)

    dashboardid, err = create_or_update_dashboard(
        url, token, edges, args.dashboard_name, debug=args.debug, show_threshold=show_threshold
    )
    if err:
        print(err, file=sys.stderr)
        sys.exit(1)
    print("OK: дашборд «{}» (id={})".format(args.dashboard_name, dashboardid))

    if args.dashboard_by_location:
        dashboardid2, err2 = create_dashboard_by_location(
            url, token, edges, args.dashboard_by_location, debug=args.debug, show_threshold=show_threshold
        )
        if err2:
            print(err2, file=sys.stderr)
            sys.exit(1)
        print("OK: дашборд «{}» (id={})".format(args.dashboard_by_location, dashboardid2))


if __name__ == "__main__":
    main()

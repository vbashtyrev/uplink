#!/usr/bin/env python3
"""Create or update Zabbix dashboards with uplink traffic widgets (per-link, per-location, per-provider)."""

import argparse
import json
import os
import sys

import pynetbox

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
    zabbix_request,
    _normalize_interface_name,
    _get_zabbix_url_token,
)
from uplinks_config import (
    DASHBOARD_NAME,
    DASHBOARD_NAME_BY_LOCATION,
    DASHBOARD_NAME_BY_PROVIDER,
    NETBOX_AUTOMATION_TAG,
    PROVIDERS_FOR_SUMMARY,
    UPLINKS_AGGREGATE_HOST_PREFIX,
)

load_env_file_if_present()

# Calculated item keys on aggregate hosts `Uplinks {Provider}` (must match zabbix_provider_aggregate)
AGGREGATE_ITEM_KEY_IN = "aggregate.bits.in[]"
AGGREGATE_ITEM_KEY_OUT = "aggregate.bits.out[]"


def _get_providers_from_netbox(tag, debug=False):
    """Return provider names from NetBox by tag or [] on error."""
    url = os.environ.get("NETBOX_URL", "").strip()
    token = os.environ.get("NETBOX_TOKEN", "").strip()
    if not url or not token:
        if debug:
            print("NetBox: NETBOX_URL/NETBOX_TOKEN не заданы — провайдеры только из конфига", file=sys.stderr)
        return []
    try:
        nb = pynetbox.api(url, token=token)
        providers = list(nb.circuits.providers.filter(tag=tag))
        names = [p.name for p in providers if getattr(p, "name", None)]
        if debug and names:
            print("NetBox: провайдеры с тегом {}: {}".format(tag, ", ".join(names)), file=sys.stderr)
        return names
    except Exception as e:
        if debug:
            print("NetBox: не удалось получить провайдеров ({}): {}".format(tag, e), file=sys.stderr)
        return []


def _build_edges(devices, host_id_by_name, items_by_host_iface, desc_to_name):
    """Build per-(host, provider) edge list similar to zabbix_map."""
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
    """Escape special characters for Zabbix item pattern (keep * as wildcard)."""
    for char in ["*", "?", "\\", "[", "]"]:
        name = name.replace(char, "\\" + char)
    return name


def _make_graph_widget(index, hostname, iface_name, isp, itemid_in, itemid_out, x, y, width=18, height=5, show_threshold=True):
    """Build svggraph widget for one uplink (Bits received/sent, optional threshold line)."""
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
    # Линия порога рисуется через Simple trigger (простой триггер max(bits_in,<period>)>{$IF.UTIL.MAX:"..."} создаётся zabbix_sync_commit_rate.py; период в uplinks_config: TRIGGER_FUNCTION_PERIOD)
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
    """Location from hostname: prefix before first dash (SITE-CORE-ROUTER-1 -> SITE)."""
    parts = hostname.split("-")
    if parts and parts[0]:
        return parts[0]
    return hostname or "other"


def create_or_update_dashboard(url, token, edges, dashboard_name, debug=False, show_threshold=True):
    """Create or update dashboard; each row is a location, widgets share width."""
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


def _get_aggregate_itemids(url, token, providers, debug=False):
    """Для каждого провайдера из списка получить itemid calculated items на хосте «Uplinks {Provider}».
    Возврат: dict provider -> (itemid_in или None, itemid_out или None)."""
    if not providers:
        return {}
    host_names = [UPLINKS_AGGREGATE_HOST_PREFIX + p for p in providers]
    out = {p: (None, None) for p in providers}

    # Сначала ищем агрегатные хосты по technical host, затем по visible name (как в карте),
    # чтобы покрыть случаи, когда host и name отличаются.
    hostname_to_id = {}
    hosts, err = zabbix_request(
        url,
        token,
        "host.get",
        {
            "output": ["hostid", "host", "name"],
            "filter": {"host": host_names},
        },
        debug=debug,
    )
    if not err:
        for h in hosts or []:
            hostname_to_id[h.get("host") or ""] = h.get("hostid")

    missing = [p for p in providers if UPLINKS_AGGREGATE_HOST_PREFIX + p not in hostname_to_id]
    if missing:
        names_filter = [UPLINKS_AGGREGATE_HOST_PREFIX + p for p in missing]
        hosts2, err2 = zabbix_request(
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
            for h in hosts2 or []:
                hostname_to_id[h.get("name") or ""] = h.get("hostid")

    id_to_provider = {}
    for p in providers:
        wanted = UPLINKS_AGGREGATE_HOST_PREFIX + p
        hid = hostname_to_id.get(wanted)
        if hid:
            id_to_provider[str(hid)] = p

    for hostid, isp in id_to_provider.items():
        items, err = zabbix_request(url, token, "item.get", {
            "output": ["itemid", "key_"],
            "hostids": [hostid],
            "search": {"key_": "aggregate.bits"},
        }, debug=debug)
        if err or not items:
            continue
        itemid_in = itemid_out = None
        for it in items:
            key = (it.get("key_") or "").strip()
            if key == AGGREGATE_ITEM_KEY_IN:
                itemid_in = it.get("itemid")
            elif key == AGGREGATE_ITEM_KEY_OUT:
                itemid_out = it.get("itemid")
        out[isp] = (itemid_in, itemid_out)
    return out


def create_dashboard_by_provider(
    url, token, edges, dashboard_name, providers_filter, debug=False, show_threshold=True
):
    """Создать/обновить дашборд с одной страницей на провайдера (только провайдеры из списка с >1 линком).

    На каждой странице:
    - Bits received (summary) и Bits sent (summary) — стеки по линкам провайдера;
    - при наличии хостов «Uplinks {Provider}» — виджеты Total Bits received/sent (aggregate)
      по calculated items aggregate.bits.in[] / aggregate.bits.out[]."""
    widget_h = 6
    row_max_width = 72
    by_provider = {}
    for edge in edges:
        isp = (edge[3] or "").strip()
        if not isp:
            continue
        by_provider.setdefault(isp, []).append(edge)
    # Только провайдеры из списка, независимо от числа линков (даже один линк = своя вкладка).
    providers_ok = [
        isp for isp in (p.strip() for p in providers_filter if p and p.strip())
        if by_provider.get(isp)
    ]
    for isp in providers_ok:
        by_provider[isp] = sorted(by_provider[isp], key=lambda e: (e[0], e[2]))

    aggregate_itemids = _get_aggregate_itemids(url, token, providers_ok, debug=debug)

    pages = []
    for isp in sorted(providers_ok):
        prov_edges = by_provider[isp]
        if not prov_edges:
            continue
        widgets = []

        # Разделяем линки по наличию In/Out
        in_edges = [e for e in prov_edges if e[4]]
        out_edges = [e for e in prov_edges if e[5]]

        def _make_summary_graph(kind, edges_list, y):
            """Собрать svggraph с одним data set (Item list) и несколькими itemids (по одному на линк).

            Используем itemids напрямую (а не паттерны), чтобы в график попадали
            только те items, которые мы выбрали как uplink (Bits received/sent),
            без посторонних item'ов по тому же интерфейсу."""
            if not edges_list:
                return None
            ref = "P{}_{}".format(kind, isp)[:20]
            title = "{} - Bits {} (summary)".format(isp, "received" if kind == "in" else "sent")
            fields = [
                {"type": 1, "name": "reference", "value": ref},
                {"type": 0, "name": "legend", "value": 1},
                {"type": 0, "name": "legend_statistic", "value": 1},
                {"type": 0, "name": "simple_triggers", "value": 1},  # линия порога 90%/100% по провайдеру
            ]
            # Сводный график: линия порога — по агрегатному триггеру провайдера (Uplinks {Provider}).
            # Один data set (ds.0) в режиме Item list: несколько конкретных itemids,
            # Zabbix стекает их (stacked=1), давая суммарную кривую по провайдеру.
            colors = [
                "1A7F37", "E02F44", "0066CC", "CC8800", "9900CC",
                "008B8B", "DC143C", "228B22", "483D8B", "FF1493",
            ]
            fields.extend([
                {"type": 0, "name": "ds.0.dataset_type", "value": 0},  # Item list
                {"type": 0, "name": "ds.0.width", "value": 1},
                {"type": 0, "name": "ds.0.transparency", "value": 5},
                {"type": 0, "name": "ds.0.fill", "value": 3},
                {"type": 0, "name": "ds.0.stacked", "value": 1},
            ])
            host_item_idx = 0
            for idx, edge in enumerate(edges_list):
                hostname, _hid, iface_name, _isp, itemid_in, itemid_out = edge[:6]
                if kind == "in" and not itemid_in:
                    continue
                if kind == "out" and not itemid_out:
                    continue
                # Добавляем конкретный itemid uplink'а
                item_id = itemid_in if kind == "in" else itemid_out
                try:
                    item_id_int = int(item_id)
                except (TypeError, ValueError):
                    continue
                color = colors[idx % len(colors)]
                fields.extend([
                    {"type": 4, "name": "ds.0.itemids.{}".format(host_item_idx), "value": item_id_int},
                    {"type": 1, "name": "ds.0.color.{}".format(host_item_idx), "value": color},
                ])
                host_item_idx += 1
            if host_item_idx == 0:
                return None
            # Легенда: фиксированный режим и число строк = числу линов провайдера (но не более 10)
            fields.extend([
                {"type": 0, "name": "legend_lines_mode", "value": 0},  # Fixed
                {"type": 0, "name": "legend_lines", "value": min(host_item_idx, 10)},
            ])
            return {
                "type": "svggraph",
                "name": title,
                "x": 0,
                "y": y,
                "width": row_max_width,
                "height": widget_h,
                "view_mode": 0,
                "fields": fields,
            }

        # Сначала виджеты суммарного трафика (aggregate) — сверху
        agg_in_id, agg_out_id = aggregate_itemids.get(isp, (None, None))
        y_agg = 0
        for kind, item_id, label in [
            ("in", agg_in_id, "Total Bits received (aggregate)"),
            ("out", agg_out_id, "Total Bits sent (aggregate)"),
        ]:
            if not item_id:
                continue
            try:
                item_id_int = int(item_id)
            except (TypeError, ValueError):
                continue
            title_agg = "{} - {}".format(isp, label)
            ref_agg = "A{}_{}".format(kind[:1], isp)[:20]
            agg_fields = [
                {"type": 1, "name": "reference", "value": ref_agg},
                {"type": 0, "name": "legend_statistic", "value": 1},
                {"type": 0, "name": "legend_lines", "value": 1},
                {"type": 0, "name": "simple_triggers", "value": 1},  # линия порога 90%/100% по _provider_limits
                {"type": 0, "name": "ds.0.dataset_type", "value": 0},
                {"type": 4, "name": "ds.0.itemids.0", "value": item_id_int},
                {"type": 1, "name": "ds.0.color.0", "value": "1A7F37" if kind == "in" else "E02F44"},
                {"type": 0, "name": "ds.0.width", "value": 2},
                {"type": 0, "name": "ds.0.transparency", "value": 5},
                {"type": 0, "name": "ds.0.fill", "value": 3},
            ]
            widgets.append({
                "type": "svggraph",
                "name": title_agg,
                "x": 0,
                "y": y_agg,
                "width": row_max_width,
                "height": widget_h,
                "view_mode": 0,
                "fields": agg_fields,
            })
            y_agg += widget_h

        # Ниже — стеки по линкам (summary)
        g_in = _make_summary_graph("in", in_edges, y=y_agg)
        g_out = _make_summary_graph("out", out_edges, y=y_agg + widget_h)
        for g in (g_in, g_out):
            if g:
                widgets.append(g)

        if widgets:
            pages.append({"name": isp, "widgets": widgets})

    if not pages:
        return None, "нет провайдеров с более чем одним линком (проверьте PROVIDERS_FOR_SUMMARY и данные)"

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
            print("Дашборд (по провайдерам) обновлён: {} (id={}, страниц: {}, виджетов: {})".format(
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
            print("Дашборд (по провайдерам) создан: {} (id={}, страниц: {}, виджетов: {})".format(
                dashboard_name, dashboardid, len(pages), total_widgets), file=sys.stderr)
        return dashboardid, None


def main():
    parser = argparse.ArgumentParser(
        description="Создать/обновить дашборд Zabbix с графиками In/Out по uplink из dry-ssh.json.",
    )
    parser.add_argument("-f", "--file", default=DEFAULT_INPUT, help="Путь к dry-ssh.json")
    parser.add_argument("-m", "--description-map", default=DESCRIPTION_MAP_FILE, help="Файл description_to_name.json")
    parser.add_argument("--dashboard-name", default=DASHBOARD_NAME, help="Название основного дашборда в Zabbix")
    parser.add_argument("--dashboard-by-location", default=DASHBOARD_NAME_BY_LOCATION, metavar="NAME",
                        help="Создать второй дашборд с графиками по страницам (одна страница = одна локация). Пустая строка — не создавать")
    parser.add_argument("--dashboard-by-provider", default=DASHBOARD_NAME_BY_PROVIDER, metavar="NAME",
                        help="Сводный дашборд по провайдерам с >1 линком (вкладка = провайдер). Пустая строка — не создавать")
    parser.add_argument("--providers", nargs="*", default=None, metavar="NAME",
                        help="Провайдеры для сводного дашборда (по умолчанию из uplinks_config: Cogent, HE)")
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

    if args.dashboard_by_provider.strip():
        if args.providers is not None:
            providers_filter = args.providers
        else:
            # Конфиг + провайдеры из NetBox с тегом automatization (без дубликатов, порядок: конфиг, затем NetBox)
            from_netbox = _get_providers_from_netbox(NETBOX_AUTOMATION_TAG, debug=args.debug)
            seen = set()
            providers_filter = []
            for p in list(PROVIDERS_FOR_SUMMARY) + from_netbox:
                name = (p or "").strip()
                if name and name not in seen:
                    seen.add(name)
                    providers_filter.append(name)
        dashboardid3, err3 = create_dashboard_by_provider(
            url, token, edges, args.dashboard_by_provider.strip(), providers_filter,
            debug=args.debug, show_threshold=show_threshold,
        )
        if err3:
            print(err3, file=sys.stderr)
            sys.exit(1)
        print("OK: дашборд «{}» (id={})".format(args.dashboard_by_provider.strip(), dashboardid3))


if __name__ == "__main__":
    main()

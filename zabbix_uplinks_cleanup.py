#!/usr/bin/env python3
"""
Очистка артефактов автоматизации в Zabbix:
- простые триггеры 90% / 100%, созданные zabbix_sync_commit_rate.py;
- старые item'ы порога net.if.threshold["..."];
- карта uplinks ([test] uplinks);
- дашборды uplinks (основной и «по локациям») по умолчанию.

По возможности элементы помечаются тегом Zabbix (trigger tags): scripts=automatization.
Скрипт удаляет только объекты, созданные автоматизацией, и не трогает хосты, items трафика и макросы commit rate.

Переменные окружения: ZABBIX_URL, ZABBIX_TOKEN.
"""

import argparse
import os
import sys

from zabbix_map import (
    MAP_NAME,
    _get_zabbix_url_token,
    validate_zabbix_token,
    zabbix_request,
)

# Ключ старых item'ов порога (то же значение, что в zabbix_sync_commit_rate)
THRESHOLD_ITEM_KEY = "net.if.threshold"


TRIGGER_DESC_90_SUFFIX = "High bandwidth (90%)"
TRIGGER_DESC_100_SUFFIX = "High bandwidth (threshold line)"
TRIGGER_TAG_NAME = "scripts"
TRIGGER_TAG_VALUE = "automatization"


def _validate_zabbix(debug=False):
    url, token = _get_zabbix_url_token()
    if not url or not token:
        print("Задайте ZABBIX_URL и ZABBIX_TOKEN", file=sys.stderr)
        sys.exit(1)
    if not validate_zabbix_token(url, token, debug=debug):
        print("Неверный или просроченный ZABBIX_TOKEN", file=sys.stderr)
        sys.exit(1)
    return url, token


def cleanup_threshold_items(url, token, dry_run=False, debug=False):
    """
    Удалить все item'ы порога net.if.threshold[...] (исторический артефакт).
    """
    res, err = zabbix_request(
        url,
        token,
        "item.get",
        {"output": ["itemid", "hostid", "key_"], "search": {"key_": THRESHOLD_ITEM_KEY}},
        debug=debug,
    )
    if err or not res:
        return 0
    to_delete = []
    for it in res:
        key_ = it.get("key_") or ""
        if key_.startswith(THRESHOLD_ITEM_KEY) and it.get("itemid"):
            to_delete.append(str(it["itemid"]))
    if not to_delete:
        return 0
    if dry_run:
        print("dry-run: item.delete {} (net.if.threshold[...])".format(len(to_delete)))
        return len(to_delete)
    _, del_err = zabbix_request(url, token, "item.delete", to_delete, debug=debug)
    if del_err:
        print("item.delete error: {}".format(del_err), file=sys.stderr)
        return 0
    return len(to_delete)


def _has_our_tag(tags):
    """
    Проверить, есть ли среди тегов scripts:automatization.
    """
    for t in tags or []:
        if t.get("tag") == TRIGGER_TAG_NAME and t.get("value") == TRIGGER_TAG_VALUE:
            return True
    return False


def cleanup_triggers(url, token, dry_run=False, debug=False):
    """
    Удалить простые триггеры 90% / 100%, создаваемые zabbix_sync_commit_rate.py.
    Фильтр по описанию + тегу scripts:automatization (если задан).
    """
    res, err = zabbix_request(
        url,
        token,
        "trigger.get",
        {
            "output": ["triggerid", "description"],
            "search": {"description": "High bandwidth ("},
            "selectTags": "extend",
        },
        debug=debug,
    )
    if err or not res:
        return 0
    to_delete = []
    for t in res:
        desc = (t.get("description") or "").strip()
        if not (
            desc.endswith(TRIGGER_DESC_90_SUFFIX)
            or desc.endswith(TRIGGER_DESC_100_SUFFIX)
        ):
            continue
        tags = t.get("tags") or []
        # Если тег есть — использовать его как основной фильтр.
        if tags and not _has_our_tag(tags):
            continue
        tid = t.get("triggerid")
        if tid:
            to_delete.append(str(tid))
    if not to_delete:
        return 0
    if dry_run:
        print("dry-run: trigger.delete {} (uplinks 90%/100%)".format(len(to_delete)))
        return len(to_delete)
    _, del_err = zabbix_request(url, token, "trigger.delete", to_delete, debug=debug)
    if del_err:
        print("trigger.delete error: {}".format(del_err), file=sys.stderr)
        return 0
    return len(to_delete)


def cleanup_map(url, token, dry_run=False, debug=False):
    """
    Удалить карту uplinks, создаваемую zabbix_map.py (MAP_NAME).
    """
    res, err = zabbix_request(
        url,
        token,
        "map.get",
        {"output": ["sysmapid", "name"], "filter": {"name": MAP_NAME}},
        debug=debug,
    )
    if err or not res:
        return 0
    ids = [m["sysmapid"] for m in res if m.get("sysmapid")]
    if not ids:
        return 0
    if dry_run:
        print("dry-run: map.delete {} ({})".format(len(ids), MAP_NAME))
        return len(ids)
    _, del_err = zabbix_request(url, token, "map.delete", ids, debug=debug)
    if del_err:
        print("map.delete error: {}".format(del_err), file=sys.stderr)
        return 0
    return len(ids)


def cleanup_dashboards(url, token, names, dry_run=False, debug=False):
    """
    Удалить дашборды по именам (основной uplinks и по локациям).
    """
    if not names:
        return 0
    res, err = zabbix_request(
        url,
        token,
        "dashboard.get",
        {"output": ["dashboardid", "name"], "filter": {"name": list(names)}},
        debug=debug,
    )
    if err or not res:
        return 0
    ids = [d["dashboardid"] for d in res if d.get("dashboardid")]
    if not ids:
        return 0
    if dry_run:
        print(
            "dry-run: dashboard.delete {} ({})".format(
                len(ids), ", ".join(sorted(set(d.get("name", "") for d in res)))
            )
        )
        return len(ids)
    _, del_err = zabbix_request(url, token, "dashboard.delete", ids, debug=debug)
    if del_err:
        print("dashboard.delete error: {}".format(del_err), file=sys.stderr)
        return 0
    return len(ids)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Очистить артефакты uplinks-автоматизации в Zabbix "
            "(триггеры 90%/100%, старые net.if.threshold, карта и дашборды uplinks)."
        )
    )
    parser.add_argument(
        "--dashboard-name",
        default="Uplinks",
        help="Имя основного дашборда uplinks (как в zabbix_uplinks_dashboard.py)",
    )
    parser.add_argument(
        "--dashboard-by-location",
        default="Uplinks (по локациям)",
        help="Имя дашборда по локациям (пустая строка — не трогать)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Только показать, что будет удалено (без изменений в Zabbix)",
    )
    parser.add_argument(
        "--debug", action="store_true", help="Отладочный вывод запросов к Zabbix API"
    )
    args = parser.parse_args()

    url, token = _validate_zabbix(debug=args.debug)

    total_items = cleanup_threshold_items(
        url, token, dry_run=args.dry_run, debug=args.debug
    )
    total_triggers = cleanup_triggers(
        url, token, dry_run=args.dry_run, debug=args.debug
    )
    total_maps = cleanup_map(url, token, dry_run=args.dry_run, debug=args.debug)

    dash_names = {args.dashboard_name}
    if args.dashboard_by_location.strip():
        dash_names.add(args.dashboard_by_location.strip())
    total_dash = cleanup_dashboards(
        url, token, dash_names, dry_run=args.dry_run, debug=args.debug
    )

    mode = "dry-run" if args.dry_run else "выполнено"
    print(
        "{}: удалено item'ов порога: {}, триггеров: {}, карт: {}, дашбордов: {}".format(
            mode, total_items, total_triggers, total_maps, total_dash
        )
    )


if __name__ == "__main__":
    main()


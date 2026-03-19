#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Create/update aggregate provider hosts in Zabbix (`Uplinks {Provider}`) with
calculated items (sum Bits in/out over all links) and optional 90%/100% limit triggers."""

import json
import os
import sys

import pynetbox

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
    validate_zabbix_token,
)
from uplinks_config import (
    THRESHOLD_PERCENT_WARN,
    THRESHOLD_PERCENT_HIGH,
    TRIGGER_FUNCTION_PERIOD,
    TRIGGER_TAG_NAME,
    TRIGGER_TAG_VALUE,
    UPLINKS_AGGREGATE_HOST_PREFIX,
    UPLINKS_AGGREGATE_GROUP,
    NETBOX_AUTOMATION_TAG,
)

DEFAULT_COMMIT_RATES = "commit_rates.json"
CALCULATED_ITEM_KEY_IN = "aggregate.bits.in[]"
CALCULATED_ITEM_KEY_OUT = "aggregate.bits.out[]"
CALCULATED_ITEM_TYPE = 15
VALUE_TYPE_NUMERIC = 3  # unsigned
# Единицы для агрегатных item'ов: биты в секунду (bps), чтобы ось графиков и пороги были в Gbps.
UNITS_BPS = "bps"


def _get_providers_from_netbox(tag, debug=False):
    """Провайдеры из NetBox с тегом automatization. Возврат списка имён или [] при ошибке/нет доступа."""
    url = os.environ.get("NETBOX_URL", "").strip()
    token = os.environ.get("NETBOX_TOKEN", "").strip()
    if not url or not token:
        if debug:
            print(
                "NetBox: NETBOX_URL/NETBOX_TOKEN не заданы — агрегаты только по провайдерам из данных",
                file=sys.stderr,
            )
        return []
    try:
        nb = pynetbox.api(url, token=token)
        providers = list(nb.circuits.providers.filter(tag=tag))
        names = [p.name for p in providers if getattr(p, "name", None)]
        if debug and names:
            print(
                "NetBox: провайдеры с тегом {}: {}".format(tag, ", ".join(names)),
                file=sys.stderr,
            )
        return names
    except Exception as e:
        if debug:
            print(
                "NetBox: не удалось получить провайдеров ({}): {}".format(tag, e),
                file=sys.stderr,
            )
        return []


def _build_edges_with_keys(devices, host_id_by_name, items_by_host_iface, desc_to_name):
    """Одно ребро на (host, ISP), с key_in/key_out для формул. Возврат [(hostname, isp, key_in, key_out), ...]."""
    edges_raw = []
    for hostname in sorted(devices.keys()):
        if not host_id_by_name.get(hostname):
            continue
        for iface in devices[hostname]:
            iface_name = iface.get("name", "")
            description = iface.get("description", "")
            isp = desc_to_name.get(description, description)
            key_norm = _normalize_interface_name(iface_name)
            rec = items_by_host_iface.get((hostname, key_norm), {})
            key_in = rec.get("bits_in") or ""
            key_out = rec.get("bits_out") or ""
            has_items = bool(key_in or key_out)
            is_logical = bool(iface.get("isLogical"))
            is_aggregate = bool(iface.get("isLag"))
            edges_raw.append((hostname, isp, key_in, key_out, has_items, is_logical, is_aggregate))

    def _priority(e):
        _, _, _, _, has_items, is_logical, is_aggregate = e
        return (has_items, is_logical, not is_aggregate)

    seen = {}
    for e in edges_raw:
        key = (e[0], e[1])
        if key not in seen or _priority(e) > _priority(seen[key]):
            seen[key] = e
    return [(e[0], e[1], e[2], e[3]) for e in sorted(seen.values(), key=lambda x: (x[0], x[1]))]


def _sanitize_provider_name(name):
    """Return safe technical host name for provider (Zabbix host field)."""
    if not name:
        return ""
    # В Zabbix в host запрещены некоторые символы (например, слэш). Заменяем всё, кроме букв, цифр, ._- на пробел.
    import re

    cleaned = re.sub(r'[^A-Za-z0-9._-]+', ' ', name)
    cleaned = " ".join(cleaned.split())
    return cleaned or "provider"


def _get_or_create_host(url, token, host_name, group_name, debug=False):
    """Get or create aggregate host in a given group."""
    grp, err = zabbix_request(url, token, "hostgroup.get", {
        "output": ["groupid"],
        "filter": {"name": [group_name]},
    }, debug=debug)
    if err or not grp:
        return None, "группа не найдена: {}".format(group_name)
    groupid = grp[0]["groupid"]

    technical_host = _sanitize_provider_name(host_name)

    res, err = zabbix_request(url, token, "host.get", {
        "output": ["hostid", "host"],
        "filter": {"host": [technical_host]},
    }, debug=debug)
    if err:
        return None, err
    if res:
        return res[0]["hostid"], None

    # Создать хост (interfaces обязательны — создаём dummy agent на 127.0.0.1)
    result, err = zabbix_request(url, token, "host.create", {
        "host": technical_host,
        "name": host_name,
        "groups": [{"groupid": groupid}],
        "interfaces": [{"type": 1, "main": 1, "useip": 1, "ip": "127.0.0.1", "dns": "", "port": "10050"}],
    }, debug=debug)
    if err:
        return None, "host.create: {}".format(err)
    return result["hostids"][0], None


def _create_or_update_calculated_item(url, token, hostid, key, name, formula, debug=False):
    """Create or update calculated item with given formula."""
    res, err = zabbix_request(url, token, "item.get", {
        "output": ["itemid", "formula"],
        "hostids": [hostid],
        "search": {"key_": key},
    }, debug=debug)
    if err:
        return None, err
    params = {
        "name": name,
        "key_": key,
        "type": CALCULATED_ITEM_TYPE,
        "value_type": VALUE_TYPE_NUMERIC,
        "units": UNITS_BPS,
        "params": formula,
        "hostid": hostid,
        "delay": "1m",
    }
    if res:
        itemid = res[0]["itemid"]
        _, err = zabbix_request(url, token, "item.update", {
            "itemid": itemid,
            "params": formula,
            "name": name,
            "units": UNITS_BPS,
            "preprocessing": [],  # без preprocessing (формула уже в bps)
        }, debug=debug)
        return itemid, err
    result, err = zabbix_request(url, token, "item.create", params, debug=debug)
    if err:
        return None, err
    return result["itemids"][0], None


def _ensure_triggers(url, token, hostid, host_technical, provider, itemid_in, limit_bps, debug=False):
    """Create or update 90%/100% provider aggregate triggers for a host.
    Если лимит в _provider_limits изменился (например 20G -> 10G), старые триггеры с другим лимитом
    удаляются, чтобы не дублировать 90%/100% по разным порогам.
    """
    warn_bps = int(limit_bps * THRESHOLD_PERCENT_WARN / 100)
    desc_warn = "Provider aggregate traffic >= {}% of limit ({} Gbps)".format(THRESHOLD_PERCENT_WARN, limit_bps / 1e9)
    desc_high = "Provider aggregate traffic >= 100% of limit ({} Gbps)".format(limit_bps / 1e9)
    expr_warn = "max(/{}/{},{})>{}".format(
        host_technical, CALCULATED_ITEM_KEY_IN, TRIGGER_FUNCTION_PERIOD, warn_bps
    )
    expr_high = "max(/{}/{},{})>{}".format(
        host_technical, CALCULATED_ITEM_KEY_IN, TRIGGER_FUNCTION_PERIOD, int(limit_bps)
    )

    tags = [{"tag": TRIGGER_TAG_NAME, "value": TRIGGER_TAG_VALUE}]
    if provider:
        tags.append({"tag": "provider", "value": provider})

    res, err = zabbix_request(url, token, "trigger.get", {
        "output": ["triggerid", "description"],
        "hostids": [hostid],
        "search": {"description": "Provider aggregate"},
    }, debug=debug)
    if err:
        return err
    all_triggers = res or []
    # Разделяем по типу: 90% и 100% (по подстроке в описании)
    warn_triggers = [t for t in all_triggers if "90%" in t["description"]]
    high_triggers = [t for t in all_triggers if "100%" in t["description"]]

    def _update_or_create_and_cleanup(desc, expr, severity, same_type_list):
        """Обновить один триггер по точному описанию или создать; удалить остальные того же типа."""
        same_type_ids = [t["triggerid"] for t in same_type_list]
        by_desc = {t["description"]: t["triggerid"] for t in same_type_list}
        kept_id = by_desc.get(desc)
        if kept_id is not None:
            _, err = zabbix_request(url, token, "trigger.update", {
                "triggerid": kept_id,
                "description": desc,
                "expression": expr,
                "priority": severity,
                "tags": tags,
            }, debug=debug)
            if err:
                return err
        else:
            result, err = zabbix_request(url, token, "trigger.create", {
                "description": desc,
                "expression": expr,
                "priority": severity,
                "tags": tags,
            }, debug=debug)
            if err:
                return err
            kept_id = result["triggerids"][0]
        # Удалить лишние триггеры того же типа (старый лимит)
        for tid in same_type_ids:
            if tid != kept_id:
                _, err = zabbix_request(url, token, "trigger.delete", [tid], debug=debug)
                if err:
                    return err
        return None

    # Уровни важности: 90% — Information, 100% — Warning
    err = _update_or_create_and_cleanup(desc_warn, expr_warn, 1, warn_triggers)
    if err:
        return err
    err = _update_or_create_and_cleanup(desc_high, expr_high, 2, high_triggers)
    if err:
        return err
    return None


def run(url, token, commit_rates_path, dry_ssh_path, desc_map_path, cache_path, debug=False):
    """Create/update provider aggregate hosts with calculated items and limit triggers."""
    ok, err = validate_zabbix_token(url, token, debug=debug)
    if not ok:
        return None, "Ошибка авторизации в Zabbix (token): {}".format(err)
    with open(commit_rates_path, "r", encoding="utf-8") as f:
        cr = json.load(f)
    provider_limits = cr.get("_provider_limits") or {}
    if not isinstance(provider_limits, dict):
        provider_limits = {}

    data, err = load_devices_json(dry_ssh_path)
    if err:
        return None, err
    devices = data["devices"]
    desc_to_name = load_description_map(desc_map_path)

    hostnames = set(devices.keys())
    host_id_by_name = {}
    items_by_host_iface = {}
    if cache_path and os.path.isfile(cache_path):
        cached_h, cached_i = load_zabbix_cache(cache_path)
        if cached_h and cached_i and set(cached_h.keys()) >= hostnames:
            host_id_by_name = {k: cached_h[k] for k in hostnames if k in cached_h}
            items_by_host_iface = {(h, i): rec for (h, i), rec in cached_i.items() if h in host_id_by_name}
    if not host_id_by_name or not items_by_host_iface:
        host_id_by_name, items_by_host_iface, err = fetch_zabbix_hosts_and_items(
            url, token, hostnames, debug=debug
        )
        if err:
            return None, err
        if cache_path:
            save_zabbix_cache(cache_path, host_id_by_name, items_by_host_iface)

    edges = _build_edges_with_keys(devices, host_id_by_name, items_by_host_iface, desc_to_name)
    by_provider = {}
    for hostname, isp, key_in, key_out in edges:
        isp = (isp or "").strip()
        if not isp:
            continue
        by_provider.setdefault(isp, []).append((hostname, key_in, key_out))

    # Кандидаты провайдеров для агрегатов: в первую очередь из NetBox по тегу automatization,
    # иначе — из данных по линкам (by_provider).
    providers_from_nb = set(_get_providers_from_netbox(NETBOX_AUTOMATION_TAG, debug=debug))

    done = []
    providers_iter = sorted(providers_from_nb) if providers_from_nb else sorted(by_provider.keys())
    for provider in providers_iter:
        if not provider:
            continue
        # Лимит для триггеров — только если задан в _provider_limits; иначе создаём только host+items.
        limit_entry = provider_limits.get(provider)
        limit_bps = None
        if limit_entry is not None:
            try:
                limit_bps = float(limit_entry) * 1e9
            except (TypeError, ValueError):
                limit_bps = None
        links = by_provider.get(provider)
        if not links:
            if debug:
                print("Провайдер {} в _provider_limits, но линков в данных нет — пропуск.".format(provider), file=sys.stderr)
            continue
        refs_in = [(h, ki) for h, ki, ko in links if ki]
        refs_out = [(h, ko) for h, ki, ko in links if ko]
        if not refs_in:
            continue
        host_name = UPLINKS_AGGREGATE_HOST_PREFIX + provider
        hostid, err = _get_or_create_host(url, token, host_name, UPLINKS_AGGREGATE_GROUP, debug=debug)
        if err:
            return None, "{}: {}".format(provider, err)
        formula_in = "+".join("last(/{}/{})".format(h, k) for h, k in refs_in)
        formula_out = "+".join("last(/{}/{})".format(h, k) for h, k in refs_out) if refs_out else "0"
        _, err = _create_or_update_calculated_item(
            url, token, hostid, CALCULATED_ITEM_KEY_IN,
            "{} total Bits received".format(provider), formula_in, debug=debug
        )
        if err:
            return None, "{} item in: {}".format(provider, err)
        _, err = _create_or_update_calculated_item(
            url, token, hostid, CALCULATED_ITEM_KEY_OUT,
            "{} total Bits sent".format(provider), formula_out, debug=debug
        )
        if err:
            return None, "{} item out: {}".format(provider, err)
        if limit_bps is not None:
            technical_host = _sanitize_provider_name(host_name)
            err = _ensure_triggers(
                url, token, hostid, technical_host, provider, None, limit_bps, debug=debug
            )
            if err:
                return None, "{} triggers: {}".format(provider, err)
        done.append((provider, host_name))
    return done, None


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Создать хосты Uplinks {Provider} с суммарным трафиком и триггерами по _provider_limits.",
    )
    parser.add_argument("-f", "--commit-rates", default=DEFAULT_COMMIT_RATES, help="Путь к commit_rates.json")
    parser.add_argument("-d", "--dry-ssh", default=DEFAULT_INPUT, help="Путь к dry-ssh.json")
    parser.add_argument("-m", "--description-map", default=DESCRIPTION_MAP_FILE, help="Файл description_to_name.json")
    parser.add_argument("--no-cache", action="store_true", help="Не использовать кэш Zabbix")
    parser.add_argument("--debug", action="store_true", help="Отладочный вывод")
    args = parser.parse_args()

    url, token = _get_zabbix_url_token()
    if not url or not token:
        print("Задайте ZABBIX_URL и ZABBIX_TOKEN", file=sys.stderr)
        sys.exit(1)
    cache_path = None if args.no_cache else os.path.join(
        os.path.dirname(os.path.abspath(args.dry_ssh)) if args.dry_ssh else ".",
        ZABBIX_CACHE_FILE,
    )
    done, err = run(
        url, token, args.commit_rates, args.dry_ssh, args.description_map, cache_path, debug=args.debug
    )
    if err:
        print(err, file=sys.stderr)
        sys.exit(1)
    if not done:
        print("Нет провайдеров в _provider_limits с линками — ничего не создано.")
        sys.exit(0)
    for provider, host_name in done:
        print("OK: {} — хост «{}», calculated items и триггеры 90%/100%".format(provider, host_name))


if __name__ == "__main__":
    main()

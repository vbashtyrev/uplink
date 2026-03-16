#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Создание в Zabbix хостов «Uplinks {Provider}» с calculated items (сумма Bits received / Bits sent
по всем линкам провайдера) и триггерами по агрегатному лимиту из commit_rates.json (_provider_limits).

Лимит задаётся в commit_rates.json:
  "_provider_limits": { "Cogent": 10, "Hurricane": 5 }
(Гбит/с — максимум по всем линкам провайдера в сумме).

Переменные: ZABBIX_URL, ZABBIX_TOKEN.
"""

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
)

DEFAULT_COMMIT_RATES = "commit_rates.json"
CALCULATED_ITEM_KEY_IN = "aggregate.bits.in[]"
CALCULATED_ITEM_KEY_OUT = "aggregate.bits.out[]"
CALCULATED_ITEM_TYPE = 15
VALUE_TYPE_NUMERIC = 3  # unsigned
# Единицы для агрегатных item'ов: биты в секунду (bps), чтобы ось графиков и пороги были в Gbps.
UNITS_BPS = "bps"


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


def _get_or_create_host(url, token, host_name, group_name, debug=False):
    """Вернуть hostid хоста host_name в группе group_name; создать хост, если нет."""
    # Найти группу
    grp, err = zabbix_request(url, token, "hostgroup.get", {
        "output": ["groupid"],
        "filter": {"name": [group_name]},
    }, debug=debug)
    if err or not grp:
        return None, "группа не найдена: {}".format(group_name)
    groupid = grp[0]["groupid"]

    res, err = zabbix_request(url, token, "host.get", {
        "output": ["hostid", "host"],
        "filter": {"host": [host_name]},
    }, debug=debug)
    if err:
        return None, err
    if res:
        return res[0]["hostid"], None

    # Создать хост (interfaces обязательны — создаём dummy agent на 127.0.0.1)
    result, err = zabbix_request(url, token, "host.create", {
        "host": host_name,
        "name": host_name,
        "groups": [{"groupid": groupid}],
        "interfaces": [{"type": 1, "main": 1, "useip": 1, "ip": "127.0.0.1", "dns": "", "port": "10050"}],
    }, debug=debug)
    if err:
        return None, "host.create: {}".format(err)
    return result["hostids"][0], None


def _create_or_update_calculated_item(url, token, hostid, key, name, formula, debug=False):
    """Создать или обновить calculated item. formula — строка, например last(/h1/k1)+last(/h2/k2)."""
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
        "delay": "1m",  # интервал пересчёта (обязателен для item.create в Zabbix 7)
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


def _ensure_triggers(url, token, hostid, host_name, itemid_in, limit_bps, debug=False):
    """Создать или обновить триггеры 90% и 100% для агрегатного линка. itemid_in — itemid calculated (Bits in)."""
    warn_bps = int(limit_bps * THRESHOLD_PERCENT_WARN / 100)
    desc_warn = "Provider aggregate traffic >= {}% of limit ({} Gbps)".format(THRESHOLD_PERCENT_WARN, limit_bps / 1e9)
    desc_high = "Provider aggregate traffic >= 100% of limit ({} Gbps)".format(limit_bps / 1e9)
    expr_warn = "max(/{}/{},{})>{}".format(host_name, CALCULATED_ITEM_KEY_IN, TRIGGER_FUNCTION_PERIOD, warn_bps)
    expr_high = "max(/{}/{},{})>{}".format(host_name, CALCULATED_ITEM_KEY_IN, TRIGGER_FUNCTION_PERIOD, int(limit_bps))

    tags = [{"tag": TRIGGER_TAG_NAME, "value": TRIGGER_TAG_VALUE}]
    # Поиск существующих по описанию
    res, err = zabbix_request(url, token, "trigger.get", {
        "output": ["triggerid", "description"],
        "hostids": [hostid],
        "search": {"description": "Provider aggregate"},
    }, debug=debug)
    if err:
        return err
    by_desc = {t["description"]: t["triggerid"] for t in res} if res else {}

    for desc, expr, severity in [
        (desc_warn, expr_warn, 2),  # Warning
        (desc_high, expr_high, 4),   # High
    ]:
        payload = {"description": desc, "expression": expr, "priority": severity, "tags": tags}
        if desc in by_desc:
            payload["triggerid"] = by_desc[desc]
            _, err = zabbix_request(url, token, "trigger.update", payload, debug=debug)
        else:
            _, err = zabbix_request(url, token, "trigger.create", payload, debug=debug)
        if err:
            return err
    return None


def run(url, token, commit_rates_path, dry_ssh_path, desc_map_path, cache_path, debug=False):
    """Создать/обновить хосты Uplinks {Provider}, calculated items и триггеры."""
    # Явная проверка токена/прав, чтобы вместо «группа не найдена» получать понятную ошибку авторизации.
    ok, err = validate_zabbix_token(url, token, debug=debug)
    if not ok:
        return None, "Ошибка авторизации в Zabbix (token): {}".format(err)
    with open(commit_rates_path, "r", encoding="utf-8") as f:
        cr = json.load(f)
    provider_limits = cr.get("_provider_limits")
    if not isinstance(provider_limits, dict) or not provider_limits:
        return [], None  # нет лимитов — не ошибка, просто нечего делать

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

    done = []
    for provider, limit_gbps in provider_limits.items():
        if not provider or limit_gbps is None:
            continue
        try:
            limit_bps = float(limit_gbps) * 1e9
        except (TypeError, ValueError):
            continue
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
        err = _ensure_triggers(url, token, hostid, host_name, None, limit_bps, debug=debug)
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

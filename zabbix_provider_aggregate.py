#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Create/update aggregate provider hosts in Zabbix (`Uplinks {Provider}`) with
calculated items (sum Bits in/out over all links) and optional 90%/100% limit triggers."""

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
    validate_zabbix_token,
)
from uplinks.netbox.inventory import (
    ERROR_AUTH_DENIED,
    ERROR_PARTIAL_READ,
    ERROR_PROVIDERS_UNAVAILABLE,
    arm_netbox_incomplete_guard,
    collect_provider_limits_gbps,
    is_uplink_iface,
    load_inventory_report,
    load_uplink_provider_context,
    resolve_provider_name_for_iface,
    scoped_devices_from_inventory_report,
)
from uplinks_config import (
    THRESHOLD_PERCENT_WARN,
    THRESHOLD_PERCENT_HIGH,
    TRIGGER_FUNCTION_PERIOD,
    TRIGGER_TAG_NAME,
    TRIGGER_TAG_VALUE,
    SLA_TRIGGER_FUNCTION_PERIOD,
    SLA_TRIGGER_TAG_NAME,
    SLA_TRIGGER_TAG_VALUE,
    UPLINKS_AGGREGATE_HOST_PREFIX,
    UPLINKS_AGGREGATE_GROUP,
)

load_env_file_if_present()

DEFAULT_COMMIT_RATES = "commit_rates.json"
CALCULATED_ITEM_KEY_IN = "aggregate.bits.in[]"
CALCULATED_ITEM_KEY_OUT = "aggregate.bits.out[]"
CALCULATED_ITEM_TYPE = 15
VALUE_TYPE_NUMERIC = 3  # unsigned
# Units for aggregate items: bits per second (bps), so that the graph axis and thresholds are in Gbps.
UNITS_BPS = "bps"


def _get_netbox_client(debug=False):
    """Return pynetbox API client or None when NetBox is not configured."""
    url = os.environ.get("NETBOX_URL", "").strip()
    token = os.environ.get("NETBOX_TOKEN", "").strip()
    if not url or not token:
        if debug:
            print(
                "NetBox: NETBOX_URL/NETBOX_TOKEN are not set - aggregates only by providers from the data",
                file=sys.stderr,
            )
        return None
    try:
        return pynetbox.api(url, token=token)
    except Exception as e:
        if debug:
            print("NetBox: failed to connect: {}".format(e), file=sys.stderr)
        return None


def _load_netbox_aggregate_context(dry_ssh_devices, debug=False, inventory_report=None):
    """
    Read-only NetBox inventory for provider aggregates.
    Return dict with device_iface_to_provider, providers, provider_limits_gbps, stats, read_error; or None.
    """
    ctx = load_uplink_provider_context(
        dry_ssh_devices,
        debug=debug,
        inventory_report=inventory_report,
    )
    if ctx is None:
        return None

    nb = _get_netbox_client(debug=debug)
    if nb is None:
        return None

    provider_limits_gbps, limits_err = collect_provider_limits_gbps(nb, debug=debug)
    stats = dict(ctx.get("stats") or {})
    read_error = bool(ctx.get("read_error"))
    if limits_err:
        if limits_err == ERROR_AUTH_DENIED:
            stats["error"] = ERROR_AUTH_DENIED
        elif limits_err == ERROR_PARTIAL_READ:
            if stats.get("error") not in (ERROR_AUTH_DENIED, ERROR_PROVIDERS_UNAVAILABLE):
                stats["error"] = ERROR_PARTIAL_READ
        read_error = True

    return {
        "device_iface_to_provider": ctx["device_iface_to_provider"],
        "providers": ctx["providers"],
        "provider_limits_gbps": provider_limits_gbps,
        "stats": stats,
        "read_error": read_error,
    }


def _resolve_provider_limit_bps(
    provider,
    netbox_limits_gbps,
    legacy_limits_gbps,
    legacy_commit_rates_fallback=False,
    debug=False,
):
    """NetBox aggregate_limit_gbps first; commit_rates.json _provider_limits only with legacy flag."""
    if provider in netbox_limits_gbps:
        return netbox_limits_gbps[provider] * 1e9

    if not legacy_commit_rates_fallback:
        return None

    legacy_entry = legacy_limits_gbps.get(provider)
    if legacy_entry is None:
        return None

    print(
        "Warning: provider {!r} has no aggregate_limit_gbps in NetBox; "
        "using commit_rates.json _provider_limits (transition fallback)".format(provider),
        file=sys.stderr,
    )
    if debug:
        print(
            "Transition fallback limit for {} from commit_rates.json: {} Gbps".format(
                provider, legacy_entry
            ),
            file=sys.stderr,
        )
    try:
        return float(legacy_entry) * 1e9
    except (TypeError, ValueError):
        return None


def _provider_name_for_iface(hostname, iface, desc_to_name, device_iface_to_provider=None):
    """Resolve provider name: NetBox inventory first, description map as fallback."""
    return resolve_provider_name_for_iface(
        hostname, iface, desc_to_name, device_iface_to_provider=device_iface_to_provider
    )


def _build_edges_with_keys(
    devices,
    host_id_by_name,
    items_by_host_iface,
    desc_to_name,
    device_iface_to_provider=None,
    inventory_scoped=False,
):
    """One edge per (host, ISP), with key_in/key_out for formulas. Return [(hostname, isp, key_in, key_out), ...]."""
    edges_raw = []
    for hostname in sorted(devices.keys()):
        if not host_id_by_name.get(hostname):
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
            isp = _provider_name_for_iface(
                hostname, iface, desc_to_name, device_iface_to_provider=device_iface_to_provider
            )
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
    # In Zabbix, some characters (for example, slash) are prohibited in host. We replace everything except letters, numbers, ._- with a space.
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
        return None, "group not found: {}".format(group_name)
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

    # Create a host (interfaces are required - create a dummy agent on 127.0.0.1)
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
            "preprocessing": [], # without preprocessing (formula is already in bps)
        }, debug=debug)
        return itemid, err
    result, err = zabbix_request(url, token, "item.create", params, debug=debug)
    if err:
        return None, err
    return result["itemids"][0], None


def _ensure_triggers(url, token, hostid, host_technical, provider, itemid_in, limit_bps, debug=False):
    """Create or update 90%/100% provider aggregate triggers for a host.
    If the limit in _provider_limits has changed (for example 20G -> 10G), old triggers with a different limit
    are removed so as not to duplicate 90%/100% across different thresholds.
    """
    warn_bps = int(limit_bps * THRESHOLD_PERCENT_WARN / 100)
    desc_warn = "Provider aggregate traffic >= {}% of limit ({} Gbps)".format(THRESHOLD_PERCENT_WARN, limit_bps / 1e9)
    desc_high = "Provider aggregate traffic >= 100% of limit ({} Gbps)".format(limit_bps / 1e9)
    desc_sla = "Provider aggregate SLA breach: >= 100% of limit for {} ({} Gbps)".format(
        SLA_TRIGGER_FUNCTION_PERIOD, limit_bps / 1e9
    )
    expr_warn = "max(/{}/{},{})>{}".format(
        host_technical, CALCULATED_ITEM_KEY_IN, TRIGGER_FUNCTION_PERIOD, warn_bps
    )
    expr_high = "max(/{}/{},{})>{}".format(
        host_technical, CALCULATED_ITEM_KEY_IN, TRIGGER_FUNCTION_PERIOD, int(limit_bps)
    )
    expr_sla = "min(/{}/{},{})>{}".format(
        host_technical, CALCULATED_ITEM_KEY_IN, SLA_TRIGGER_FUNCTION_PERIOD, int(limit_bps)
    )

    tags = [{"tag": TRIGGER_TAG_NAME, "value": TRIGGER_TAG_VALUE}]
    if provider:
        tags.append({"tag": "provider", "value": provider})
    tags_sla = list(tags) + [{"tag": SLA_TRIGGER_TAG_NAME, "value": SLA_TRIGGER_TAG_VALUE}]

    res, err = zabbix_request(url, token, "trigger.get", {
        "output": ["triggerid", "description"],
        "hostids": [hostid],
        "search": {"description": "Provider aggregate"},
    }, debug=debug)
    if err:
        return err
    all_triggers = res or []
    # We divide by type in order to correctly update old objects when the limit/period changes.
    warn_triggers = [
        t for t in all_triggers
        if t["description"].startswith("Provider aggregate traffic >=") and "90%" in t["description"]
    ]
    high_triggers = [
        t for t in all_triggers
        if t["description"].startswith("Provider aggregate traffic >=") and "100%" in t["description"]
    ]
    sla_triggers = [
        t for t in all_triggers
        if t["description"].startswith("Provider aggregate SLA breach:")
    ]

    def _update_or_create_and_cleanup(desc, expr, severity, same_type_list, trigger_tags):
        """Update one trigger with the exact description or create; delete others of the same type.
        Return: (kept_triggerid, error_or_none)
        """
        same_type_ids = [t["triggerid"] for t in same_type_list]
        by_desc = {t["description"]: t["triggerid"] for t in same_type_list}
        kept_id = by_desc.get(desc)
        if kept_id is not None:
            _, err = zabbix_request(url, token, "trigger.update", {
                "triggerid": kept_id,
                "description": desc,
                "expression": expr,
                "priority": severity,
                "tags": trigger_tags,
            }, debug=debug)
            if err:
                return None, err
        else:
            result, err = zabbix_request(url, token, "trigger.create", {
                "description": desc,
                "expression": expr,
                "priority": severity,
                "tags": trigger_tags,
            }, debug=debug)
            if err:
                return None, err
            kept_id = result["triggerids"][0]
        # Remove unnecessary triggers of the same type (old limit)
        for tid in same_type_ids:
            if tid != kept_id:
                _, err = zabbix_request(url, token, "trigger.delete", [tid], debug=debug)
                if err:
                    return None, err
        return kept_id, None

    def _set_dependency(child_triggerid, parent_triggerid):
        """Set single dependency: child depends on parent."""
        if not child_triggerid or not parent_triggerid:
            return None
        _, err = zabbix_request(url, token, "trigger.update", {
            "triggerid": child_triggerid,
            "dependencies": [{"triggerid": parent_triggerid}],
        }, debug=debug)
        return err

    # Severity levels: 90% - Information, 100% - Warning
    warn_id, err = _update_or_create_and_cleanup(desc_warn, expr_warn, 1, warn_triggers, tags)
    if err:
        return err
    high_id, err = _update_or_create_and_cleanup(desc_high, expr_high, 2, high_triggers, tags)
    if err:
        return err
    # SLA is taken into account only if it is consistently exceeded 100% during the SLA_TRIGGER_FUNCTION_PERIOD.
    _, err = _update_or_create_and_cleanup(desc_sla, expr_sla, 2, sla_triggers, tags_sla)
    if err:
        return err
    # To avoid duplicating noise, we make 90% dependent on 100%.
    err = _set_dependency(warn_id, high_id)
    if err:
        return err
    return None


def _delete_provider_aggregate_triggers(url, token, hostid, debug=False):
    """Delete provider aggregate triggers on a host (used when provider has no limit)."""
    res, err = zabbix_request(url, token, "trigger.get", {
        "output": ["triggerid", "description"],
        "hostids": [hostid],
        "search": {"description": "Provider aggregate"},
    }, debug=debug)
    if err:
        return err
    tids = [t["triggerid"] for t in (res or [])]
    if not tids:
        return None
    _, err = zabbix_request(url, token, "trigger.delete", tids, debug=debug)
    return err


NETBOX_READ_PARTIAL_MSG = (
    "NetBox read incomplete; skipped calculated item formulas and deletions"
)


def run(
    url,
    token,
    commit_rates_path,
    dry_ssh_path,
    desc_map_path,
    cache_path,
    debug=False,
    prune_triggers_without_limits=True,
    legacy_commit_rates_fallback=False,
    inventory_scoped=True,
    inventory_file=None,
):
    """Create/update provider aggregate hosts with calculated items and limit triggers."""
    ok, err = validate_zabbix_token(url, token, debug=debug)
    if not ok:
        return None, "Authorization error in Zabbix (token): {}".format(err)

    provider_limits = {}
    if legacy_commit_rates_fallback:
        if not os.path.isfile(commit_rates_path):
            return None, "commit_rates file not found: {}".format(commit_rates_path)
        with open(commit_rates_path, "r", encoding="utf-8") as f:
            cr = json.load(f)
        provider_limits = cr.get("_provider_limits") or {}
        if not isinstance(provider_limits, dict):
            provider_limits = {}

    inventory_report = None
    if inventory_file:
        try:
            inventory_report = load_inventory_report(inventory_file)
        except (OSError, json.JSONDecodeError) as e:
            return None, "failed to read inventory file {}: {}".format(inventory_file, e)
        inv_ctx = load_uplink_provider_context(inventory_report=inventory_report, debug=debug)
        device_iface_to_provider = {}
        if inv_ctx:
            device_iface_to_provider = inv_ctx.get("device_iface_to_provider") or {}
        devices = scoped_devices_from_inventory_report(
            inventory_report, device_iface_to_provider
        )
        if not devices:
            return None, "No devices in scoped inventory"
    else:
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
        if cached_h and cached_i:
            # We take everything that is already in the cache, even if a host appears/disappears in Zabbix.
            host_id_by_name = {k: cached_h[k] for k in hostnames if k in cached_h}
            items_by_host_iface = {(h, i): rec for (h, i), rec in cached_i.items() if h in host_id_by_name}

    missing_in_cache = sorted(hostnames - set(host_id_by_name.keys()))
    if missing_in_cache:
        # Critical: if some of the hosts are not created in Zabbix, we don’t crash entirely -
        # We count the aggregates based on what we have.
        res, host_err = zabbix_request(
            url,
            token,
            "host.get",
            {
                "output": ["hostid", "host", "name"],
                "filter": {"host": missing_in_cache},
            },
            debug=debug,
        )
        if host_err:
            return None, host_err
        found = set()
        for h in (res or []):
            hn = h.get("host")
            hid = h.get("hostid")
            if hn and hid:
                host_id_by_name[hn] = hid
                found.add(hn)
        still_missing = sorted(set(missing_in_cache) - found)
        if still_missing:
            print(
                "Warning: hosts not found in Zabbix, missing: {}".format(", ".join(still_missing)),
                file=sys.stderr,
            )

    if not host_id_by_name:
        source = "inventory" if inventory_file else "dry-ssh.json"
        return None, "There are no hosts from {} in Zabbix".format(source)

    # We load items only for hosts that actually exist in Zabbix.
    # fetch_zabbix_hosts_and_items requires that all hostnames be found.
    missing_items_hosts = set(host_id_by_name.keys()) - {h for (h, _iface) in items_by_host_iface.keys()}
    if missing_items_hosts:
        fetched_h, fetched_items, err = fetch_zabbix_hosts_and_items(
            url, token, set(missing_items_hosts), debug=debug
        )
        if err:
            return None, err
        host_id_by_name.update(fetched_h)
        items_by_host_iface.update(fetched_items)
        if cache_path:
            save_zabbix_cache(cache_path, host_id_by_name, items_by_host_iface)

    nb_ctx = _load_netbox_aggregate_context(
        devices,
        debug=debug,
        inventory_report=inventory_report,
    )
    device_iface_to_provider = {}
    providers_from_inventory = set()
    netbox_limits_gbps = {}
    nb_read_failed = False
    if nb_ctx:
        device_iface_to_provider = nb_ctx.get("device_iface_to_provider") or {}
        providers_from_inventory = nb_ctx.get("providers") or set()
        netbox_limits_gbps = nb_ctx.get("provider_limits_gbps") or {}
        nb_read_failed = bool(nb_ctx.get("read_error"))
        if nb_read_failed:
            arm_netbox_incomplete_guard(nb_ctx.get("stats") or {})

    edges = _build_edges_with_keys(
        devices,
        host_id_by_name,
        items_by_host_iface,
        desc_to_name,
        device_iface_to_provider=device_iface_to_provider,
        inventory_scoped=inventory_scoped,
    )
    by_provider = {}
    for hostname, isp, key_in, key_out in edges:
        isp = (isp or "").strip()
        if not isp:
            continue
        by_provider.setdefault(isp, []).append((hostname, key_in, key_out))

    # Provider candidates: inventory providers with links in input data; otherwise all from data.
    if providers_from_inventory:
        providers_iter = sorted(p for p in providers_from_inventory if p in by_provider)
    else:
        providers_iter = sorted(by_provider.keys())

    skip_formula_updates = nb_read_failed
    done = []
    for provider in providers_iter:
        if not provider:
            continue
        limit_bps = _resolve_provider_limit_bps(
            provider,
            netbox_limits_gbps,
            provider_limits,
            legacy_commit_rates_fallback=legacy_commit_rates_fallback,
            debug=debug,
        )
        links = by_provider.get(provider)
        if not links:
            if debug:
                print("Provider {} has no links in input data - skip.".format(provider), file=sys.stderr)
            continue
        refs_in = [(h, ki) for h, ki, ko in links if ki]
        refs_out = [(h, ko) for h, ki, ko in links if ko]
        if not refs_in:
            continue
        host_name = UPLINKS_AGGREGATE_HOST_PREFIX + provider
        hostid, err = _get_or_create_host(url, token, host_name, UPLINKS_AGGREGATE_GROUP, debug=debug)
        if err:
            return None, "{}: {}".format(provider, err)
        has_triggers = False
        if skip_formula_updates:
            print(
                "Skipping calculated items and triggers for {}: NetBox read incomplete".format(provider),
                file=sys.stderr,
            )
        else:
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
                has_triggers = True
            elif prune_triggers_without_limits:
                err = _delete_provider_aggregate_triggers(url, token, hostid, debug=debug)
                if err:
                    return None, "{} trigger cleanup: {}".format(provider, err)
        done.append((provider, host_name, has_triggers))
    if nb_read_failed:
        return done, NETBOX_READ_PARTIAL_MSG
    return done, None


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Create Uplinks {Provider} hosts with total traffic and triggers by _provider_limits.",
    )
    parser.add_argument("-f", "--commit-rates", default=DEFAULT_COMMIT_RATES, help="Path to commit_rates.json")
    parser.add_argument("-d", "--dry-ssh", default=None, help="Legacy path to dry-ssh.json")
    parser.add_argument(
        "--inventory-file",
        default=None,
        metavar="FILE",
        help="Scoped inventory JSON from netbox_uplinks_inventory.py --json",
    )
    parser.add_argument("-m", "--description-map", default=DESCRIPTION_MAP_FILE, help="File description_to_name.json")
    parser.add_argument("--no-cache", action="store_true", help="Do not use Zabbix cache")
    parser.add_argument(
        "--keep-triggers-without-limits",
        action="store_true",
        help="Do not delete existing aggregate triggers for providers without aggregate_limit_gbps",
    )
    parser.add_argument(
        "--legacy-commit-rates-fallback",
        action="store_true",
        help="Read _provider_limits from commit_rates.json when aggregate_limit_gbps is missing in NetBox",
    )
    parser.add_argument(
        "--legacy-provider-filter",
        action="store_true",
        help="Select uplink interfaces by Uplink: in description instead of NetBox inventory scope",
    )
    parser.add_argument("--debug", action="store_true", help="Debug output")
    args = parser.parse_args()

    url, token = _get_zabbix_url_token()
    if not url or not token:
        print("Set ZABBIX_URL and ZABBIX_TOKEN", file=sys.stderr)
        sys.exit(1)
    dry_ssh_path = args.dry_ssh or (DEFAULT_INPUT if os.path.isfile(DEFAULT_INPUT) else None)
    if not args.inventory_file and not dry_ssh_path:
        print("Provide --inventory-file or -d dry-ssh.json", file=sys.stderr)
        sys.exit(1)
    cache_base = args.inventory_file or dry_ssh_path or "."
    cache_path = None if args.no_cache else os.path.join(
        os.path.dirname(os.path.abspath(cache_base)) if cache_base else ".",
        ZABBIX_CACHE_FILE,
    )
    done, err = run(
        url,
        token,
        args.commit_rates,
        dry_ssh_path,
        args.description_map,
        cache_path,
        debug=args.debug,
        prune_triggers_without_limits=(not args.keep_triggers_without_limits),
        legacy_commit_rates_fallback=args.legacy_commit_rates_fallback,
        inventory_scoped=(not args.legacy_provider_filter),
        inventory_file=args.inventory_file,
    )
    if not done:
        if err:
            print(err, file=sys.stderr)
            sys.exit(1)
        print("No providers with links - nothing created.")
        sys.exit(0)
    for provider, host_name, has_triggers in done:
        if err:
            print(
                'OK: {} - host "{}" (skipped calculated items and triggers: NetBox read incomplete)'.format(
                    provider, host_name
                )
            )
        elif has_triggers:
            print('OK: {} - host "{}", calculated items and triggers 90%/100%'.format(provider, host_name))
        else:
            print(
                'OK: {} - host "{}", calculated items only (no triggers: no aggregate limit)'.format(
                    provider, host_name
                )
            )
    if err:
        print("Apply was partial: {}".format(err), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

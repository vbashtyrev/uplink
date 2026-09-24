#!/usr/bin/env python3
"""Sync commit-rate macros (and optionally 90%/100% triggers) in Zabbix from NetBox circuits."""

import json
import os
import sys

import pynetbox

from env_urls import load_env_file_if_present
# General Zabbix API logic from zabbix_map
from zabbix_map import (
    _get_zabbix_url_token,
    _interface_from_key,
    _interface_from_item_name,
    _normalize_interface_name,
    validate_zabbix_token,
    zabbix_request,
)
from uplinks.netbox.inventory import (
    ERROR_AUTH_DENIED,
    ERROR_PROVIDERS_UNAVAILABLE,
    arm_netbox_incomplete_guard,
    burst_metadata_from_inventory,
    burst_pairs_from_inventory,
    collect_netbox_interface_relations,
    collect_uplink_inventory,
    device_names_from_complete_inventory,
    expand_burst_metadata_for_zabbix,
    expand_burst_pairs_for_zabbix,
    inventory_read_failed,
    is_netbox_auth_error,
    load_inventory_report,
    netbox_interface_relations_from_report,
    map_commit_rates_to_zabbix_ifaces,
    project_circuit_scope,
    resolve_border_device_tag,
)
from uplinks.zabbix.client import stale_util_triggers, util_trigger_get_params
from uplinks_config import (
    THRESHOLD_ITEM_KEY,
    THRESHOLD_PERCENT_HIGH,
    THRESHOLD_PERCENT_WARN,
    TRIGGER_DESC_90_SUFFIX,
    TRIGGER_DESC_100_SUFFIX,
    TRIGGER_DESC_SLA_BREACH_SUFFIX,
    TRIGGER_DESC_UTIL_CRIT_SUFFIX,
    TRIGGER_DESC_UTIL_WARN_SUFFIX,
    TRIGGER_FUNCTION_PERIOD,
    TRIGGER_TAG_NAME,
    TRIGGER_TAG_VALUE,
    SLA_TRIGGER_FUNCTION_PERIOD,
    SLA_TRIGGER_TAG_NAME,
    SLA_TRIGGER_TAG_VALUE,
    UPLINK_UTIL_CRIT_PERCENT,
    UPLINK_UTIL_CRIT_PERIOD,
    UPLINK_UTIL_WARN_PERCENT,
    UPLINK_UTIL_WARN_PERIOD,
)

load_env_file_if_present()

# IMPORTANT: do not use {$IF.UTIL.*} so as not to break standard template triggers
# (they expect interest). These macros store the absolute bps threshold for our simple triggers.
UPLINK_MACRO_PREFIX_MAX = "{$UPLINK.BPS.MAX"
UPLINK_MACRO_PREFIX_WARN = "{$UPLINK.BPS.WARN"
UPLINK_UTIL_MACRO_PREFIX_WARN = "{$UPLINK.UTIL.WARN"
UPLINK_UTIL_MACRO_PREFIX_CRIT = "{$UPLINK.UTIL.CRIT"
# NetBox commit_rate in Kbps → in bps for Zabbix
KBPS_TO_BPS = 1000


def _macro_name_for_interface(iface_name):
    """Full macro name with interface context - for use in a trigger."""
    if not iface_name:
        iface_name = ""
    return UPLINK_MACRO_PREFIX_MAX + ':"' + iface_name.strip() + '"}'


def _macro_name_warn_for_interface(iface_name):
    """Macro 90% threshold - for the "yellow" trigger on the map."""
    if not iface_name:
        iface_name = ""
    return UPLINK_MACRO_PREFIX_WARN + ':"' + iface_name.strip() + '"}'


def _macro_name_util_warn_for_interface(iface_name):
    if not iface_name:
        iface_name = ""
    return UPLINK_UTIL_MACRO_PREFIX_WARN + ':"' + iface_name.strip() + '"}'


def _macro_name_util_crit_for_interface(iface_name):
    if not iface_name:
        iface_name = ""
    return UPLINK_UTIL_MACRO_PREFIX_CRIT + ':"' + iface_name.strip() + '"}'


def load_dry_ssh(path):
    """Load dry-ssh.json. Return devices dict or None."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data.get("devices") or None


def is_physical_uplink_iface(iface_entry):
    """
    Physical uplink port for utilization monitoring (not LAG aeN nor logical unit aeN.0).
    Arista entries without Juniper flags are treated as physical.
    """
    if not isinstance(iface_entry, dict):
        return True
    name = (iface_entry.get("name") or "").strip()
    if iface_entry.get("isLag"):
        return False
    if iface_entry.get("isLogical"):
        return False
    if name.startswith("ae"):
        return False
    return True


def util_interfaces_by_host_from_inventory(
    dry_ssh_devices, inventory_report, debug=False, netbox_relations=None
):
    """
    Physical util interface names per host from scoped inventory complete rows.
    Without dry-ssh, uses complete inventory interfaces directly (cable termination points).
    With dry-ssh, physical-only candidates are filtered by scoped inventory (legacy).
    """
    from uplinks.netbox.inventory import (
        device_iface_provider_map_from_inventory,
        iface_has_inventory_entry,
    )

    expanded_map = device_iface_provider_map_from_inventory(
        inventory_report,
        dry_ssh_devices=dry_ssh_devices,
        netbox_relations=netbox_relations,
        debug=debug,
    )
    result = {}
    if not dry_ssh_devices:
        for row in inventory_report.get("complete") or []:
            dev_name = (row.get("device") or "").strip()
            iface_name = (row.get("interface") or "").strip()
            if not dev_name or not iface_name:
                continue
            result.setdefault(dev_name, [])
            if iface_name not in result[dev_name]:
                result[dev_name].append(iface_name)
    else:
        for dev_name, ifaces in dry_ssh_devices.items():
            if not isinstance(ifaces, list):
                continue
            seen = set()
            names = []
            for entry in ifaces:
                if not isinstance(entry, dict):
                    continue
                if not is_physical_uplink_iface(entry):
                    continue
                name = (entry.get("name") or "").strip()
                if not name or name in seen:
                    continue
                if iface_has_inventory_entry(dev_name, entry, expanded_map):
                    seen.add(name)
                    names.append(name)
            if names:
                result[dev_name] = names
    if debug:
        total = sum(len(v) for v in result.values())
        print(
            "Util interfaces from scoped inventory: {} hosts, {} physical ifaces".format(
                len(result), total
            ),
            file=sys.stderr,
        )
    return result


def load_burst_pairs(
    inventory_report=None,
    dry_ssh_devices=None,
    netbox_relations=None,
    debug=False,
):
    """Burst (device, interface) pairs from NetBox inventory (Zabbix iface names when dry-ssh given)."""
    if inventory_report is None:
        return set()
    netbox_pairs = burst_pairs_from_inventory(inventory_report)
    if dry_ssh_devices or netbox_relations:
        netbox_pairs = expand_burst_pairs_for_zabbix(
            netbox_pairs,
            dry_ssh_devices=dry_ssh_devices,
            netbox_relations=netbox_relations,
            debug=debug,
        )
    merged = set(netbox_pairs)
    if merged and debug:
        print(
            "Burst pairs from NetBox inventory: {}".format(len(merged)),
            file=sys.stderr,
        )
    return merged


def load_burst_metadata(
    inventory_report=None,
    dry_ssh_devices=None,
    netbox_relations=None,
    debug=False,
):
    """Burst link metadata from NetBox inventory (Zabbix iface names when dry-ssh given)."""
    if inventory_report is None:
        return {}
    netbox_meta = burst_metadata_from_inventory(inventory_report)
    if dry_ssh_devices or netbox_relations:
        netbox_meta = expand_burst_metadata_for_zabbix(
            netbox_meta,
            dry_ssh_devices=dry_ssh_devices,
            netbox_relations=netbox_relations,
            debug=debug,
        )
    if netbox_meta and debug:
        print(
            "Burst metadata from NetBox inventory: {} pairs".format(len(netbox_meta)),
            file=sys.stderr,
        )
    return dict(netbox_meta)


def burst_link_trigger_tags_no_sla(provider, circuit_id):
    """90%/100% on the map - without sla=true (like aggregate warn/high)."""
    return [
        TRIGGER_TAG_SCRIPTS,
        {"tag": "provider", "value": provider},
        {"tag": "circuit", "value": circuit_id},
        {"tag": "billing", "value": "burst"},
    ]


def burst_sla_breach_trigger_tags(provider, circuit_id):
    """Only SLA breach trigger - matches the problem_tags of the Uplinks Burst service."""
    return burst_link_trigger_tags_no_sla(provider, circuit_id) + [
        {"tag": SLA_TRIGGER_TAG_NAME, "value": SLA_TRIGGER_TAG_VALUE},
    ]


def build_physical_to_logical(dry_ssh_devices):
    """
    By dry-ssh: for each (device, physical_interface) a list of logical interfaces,
    for which physicalInterface == physical_interface.
    LAG members (aggregateInterface on a physical port, e.g. et-0/0/3 -> ae5)
    inherit logical units from their aggregate anchor.
    Return: dict (dev_name, physical_iface) -> [logical_name, ...]
    """
    out = {}
    member_to_aggregate = {}
    if not dry_ssh_devices:
        return out
    for dev_name, ifaces in dry_ssh_devices.items():
        if not isinstance(ifaces, list):
            continue
        for entry in ifaces:
            if not isinstance(entry, dict):
                continue
            name = (entry.get("name") or "").strip()
            phys = (entry.get("physicalInterface") or "").strip()
            aggregate = (entry.get("aggregateInterface") or "").strip()
            if name and aggregate:
                member_to_aggregate[(dev_name, _normalize_interface_name(name))] = aggregate
            if not name or not phys:
                continue
            key = (dev_name, _normalize_interface_name(phys))
            out.setdefault(key, []).append(name)
    for (dev_name, member), aggregate in member_to_aggregate.items():
        logicals = out.get((dev_name, _normalize_interface_name(aggregate)))
        if not logicals:
            continue
        existing = out.setdefault((dev_name, member), [])
        for logical in logicals:
            if logical not in existing:
                existing.append(logical)
    return out


def _pick_one_logical(logicals):
    """
    From several logical interfaces on one physical one, select one for the Zabbix macro.
    Priority: unit .0 (ae5.0, ae3.0) - main uplink LAG; otherwise first on the list.
    """
    if not logicals:
        return None
    if len(logicals) == 1:
        return logicals[0]
    for name in logicals:
        if name.endswith(".0"):
            return name
    return logicals[0]


def apply_logical_context(commit_rates, dry_ssh_devices, debug=False):
    """
    If dry_ssh is specified: for pairs (dev, physical_iface) from NetBox, substitute the context according to logical
    name (as in Zabbix). One circuit → one macro per logical interface (with several
    logical on one physics one is taken, the priority is unit .0, for example. ae5.0).
    Return: dict (device_name, iface_name_for_zabbix) -> commit_rate_bps
    """
    phys_to_logical = build_physical_to_logical(dry_ssh_devices)
    result = {}
    substituted = []
    for (dev_name, iface_name), bps in commit_rates.items():
        key = (dev_name, _normalize_interface_name(iface_name))
        logicals = phys_to_logical.get(key, [])
        if logicals:
            logical = _pick_one_logical(logicals)
            if logical:
                result[(dev_name, logical)] = bps
                substituted.append((dev_name, iface_name, logical))
            else:
                result[(dev_name, iface_name)] = bps
        else:
            result[(dev_name, iface_name)] = bps
    if debug and substituted:
        for dev, phys, logical in substituted:
            print("Context for Zabbix: {} {} -> {}".format(dev, phys, logical), file=sys.stderr)
    return result


_NETBOX_AUTH_MESSAGE = (
    "NetBox error: token has expired or access is denied (403). "
    "Check NETBOX_TOKEN and update the token if necessary."
)


# Backward-compatible alias for tests and callers.
_is_netbox_auth_error = is_netbox_auth_error


def commit_rates_from_inventory_report(report, debug=False):
    """Build (device, interface) -> commit_rate_bps from inventory complete rows."""
    stats = report.get("stats") or {}
    error = stats.get("error")
    if error == ERROR_PROVIDERS_UNAVAILABLE:
        if debug:
            print("NetBox: providers unavailable", file=sys.stderr)
        return {}

    result = {}
    skipped_missing_commit_rate = 0

    for row in report.get("complete") or []:
        commit_rate_kbps = row.get("commit_rate_kbps")
        if commit_rate_kbps is None:
            skipped_missing_commit_rate += 1
            if debug:
                print(
                    "Skip missing commit_rate: device={} interface={} circuit={}".format(
                        row.get("device") or "?",
                        row.get("interface") or "?",
                        row.get("circuit_id") or "?",
                    ),
                    file=sys.stderr,
                )
            continue
        try:
            commit_rate_kbps = int(commit_rate_kbps)
        except (TypeError, ValueError):
            skipped_missing_commit_rate += 1
            continue

        device_name = row.get("device") or ""
        iface_name = row.get("interface") or ""
        if not device_name or not iface_name:
            continue
        result[(device_name, iface_name)] = commit_rate_kbps * KBPS_TO_BPS

    if debug:
        incomplete = report.get("incomplete") or []
        print(
            "NetBox inventory: active={}, complete={}, incomplete={}, missing commit_rate={}, pairs with rate: {}".format(
                stats.get("circuits_active", 0),
                stats.get("complete", 0),
                len(incomplete),
                skipped_missing_commit_rate,
                len(result),
            ),
            file=sys.stderr,
        )
        for row in incomplete:
            print(
                "INCOMPLETE circuit={} reason={} ({})".format(
                    row.get("circuit_id") or "?",
                    row.get("reason") or "?",
                    row.get("reason_label") or row.get("reason") or "?",
                ),
                file=sys.stderr,
            )
    return result


def border_device_tag(tag=None):
    """Border device tag from env/argument; monitor tag is not a device tag."""
    return resolve_border_device_tag(tag)


def fetch_uplink_inventory_report(nb, tag, debug=False, exit_on_auth=True):
    """Collect uplink inventory; exit on auth error when exit_on_auth is True."""
    report = collect_uplink_inventory(
        nb,
        tag=tag,
        debug=debug,
        active_only=True,
        circuit_scope=project_circuit_scope(),
    )
    stats = report.get("stats") or {}
    error = stats.get("error")
    if error == ERROR_AUTH_DENIED:
        print(_NETBOX_AUTH_MESSAGE, file=sys.stderr)
        if debug:
            print("collect_uplink_inventory: {}".format(error), file=sys.stderr)
        if exit_on_auth:
            sys.exit(1)
    return report


def set_zabbix_host_macros_for_prefixes(url, token, hostid, new_macro_list, macro_prefixes, debug=False):
    """
    Replace host macros whose names start with any of macro_prefixes, then create new_macro_list.
    new_macro_list: list {"macro", "value", "type"}.
    Return (True, None) or (False, error_message).
    """
    to_delete = []
    for prefix in macro_prefixes:
        result, err = zabbix_request(
            url, token, "usermacro.get",
            {"hostids": [hostid], "output": ["hostmacroid", "macro"], "search": {"macro": prefix}},
            debug=debug,
        )
        if err:
            return False, err
        to_delete.extend(m["hostmacroid"] for m in (result or []) if m.get("hostmacroid"))
    if to_delete:
        result_del, err_del = zabbix_request(url, token, "usermacro.delete", to_delete, debug=debug)
        if err_del:
            return False, err_del
    if not new_macro_list:
        return True, None
    create_list = [
        {"hostid": str(hostid), "macro": entry["macro"], "value": entry["value"], "type": int(entry.get("type") or 0)}
        for entry in new_macro_list
    ]
    result_c, err_c = zabbix_request(url, token, "usermacro.create", create_list, debug=debug)
    if err_c:
        return False, err_c
    return True, None


def set_zabbix_host_if_util_macros(url, token, hostid, new_if_util_list, debug=False):
    """
    Set commit rate macros for interfaces. Macro names with context:
    {$UPLINK.BPS.MAX:"Ethernet51/1"} and {$UPLINK.BPS.WARN:"Ethernet51/1"}.
    """
    return set_zabbix_host_macros_for_prefixes(
        url,
        token,
        hostid,
        new_if_util_list,
        (UPLINK_MACRO_PREFIX_MAX, UPLINK_MACRO_PREFIX_WARN),
        debug=debug,
    )


def set_zabbix_host_uplink_util_macros(url, token, hostid, new_util_macro_list, debug=False):
    """Set {$UPLINK.UTIL.WARN} and {$UPLINK.UTIL.CRIT} per interface (percent values)."""
    return set_zabbix_host_macros_for_prefixes(
        url,
        token,
        hostid,
        new_util_macro_list,
        (UPLINK_UTIL_MACRO_PREFIX_WARN, UPLINK_UTIL_MACRO_PREFIX_CRIT),
        debug=debug,
    )


TRIGGER_TAG_SCRIPTS = {"tag": TRIGGER_TAG_NAME, "value": TRIGGER_TAG_VALUE}
LEGACY_TRIGGER_DESC_90_SUFFIX = "High bandwidth ({}%)".format(THRESHOLD_PERCENT_WARN)
LEGACY_TRIGGER_DESC_100_SUFFIX = "High bandwidth (threshold line)"


def delete_util_triggers(url, token, debug=False):
    """Remove uplink utilization warn/crit triggers (scripts:automatization)."""
    res, err = zabbix_request(
        url,
        token,
        "trigger.get",
        {
            "output": ["triggerid", "description"],
            "search": {"description": "Interface "},
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
            desc.endswith(TRIGGER_DESC_UTIL_WARN_SUFFIX)
            or desc.endswith(TRIGGER_DESC_UTIL_CRIT_SUFFIX)
        ):
            continue
        tags = t.get("tags") or []
        if tags:
            has_tag = any(
                tg.get("tag") == TRIGGER_TAG_NAME and tg.get("value") == TRIGGER_TAG_VALUE
                for tg in tags
            )
            if not has_tag:
                continue
        tid = t.get("triggerid")
        if tid:
            to_delete.append(str(tid))
    if not to_delete:
        return 0
    _, del_err = zabbix_request(url, token, "trigger.delete", to_delete, debug=debug)
    if del_err:
        print("trigger.delete error: {}".format(del_err), file=sys.stderr)
        return 0
    return len(to_delete)


def prune_util_triggers_on_host(url, token, hostid, allowed_iface_names, debug=False):
    """
    Remove utilization warn/crit triggers on this host for interfaces not in allowed_iface_names
    (e.g. after switching from LAG names to physical-only).
    Return (deleted_count, error_message).
    """
    res, err = zabbix_request(
        url,
        token,
        "trigger.get",
        util_trigger_get_params([hostid]),
        debug=debug,
    )
    if err:
        return 0, "trigger.get: {}".format(err)
    to_delete = []
    for t in stale_util_triggers(res, allowed_iface_names):
        tid = t.get("triggerid")
        if tid:
            to_delete.append(str(tid))
    if not to_delete:
        return 0, None
    _, del_err = zabbix_request(url, token, "trigger.delete", to_delete, debug=debug)
    if del_err:
        return 0, "trigger.delete: {}".format(del_err)
    return len(to_delete), None


def delete_link_triggers(url, token, debug=False):
    """
    Remove simple 90%/100% triggers on interfaces created by uplinks scripts
    (according to the description and tag scripts:automatization).
    Return: number of triggers removed.
    """
    res, err = zabbix_request(
        url,
        token,
        "trigger.get",
        {
            "output": ["triggerid", "description"],
            # We search by the common prefix of interface triggers to find legacy,
            # and new wording.
            "search": {"description": "Interface "},
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
            or desc.endswith(TRIGGER_DESC_SLA_BREACH_SUFFIX)
            or desc.endswith(LEGACY_TRIGGER_DESC_90_SUFFIX)
            or desc.endswith(LEGACY_TRIGGER_DESC_100_SUFFIX)
        ):
            continue
        tags = t.get("tags") or []
        if tags:
            has_tag = any(
                tg.get("tag") == TRIGGER_TAG_NAME and tg.get("value") == TRIGGER_TAG_VALUE
                for tg in tags
            )
            if not has_tag:
                continue
        tid = t.get("triggerid")
        if tid:
            to_delete.append(str(tid))
    if not to_delete:
        return 0
    _, del_err = zabbix_request(
        url, token, "trigger.delete", to_delete, debug=debug
    )
    if del_err:
        print("trigger.delete error: {}".format(del_err), file=sys.stderr)
        return 0
    return len(to_delete)


def get_bits_received_item_key(url, token, hostid, iface_name, debug=False):
    """
    Find the item's "Bits received" key for the given host and interface (to express a simple trigger).
    Match by key (Ethernet51/1 in []) or by item name (Interface Ethernet51/1(...)).
    Return key_ or None.
    """
    res, err = zabbix_request(
        url, token, "item.get",
        {"hostids": [hostid], "output": ["key_", "name"], "search": {"name": "Bits received"}},
        debug=debug,
    )
    if err or not res:
        return None
    iface_norm = _normalize_interface_name((iface_name or "").strip())
    for it in res:
        key_str = it.get("key_") or ""
        name_str = it.get("name") or ""
        iface_from_k = _interface_from_key(key_str)
        iface_from_n = _interface_from_item_name(name_str)
        match = (iface_from_k and _normalize_interface_name(iface_from_k) == iface_norm) or (
            iface_from_n and _normalize_interface_name(iface_from_n) == iface_norm
        )
        if match:
            return key_str
    return None


def get_net_if_bandwidth_item_keys(url, token, hostid, iface_name, debug=False):
    """
    Find net.if.in, net.if.out and net.if.speed item keys for an interface (SNMP index in key).
    Match by item name prefix 'Interface {iface_name}(...'.
    Return {"in": key, "out": key, "speed": key} or None if any key is missing.
    """
    search_name = "Interface {}".format((iface_name or "").strip())
    res, err = zabbix_request(
        url,
        token,
        "item.get",
        {
            "hostids": [hostid],
            "output": ["key_", "name"],
            "search": {"name": search_name},
            "searchByAny": True,
        },
        debug=debug,
    )
    if err or not res:
        return None
    iface_norm = _normalize_interface_name((iface_name or "").strip())
    keys = {}
    for it in res:
        key_str = it.get("key_") or ""
        name_str = it.get("name") or ""
        if _normalize_interface_name(_interface_from_item_name(name_str)) != iface_norm:
            continue
        if key_str.startswith("net.if.in[") and "discards" not in key_str and "errors" not in key_str:
            keys["in"] = key_str
        elif key_str.startswith("net.if.out[") and "discards" not in key_str and "errors" not in key_str:
            keys["out"] = key_str
        elif key_str.startswith("net.if.speed["):
            keys["speed"] = key_str
    if keys.get("in") and keys.get("out") and keys.get("speed"):
        return keys
    return None


def build_bandwidth_util_expression(host_technical, item_keys, macro_ref, period):
    """
    Template-style utilization: avg(in) or avg(out) vs (macro%/100)*speed, speed must be > 0.
    item_keys: dict from get_net_if_bandwidth_item_keys.
    """
    in_k = item_keys["in"]
    out_k = item_keys["out"]
    speed_k = item_keys["speed"]
    threshold = "({}/100)*last(/{}/{})".format(macro_ref, host_technical, speed_k)
    return (
        "(avg(/{}/{},{})>{} "
        "or avg(/{}/{},{})>{}) "
        "and last(/{}/{})>0"
    ).format(
        host_technical,
        in_k,
        period,
        threshold,
        host_technical,
        out_k,
        period,
        threshold,
        host_technical,
        speed_k,
    )


# Priorities for display on the map: 2 = Warning (yellow), 4 = High (red)
TRIGGER_PRIORITY_WARN = 2 # 90% - yellow link
TRIGGER_PRIORITY_HIGH = 4 # 100% - red link
TRIGGER_PRIORITY_SLA_BREACH = 2 # as an aggregate SLA breach (not a red card - 100% gives it)


def normalize_trigger_tags(tags):
    """Sort trigger tags for stable comparison."""
    out = []
    for tag in tags or []:
        if not isinstance(tag, dict):
            continue
        name = tag.get("tag")
        if not name:
            continue
        out.append((str(name), str(tag.get("value") or "")))
    return sorted(out)


def expected_burst_trigger_specs(host_technical, iface_name, item_key, provider=None, circuit_id=None):
    """Expected Burst link trigger definitions shared by sync and read-only plan."""
    iface = (iface_name or "").strip()
    link_tags = (
        burst_link_trigger_tags_no_sla(provider, circuit_id)
        if provider and circuit_id
        else [TRIGGER_TAG_SCRIPTS]
    )
    sla_tags = (
        burst_sla_breach_trigger_tags(provider, circuit_id)
        if provider and circuit_id
        else [TRIGGER_TAG_SCRIPTS]
    )
    macro_max = _macro_name_for_interface(iface_name)
    macro_warn = _macro_name_warn_for_interface(iface_name)
    high_desc = "Interface {}: {}".format(iface, TRIGGER_DESC_100_SUFFIX)
    warn_desc = "Interface {}: {}".format(iface, TRIGGER_DESC_90_SUFFIX)
    sla_desc = "Interface {}: {}".format(iface, TRIGGER_DESC_SLA_BREACH_SUFFIX)
    high_expr = "max(/{}/{}, {})>{}".format(
        host_technical, item_key, TRIGGER_FUNCTION_PERIOD, macro_max
    )
    warn_expr = "max(/{}/{}, {})>{}".format(
        host_technical, item_key, TRIGGER_FUNCTION_PERIOD, macro_warn
    )
    sla_expr = "min(/{}/{},{})>{}".format(
        host_technical, item_key, SLA_TRIGGER_FUNCTION_PERIOD, macro_max
    )
    return [
        {
            "role": "high",
            "description": high_desc,
            "expression": high_expr,
            "priority": str(TRIGGER_PRIORITY_HIGH),
            "tags": link_tags,
            "depends_on_role": None,
        },
        {
            "role": "warn",
            "description": warn_desc,
            "expression": warn_expr,
            "priority": str(TRIGGER_PRIORITY_WARN),
            "tags": link_tags,
            "depends_on_role": "high",
        },
        {
            "role": "sla",
            "description": sla_desc,
            "expression": sla_expr,
            "priority": str(TRIGGER_PRIORITY_SLA_BREACH),
            "tags": sla_tags,
            "depends_on_role": None,
        },
    ]


def _get_trigger_id_for_description_suffix(url, token, hostid, iface_name, suffix, debug=False):
    """Find triggerid on host for Interface {iface}: *suffix*."""
    prefix = "Interface {}:".format((iface_name or "").strip())
    existing, err = zabbix_request(
        url,
        token,
        "trigger.get",
        {
            "hostids": [hostid],
            "output": ["triggerid", "description"],
            "search": {"description": prefix},
        },
        debug=debug,
    )
    if err or not existing:
        return None
    for t in existing:
        desc = t.get("description") or ""
        if desc == prefix + " " + suffix or desc.endswith(suffix):
            return t.get("triggerid")
    return None


def ensure_util_crit_trigger(url, token, host_technical, hostid, iface_name, debug=False):
    """Critical uplink utilization (avg over UPLINK_UTIL_CRIT_PERIOD > {$UPLINK.UTIL.CRIT})."""
    item_keys = get_net_if_bandwidth_item_keys(url, token, hostid, iface_name, debug=debug)
    if not item_keys:
        return False, "net.if.in/out/speed not found for interface {}".format(iface_name)
    macro_ref = _macro_name_util_crit_for_interface(iface_name)
    expression = build_bandwidth_util_expression(
        host_technical, item_keys, macro_ref, UPLINK_UTIL_CRIT_PERIOD
    )
    description = "Interface {}: {}".format((iface_name or "").strip(), TRIGGER_DESC_UTIL_CRIT_SUFFIX)
    existing_id = _get_trigger_id_for_description_suffix(
        url, token, hostid, iface_name, TRIGGER_DESC_UTIL_CRIT_SUFFIX, debug=debug
    )
    if existing_id:
        zabbix_request(
            url,
            token,
            "trigger.update",
            {
                "triggerid": existing_id,
                "description": description,
                "expression": expression,
                "priority": TRIGGER_PRIORITY_HIGH,
                "status": 0,
                "tags": [TRIGGER_TAG_SCRIPTS],
            },
            debug=debug,
        )
        return True, None
    create_res, create_err = zabbix_request(
        url,
        token,
        "trigger.create",
        {
            "description": description,
            "expression": expression,
            "priority": TRIGGER_PRIORITY_HIGH,
            "tags": [TRIGGER_TAG_SCRIPTS],
        },
        debug=debug,
    )
    if create_err or not create_res or not create_res.get("triggerids"):
        return False, create_err or "trigger.create did not return triggerid"
    return True, None


def ensure_util_warn_trigger(url, token, host_technical, hostid, iface_name, debug=False):
    """Warning uplink utilization; depends on critical trigger (no duplicate PROBLEM)."""
    item_keys = get_net_if_bandwidth_item_keys(url, token, hostid, iface_name, debug=debug)
    if not item_keys:
        return False, "net.if.in/out/speed not found for interface {}".format(iface_name)
    macro_ref = _macro_name_util_warn_for_interface(iface_name)
    expression = build_bandwidth_util_expression(
        host_technical, item_keys, macro_ref, UPLINK_UTIL_WARN_PERIOD
    )
    description = "Interface {}: {}".format((iface_name or "").strip(), TRIGGER_DESC_UTIL_WARN_SUFFIX)
    crit_id = _get_trigger_id_for_description_suffix(
        url, token, hostid, iface_name, TRIGGER_DESC_UTIL_CRIT_SUFFIX, debug=debug
    )
    existing_id = _get_trigger_id_for_description_suffix(
        url, token, hostid, iface_name, TRIGGER_DESC_UTIL_WARN_SUFFIX, debug=debug
    )
    payload = {
        "description": description,
        "expression": expression,
        "priority": TRIGGER_PRIORITY_WARN,
        "status": 0,
        "tags": [TRIGGER_TAG_SCRIPTS],
    }
    if crit_id:
        payload["dependencies"] = [{"triggerid": str(crit_id)}]
    if existing_id:
        payload["triggerid"] = existing_id
        zabbix_request(url, token, "trigger.update", payload, debug=debug)
        return True, None
    create_res, create_err = zabbix_request(
        url, token, "trigger.create", payload, debug=debug
    )
    if create_err or not create_res or not create_res.get("triggerids"):
        return False, create_err or "trigger.create did not return triggerid"
    return True, None


def sync_uplink_utilization_for_host(
    url, token, host_technical, hostid, iface_names, dry_run=False, debug=False, netbox_data_complete=True
):
    """
    Set {$UPLINK.UTIL.*} macros and warn/crit triggers for all iface_names on one host.
    Return (macros_count, triggers_ok_count, errors_list).
    """
    util_macros = []
    for iface_name in iface_names:
        util_macros.append({
            "macro": _macro_name_util_warn_for_interface(iface_name),
            "value": str(UPLINK_UTIL_WARN_PERCENT),
            "type": "0",
        })
        util_macros.append({
            "macro": _macro_name_util_crit_for_interface(iface_name),
            "value": str(UPLINK_UTIL_CRIT_PERCENT),
            "type": "0",
        })
    if dry_run:
        return len(util_macros), len(iface_names), []
    pruned = 0
    if netbox_data_complete:
        pruned, prune_err = prune_util_triggers_on_host(
            url, token, hostid, iface_names, debug=debug
        )
        if prune_err:
            return 0, 0, [prune_err]
        if pruned and debug:
            print("Pruned {} stale util triggers on hostid {}".format(pruned, hostid), file=sys.stderr)
        ok, err = set_zabbix_host_uplink_util_macros(url, token, hostid, util_macros, debug=debug)
        if not ok:
            return 0, 0, [err or "usermacro"]
    else:
        print(
            "Skipping utilization macro update for hostid {}: NetBox data is incomplete".format(hostid),
            file=sys.stderr,
        )
    triggers_ok = 0
    errors = []
    if netbox_data_complete:
        for iface_name in iface_names:
            ok_c, err_c = ensure_util_crit_trigger(
                url, token, host_technical, hostid, iface_name, debug=debug
            )
            if not ok_c:
                errors.append("{} crit: {}".format(iface_name, err_c))
                continue
            ok_w, err_w = ensure_util_warn_trigger(
                url, token, host_technical, hostid, iface_name, debug=debug
            )
            if not ok_w:
                errors.append("{} warn: {}".format(iface_name, err_w))
                continue
            triggers_ok += 1
    elif iface_names:
        print(
            "Skipping utilization trigger create/update for hostid {}: NetBox data is incomplete".format(
                hostid
            ),
            file=sys.stderr,
        )
    return len(util_macros), triggers_ok, errors


def _ambiguous_burst_trigger_error(stable_description):
    return "ambiguous burst trigger description match: {}".format(stable_description)


def _resolve_burst_trigger_match(existing, stable_description, role_suffix=None, legacy_suffixes=()):
    """
    Resolve one existing Burst trigger row for a stable role description.
    Return (row, error). row is None and error set when ambiguous; both None when not found.
    """
    exact = [
        row
        for row in (existing or [])
        if (row.get("description") or "").strip() == stable_description
    ]
    if len(exact) > 1:
        return None, _ambiguous_burst_trigger_error(stable_description)
    if len(exact) == 1:
        return exact[0], None

    legacy = []
    for row in existing or []:
        desc = (row.get("description") or "").strip()
        if not desc or desc == stable_description:
            continue
        if role_suffix and desc.endswith(role_suffix):
            legacy.append(row)
            continue
        for legacy_suffix in legacy_suffixes or ():
            if desc.endswith(legacy_suffix):
                legacy.append(row)
                break
    if len(legacy) > 1:
        return None, _ambiguous_burst_trigger_error(stable_description)
    if len(legacy) == 1:
        return legacy[0], None
    return None, None


def _dependency_id_list_from_trigger(row):
    """Sorted triggerid list from dependencies (duplicates preserved for comparison)."""
    deps = (row or {}).get("dependencies") or []
    ids = [str(d.get("triggerid")) for d in deps if d.get("triggerid")]
    return sorted(ids)


def _apply_dependency_update_if_needed(upd, matched, expected_dep_ids):
    """Add dependencies to trigger.update payload when sorted ID lists differ (incl. duplicates)."""
    expected_ids = [str(i) for i in (expected_dep_ids or []) if i]
    expected_sorted = sorted(expected_ids)
    actual_sorted = _dependency_id_list_from_trigger(matched)
    if actual_sorted == expected_sorted:
        return
    if expected_ids:
        upd["dependencies"] = [{"triggerid": i} for i in expected_ids]
    else:
        upd["dependencies"] = []


def _resolve_high_burst_trigger_id(url, token, hostid, iface_name, debug=False):
    """Resolve 100% burst triggerid for warn dependency. Return (triggerid, error)."""
    iface = (iface_name or "").strip()
    high_description = "Interface {}: {}".format(iface, TRIGGER_DESC_100_SUFFIX)
    res_h, err_h = zabbix_request(
        url,
        token,
        "trigger.get",
        {
            "hostids": [hostid],
            "output": ["triggerid", "description"],
            "search": {"description": "Interface {}:".format(iface)},
        },
        debug=debug,
    )
    if err_h:
        return None, err_h
    if not res_h:
        return None, "100% burst trigger was not found for interface {}".format(iface_name)
    high_row, high_err = _resolve_burst_trigger_match(
        res_h,
        high_description,
        role_suffix=TRIGGER_DESC_100_SUFFIX,
        legacy_suffixes=(LEGACY_TRIGGER_DESC_100_SUFFIX,),
    )
    if high_err:
        return None, high_err
    if high_row is None:
        return None, "100% burst trigger was not found for interface {}".format(iface_name)
    high_id = high_row.get("triggerid")
    if not high_id:
        return None, "100% burst trigger was not found for interface {}".format(iface_name)
    return high_id, None


def ensure_simple_threshold_trigger(url, token, host_technical, hostid, iface_name, debug=False, link_tags=None):
    """
    Create/update a simple trigger max(Bits received, TRIGGER_FUNCTION_PERIOD) > {$UPLINK.BPS.MAX:"iface"}.
    Threshold line on the chart (Simple triggers) and red link on the map at 100%.
    Return (True, None) or (False, error_message).
    """
    key = get_bits_received_item_key(url, token, hostid, iface_name, debug=debug)
    if not key:
        return False, "item Bits received was not found for interface {}".format(iface_name)
    macro_ref = _macro_name_for_interface(iface_name)
    expression = "max(/{}/{}, {})>{}".format(host_technical, key, TRIGGER_FUNCTION_PERIOD, macro_ref)
    description = "Interface {}: {}".format((iface_name or "").strip(), TRIGGER_DESC_100_SUFFIX)
    existing, err = zabbix_request(
        url, token, "trigger.get",
        {
            "hostids": [hostid],
            "output": ["triggerid", "description", "priority", "status", "expression", "dependencies"],
            "selectDependencies": "extend",
            "search": {"description": "Interface {}:".format((iface_name or "").strip())},
        },
        debug=debug,
    )
    if err:
        return False, err
    matched, match_err = _resolve_burst_trigger_match(
        existing,
        description,
        role_suffix=TRIGGER_DESC_100_SUFFIX,
        legacy_suffixes=(LEGACY_TRIGGER_DESC_100_SUFFIX,),
    )
    if match_err:
        return False, match_err
    if matched is not None:
        desc = (matched.get("description") or "").strip()
        tid = matched.get("triggerid")
        if tid:
            upd = {}
            if desc != description:
                upd["description"] = description
            if (matched.get("expression") or "").strip() != expression:
                upd["expression"] = expression
            if str(matched.get("priority", "0")) != str(TRIGGER_PRIORITY_HIGH):
                upd["priority"] = TRIGGER_PRIORITY_HIGH
            if str(matched.get("status", "0")) != "0":  # enable if disabled
                upd["status"] = "0"
            if link_tags is not None:
                upd["tags"] = link_tags
            _apply_dependency_update_if_needed(upd, matched, [])
            if upd:
                _, upd_err = zabbix_request(
                    url, token, "trigger.update", {"triggerid": tid, **upd}, debug=debug
                )
                if upd_err:
                    return False, upd_err
        return True, None
    tags_payload = link_tags if link_tags is not None else [TRIGGER_TAG_SCRIPTS]
    create_res, create_err = zabbix_request(
        url, token, "trigger.create",
        {
            "description": description,
            "expression": expression,
            "priority": TRIGGER_PRIORITY_HIGH,
            "tags": tags_payload,
        },
        debug=debug,
    )
    if create_err or not create_res or not create_res.get("triggerids"):
        return False, create_err or "trigger.create did not return triggerid"
    return True, None


def ensure_simple_warn_trigger(url, token, host_technical, hostid, iface_name, debug=False, link_tags=None):
    """
    Create a simple WARN trigger: max(Bits received, TRIGGER_FUNCTION_PERIOD) > {$UPLINK.BPS.WARN:"iface"}.
    The link on the map turns yellow when the WARN threshold is reached.
    Return (True, None) or (False, error_message).
    """
    key = get_bits_received_item_key(url, token, hostid, iface_name, debug=debug)
    if not key:
        return False, "item Bits received was not found for interface {}".format(iface_name)
    macro_ref = _macro_name_warn_for_interface(iface_name)
    expression = "max(/{}/{}, {})>{}".format(host_technical, key, TRIGGER_FUNCTION_PERIOD, macro_ref)
    description = "Interface {}: {}".format((iface_name or "").strip(), TRIGGER_DESC_90_SUFFIX)
    existing, err = zabbix_request(
        url, token, "trigger.get",
        {
            "hostids": [hostid],
            "output": ["triggerid", "description", "status", "expression", "priority", "dependencies"],
            "selectDependencies": "extend",
            "search": {"description": "Interface {}:".format((iface_name or "").strip())},
        },
        debug=debug,
    )
    if err:
        return False, err
    matched, match_err = _resolve_burst_trigger_match(
        existing,
        description,
        role_suffix=TRIGGER_DESC_90_SUFFIX,
        legacy_suffixes=(LEGACY_TRIGGER_DESC_90_SUFFIX,),
    )
    if match_err:
        return False, match_err
    if matched is not None:
        desc = (matched.get("description") or "").strip()
        tid = matched.get("triggerid")
        if tid:
            upd = {}
            if desc != description:
                upd["description"] = description
            if (matched.get("expression") or "").strip() != expression:
                upd["expression"] = expression
            if str(matched.get("priority", "0")) != str(TRIGGER_PRIORITY_WARN):
                upd["priority"] = TRIGGER_PRIORITY_WARN
            if str(matched.get("status", "0")) != "0":
                upd["status"] = "0"
            high_id, high_err = _resolve_high_burst_trigger_id(
                url, token, hostid, iface_name, debug=debug
            )
            if high_err:
                return False, high_err
            _apply_dependency_update_if_needed(upd, matched, [high_id])
            if link_tags is not None:
                upd["tags"] = link_tags
            if upd:
                _, upd_err = zabbix_request(
                    url, token, "trigger.update", {"triggerid": tid, **upd}, debug=debug
                )
                if upd_err:
                    return False, upd_err
        return True, None
    tags_payload = link_tags if link_tags is not None else [TRIGGER_TAG_SCRIPTS]
    # For new 90% triggers, we also set a dependence on 100%, so that there are no two PROBLEMs at the same time.
    high_id, high_err = _resolve_high_burst_trigger_id(
        url, token, hostid, iface_name, debug=debug
    )
    if high_err:
        return False, high_err

    create_payload = {
        "description": description,
        "expression": expression,
        "priority": TRIGGER_PRIORITY_WARN,
        "tags": tags_payload,
        "dependencies": [{"triggerid": str(high_id)}],
    }

    create_res, create_err = zabbix_request(
        url, token, "trigger.create",
        create_payload,
        debug=debug,
    )
    if create_err or not create_res or not create_res.get("triggerids"):
        return False, create_err or "trigger.create did not return triggerid"
    return True, None


def ensure_burst_sla_breach_trigger(url, token, host_technical, hostid, iface_name, debug=False, link_tags=None):
    """
    Burst SLA breach: min(Bits received, SLA_TRIGGER_FUNCTION_PERIOD) > commit - only this trigger with sla=true.
    Analogous to Provider aggregate SLA breach.
    """
    key = get_bits_received_item_key(url, token, hostid, iface_name, debug=debug)
    if not key:
        return False, "item Bits received was not found for interface {}".format(iface_name)
    macro_ref = _macro_name_for_interface(iface_name)
    expression = "min(/{}/{},{})>{}".format(
        host_technical, key, SLA_TRIGGER_FUNCTION_PERIOD, macro_ref
    )
    description = "Interface {}: {}".format(
        (iface_name or "").strip(), TRIGGER_DESC_SLA_BREACH_SUFFIX
    )
    existing, err = zabbix_request(
        url,
        token,
        "trigger.get",
        {
            "hostids": [hostid],
            "output": ["triggerid", "description", "priority", "status", "expression", "dependencies"],
            "selectDependencies": "extend",
            "search": {"description": "Interface {}:".format((iface_name or "").strip())},
        },
        debug=debug,
    )
    if err:
        return False, err
    matched, match_err = _resolve_burst_trigger_match(
        existing,
        description,
        role_suffix=TRIGGER_DESC_SLA_BREACH_SUFFIX,
    )
    if match_err:
        return False, match_err
    if matched is not None:
        desc = (matched.get("description") or "").strip()
        tid = matched.get("triggerid")
        if tid:
            upd = {}
            if desc != description:
                upd["description"] = description
            if (matched.get("expression") or "").strip() != expression:
                upd["expression"] = expression
            if str(matched.get("priority", "0")) != str(TRIGGER_PRIORITY_SLA_BREACH):
                upd["priority"] = TRIGGER_PRIORITY_SLA_BREACH
            if str(matched.get("status", "0")) != "0":
                upd["status"] = "0"
            if link_tags is not None:
                upd["tags"] = link_tags
            _apply_dependency_update_if_needed(upd, matched, [])
            if upd:
                _, upd_err = zabbix_request(
                    url, token, "trigger.update", {"triggerid": tid, **upd}, debug=debug
                )
                if upd_err:
                    return False, upd_err
        return True, None
    tags_payload = link_tags if link_tags is not None else [TRIGGER_TAG_SCRIPTS]
    create_res, create_err = zabbix_request(
        url,
        token,
        "trigger.create",
        {
            "description": description,
            "expression": expression,
            "priority": TRIGGER_PRIORITY_SLA_BREACH,
            "tags": tags_payload,
        },
        debug=debug,
    )
    if create_err or not create_res or not create_res.get("triggerids"):
        return False, create_err or "trigger.create did not return triggerid"
    return True, None


def remove_threshold_items(url, token, hostid, debug=False):
    """
    Delete all threshold items on the host (key net.if.threshold[...]), no longer used - the line is drawn with a simple trigger.
    Return (deleted_count, None) or (0, error_message).
    """
    res, err = zabbix_request(
        url, token, "item.get",
        {"hostids": [hostid], "output": ["itemid", "key_"], "search": {"key_": THRESHOLD_ITEM_KEY}},
        debug=debug,
    )
    if err:
        return 0, err
    ids = []
    for it in (res or []):
        key_str = it.get("key_") or ""
        if key_str.startswith(THRESHOLD_ITEM_KEY):
            itemid = it.get("itemid")
            if itemid:
                ids.append(str(itemid))
    if not ids:
        return 0, None
    _, del_err = zabbix_request(url, token, "item.delete", ids, debug=debug)
    if del_err:
        return 0, del_err
    return len(ids), None


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description=(
            "Sync Zabbix uplink macros and triggers from NetBox inventory. "
            "With --inventory-file: commit rates and utilization scope come from the inventory report; "
            "{$UPLINK.UTIL.*} macros and utilization triggers follow scoped inventory entries. "
            "Optional --dry-ssh adds physical/logical interface mapping and extra device metadata."
        ),
    )
    parser.add_argument(
        "-d",
        "--dry-ssh",
        default=None,
        metavar="FILE",
        help=(
            "Optional dry-ssh.json for physical/logical interface mapping and supplemental device data "
            "(utilization scope and BPS macros still come from NetBox inventory when --inventory-file is set)"
        ),
    )
    parser.add_argument(
        "--inventory-file",
        default=None,
        metavar="FILE",
        help="Scoped inventory JSON from netbox_uplinks_inventory.py --json",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not change macros in Zabbix, just display what would have been installed")
    parser.add_argument("--debug", action="store_true", help="Debug output (NetBox statistics, logical name substitution)")
    parser.add_argument(
        "--create-link-triggers",
        action="store_true",
        help=(
            "Create/update simple triggers 90%%/100%%/SLA breach only for billing_model=Burst "
            "(default: none, use template triggers)"
        ),
    )
    parser.add_argument(
        "--no-util-triggers",
        action="store_true",
        help="Do not create {$UPLINK.UTIL.*} macros or utilization triggers (default: enabled from inventory)",
    )
    parser.add_argument(
        "--delete-link-triggers",
        action="store_true",
        help="Remove simple triggers 90%%/100%%/SLA breach (scripts:automatization) for uplink interfaces and exit",
    )
    parser.add_argument(
        "--delete-util-triggers",
        action="store_true",
        help="Remove uplink utilization warn/crit triggers (scripts:automatization) and exit",
    )
    args = parser.parse_args()

    nb_url = os.environ.get("NETBOX_URL")
    nb_token = os.environ.get("NETBOX_TOKEN")
    tag = border_device_tag()

    zabbix_url, zabbix_token = _get_zabbix_url_token()
    if not zabbix_url or not zabbix_token:
        print("Set ZABBIX_URL and ZABBIX_TOKEN", file=sys.stderr)
        sys.exit(1)

    if not validate_zabbix_token(zabbix_url, zabbix_token, debug=args.debug):
        print("Invalid or expired ZABBIX_TOKEN", file=sys.stderr)
        sys.exit(1)

    if args.dry_run and (args.delete_link_triggers or args.delete_util_triggers):
        print(
            "Error: --dry-run cannot be used with --delete-link-triggers or --delete-util-triggers",
            file=sys.stderr,
        )
        sys.exit(1)

    dry_ssh_path = getattr(args, "dry_ssh", None)
    dry_ssh_devices = load_dry_ssh(dry_ssh_path) if dry_ssh_path else None

    if args.inventory_file:
        try:
            inventory_report = load_inventory_report(args.inventory_file)
        except (OSError, json.JSONDecodeError) as e:
            print("failed to read inventory file {}: {}".format(args.inventory_file, e), file=sys.stderr)
            sys.exit(1)
        netbox_relations = netbox_interface_relations_from_report(inventory_report)
        if netbox_relations is None:
            from uplinks.netbox.inventory import _empty_netbox_interface_relations

            netbox_relations = _empty_netbox_interface_relations()
    else:
        if not nb_url or not nb_token:
            print("Set NETBOX_URL and NETBOX_TOKEN", file=sys.stderr)
            sys.exit(1)
        nb = pynetbox.api(nb_url, token=nb_token)
        inventory_report = fetch_uplink_inventory_report(nb, tag, debug=args.debug, exit_on_auth=True)
        netbox_relations = netbox_interface_relations_from_report(inventory_report)
        if netbox_relations is None:
            device_names = device_names_from_complete_inventory(inventory_report)
            netbox_relations = collect_netbox_interface_relations(
                nb, device_names, debug=args.debug, stats=inventory_report.get("stats")
            )
    from uplinks.netbox.inventory import finalize_inventory_read_stats

    finalize_inventory_read_stats(inventory_report.get("stats"))
    inventory_read_error = inventory_read_failed(inventory_report)
    if inventory_read_error:
        arm_netbox_incomplete_guard(inventory_report.get("stats"))
    commit_rates = commit_rates_from_inventory_report(inventory_report, debug=args.debug)
    host_to_util_ifaces = util_interfaces_by_host_from_inventory(
        dry_ssh_devices,
        inventory_report,
        debug=args.debug,
        netbox_relations=netbox_relations,
    )
    sync_util = bool(host_to_util_ifaces) and not args.no_util_triggers
    if commit_rates:
        if netbox_relations and (
            netbox_relations.get("member_to_aggregate")
            or netbox_relations.get("parent_children")
        ):
            commit_rates = map_commit_rates_to_zabbix_ifaces(
                commit_rates,
                netbox_relations=netbox_relations,
                dry_ssh_devices=dry_ssh_devices,
                debug=args.debug,
            )
        elif dry_ssh_devices:
            commit_rates = apply_logical_context(commit_rates, dry_ssh_devices, debug=args.debug)
    elif dry_ssh_path and dry_ssh_devices and args.debug:
        print("dry-ssh loaded; NetBox commit_rates empty", file=sys.stderr)

    if inventory_read_error:
        inv_error = (inventory_report.get("stats") or {}).get("error")
        if args.delete_link_triggers or args.delete_util_triggers:
            if args.delete_link_triggers:
                print(
                    "Skipping --delete-link-triggers: NetBox inventory read failed",
                    file=sys.stderr,
                )
            if args.delete_util_triggers:
                print(
                    "Skipping --delete-util-triggers: NetBox inventory read failed",
                    file=sys.stderr,
                )
            delete_only_link = (
                args.delete_link_triggers
                and not args.create_link_triggers
                and not args.dry_run
                and not args.delete_util_triggers
            )
            delete_only_util = (
                args.delete_util_triggers
                and not args.create_link_triggers
                and not args.dry_run
                and not args.delete_link_triggers
            )
            if delete_only_link or delete_only_util:
                sys.exit(1)
        if inv_error == ERROR_PROVIDERS_UNAVAILABLE:
            print("Error: NetBox providers unavailable", file=sys.stderr)
            sys.exit(1)
    else:
        if args.delete_link_triggers:
            deleted = delete_link_triggers(zabbix_url, zabbix_token, debug=args.debug)
            print("Deleted triggers uplinks 90%/100%/SLA breach: {}".format(deleted))
            if not args.create_link_triggers and not args.dry_run and not args.delete_util_triggers:
                return

        if args.delete_util_triggers:
            deleted_util = delete_util_triggers(zabbix_url, zabbix_token, debug=args.debug)
            print("Deleted uplink utilization triggers: {}".format(deleted_util))
            if not args.create_link_triggers and not args.dry_run and not args.delete_link_triggers:
                return

    if not commit_rates and not sync_util:
        if inventory_read_error:
            print(
                "NetBox inventory is incomplete; sync run marked as failed",
                file=sys.stderr,
            )
            sys.exit(1)
        print(
            "Nothing to sync: no NetBox circuits with cable and no uplinks in dry-ssh "
            "(or use --no-util-triggers). Check NETBOX_TAG / dry-ssh.json.",
            file=sys.stderr,
        )
        sys.exit(0)

    # Group by host for Zabbix (commit macros)
    host_to_iface_bps = {}
    for (dev_name, iface_name), bps in commit_rates.items():
        host_to_iface_bps.setdefault(dev_name, []).append((iface_name, bps))

    # Hosts in Zabbix by name (host or name); technical hostname for triggers
    hostnames = sorted(set(host_to_iface_bps.keys()) | set(host_to_util_ifaces.keys()))
    result, err = zabbix_request(
        zabbix_url, zabbix_token, "host.get",
        {"output": ["hostid", "host", "name"], "filter": {"host": hostnames}},
        debug=args.debug,
    )
    if err:
        print("Zabbix host.get: {}".format(err), file=sys.stderr)
        sys.exit(1)
    hostid_by_host = {h["host"]: h["hostid"] for h in result}
    host_technical_by_hostid = {h["hostid"]: h["host"] for h in result}
    missing = set(hostnames) - set(hostid_by_host.keys())
    if missing:
        result2, err2 = zabbix_request(
            zabbix_url, zabbix_token, "host.get",
            {"output": ["hostid", "host", "name"], "filter": {"name": list(missing)}},
            debug=args.debug,
        )
        if not err2 and result2:
            for h in result2:
                hostid_by_host[h["name"]] = h["hostid"]
                host_technical_by_hostid[h["hostid"]] = h["host"]
        missing = set(hostnames) - set(hostid_by_host.keys())
    if missing:
        print("Hosts not found in Zabbix: {}".format(", ".join(sorted(missing))), file=sys.stderr)

    updated = 0
    try:
        burst_pairs = (
            load_burst_pairs(
                inventory_report=inventory_report,
                dry_ssh_devices=dry_ssh_devices,
                netbox_relations=netbox_relations,
                debug=args.debug,
            )
            if args.create_link_triggers
            else set()
        )
        burst_meta = (
            load_burst_metadata(
                inventory_report=inventory_report,
                dry_ssh_devices=dry_ssh_devices,
                netbox_relations=netbox_relations,
                debug=args.debug,
            )
            if args.create_link_triggers
            else {}
        )
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    for dev_name in hostnames:
        if dev_name not in hostid_by_host:
            continue
        hostid = hostid_by_host[dev_name]
        zabbix_host = host_technical_by_hostid.get(hostid) or dev_name
        iface_bps_list = host_to_iface_bps.get(dev_name, [])
        util_ifaces = host_to_util_ifaces.get(dev_name, [])

        if args.dry_run:
            parts = []
            if iface_bps_list:
                bps_macros = []
                for iface_name, bps in iface_bps_list:
                    bps_macros.append("{}={}".format(_macro_name_for_interface(iface_name), int(bps * THRESHOLD_PERCENT_HIGH / 100)))
                parts.append("BPS: " + ", ".join(bps_macros))
            if sync_util and util_ifaces:
                parts.append(
                    "UTIL: {} ifaces ({}%/{}%)".format(
                        len(util_ifaces), UPLINK_UTIL_WARN_PERCENT, UPLINK_UTIL_CRIT_PERCENT
                    )
                )
            print("[dry-run] {} (hostid {}): {}".format(dev_name, hostid, "; ".join(parts)), file=sys.stderr)
            updated += 1
            continue

        if iface_bps_list and not inventory_read_error:
            new_bps_macros = []
            for iface_name, bps in iface_bps_list:
                new_bps_macros.append({
                    "macro": _macro_name_for_interface(iface_name),
                    "value": str(int(bps * THRESHOLD_PERCENT_HIGH / 100)),
                    "type": "0",
                })
                new_bps_macros.append({
                    "macro": _macro_name_warn_for_interface(iface_name),
                    "value": str(int(bps * THRESHOLD_PERCENT_WARN / 100)),
                    "type": "0",
                })
            ok, err = set_zabbix_host_if_util_macros(
                zabbix_url, zabbix_token, hostid, new_bps_macros, debug=args.debug
            )
            if not ok:
                print(
                    "Error updating BPS macros for {}: {}".format(dev_name, err or "usermacro"),
                    file=sys.stderr,
                )
        elif iface_bps_list and inventory_read_error:
            print(
                "Skipping BPS macro update for {}: NetBox data is incomplete".format(dev_name),
                file=sys.stderr,
            )

        util_macros_n = 0
        util_triggers_n = 0
        if sync_util and util_ifaces:
            util_macros_n, util_triggers_n, util_errors = sync_uplink_utilization_for_host(
                zabbix_url,
                zabbix_token,
                zabbix_host,
                hostid,
                util_ifaces,
                dry_run=False,
                debug=args.debug,
                netbox_data_complete=not inventory_read_error,
            )
            for line in util_errors:
                print(" {}: {}".format(dev_name, line), file=sys.stderr)

        created_triggers_for = 0
        if args.create_link_triggers and iface_bps_list and not inventory_read_error:
            for iface_name, _bps in iface_bps_list:
                if (dev_name, iface_name) not in burst_pairs:
                    continue
                binfo = burst_meta.get((dev_name, iface_name))
                link_tags = (
                    burst_link_trigger_tags_no_sla(binfo["provider"], binfo["circuit_id"])
                    if binfo
                    else None
                )
                sla_tags = (
                    burst_sla_breach_trigger_tags(binfo["provider"], binfo["circuit_id"])
                    if binfo
                    else None
                )
                ok_tr, err_tr = ensure_simple_threshold_trigger(
                    zabbix_url, zabbix_token, zabbix_host, hostid, iface_name, debug=args.debug, link_tags=link_tags
                )
                if not ok_tr:
                    print(" {}: trigger 100% - {}".format(iface_name, err_tr or "error"), file=sys.stderr)
                ok_w, err_w = ensure_simple_warn_trigger(
                    zabbix_url, zabbix_token, zabbix_host, hostid, iface_name, debug=args.debug, link_tags=link_tags
                )
                if not ok_w:
                    print(" {}: trigger 90% - {}".format(iface_name, err_w or "error"), file=sys.stderr)
                ok_sla, err_sla = ensure_burst_sla_breach_trigger(
                    zabbix_url, zabbix_token, zabbix_host, hostid, iface_name, debug=args.debug, link_tags=sla_tags
                )
                if not ok_sla:
                    print(" {}: SLA breach trigger - {}".format(iface_name, err_sla or "error"), file=sys.stderr)
                if ok_tr and ok_w and ok_sla:
                    created_triggers_for += 1

        removed = 0
        if not inventory_read_error:
            removed, rem_err = remove_threshold_items(
                zabbix_url, zabbix_token, hostid, debug=args.debug
            )
            if rem_err:
                print(" {}: deleting threshold items - {}".format(dev_name, rem_err), file=sys.stderr)
        else:
            print(
                "Skipping threshold item cleanup for {}: NetBox data is incomplete".format(
                    dev_name
                ),
                file=sys.stderr,
            )

        msg_parts = []
        if iface_bps_list and not inventory_read_error:
            msg_parts.append("{} BPS macros".format(len(iface_bps_list) * 2))
        if sync_util and util_ifaces:
            if not inventory_read_error:
                msg_parts.append(
                    "util {} macros, triggers {}/{}".format(
                        util_macros_n, util_triggers_n, len(util_ifaces)
                    )
                )
            elif util_triggers_n:
                msg_parts.append(
                    "util triggers {}/{}".format(util_triggers_n, len(util_ifaces))
                )
        if args.create_link_triggers:
            msg_parts.append("Burst link triggers: {}".format(created_triggers_for))
        if removed:
            msg_parts.append("removed {} threshold items".format(removed))
        print("OK: {} - {}".format(dev_name, ", ".join(msg_parts) if msg_parts else "no changes"))
        updated += 1

    if inventory_read_error:
        print(
            "Partial apply: {} hosts processed, {} commit pairs from NetBox, {} hosts with util interfaces.".format(
                updated, len(commit_rates), len(host_to_util_ifaces)
            )
        )
        print(
            "NetBox inventory is incomplete; sync run marked as failed",
            file=sys.stderr,
        )
        sys.exit(1)
    print(
        "Done: {} hosts updated, {} commit pairs from NetBox, {} hosts with util interfaces.".format(
            updated, len(commit_rates), len(host_to_util_ifaces)
        )
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Sync commit-rate macros (and optionally 90%/100% triggers) in Zabbix from NetBox circuits."""

import json
import os
import re
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
    collect_uplink_inventory,
    is_netbox_auth_error,
)
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
    TRIGGER_DESC_SEARCH,
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
DEFAULT_DRY_SSH = "dry-ssh.json"
DEFAULT_COMMIT_RATES = "commit_rates.json"


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


def interfaces_by_host_from_dry_ssh(dry_ssh_devices, physical_only=False):
    """
    Interface names per device from dry-ssh.json (uplink list from SSH collection).
    physical_only: skip LAG (aeN) and logical units (aeN.0); keep physical members (et-*, Ethernet*).
    Return: dict device_name -> [iface_name, ...] (unique, stable order).
    """
    result = {}
    if not dry_ssh_devices:
        return result
    for dev_name, ifaces in dry_ssh_devices.items():
        if not isinstance(ifaces, list):
            continue
        seen = set()
        names = []
        for entry in ifaces:
            if not isinstance(entry, dict):
                continue
            if physical_only and not is_physical_uplink_iface(entry):
                continue
            name = (entry.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)
        if names:
            result[dev_name] = names
    return result


def load_burst_pairs(path):
    """Load pairs (device, interface) with billing_model == 'Burst' from commit_rates.json."""
    if not path or not os.path.isfile(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return set()
    out = set()
    for dev_name, ifaces in (data or {}).items():
        if not isinstance(dev_name, str) or dev_name.startswith("_"):
            continue
        if not isinstance(ifaces, dict):
            continue
        for iface_name, entry in ifaces.items():
            if not isinstance(entry, dict):
                continue
            model = (entry.get("billing_model") or "").strip().lower()
            if model == "burst":
                out.add((dev_name, (iface_name or "").strip()))
    return out


def load_burst_metadata(path):
    """(device, interface) -> {provider, circuit_id} for billing_model=Burst."""
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    out = {}
    for dev_name, ifaces in (data or {}).items():
        if not isinstance(dev_name, str) or dev_name.startswith("_"):
            continue
        if not isinstance(ifaces, dict):
            continue
        for iface_name, entry in ifaces.items():
            if not isinstance(entry, dict):
                continue
            if (entry.get("billing_model") or "").strip().lower() != "burst":
                continue
            prov = (entry.get("provider") or "").strip()
            cid = (entry.get("circuit_id") or "").strip()
            if not prov or not cid:
                continue
            out[(dev_name, (iface_name or "").strip())] = {"provider": prov, "circuit_id": cid}
    return out


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
    Return: dict (dev_name, physical_iface) -> [logical_name, ...]
    """
    out = {}
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
            if not name or not phys:
                continue
            key = (dev_name, phys)
            out.setdefault(key, []).append(name)
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
        key = (dev_name, iface_name)
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


def get_commit_rates_from_netbox(nb, tag, debug=False):
    """
    By NetBox: active circuits on border devices (via shared inventory collector).
    Return: dict (device_name, interface_name) -> commit_rate_bps (int).
    Only devices with the tag tag are taken into account when tag is set.
    """
    report = collect_uplink_inventory(nb, tag=tag, debug=debug, active_only=True)
    stats = report.get("stats") or {}
    error = stats.get("error")
    if error == ERROR_AUTH_DENIED:
        print(_NETBOX_AUTH_MESSAGE, file=sys.stderr)
        if debug:
            print("collect_uplink_inventory: {}".format(error), file=sys.stderr)
        sys.exit(1)
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


def get_zabbix_host_macros(url, token, hostids, debug=False):
    """Get host macros. Return hostid -> list of {macro, value, type, context?, hostmacroid?}."""
    if not hostids:
        return {}
    result, err = zabbix_request(
        url, token, "usermacro.get",
        {"hostids": list(hostids), "output": ["macro", "value", "type", "context", "hostmacroid"]},
        debug=debug,
    )
    if err:
        return {str(hid): [] for hid in hostids}
    out = {str(hid): [] for hid in hostids}
    for m in (result or []):
        hid = str(m.get("hostid", ""))
        if not hid or hid not in out:
            continue
        entry = {"macro": m.get("macro", ""), "value": m.get("value", ""), "type": str(m.get("type", "0"))}
        if m.get("context") not in (None, ""):
            entry["context"] = m.get("context")
        if m.get("hostmacroid") is not None:
            entry["hostmacroid"] = m["hostmacroid"]
        out[hid].append(entry)
    return out


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
    """
    allowed = {(n or "").strip() for n in (allowed_iface_names or [])}
    res, err = zabbix_request(
        url,
        token,
        "trigger.get",
        {
            "hostids": [hostid],
            "output": ["triggerid", "description"],
            "search": {"description": "Uplink utilization"},
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
        if tags and not any(
            tg.get("tag") == TRIGGER_TAG_NAME and tg.get("value") == TRIGGER_TAG_VALUE
            for tg in tags
        ):
            continue
        m = re.match(r"Interface\s+([^:]+):", desc)
        if not m:
            continue
        iface = m.group(1).strip()
        if iface in allowed:
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
    url, token, host_technical, hostid, iface_names, dry_run=False, debug=False
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
    pruned = prune_util_triggers_on_host(url, token, hostid, iface_names, debug=debug)
    if pruned and debug:
        print("Pruned {} stale util triggers on hostid {}".format(pruned, hostid), file=sys.stderr)
    ok, err = set_zabbix_host_uplink_util_macros(url, token, hostid, util_macros, debug=debug)
    if not ok:
        return 0, 0, [err or "usermacro"]
    triggers_ok = 0
    errors = []
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
    return len(util_macros), triggers_ok, errors


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
            "output": ["triggerid", "description", "priority", "status", "expression"],
            "search": {"description": "Interface {}:".format((iface_name or "").strip())},
        },
        debug=debug,
    )
    if err:
        return False, err
    for t in (existing or []):
        desc = t.get("description") or ""
        if not (
            desc == description
            or desc.endswith(TRIGGER_DESC_100_SUFFIX)
            or desc.endswith(LEGACY_TRIGGER_DESC_100_SUFFIX)
        ):
            continue
        tid = t.get("triggerid")
        if tid:
            upd = {}
            if desc != description:
                upd["description"] = description
            if (t.get("expression") or "").strip() != expression:
                upd["expression"] = expression
            if str(t.get("priority", "0")) != str(TRIGGER_PRIORITY_HIGH):
                upd["priority"] = TRIGGER_PRIORITY_HIGH
            if str(t.get("status", "0")) != "0": # enable if disabled
                upd["status"] = "0"
            if link_tags is not None:
                upd["tags"] = link_tags
            if upd:
                zabbix_request(url, token, "trigger.update", {"triggerid": tid, **upd}, debug=debug)
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
    high_description = "Interface {}: {}".format((iface_name or "").strip(), TRIGGER_DESC_100_SUFFIX)
    legacy_high_description = "Interface {}: {}".format((iface_name or "").strip(), LEGACY_TRIGGER_DESC_100_SUFFIX)
    existing, err = zabbix_request(
        url, token, "trigger.get",
        {
            "hostids": [hostid],
            "output": ["triggerid", "description", "status", "expression"],
            "search": {"description": "Interface {}:".format((iface_name or "").strip())},
        },
        debug=debug,
    )
    if err:
        return False, err
    for t in (existing or []):
        desc = t.get("description") or ""
        if not (
            desc == description
            or desc.endswith(TRIGGER_DESC_90_SUFFIX)
            or desc.endswith(LEGACY_TRIGGER_DESC_90_SUFFIX)
        ):
            continue
        tid = t.get("triggerid")
        if tid:
            upd = {}
            if desc != description:
                upd["description"] = description
            if (t.get("expression") or "").strip() != expression:
                upd["expression"] = expression
            if str(t.get("status", "0")) != "0":
                upd["status"] = "0"
            # Let's find a trigger 100% on the same interface and add a dependency 90% -> 100%.
            high_id = None
            res_h, err_h = zabbix_request(
                url, token, "trigger.get",
                {"hostids": [hostid], "output": ["triggerid", "description"], "search": {"description": "Interface {}:".format((iface_name or "").strip())}},
                debug=debug,
            )
            if not err_h and res_h:
                for th in res_h:
                    d = th.get("description") or ""
                    if d == high_description or d == legacy_high_description or d.endswith(TRIGGER_DESC_100_SUFFIX) or d.endswith(LEGACY_TRIGGER_DESC_100_SUFFIX):
                        high_id = th.get("triggerid")
                        if high_id:
                            break
            if high_id:
                upd["dependencies"] = [{"triggerid": str(high_id)}]
            if link_tags is not None:
                upd["tags"] = link_tags
            if upd:
                zabbix_request(url, token, "trigger.update", {"triggerid": tid, **upd}, debug=debug)
        return True, None
    tags_payload = link_tags if link_tags is not None else [TRIGGER_TAG_SCRIPTS]
    # For new 90% triggers, we also set a dependence on 100%, so that there are no two PROBLEMs at the same time.
    high_id = None
    res_h, err_h = zabbix_request(
        url,
        token,
        "trigger.get",
        {
            "hostids": [hostid],
            "output": ["triggerid", "description"],
            "search": {"description": "Interface {}:".format((iface_name or "").strip())},
        },
        debug=debug,
    )
    if not err_h and res_h:
        for th in res_h:
            d = th.get("description") or ""
            if (
                d == high_description
                or d == legacy_high_description
                or d.endswith(TRIGGER_DESC_100_SUFFIX)
                or d.endswith(LEGACY_TRIGGER_DESC_100_SUFFIX)
            ):
                high_id = th.get("triggerid")
                if high_id:
                    break

    create_payload = {
        "description": description,
        "expression": expression,
        "priority": TRIGGER_PRIORITY_WARN,
        "tags": tags_payload,
    }
    if high_id:
        create_payload["dependencies"] = [{"triggerid": str(high_id)}]

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
            "output": ["triggerid", "description", "priority", "status", "expression"],
            "search": {"description": "Interface {}:".format((iface_name or "").strip())},
        },
        debug=debug,
    )
    if err:
        return False, err
    for t in (existing or []):
        desc = t.get("description") or ""
        if not (desc == description or desc.endswith(TRIGGER_DESC_SLA_BREACH_SUFFIX)):
            continue
        tid = t.get("triggerid")
        if tid:
            upd = {}
            if desc != description:
                upd["description"] = description
            if (t.get("expression") or "").strip() != expression:
                upd["expression"] = expression
            if str(t.get("priority", "0")) != str(TRIGGER_PRIORITY_SLA_BREACH):
                upd["priority"] = TRIGGER_PRIORITY_SLA_BREACH
            if str(t.get("status", "0")) != "0":
                upd["status"] = "0"
            if link_tags is not None:
                upd["tags"] = link_tags
            if upd:
                zabbix_request(url, token, "trigger.update", {"triggerid": tid, **upd}, debug=debug)
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
            "Sync Zabbix: {$UPLINK.BPS.*} from NetBox; {$UPLINK.UTIL.*} + utilization triggers "
            "from dry-ssh.json (all uplinks in file)."
        ),
    )
    parser.add_argument("-d", "--dry-ssh", default=None, metavar="FILE", help="dry-ssh.json: for a physics cable (e.g. et-0/0/3) set the macro context by logical name (ae5.0) for Zabbix")
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
        help="Do not create {$UPLINK.UTIL.*} macros or utilization triggers (default: enabled when dry-ssh is loaded)",
    )
    parser.add_argument(
        "-f", "--commit-rates", default=DEFAULT_COMMIT_RATES,
        help="Path to commit_rates.json (for trigger filter by billing_model=Burst)",
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
    tag = (os.environ.get("NETBOX_TAG") or "").strip() or "border"
    if not nb_url or not nb_token:
        print("Set NETBOX_URL and NETBOX_TOKEN", file=sys.stderr)
        sys.exit(1)

    zabbix_url, zabbix_token = _get_zabbix_url_token()
    if not zabbix_url or not zabbix_token:
        print("Set ZABBIX_URL and ZABBIX_TOKEN", file=sys.stderr)
        sys.exit(1)

    if not validate_zabbix_token(zabbix_url, zabbix_token, debug=args.debug):
        print("Invalid or expired ZABBIX_TOKEN", file=sys.stderr)
        sys.exit(1)

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

    dry_ssh_path = getattr(args, "dry_ssh", None) or (DEFAULT_DRY_SSH if os.path.isfile(DEFAULT_DRY_SSH) else None)
    dry_ssh_devices = load_dry_ssh(dry_ssh_path) if dry_ssh_path else None
    host_to_util_ifaces = interfaces_by_host_from_dry_ssh(dry_ssh_devices, physical_only=True)
    sync_util = bool(host_to_util_ifaces) and not args.no_util_triggers
    if dry_ssh_path and not dry_ssh_devices and not args.no_util_triggers:
        print(
            "dry-ssh not loaded (file empty or missing); utilization triggers skipped.",
            file=sys.stderr,
        )

    nb = pynetbox.api(nb_url, token=nb_token)
    commit_rates = get_commit_rates_from_netbox(nb, tag, debug=args.debug)
    if dry_ssh_devices and commit_rates:
        commit_rates = apply_logical_context(commit_rates, dry_ssh_devices, debug=args.debug)
    elif dry_ssh_path and dry_ssh_devices and args.debug:
        print("dry-ssh loaded; NetBox commit_rates empty", file=sys.stderr)

    if not commit_rates and not sync_util:
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
    burst_pairs = load_burst_pairs(args.commit_rates) if args.create_link_triggers else set()
    burst_meta = load_burst_metadata(args.commit_rates) if args.create_link_triggers else {}
    if args.create_link_triggers and args.debug:
        print("Burst pairs from {}: {}".format(args.commit_rates, len(burst_pairs)), file=sys.stderr)
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

        if iface_bps_list:
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
            )
            for line in util_errors:
                print(" {}: {}".format(dev_name, line), file=sys.stderr)

        created_triggers_for = 0
        if args.create_link_triggers and iface_bps_list:
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

        removed, rem_err = remove_threshold_items(zabbix_url, zabbix_token, hostid, debug=args.debug)
        if rem_err:
            print(" {}: deleting threshold items - {}".format(dev_name, rem_err), file=sys.stderr)

        msg_parts = []
        if iface_bps_list:
            msg_parts.append("{} BPS macros".format(len(iface_bps_list) * 2))
        if sync_util and util_ifaces:
            msg_parts.append(
                "util {} macros, triggers {}/{}".format(
                    util_macros_n, util_triggers_n, len(util_ifaces)
                )
            )
        if args.create_link_triggers:
            msg_parts.append("Burst link triggers: {}".format(created_triggers_for))
        if removed:
            msg_parts.append("removed {} threshold items".format(removed))
        print("OK: {} - {}".format(dev_name, ", ".join(msg_parts) if msg_parts else "no changes"))
        updated += 1

    print(
        "Done: {} hosts updated, {} commit pairs from NetBox, {} hosts with util from dry-ssh.".format(
            updated, len(commit_rates), len(host_to_util_ifaces)
        )
    )


if __name__ == "__main__":
    main()

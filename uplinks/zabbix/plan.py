"""Read-only Zabbix uplinks plan: current state vs planned sync changes."""

import json
import os
import re

import pynetbox

from uplinks.netbox.inventory import (
    ERROR_AUTH_DENIED,
    ERROR_PARTIAL_READ,
    ERROR_PROVIDERS_UNAVAILABLE,
    NetBoxAuthError,
    burst_circuits_unique_from_inventory,
    collect_netbox_interface_relations,
    collect_provider_limits_gbps,
    collect_provider_slo_percent,
    collect_uplink_inventory,
    device_names_from_complete_inventory,
    enrich_inventory_provider_stats,
    finalize_inventory_read_stats,
    inventory_read_failed,
    load_inventory_report,
    load_uplink_provider_context,
    map_commit_rates_to_zabbix_ifaces,
    netbox_client_from_env,
    netbox_interface_relations_from_report,
    project_circuit_scope,
    providers_from_complete_inventory,
    resolve_border_device_tag,
    scoped_devices_from_inventory_report,
)
from uplinks.zabbix.client import (
    fetch_zabbix_hosts_and_items,
    get_zabbix_url_token,
    stale_util_triggers,
    util_trigger_get_params,
    validate_zabbix_token,
    zabbix_request,
)
from uplinks_config import (
    DASHBOARD_NAME,
    DASHBOARD_NAME_BY_LOCATION,
    DASHBOARD_NAME_BY_PROVIDER,
    MAP_NAME,
    PROJECT_PROVIDER_SLO_PERCENT,
    PROVIDERS_FOR_SUMMARY,
    SLA_TRIGGER_FUNCTION_PERIOD,
    THRESHOLD_PERCENT_HIGH,
    THRESHOLD_PERCENT_WARN,
    TRIGGER_DESC_UTIL_CRIT_SUFFIX,
    TRIGGER_DESC_UTIL_WARN_SUFFIX,
    UPLINKS_AGGREGATE_HOST_PREFIX,
)

# Used by tests to ensure plan mode never calls mutating API methods.
ZABBIX_MUTATING_METHODS = frozenset(
    {
        "host.create",
        "host.update",
        "host.delete",
        "item.create",
        "item.update",
        "item.delete",
        "trigger.create",
        "trigger.update",
        "trigger.delete",
        "usermacro.create",
        "usermacro.update",
        "usermacro.delete",
        "map.create",
        "map.update",
        "map.delete",
        "dashboard.create",
        "dashboard.update",
        "dashboard.delete",
        "service.create",
        "service.update",
        "service.delete",
        "sla.create",
        "sla.update",
        "sla.delete",
    }
)

NOT_EVALUATED = "not_evaluated"
AGGREGATE_ITEM_KEY_IN = "aggregate.bits.in[]"
AGGREGATE_ITEM_KEY_OUT = "aggregate.bits.out[]"
MAP_ELEMENT_TYPE_HOST = 0
MAP_ELEMENT_TYPE_IMAGE = 4
DEFAULT_PARENT_SERVICE = "Uplinks providers"

def inventory_plan_gate(report):
    """Fail-closed gate for scoped inventory used by --plan."""
    stats = report.get("stats") or {}
    error = stats.get("error")
    if error == ERROR_AUTH_DENIED:
        return False, "NetBox authentication failed (check NETBOX_TOKEN)"
    if error == ERROR_PROVIDERS_UNAVAILABLE:
        return False, "NetBox providers unavailable"
    if error == ERROR_PARTIAL_READ:
        return False, "NetBox inventory is incomplete (partial read failure)"
    if not report.get("complete"):
        return False, "No complete scoped inventory entries"
    return True, ""


def collect_scoped_inventory(nb, tag, debug=False):
    """Project inventory: Circuit type Uplink + built-in status Active."""
    return collect_uplink_inventory(
        nb,
        tag=tag,
        debug=debug,
        active_only=True,
        circuit_scope=project_circuit_scope(),
    )


def _empty_categories():
    return {"create": [], "update": [], "unchanged": [], "delete": [], "skipped": []}


def _category_not_evaluated(reason):
    return {
        "status": NOT_EVALUATED,
        "reason": reason,
        "create": [],
        "update": [],
        "unchanged": [],
        "delete": [],
        "skipped": [],
    }


def _macro_entries_for_iface(iface_name, bps):
    from zabbix_sync_commit_rate import (
        _macro_name_for_interface,
        _macro_name_warn_for_interface,
    )

    return [
        {
            "macro": _macro_name_for_interface(iface_name),
            "value": str(int(bps * THRESHOLD_PERCENT_HIGH / 100)),
        },
        {
            "macro": _macro_name_warn_for_interface(iface_name),
            "value": str(int(bps * THRESHOLD_PERCENT_WARN / 100)),
        },
    ]


def _resolve_hostids(url, token, hostnames, debug=False):
    if not hostnames:
        return {}, {}
    result, err = zabbix_request(
        url,
        token,
        "host.get",
        {"output": ["hostid", "host", "name"], "filter": {"host": list(hostnames)}},
        debug=debug,
    )
    if err:
        return None, "host.get: {}".format(err)
    hostid_by_name = {h["host"]: h["hostid"] for h in result}
    host_technical_by_hostid = {h["hostid"]: h["host"] for h in result}
    missing = set(hostnames) - set(hostid_by_name.keys())
    if missing:
        result2, err2 = zabbix_request(
            url,
            token,
            "host.get",
            {"output": ["hostid", "host", "name"], "filter": {"name": list(missing)}},
            debug=debug,
        )
        if not err2 and result2:
            for h in result2:
                hostid_by_name[h["name"]] = h["hostid"]
                host_technical_by_hostid[h["hostid"]] = h["host"]
    return hostid_by_name, host_technical_by_hostid


def _get_host_macros(url, token, hostids, debug=False):
    """Plan-only macro read; fail closed on API error (unlike sync helper)."""
    if not hostids:
        return {}, None
    result, err = zabbix_request(
        url,
        token,
        "usermacro.get",
        {
            "hostids": list(hostids),
            "output": ["hostid", "macro", "value", "type", "context", "hostmacroid"],
        },
        debug=debug,
    )
    if err:
        return None, "usermacro.get: {}".format(err)
    out = {str(hid): [] for hid in hostids}
    for m in result or []:
        hid = str(m.get("hostid", ""))
        if not hid or hid not in out:
            continue
        entry = {"macro": m.get("macro", ""), "value": m.get("value", ""), "type": str(m.get("type", "0"))}
        if m.get("context") not in (None, ""):
            entry["context"] = m.get("context")
        if m.get("hostmacroid") is not None:
            entry["hostmacroid"] = m["hostmacroid"]
        out[hid].append(entry)
    return out, None


def _plan_bps_macros(url, token, commit_rates, hostid_by_name, debug=False):
    categories = _empty_categories()
    current = {}
    hostids = [hostid_by_name[h] for h in commit_rates.keys() if h in hostid_by_name]
    macros_by_hostid, macro_err = _get_host_macros(url, token, hostids, debug=debug)
    if macro_err:
        return None, macro_err
    for dev_name in sorted(commit_rates.keys()):
        iface_bps = commit_rates[dev_name]
        if dev_name not in hostid_by_name:
            for iface_name, bps in sorted(iface_bps):
                categories["skipped"].append(
                    {
                        "host": dev_name,
                        "interface": iface_name,
                        "macros": _macro_entries_for_iface(iface_name, bps),
                        "reason": "host not found in Zabbix",
                    }
                )
            continue
        hostid = str(hostid_by_name[dev_name])
        existing = {m.get("macro"): m.get("value") for m in macros_by_hostid.get(hostid, [])}
        current[dev_name] = [
            {"macro": macro, "value": value}
            for macro, value in sorted(existing.items())
            if macro.startswith("{$UPLINK.BPS.")
        ]
        for iface_name, bps in sorted(iface_bps):
            expected = {e["macro"]: e["value"] for e in _macro_entries_for_iface(iface_name, bps)}
            row = {"host": dev_name, "interface": iface_name, "macros": list(expected.items())}
            missing = [macro for macro in expected if macro not in existing]
            changed = [
                macro
                for macro, value in expected.items()
                if macro in existing and str(existing[macro]) != str(value)
            ]
            if missing:
                categories["create"].append(
                    {
                        "host": dev_name,
                        "interface": iface_name,
                        "macros": [{"macro": m, "value": expected[m]} for m in missing],
                    }
                )
            if changed:
                categories["update"].append(
                    {
                        "host": dev_name,
                        "interface": iface_name,
                        "macros": [{"macro": m, "value": expected[m]} for m in changed],
                    }
                )
            if not missing and not changed:
                categories["unchanged"].append(row)
    return categories, current


def _plan_util_triggers(url, token, host_to_ifaces, hostid_by_name, allow_delete=True, debug=False):
    categories = _empty_categories()
    current = {}
    for dev_name, ifaces in sorted(host_to_ifaces.items()):
        if dev_name not in hostid_by_name:
            for iface_name in ifaces:
                categories["skipped"].append(
                    {
                        "host": dev_name,
                        "interface": iface_name,
                        "triggers": [TRIGGER_DESC_UTIL_WARN_SUFFIX, TRIGGER_DESC_UTIL_CRIT_SUFFIX],
                        "reason": "host not found in Zabbix",
                    }
                )
            continue
        hostid = hostid_by_name[dev_name]
        allowed = {(n or "").strip() for n in (ifaces or [])}
        res, err = zabbix_request(
            url,
            token,
            "trigger.get",
            util_trigger_get_params([hostid]),
            debug=debug,
        )
        if err:
            return None, "trigger.get: {}".format(err)
        host_triggers = []
        for trig in res or []:
            desc = (trig.get("description") or "").strip()
            if not (
                desc.endswith(TRIGGER_DESC_UTIL_WARN_SUFFIX)
                or desc.endswith(TRIGGER_DESC_UTIL_CRIT_SUFFIX)
            ):
                continue
            host_triggers.append(
                {
                    "triggerid": trig.get("triggerid"),
                    "description": desc,
                    "tags": trig.get("tags") or [],
                }
            )
        for trig in stale_util_triggers(res, allowed):
            desc = (trig.get("description") or "").strip()
            m = re.match(r"Interface\s+([^:]+):", desc)
            iface = m.group(1).strip() if m else ""
            tid = trig.get("triggerid")
            if not tid:
                continue
            if allow_delete:
                categories["delete"].append(
                    {
                        "host": dev_name,
                        "interface": iface,
                        "triggerid": tid,
                        "description": desc,
                    }
                )
            else:
                categories["skipped"].append(
                    {
                        "host": dev_name,
                        "interface": iface,
                        "triggerid": tid,
                        "description": desc,
                        "suppressed": "delete",
                        "reason": "delete suppressed (destructive plan disabled)",
                    }
                )
        current[dev_name] = [
            {"triggerid": t["triggerid"], "description": t["description"]} for t in host_triggers
        ]
        existing_desc = {t["description"] for t in host_triggers}
        for iface_name in ifaces:
            want = [
                "Interface {}: {}".format(iface_name, TRIGGER_DESC_UTIL_WARN_SUFFIX),
                "Interface {}: {}".format(iface_name, TRIGGER_DESC_UTIL_CRIT_SUFFIX),
            ]
            missing = [d for d in want if d not in existing_desc]
            present = [d for d in want if d in existing_desc]
            if missing:
                categories["create"].append({"host": dev_name, "interface": iface_name, "triggers": missing})
            if present and not missing:
                categories["unchanged"].append({"host": dev_name, "interface": iface_name, "triggers": present})
    return categories, current


def _sanitize_provider_host_name(name):
    from zabbix_provider_aggregate import _sanitize_provider_name

    return _sanitize_provider_name(UPLINKS_AGGREGATE_HOST_PREFIX + name)


def _aggregate_hostid_by_provider(url, token, providers, debug=False):
    """Return provider -> hostid for existing aggregate hosts (read-only)."""
    if not providers:
        return {}, None
    technical_names = [_sanitize_provider_host_name(p) for p in providers]
    technical_to_provider = dict(zip(technical_names, providers))
    result, err = zabbix_request(
        url,
        token,
        "host.get",
        {"output": ["hostid", "host", "name"], "filter": {"host": technical_names}},
        debug=debug,
    )
    if err:
        return None, "host.get aggregate: {}".format(err)
    hostid_by_provider = {}
    found_technical = set()
    for row in result or []:
        technical = row.get("host") or ""
        if technical in technical_to_provider:
            hostid_by_provider[technical_to_provider[technical]] = str(row.get("hostid"))
            found_technical.add(technical)
    missing = [p for p in providers if _sanitize_provider_host_name(p) not in found_technical]
    if missing:
        names_filter = [UPLINKS_AGGREGATE_HOST_PREFIX + p for p in missing]
        result2, err2 = zabbix_request(
            url,
            token,
            "host.get",
            {"output": ["hostid", "host", "name"], "filter": {"name": names_filter}},
            debug=debug,
        )
        if err2:
            return None, "host.get aggregate: {}".format(err2)
        for row in result2 or []:
            visible = row.get("name") or ""
            if not visible.startswith(UPLINKS_AGGREGATE_HOST_PREFIX):
                continue
            provider = visible[len(UPLINKS_AGGREGATE_HOST_PREFIX) :]
            if provider in missing:
                hostid_by_provider[provider] = str(row.get("hostid"))
    return hostid_by_provider, None


def _expected_aggregate_trigger_descriptions(limit_bps):
    limit_gbps = limit_bps / 1e9
    return {
        "warn": "Provider aggregate traffic >= {}% of limit ({} Gbps)".format(
            THRESHOLD_PERCENT_WARN, limit_gbps
        ),
        "high": "Provider aggregate traffic >= 100% of limit ({} Gbps)".format(limit_gbps),
        "sla": "Provider aggregate SLA breach: >= 100% of limit for {} ({} Gbps)".format(
            SLA_TRIGGER_FUNCTION_PERIOD, limit_gbps
        ),
    }


def _limit_gbps_from_trigger_description(description):
    match = re.search(r"\(([\d.]+)\s*Gbps\)", description or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except (TypeError, ValueError):
        return None


def _plan_aggregate_hosts(url, token, providers, debug=False):
    categories = _empty_categories()
    categories["calculated_items"] = _empty_categories()
    categories["limit_triggers"] = _empty_categories()
    current = []
    hostid_by_provider, err = _aggregate_hostid_by_provider(url, token, providers, debug=debug)
    if err:
        return None, err
    for provider in sorted(providers):
        technical = _sanitize_provider_host_name(provider)
        hostid = hostid_by_provider.get(provider)
        if hostid:
            current.append(
                {
                    "provider": provider,
                    "hostid": hostid,
                    "host": technical,
                    "name": UPLINKS_AGGREGATE_HOST_PREFIX + provider,
                }
            )
            categories["unchanged"].append({"provider": provider, "host": technical})
        else:
            categories["create"].append(
                {
                    "provider": provider,
                    "host": technical,
                    "display_name": UPLINKS_AGGREGATE_HOST_PREFIX + provider,
                }
            )
    return categories, current


def _limit_triggers_not_evaluated(reason):
    return _category_not_evaluated(reason)


def _plan_aggregate_items_and_triggers(
    url,
    token,
    providers,
    uplink_ctx,
    hostid_by_provider,
    allow_delete,
    debug=False,
):
    """Practical diff for calculated items and provider limit triggers."""
    if uplink_ctx.get("zabbix_items_read_error"):
        detail = uplink_ctx.get("zabbix_items_read_detail") or "Zabbix uplink items unread"
        msg = "{}; aggregate item/trigger diff suppressed".format(detail)
        return _category_not_evaluated(msg), _limit_triggers_not_evaluated(msg), None

    items_plan = _empty_categories()
    limits_read = uplink_ctx.get("provider_limits_read", "ok")
    if limits_read != "ok":
        if limits_read == "unavailable":
            trig_reason = (
                "NetBox client unavailable; provider aggregate limits unread "
                "(cannot distinguish missing limit from unread limits)"
            )
        else:
            trig_reason = "NetBox provider limits read failed; limit trigger diff suppressed"
        triggers_plan = _limit_triggers_not_evaluated(trig_reason)
    else:
        triggers_plan = _empty_categories()

    edges = uplink_ctx.get("edges") or []
    provider_limits_gbps = uplink_ctx.get("provider_limits_gbps") or {}

    by_provider = {}
    for edge in edges:
        isp = (edge[3] or "").strip()
        if not isp:
            continue
        by_provider.setdefault(isp, []).append(edge)

    providers_with_links = sorted(p for p in providers if by_provider.get(p))
    for provider in providers_with_links:
        hostid = hostid_by_provider.get(provider)
        pending_host_create = hostid is None
        refs_in = [edge for edge in by_provider[provider] if edge[4]]
        if not refs_in:
            items_plan["skipped"].append(
                {"provider": provider, "reason": "no Bits received items for aggregate formula"}
            )
            continue

        if not pending_host_create:
            res, err = zabbix_request(
                url,
                token,
                "item.get",
                {
                    "output": ["itemid", "key_"],
                    "hostids": [hostid],
                    "search": {"key_": "aggregate.bits"},
                },
                debug=debug,
            )
            if err:
                detail = "item.get aggregate: {}".format(err)
                uplink_ctx["zabbix_items_read_error"] = True
                uplink_ctx["zabbix_items_read_detail"] = detail
                msg = "{}; aggregate item/trigger diff suppressed".format(detail)
                return _category_not_evaluated(msg), _limit_triggers_not_evaluated(msg), None
            keys_present = {(row.get("key_") or "").strip() for row in (res or [])}
            missing_keys = [
                key
                for key in (AGGREGATE_ITEM_KEY_IN, AGGREGATE_ITEM_KEY_OUT)
                if key not in keys_present
            ]
            if missing_keys:
                items_plan["create"].append({"provider": provider, "keys": missing_keys})
            else:
                items_plan["unchanged"].append(
                    {"provider": provider, "keys": [AGGREGATE_ITEM_KEY_IN, AGGREGATE_ITEM_KEY_OUT]}
                )
        else:
            items_plan["create"].append(
                {
                    "provider": provider,
                    "keys": [AGGREGATE_ITEM_KEY_IN, AGGREGATE_ITEM_KEY_OUT],
                    "pending_host_create": True,
                }
            )

        if limits_read != "ok":
            continue

        limit_gbps = provider_limits_gbps.get(provider)
        limit_bps = limit_gbps * 1e9 if limit_gbps is not None else None
        if limit_bps is None:
            if pending_host_create:
                continue
            if allow_delete:
                res, err = zabbix_request(
                    url,
                    token,
                    "trigger.get",
                    {
                        "output": ["triggerid", "description"],
                        "hostids": [hostid],
                        "search": {"description": "Provider aggregate"},
                    },
                    debug=debug,
                )
                if err:
                    return None, None, "trigger.get aggregate: {}".format(err)
                for trig in res or []:
                    triggers_plan["delete"].append(
                        {
                            "provider": provider,
                            "triggerid": trig.get("triggerid"),
                            "description": trig.get("description"),
                            "reason": "provider has no aggregate limit",
                        }
                    )
            continue

        expected = _expected_aggregate_trigger_descriptions(limit_bps)
        expected_desc = set(expected.values())
        if pending_host_create:
            triggers_plan["create"].append(
                {"provider": provider, "triggers": sorted(expected_desc), "pending_host_create": True}
            )
            continue

        res, err = zabbix_request(
            url,
            token,
            "trigger.get",
            {
                "output": ["triggerid", "description"],
                "hostids": [hostid],
                "search": {"description": "Provider aggregate"},
            },
            debug=debug,
        )
        if err:
            return None, None, "trigger.get aggregate: {}".format(err)
        existing = [((row.get("description") or "").strip(), row) for row in (res or [])]
        existing_desc = {desc for desc, _row in existing if desc}
        missing_desc = sorted(expected_desc - existing_desc)
        if missing_desc:
            triggers_plan["create"].append({"provider": provider, "triggers": missing_desc})

        matched_desc = expected_desc & existing_desc
        if matched_desc and not missing_desc and len(matched_desc) == len(expected_desc):
            triggers_plan["unchanged"].append(
                {"provider": provider, "triggers": sorted(matched_desc)}
            )
        else:
            for desc, row in existing:
                if desc in expected_desc:
                    continue
                limit_in_desc = _limit_gbps_from_trigger_description(desc)
                expected_limit = limit_gbps
                if limit_in_desc is not None and expected_limit is not None:
                    if abs(limit_in_desc - expected_limit) > 0.001:
                        if allow_delete:
                            triggers_plan["delete"].append(
                                {
                                    "provider": provider,
                                    "triggerid": row.get("triggerid"),
                                    "description": desc,
                                    "reason": "stale aggregate limit",
                                }
                            )
                        else:
                            triggers_plan["skipped"].append(
                                {
                                    "provider": provider,
                                    "triggerid": row.get("triggerid"),
                                    "description": desc,
                                    "suppressed": "delete",
                                    "reason": "stale aggregate limit (delete suppressed)",
                                }
                            )
                    else:
                        triggers_plan["update"].append(
                            {
                                "provider": provider,
                                "triggerid": row.get("triggerid"),
                                "description": desc,
                                "reason": "unexpected aggregate trigger variant",
                            }
                        )
                elif desc.startswith("Provider aggregate"):
                    if allow_delete:
                        triggers_plan["delete"].append(
                            {
                                "provider": provider,
                                "triggerid": row.get("triggerid"),
                                "description": desc,
                                "reason": "obsolete aggregate trigger",
                            }
                        )
                    else:
                        triggers_plan["skipped"].append(
                            {
                                "provider": provider,
                                "triggerid": row.get("triggerid"),
                                "description": desc,
                                "suppressed": "delete",
                                "reason": "obsolete aggregate trigger (delete suppressed)",
                            }
                        )

    return items_plan, triggers_plan, None


def _prepare_uplink_plan_context(
    url,
    token,
    inventory_report,
    dry_ssh_devices,
    hostid_by_name,
    netbox_relations,
    debug=False,
):
    """Load scoped edges and optional NetBox provider limits for map/dashboard/aggregate planning."""
    from zabbix_uplinks_dashboard import _build_edges

    inv_ctx = load_uplink_provider_context(
        dry_ssh_devices=dry_ssh_devices,
        debug=debug,
        inventory_report=inventory_report,
    )
    if inv_ctx is None:
        from uplinks.netbox.inventory import device_iface_provider_map_from_inventory

        inv_ctx = {
            "device_iface_to_provider": device_iface_provider_map_from_inventory(
                inventory_report,
                dry_ssh_devices=dry_ssh_devices,
                netbox_relations=netbox_relations,
                debug=debug,
            ),
            "providers": providers_from_complete_inventory(inventory_report),
            "netbox_interface_relations": netbox_relations,
        }

    device_iface_to_provider = inv_ctx.get("device_iface_to_provider") or {}
    devices = scoped_devices_from_inventory_report(
        inventory_report, device_iface_to_provider
    )
    found_hostnames = sorted(set(devices.keys()) & set(hostid_by_name.keys()))
    items_by_host_iface = {}
    resolved_hostids = {name: hostid_by_name[name] for name in found_hostnames}
    zabbix_items_read_error = False
    zabbix_items_read_detail = ""
    if found_hostnames:
        _hosts, items_by_host_iface, err = fetch_zabbix_hosts_and_items(
            url, token, set(found_hostnames), debug=debug
        )
        if err:
            zabbix_items_read_error = True
            zabbix_items_read_detail = err
            items_by_host_iface = {}

    edges = _build_edges(
        devices,
        resolved_hostids,
        items_by_host_iface,
        desc_to_name={},
        device_iface_to_provider=device_iface_to_provider,
        inventory_scoped=True,
        netbox_relations=netbox_relations,
    )

    provider_limits_gbps = {}
    provider_slo_percent = {}
    provider_limits_read = "ok"
    provider_slo_read = "ok"
    nb = netbox_client_from_env(debug=debug)
    if nb is None:
        provider_limits_read = "unavailable"
        provider_slo_read = "unavailable"
    else:
        provider_limits_gbps, limits_err = collect_provider_limits_gbps(nb, debug=debug)
        if limits_err:
            provider_limits_read = "error"
        slo, slo_err = collect_provider_slo_percent(nb, debug=debug)
        if slo_err:
            provider_slo_read = "error"
        else:
            provider_slo_percent = slo or {}

    limits_read_error = provider_limits_read != "ok"
    slo_read_error = provider_slo_read != "ok"

    return {
        "devices": devices,
        "edges": edges,
        "device_iface_to_provider": device_iface_to_provider,
        "providers": providers_from_complete_inventory(inventory_report),
        "burst_circuits": burst_circuits_unique_from_inventory(inventory_report),
        "provider_limits_gbps": provider_limits_gbps,
        "provider_slo_percent": provider_slo_percent,
        "provider_limits_read": provider_limits_read,
        "provider_slo_read": provider_slo_read,
        "limits_read_error": limits_read_error,
        "slo_read_error": slo_read_error,
        "zabbix_items_read_error": zabbix_items_read_error,
        "zabbix_items_read_detail": zabbix_items_read_detail,
        "netbox_relations": netbox_relations,
    }, None


def _map_selement_hostid(el):
    from zabbix_map import _selement_hostid

    return _selement_hostid(el)


def _expected_map_identities(edges):
    expected_hosts = set()
    expected_providers = set()
    expected_links = set()
    for edge in edges:
        _hostname, hostid, _iface, isp = edge[:4]
        if not hostid or not isp:
            continue
        hid = str(hostid)
        expected_hosts.add(hid)
        expected_providers.add(isp)
        expected_links.add((hid, isp))
    return expected_hosts, expected_providers, expected_links


def _actual_map_identities(selements, links):
    host_selement = {}
    isp_selement = {}
    actual_hosts = set()
    actual_providers = set()
    for el in selements or []:
        etype = int(el.get("elementtype", 0))
        if etype == MAP_ELEMENT_TYPE_HOST:
            hid = _map_selement_hostid(el)
            if hid:
                actual_hosts.add(hid)
                host_selement[hid] = str(el.get("selementid") or "")
        elif etype == MAP_ELEMENT_TYPE_IMAGE:
            label = (el.get("label") or "").strip()
            if label:
                actual_providers.add(label)
                isp_selement[label] = str(el.get("selementid") or "")

    selement_to_host = {sid: hid for hid, sid in host_selement.items() if sid}
    selement_to_isp = {sid: isp for isp, sid in isp_selement.items() if sid}
    actual_links = set()
    for link in links or []:
        s1 = str(link.get("selementid1") or "")
        s2 = str(link.get("selementid2") or "")
        hid = selement_to_host.get(s1) or selement_to_host.get(s2)
        isp = selement_to_isp.get(s1) or selement_to_isp.get(s2)
        if hid and isp:
            actual_links.add((hid, isp))
    return actual_hosts, actual_providers, actual_links


def _plan_maps(url, token, uplink_ctx, allow_delete, debug=False):
    categories = _empty_categories()
    current = {}
    edges = uplink_ctx.get("edges") or []
    expected_hosts, expected_providers, expected_links = _expected_map_identities(edges)

    if not expected_hosts and not expected_providers:
        categories["skipped"].append({"map": MAP_NAME, "reason": "no scoped hosts/providers for map"})
        return categories, current, None

    result, err = zabbix_request(
        url,
        token,
        "map.get",
        {
            "filter": {"name": MAP_NAME},
            "output": ["sysmapid", "name"],
            "selectSelements": ["selementid", "elementtype", "label", "elementid", "elements"],
            "selectLinks": ["linkid", "selementid1", "selementid2", "label"],
        },
        debug=debug,
    )
    if err:
        return None, None, "map.get: {}".format(err)

    if result:
        selements = result[0].get("selements") or []
        links = result[0].get("links") or []
        actual_hosts, actual_providers, actual_links = _actual_map_identities(selements, links)
        current = {
            "map": MAP_NAME,
            "sysmapid": result[0].get("sysmapid"),
            "hosts": len(actual_hosts),
            "providers": len(actual_providers),
            "links": len(actual_links),
        }

    if uplink_ctx.get("zabbix_items_read_error"):
        detail = uplink_ctx.get("zabbix_items_read_detail") or "Zabbix uplink items unread"
        return (
            _category_not_evaluated("{}; map diff suppressed".format(detail)),
            current,
            None,
        )

    if not result:
        categories["create"].append(
            {
                "map": MAP_NAME,
                "hosts": len(expected_hosts),
                "providers": len(expected_providers),
                "links": len(expected_links),
            }
        )
        return categories, current, None

    sysmapid = result[0].get("sysmapid")
    selements = result[0].get("selements") or []
    links = result[0].get("links") or []
    actual_hosts, actual_providers, actual_links = _actual_map_identities(selements, links)

    missing_hosts = expected_hosts - actual_hosts
    missing_providers = expected_providers - actual_providers
    missing_links = expected_links - actual_links
    extra_hosts = actual_hosts - expected_hosts
    extra_providers = actual_providers - expected_providers
    extra_links = actual_links - expected_links

    if (
        not missing_hosts
        and not missing_providers
        and not missing_links
        and not extra_hosts
        and not extra_providers
        and not extra_links
    ):
        categories["unchanged"].append(
            {
                "map": MAP_NAME,
                "hosts": len(expected_hosts),
                "providers": len(expected_providers),
                "links": len(expected_links),
            }
        )
        return categories, current, None

    if missing_hosts or missing_providers or missing_links:
        categories["update"].append(
            {
                "map": MAP_NAME,
                "missing_hosts": len(missing_hosts),
                "missing_providers": len(missing_providers),
                "missing_links": len(missing_links),
            }
        )
    elif not (extra_hosts or extra_providers or extra_links):
        categories["unchanged"].append(
            {
                "map": MAP_NAME,
                "hosts": len(expected_hosts),
                "providers": len(expected_providers),
                "links": len(expected_links),
            }
        )

    for hid in sorted(extra_hosts):
        entry = {"map": MAP_NAME, "hostid": hid, "kind": "host"}
        if allow_delete:
            categories["delete"].append(entry)
        else:
            categories["skipped"].append(
                dict(
                    entry,
                    suppressed="delete",
                    reason="delete suppressed (destructive plan disabled)",
                )
            )
    for isp in sorted(extra_providers):
        entry = {"map": MAP_NAME, "provider": isp, "kind": "provider"}
        if allow_delete:
            categories["delete"].append(entry)
        else:
            categories["skipped"].append(
                dict(
                    entry,
                    suppressed="delete",
                    reason="delete suppressed (destructive plan disabled)",
                )
            )
    for hid, isp in sorted(extra_links - expected_links):
        entry = {"map": MAP_NAME, "hostid": hid, "provider": isp, "kind": "link"}
        if allow_delete:
            categories["delete"].append(entry)
        else:
            categories["skipped"].append(
                dict(
                    entry,
                    suppressed="delete",
                    reason="delete suppressed (destructive plan disabled)",
                )
            )
    return categories, current, None


def _location_from_hostname(hostname):
    from zabbix_uplinks_dashboard import _location_from_hostname as _dash_location

    return _dash_location(hostname)


def _dashboard_widget_identities(pages):
    identities = set()
    for page in pages or []:
        page_name = (page.get("name") or "").strip()
        for widget in page.get("widgets") or []:
            widget_name = (widget.get("name") or "").strip()
            if widget_name:
                identities.add((page_name, widget_name))
    return identities


def _expected_main_dashboard_identities(edges):
    identities = set()
    for edge in edges:
        hostname, _hid, iface_name, isp, itemid_in, itemid_out = edge[:6]
        if not itemid_in and not itemid_out:
            continue
        title = "{} - {} ({})".format(hostname, iface_name, isp or "—").strip()
        identities.add(("", title))
    return identities


def _expected_location_dashboard_identities(edges):
    by_location = {}
    for edge in edges:
        loc = _location_from_hostname(edge[0])
        by_location.setdefault(loc, []).append(edge)
    identities = set()
    for loc in sorted(by_location.keys()):
        loc_edges = by_location[loc]
        by_provider = {}
        for edge in loc_edges:
            isp = edge[3] or ""
            by_provider.setdefault(isp, []).append(edge)
        for isp in sorted(by_provider.keys()):
            prov_edges = by_provider[isp]
            if not any(e[4] or e[5] for e in prov_edges):
                continue
            identities.add((loc, "{} (summary)".format(isp or "—")))
    return identities


def _expected_provider_dashboard_identities(edges, providers_filter, aggregate_itemids=None):
    aggregate_itemids = aggregate_itemids or {}
    by_provider = {}
    for edge in edges:
        isp = (edge[3] or "").strip()
        if not isp:
            continue
        by_provider.setdefault(isp, []).append(edge)
    providers_ok = [
        isp
        for isp in (p.strip() for p in providers_filter if p and p.strip())
        if by_provider.get(isp)
    ]
    identities = set()
    for isp in sorted(providers_ok):
        prov_edges = by_provider[isp]
        in_edges = [e for e in prov_edges if e[4]]
        out_edges = [e for e in prov_edges if e[5]]
        if in_edges:
            identities.add((isp, "{} - Bits received (summary)".format(isp)))
        if out_edges:
            identities.add((isp, "{} - Bits sent (summary)".format(isp)))
        agg_in_id, agg_out_id = aggregate_itemids.get(isp, (None, None))
        if agg_in_id:
            identities.add((isp, "{} - Total Bits received (aggregate)".format(isp)))
        if agg_out_id:
            identities.add((isp, "{} - Total Bits sent (aggregate)".format(isp)))
    return identities


def _fetch_aggregate_itemids_for_plan(url, token, providers, debug=False):
    """Read aggregate item IDs for dashboard planning; fail closed on host/item read errors."""
    if not providers:
        return {}, None

    host_names = [UPLINKS_AGGREGATE_HOST_PREFIX + p for p in providers]
    out = {p: (None, None) for p in providers}
    hostname_to_id = {}

    hosts, err = zabbix_request(
        url,
        token,
        "host.get",
        {"output": ["hostid", "host", "name"], "filter": {"host": host_names}},
        debug=debug,
    )
    if err:
        return None, "host.get aggregate dashboard: {}".format(err)
    for h in hosts or []:
        hostname_to_id[h.get("host") or ""] = h.get("hostid")

    missing = [p for p in providers if UPLINKS_AGGREGATE_HOST_PREFIX + p not in hostname_to_id]
    if missing:
        names_filter = [UPLINKS_AGGREGATE_HOST_PREFIX + p for p in missing]
        hosts2, err2 = zabbix_request(
            url,
            token,
            "host.get",
            {"output": ["hostid", "host", "name"], "filter": {"name": names_filter}},
            debug=debug,
        )
        if err2:
            return None, "host.get aggregate dashboard: {}".format(err2)
        for h in hosts2 or []:
            hostname_to_id[h.get("name") or ""] = h.get("hostid")

    id_to_provider = {}
    for p in providers:
        wanted = UPLINKS_AGGREGATE_HOST_PREFIX + p
        hid = hostname_to_id.get(wanted)
        if hid:
            id_to_provider[str(hid)] = p

    for hostid, isp in id_to_provider.items():
        items, item_err = zabbix_request(
            url,
            token,
            "item.get",
            {
                "output": ["itemid", "key_"],
                "hostids": [hostid],
                "search": {"key_": "aggregate.bits"},
            },
            debug=debug,
        )
        if item_err:
            return None, "item.get aggregate dashboard: {}".format(item_err)
        if not items:
            continue
        itemid_in = itemid_out = None
        for it in items:
            key = (it.get("key_") or "").strip()
            if key == AGGREGATE_ITEM_KEY_IN:
                itemid_in = it.get("itemid")
            elif key == AGGREGATE_ITEM_KEY_OUT:
                itemid_out = it.get("itemid")
        out[isp] = (itemid_in, itemid_out)
    return out, None


def _plan_one_dashboard(
    url, token, dashboard_name, expected_identities, allow_destructive=True, debug=False
):
    categories = _empty_categories()
    current = {}
    if not expected_identities:
        categories["skipped"].append({"dashboard": dashboard_name, "reason": "no widgets expected"})
        return categories, current, None

    result, err = zabbix_request(
        url,
        token,
        "dashboard.get",
        {
            "filter": {"name": dashboard_name},
            "output": ["dashboardid", "name"],
            "selectPages": "extend",
        },
        debug=debug,
    )
    if err:
        return None, None, "dashboard.get: {}".format(err)

    if not result:
        if allow_destructive:
            categories["create"].append(
                {
                    "dashboard": dashboard_name,
                    "widgets": len(expected_identities),
                    "pages": len({page for page, _name in expected_identities}),
                }
            )
        else:
            categories["skipped"].append(
                {
                    "dashboard": dashboard_name,
                    "widgets": len(expected_identities),
                    "suppressed": "create",
                    "reason": "create suppressed (destructive plan disabled)",
                }
            )
        return categories, current, None

    pages = result[0].get("pages") or []
    actual_identities = _dashboard_widget_identities(pages)
    current = {
        "dashboard": dashboard_name,
        "dashboardid": result[0].get("dashboardid"),
        "widgets": len(actual_identities),
        "pages": len(pages),
    }
    if actual_identities == expected_identities:
        categories["unchanged"].append(
            {
                "dashboard": dashboard_name,
                "widgets": len(expected_identities),
                "pages": len({page for page, _name in expected_identities}),
            }
        )
    elif allow_destructive:
        categories["update"].append(
            {
                "dashboard": dashboard_name,
                "widgets_expected": len(expected_identities),
                "widgets_current": len(actual_identities),
                "missing_widgets": len(expected_identities - actual_identities),
                "extra_widgets": len(actual_identities - expected_identities),
            }
        )
    else:
        categories["skipped"].append(
            {
                "dashboard": dashboard_name,
                "widgets_expected": len(expected_identities),
                "widgets_current": len(actual_identities),
                "missing_widgets": len(expected_identities - actual_identities),
                "extra_widgets": len(actual_identities - expected_identities),
                "suppressed": "update",
                "reason": "update suppressed (destructive plan disabled)",
            }
        )
    return categories, current, None


def _plan_dashboards(url, token, uplink_ctx, allow_destructive=True, debug=False):
    if uplink_ctx.get("zabbix_items_read_error"):
        detail = uplink_ctx.get("zabbix_items_read_detail") or "Zabbix uplink items unread"
        return (
            _category_not_evaluated("{}; dashboard diff suppressed".format(detail)),
            {},
            None,
        )

    if uplink_ctx.get("limits_read_error"):
        limits_read = uplink_ctx.get("provider_limits_read", "unknown")
        if limits_read == "unavailable":
            reason = (
                "NetBox client unavailable; provider limits unread; dashboard diff suppressed"
            )
        else:
            reason = "NetBox provider limits read failed; dashboard diff suppressed"
        return _category_not_evaluated(reason), {}, None

    edges = uplink_ctx.get("edges") or []
    providers_filter = PROVIDERS_FOR_SUMMARY or sorted(
        {edge[3] for edge in edges if edge[3]}
    )
    providers_ok = [
        isp
        for isp in (p.strip() for p in providers_filter if p and p.strip())
        if any((edge[3] or "").strip() == isp for edge in edges)
    ]
    aggregate_itemids, agg_err = _fetch_aggregate_itemids_for_plan(
        url, token, providers_ok, debug=debug
    )
    if agg_err:
        return (
            _category_not_evaluated("{}; dashboard diff suppressed".format(agg_err)),
            {},
            None,
        )

    combined = _empty_categories()
    current = []
    specs = [
        (DASHBOARD_NAME, _expected_main_dashboard_identities(edges)),
        (DASHBOARD_NAME_BY_LOCATION, _expected_location_dashboard_identities(edges)),
        (
            DASHBOARD_NAME_BY_PROVIDER,
            _expected_provider_dashboard_identities(
                edges, providers_filter, aggregate_itemids=aggregate_itemids
            ),
        ),
    ]
    for dashboard_name, expected in specs:
        part, part_current, err = _plan_one_dashboard(
            url,
            token,
            dashboard_name,
            expected,
            allow_destructive=allow_destructive,
            debug=debug,
        )
        if err:
            return None, None, err
        for key in ("create", "update", "unchanged", "delete", "skipped"):
            combined[key].extend(part.get(key) or [])
        if part_current:
            current.append(part_current)
    return combined, current, None


def _resolve_provider_slo(provider, uplink_ctx):
    if uplink_ctx.get("provider_slo_read", "ok") != "ok":
        return None
    netbox_slo = uplink_ctx.get("provider_slo_percent") or {}
    if provider in netbox_slo:
        return netbox_slo[provider]
    if PROJECT_PROVIDER_SLO_PERCENT is not None:
        return PROJECT_PROVIDER_SLO_PERCENT
    return None


def _services_by_name(url, token, names, debug=False):
    if not names:
        return {}, None
    result, err = zabbix_request(
        url,
        token,
        "service.get",
        {
            "output": ["serviceid", "name"],
            "filter": {"name": list(names)},
            "selectParents": ["serviceid", "name"],
        },
        debug=debug,
    )
    if err:
        return None, "service.get: {}".format(err)
    return {(row.get("name") or ""): row for row in (result or []) if row.get("name")}, None


def _slas_by_name(url, token, names, debug=False):
    if not names:
        return {}, None
    result, err = zabbix_request(
        url,
        token,
        "sla.get",
        {
            "output": ["slaid", "name", "slo"],
            "filter": {"name": list(names)},
        },
        debug=debug,
    )
    if err:
        return None, "sla.get: {}".format(err)
    return {(row.get("name") or ""): row for row in (result or []) if row.get("name")}, None


def _plan_services(
    url,
    token,
    uplink_ctx,
    parent_service=None,
    allow_delete=True,
    debug=False,
):
    categories = _empty_categories()
    categories["sla"] = _empty_categories()
    current = {}
    providers = sorted(uplink_ctx.get("providers") or [])
    burst_pairs = list(uplink_ctx.get("burst_circuits") or [])

    expected_services = []
    if parent_service:
        expected_services.append(parent_service)
    expected_services.extend("Uplinks {}".format(p) for p in providers)
    expected_services.extend("Uplinks Burst {}".format(cid) for cid, _prov in burst_pairs)
    expected_service_names = set(expected_services)

    slo_read = uplink_ctx.get("provider_slo_read", "ok")
    sla_plan_enabled = slo_read == "ok"
    expected_sla_names = set()
    if sla_plan_enabled:
        for provider in providers:
            if _resolve_provider_slo(provider, uplink_ctx) is not None:
                expected_sla_names.add("Uplinks {} SLA".format(provider))
        for circuit_id, b_provider in burst_pairs:
            if _resolve_provider_slo(b_provider, uplink_ctx) is not None:
                expected_sla_names.add("Uplinks Burst {} SLA".format(circuit_id))

    services_by_name, err = _services_by_name(
        url, token, expected_services + [n for p in providers for n in ("Uplinks {} SLA source".format(p),)], debug=debug
    )
    if err:
        return None, None, err
    slas_by_name = {}
    if sla_plan_enabled and expected_sla_names:
        slas_by_name, err = _slas_by_name(url, token, sorted(expected_sla_names), debug=debug)
        if err:
            return None, None, err

    parentid = None
    if parent_service:
        parent_row = services_by_name.get(parent_service)
        if parent_row:
            parentid = parent_row.get("serviceid")
            categories["unchanged"].append({"service": parent_service, "role": "parent"})
        else:
            categories["create"].append({"service": parent_service, "role": "parent"})

    for provider in providers:
        service_name = "Uplinks {}".format(provider)
        row = services_by_name.get(service_name)
        if row:
            needs_update = False
            if parentid:
                parents = row.get("parents") or []
                if not any(str(p.get("serviceid")) == str(parentid) for p in parents):
                    needs_update = True
            if needs_update:
                categories["update"].append({"service": service_name, "role": "provider"})
            else:
                categories["unchanged"].append({"service": service_name, "role": "provider"})
        else:
            categories["create"].append({"service": service_name, "role": "provider"})

        legacy_name = "Uplinks {} SLA source".format(provider)
        if services_by_name.get(legacy_name):
            if allow_delete:
                categories["delete"].append(
                    {"service": legacy_name, "role": "legacy_sla_source", "provider": provider}
                )
            else:
                categories["skipped"].append(
                    {
                        "service": legacy_name,
                        "role": "legacy_sla_source",
                        "suppressed": "delete",
                        "reason": "delete suppressed (NetBox read incomplete)",
                    }
                )

        if sla_plan_enabled:
            slo = _resolve_provider_slo(provider, uplink_ctx)
            sla_name = "Uplinks {} SLA".format(provider)
            if slo is not None:
                sla_row = slas_by_name.get(sla_name)
                if sla_row:
                    try:
                        current_slo = float(sla_row.get("slo"))
                    except (TypeError, ValueError):
                        current_slo = None
                    if current_slo is not None and abs(current_slo - float(slo)) <= 0.0001:
                        categories["sla"]["unchanged"].append(
                            {"sla": sla_name, "provider": provider}
                        )
                    else:
                        categories["sla"]["update"].append(
                            {"sla": sla_name, "provider": provider, "slo": slo}
                        )
                else:
                    categories["sla"]["create"].append(
                        {"sla": sla_name, "provider": provider, "slo": slo}
                    )

    for circuit_id, b_provider in burst_pairs:
        service_name = "Uplinks Burst {}".format(circuit_id)
        row = services_by_name.get(service_name)
        if row:
            needs_update = False
            if parentid:
                parents = row.get("parents") or []
                if not any(str(p.get("serviceid")) == str(parentid) for p in parents):
                    needs_update = True
            if needs_update:
                categories["update"].append(
                    {"service": service_name, "role": "burst", "circuit_id": circuit_id}
                )
            else:
                categories["unchanged"].append(
                    {"service": service_name, "role": "burst", "circuit_id": circuit_id}
                )
        else:
            categories["create"].append(
                {"service": service_name, "role": "burst", "circuit_id": circuit_id}
            )

        if sla_plan_enabled:
            slo = _resolve_provider_slo(b_provider, uplink_ctx)
            sla_name = "Uplinks Burst {} SLA".format(circuit_id)
            if slo is not None:
                sla_row = slas_by_name.get(sla_name)
                if sla_row:
                    try:
                        current_slo = float(sla_row.get("slo"))
                    except (TypeError, ValueError):
                        current_slo = None
                    if current_slo is not None and abs(current_slo - float(slo)) <= 0.0001:
                        categories["sla"]["unchanged"].append(
                            {"sla": sla_name, "circuit_id": circuit_id}
                        )
                    else:
                        categories["sla"]["update"].append(
                            {"sla": sla_name, "circuit_id": circuit_id, "slo": slo}
                        )
                else:
                    categories["sla"]["create"].append(
                        {"sla": sla_name, "circuit_id": circuit_id, "slo": slo}
                    )

    if not sla_plan_enabled:
        if slo_read == "unavailable":
            sla_reason = "NetBox client unavailable; SLA diff suppressed"
        else:
            sla_reason = "NetBox provider SLO read failed; SLA diff suppressed"
        categories["sla"] = _category_not_evaluated(sla_reason)

    if allow_delete and sla_plan_enabled:
        uplinks_services, err = zabbix_request(
            url,
            token,
            "service.get",
            {
                "output": ["serviceid", "name"],
                "search": {"name": "Uplinks"},
            },
            debug=debug,
        )
        if err:
            return None, None, "service.get: {}".format(err)
        for row in uplinks_services or []:
            name = (row.get("name") or "").strip()
            if not name or name in expected_service_names:
                continue
            if name.endswith(" SLA source"):
                continue
            categories["delete"].append({"service": name, "role": "orphan"})

        uplinks_slas, err = zabbix_request(
            url,
            token,
            "sla.get",
            {
                "output": ["slaid", "name"],
                "search": {"name": "Uplinks"},
            },
            debug=debug,
        )
        if err:
            return None, None, "sla.get: {}".format(err)
        for row in uplinks_slas or []:
            name = (row.get("name") or "").strip()
            if not name or name in expected_sla_names:
                continue
            categories["sla"]["delete"].append({"sla": name})

    current = {
        "services": len(expected_service_names),
        "slas": len(expected_sla_names),
    }
    return categories, current, None


def build_zabbix_plan(
    dry_ssh_path,
    tag=None,
    debug=False,
    create_link_triggers=False,
    inventory_file=None,
    parent_service=DEFAULT_PARENT_SERVICE,
):
    """
    Read-only plan: scoped inventory, current Zabbix objects, planned categories.
    Return (report_dict, error_message). error_message is set on fatal errors.
    """
    from zabbix_sync_commit_rate import (
        apply_logical_context,
        commit_rates_from_inventory_report,
        load_dry_ssh,
        util_interfaces_by_host_from_inventory,
    )

    zabbix_url, zabbix_token = get_zabbix_url_token()
    if not zabbix_url or not zabbix_token:
        return None, "ZABBIX_URL and ZABBIX_TOKEN must be set"
    valid, auth_err = validate_zabbix_token(zabbix_url, zabbix_token, debug=debug)
    if not valid:
        return None, auth_err or "Invalid ZABBIX_TOKEN"

    dry_ssh_devices = load_dry_ssh(dry_ssh_path) if dry_ssh_path else None

    if inventory_file:
        try:
            inventory_report = load_inventory_report(inventory_file)
        except (OSError, json.JSONDecodeError) as e:
            return None, "failed to read inventory file {}: {}".format(inventory_file, e)
    else:
        tag = resolve_border_device_tag(tag)
        nb_url = os.environ.get("NETBOX_URL", "").strip()
        nb_token = os.environ.get("NETBOX_TOKEN", "").strip()
        if not nb_url or not nb_token:
            return None, "NETBOX_URL and NETBOX_TOKEN must be set"

        try:
            nb = pynetbox.api(nb_url, token=nb_token)
        except Exception as e:
            return None, "NetBox client: {}".format(e)

        inventory_report = collect_scoped_inventory(nb, tag, debug=debug)

    netbox_relations = netbox_interface_relations_from_report(inventory_report)
    nb_for_relations = None
    if netbox_relations is None:
        if inventory_file:
            nb_url = os.environ.get("NETBOX_URL", "").strip()
            nb_token = os.environ.get("NETBOX_TOKEN", "").strip()
            if nb_url and nb_token:
                try:
                    nb_for_relations = pynetbox.api(nb_url, token=nb_token)
                except Exception:
                    nb_for_relations = None
        else:
            nb_for_relations = nb
        if nb_for_relations is not None:
            try:
                device_names = device_names_from_complete_inventory(inventory_report)
                netbox_relations = collect_netbox_interface_relations(
                    nb_for_relations,
                    device_names,
                    debug=debug,
                    stats=inventory_report.get("stats"),
                )
            except NetBoxAuthError:
                stats = inventory_report.setdefault("stats", {})
                stats["error"] = ERROR_AUTH_DENIED
                netbox_relations = None
            except Exception:
                stats = inventory_report.setdefault("stats", {})
                stats["read_errors"] = stats.get("read_errors", 0) + 1
                netbox_relations = None
    enrich_inventory_provider_stats(inventory_report)
    finalize_inventory_read_stats(inventory_report.get("stats"))
    gate_ok, gate_detail = inventory_plan_gate(inventory_report)
    if not gate_ok:
        return None, gate_detail

    commit_rates = commit_rates_from_inventory_report(inventory_report, debug=debug)
    if commit_rates:
        if netbox_relations and (
            netbox_relations.get("member_to_aggregate")
            or netbox_relations.get("parent_children")
        ):
            commit_rates = map_commit_rates_to_zabbix_ifaces(
                commit_rates,
                netbox_relations=netbox_relations,
                dry_ssh_devices=dry_ssh_devices,
                debug=debug,
            )
        elif dry_ssh_devices:
            commit_rates = apply_logical_context(commit_rates, dry_ssh_devices, debug=debug)

    host_to_iface_bps = {}
    for (dev_name, iface_name), bps in commit_rates.items():
        host_to_iface_bps.setdefault(dev_name, []).append((iface_name, bps))

    host_to_util_ifaces = util_interfaces_by_host_from_inventory(
        dry_ssh_devices,
        inventory_report,
        debug=debug,
        netbox_relations=netbox_relations,
    )
    hostnames = sorted(set(host_to_iface_bps.keys()) | set(host_to_util_ifaces.keys()))
    hostid_by_name, _host_technical = _resolve_hostids(zabbix_url, zabbix_token, hostnames, debug=debug)
    if hostid_by_name is None:
        return None, _host_technical

    providers = providers_from_complete_inventory(inventory_report)
    allow_delete = not inventory_read_failed(inventory_report)

    uplink_ctx, uplink_err = _prepare_uplink_plan_context(
        zabbix_url,
        zabbix_token,
        inventory_report,
        dry_ssh_devices,
        hostid_by_name,
        netbox_relations,
        debug=debug,
    )
    if uplink_err:
        return None, uplink_err

    if (
        uplink_ctx.get("limits_read_error")
        or uplink_ctx.get("slo_read_error")
        or uplink_ctx.get("zabbix_items_read_error")
    ):
        allow_delete = False

    macro_plan, macro_current = _plan_bps_macros(
        zabbix_url,
        zabbix_token,
        host_to_iface_bps,
        hostid_by_name,
        debug=debug,
    )
    if macro_plan is None:
        return None, macro_current
    util_plan, util_current = _plan_util_triggers(
        zabbix_url,
        zabbix_token,
        host_to_util_ifaces,
        hostid_by_name,
        allow_delete=allow_delete,
        debug=debug,
    )
    if util_plan is None:
        return None, util_current
    agg_plan, agg_current = _plan_aggregate_hosts(zabbix_url, zabbix_token, providers, debug=debug)
    if agg_plan is None:
        return None, agg_current

    hostid_by_provider, agg_host_err = _aggregate_hostid_by_provider(
        zabbix_url, zabbix_token, providers, debug=debug
    )
    if agg_host_err:
        return None, agg_host_err

    items_plan, triggers_plan, agg_items_err = _plan_aggregate_items_and_triggers(
        zabbix_url,
        zabbix_token,
        providers,
        uplink_ctx,
        hostid_by_provider,
        allow_delete,
        debug=debug,
    )
    if agg_items_err:
        return None, agg_items_err
    agg_plan["calculated_items"] = items_plan
    agg_plan["limit_triggers"] = triggers_plan
    if uplink_ctx.get("zabbix_items_read_error"):
        allow_delete = False

    map_plan, map_current, map_err = _plan_maps(
        zabbix_url, zabbix_token, uplink_ctx, allow_delete, debug=debug
    )
    if map_err:
        return None, map_err

    dash_plan, dash_current, dash_err = _plan_dashboards(
        zabbix_url,
        zabbix_token,
        uplink_ctx,
        allow_destructive=allow_delete,
        debug=debug,
    )
    if dash_err:
        return None, dash_err

    svc_plan, svc_current, svc_err = _plan_services(
        zabbix_url,
        zabbix_token,
        uplink_ctx,
        parent_service=parent_service,
        allow_delete=allow_delete,
        debug=debug,
    )
    if svc_err:
        return None, svc_err

    burst_plan = _empty_categories()
    if create_link_triggers:
        burst_plan = _category_not_evaluated(
            "Burst link trigger diff not implemented in plan mode; use zabbix_sync_commit_rate --dry-run"
        )
    else:
        burst_plan = _category_not_evaluated("Burst link triggers disabled (no --create-link-triggers)")

    report = {
        "read_only": True,
        "dry_ssh": os.path.abspath(dry_ssh_path) if dry_ssh_path else None,
        "inventory": {
            "complete": inventory_report.get("complete") or [],
            "incomplete": inventory_report.get("incomplete") or [],
            "stats": inventory_report.get("stats") or {},
            "circuit_scope": project_circuit_scope(),
        },
        "zabbix_current": {
            "hosts_resolved": {name: str(hostid) for name, hostid in sorted(hostid_by_name.items())},
            "macros": macro_current,
            "util_triggers": util_current,
            "aggregate_hosts": agg_current,
            "maps": map_current,
            "dashboards": dash_current,
            "services": svc_current,
        },
        "planned": {
            "macros": macro_plan,
            "util_triggers": util_plan,
            "burst_triggers": burst_plan,
            "aggregate_hosts": agg_plan,
            "maps": map_plan,
            "dashboards": dash_plan,
            "services": svc_plan,
        },
    }
    report["summary"] = _summarize_plan(report["planned"])
    return report, None


def _summarize_plan(planned):
    summary = {
        "create": 0,
        "update": 0,
        "unchanged": 0,
        "delete": 0,
        "skipped": 0,
        "not_evaluated": 0,
    }
    for section, data in planned.items():
        if isinstance(data, dict) and data.get("status") == NOT_EVALUATED:
            summary["not_evaluated"] += 1
            continue
        if not isinstance(data, dict):
            continue
        for key in ("create", "update", "unchanged", "delete", "skipped"):
            summary[key] += len(data.get(key) or [])
        for sub_key, sub_val in data.items():
            if sub_key in ("create", "update", "unchanged", "delete", "skipped", "status", "reason"):
                continue
            if isinstance(sub_val, dict) and sub_val.get("status") == NOT_EVALUATED:
                summary["not_evaluated"] += 1
                continue
            if isinstance(sub_val, dict) and any(
                k in sub_val for k in ("create", "update", "unchanged", "delete", "skipped")
            ):
                for key in ("create", "update", "unchanged", "delete", "skipped"):
                    summary[key] += len(sub_val.get(key) or [])
    return summary


def _format_section_create_details(section, data):
    creates = data.get("create") or []
    if not creates:
        return []
    lines = []
    if section == "util_triggers":
        lines.append("  util_triggers create ({} entries):".format(len(creates)))
        for entry in creates:
            host = entry.get("host") or "?"
            iface = entry.get("interface") or "?"
            triggers = entry.get("triggers") or []
            trigger_count = len(triggers)
            trigger_label = "trigger" if trigger_count == 1 else "triggers"
            lines.append(
                "    {}/{} ({} {}): {}".format(
                    host,
                    iface,
                    trigger_count,
                    trigger_label,
                    ", ".join(triggers) if triggers else "(none)",
                )
            )
    elif section == "aggregate_hosts":
        lines.append("  aggregate_hosts create ({} entries):".format(len(creates)))
        for entry in creates:
            lines.append(
                "    provider={} host={}".format(
                    entry.get("provider") or "?",
                    entry.get("host") or "?",
                )
            )
    return lines


def format_plan_text(report):
    """Concise human-readable summary."""
    lines = ["Zabbix uplinks plan (read-only)"]
    inventory = report.get("inventory") or {}
    stats = inventory.get("stats") or {}
    providers_total = stats.get("providers", 0)
    providers_in_scope = stats.get("providers_in_scope")
    if providers_in_scope is None:
        providers_in_scope = len(providers_from_complete_inventory(inventory))
    lines.append(
        "Inventory: providers_total={}, providers_in_scope={}, complete={}, incomplete={}".format(
            providers_total,
            providers_in_scope,
            stats.get("complete", 0),
            stats.get("incomplete", 0),
        )
    )
    summary = report.get("summary") or {}
    lines.append(
        "Planned changes: create={}, update={}, unchanged={}, delete={}, skipped={}, not_evaluated_sections={}".format(
            summary.get("create", 0),
            summary.get("update", 0),
            summary.get("unchanged", 0),
            summary.get("delete", 0),
            summary.get("skipped", 0),
            summary.get("not_evaluated", 0),
        )
    )
    planned = report.get("planned") or {}
    for section in ("macros", "util_triggers", "burst_triggers", "aggregate_hosts", "maps", "dashboards", "services"):
        data = planned.get(section) or {}
        if data.get("status") == NOT_EVALUATED:
            lines.append("{}: {} ({})".format(section, NOT_EVALUATED, data.get("reason", "")))
            continue
        line = "{}: create={}, update={}, unchanged={}, delete={}, skipped={}".format(
            section,
            len(data.get("create") or []),
            len(data.get("update") or []),
            len(data.get("unchanged") or []),
            len(data.get("delete") or []),
            len(data.get("skipped") or []),
        )
        if section == "aggregate_hosts":
            calc = data.get("calculated_items") or {}
            trig = data.get("limit_triggers") or {}
            if calc.get("status") == NOT_EVALUATED:
                line += "; calculated_items={} ({})".format(
                    NOT_EVALUATED, calc.get("reason", "")
                )
            else:
                line += "; calculated_items create={}".format(len(calc.get("create") or []))
            if trig.get("status") == NOT_EVALUATED:
                line += "; limit_triggers={} ({})".format(
                    NOT_EVALUATED, trig.get("reason", "")
                )
            else:
                line += ", limit_triggers create={}".format(len(trig.get("create") or []))
        if section == "services" and isinstance(data.get("sla"), dict):
            sla = data["sla"]
            if sla.get("status") == NOT_EVALUATED:
                line += "; sla={} ({})".format(NOT_EVALUATED, sla.get("reason", ""))
            else:
                line += "; sla create={}, update={}, delete={}".format(
                    len(sla.get("create") or []),
                    len(sla.get("update") or []),
                    len(sla.get("delete") or []),
                )
        lines.append(line)
        lines.extend(_format_section_create_details(section, data))
    return "\n".join(lines)


def write_plan_outputs(report, json_path=None, text_path=None, print_json=False):
    """Write plan JSON/text files; optionally print JSON to stdout."""
    if json_path:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
    text = format_plan_text(report)
    if text_path:
        with open(text_path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    if print_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(text)
    return text

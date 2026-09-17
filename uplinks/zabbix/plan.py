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
    collect_netbox_interface_relations,
    collect_uplink_inventory,
    device_names_from_complete_inventory,
    enrich_inventory_provider_stats,
    finalize_inventory_read_stats,
    map_commit_rates_to_zabbix_ifaces,
    project_circuit_scope,
    providers_from_complete_inventory,
    resolve_border_device_tag,
)
from uplinks.zabbix.client import (
    get_zabbix_url_token,
    stale_util_triggers,
    util_trigger_get_params,
    validate_zabbix_token,
    zabbix_request,
)
from uplinks_config import (
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
    }
)

NOT_EVALUATED = "not_evaluated"

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


def _plan_util_triggers(url, token, host_to_ifaces, hostid_by_name, debug=False):
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
            if tid:
                categories["delete"].append(
                    {
                        "host": dev_name,
                        "interface": iface,
                        "triggerid": tid,
                        "description": desc,
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


def _plan_aggregate_hosts(url, token, providers, debug=False):
    categories = _empty_categories()
    categories["items_and_triggers"] = NOT_EVALUATED
    categories["reason"] = (
        "Calculated items and limit triggers on existing aggregate hosts are not compared in preview mode."
    )
    current = []
    for provider in sorted(providers):
        technical = _sanitize_provider_host_name(provider)
        res, err = zabbix_request(
            url,
            token,
            "host.get",
            {"output": ["hostid", "host", "name"], "filter": {"host": [technical]}},
            debug=debug,
        )
        if err:
            return None, "host.get aggregate: {}".format(err)
        if res:
            current.append(
                {
                    "provider": provider,
                    "hostid": res[0].get("hostid"),
                    "host": res[0].get("host"),
                    "name": res[0].get("name"),
                }
            )
            categories["unchanged"].append({"provider": provider, "host": technical})
        else:
            categories["create"].append(
                {"provider": provider, "host": technical, "display_name": UPLINKS_AGGREGATE_HOST_PREFIX + provider}
            )
    return categories, current


def build_zabbix_plan(
    dry_ssh_path,
    tag=None,
    debug=False,
    create_link_triggers=False,
    inventory_file=None,
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
            with open(inventory_file, "r", encoding="utf-8") as f:
                inventory_report = json.load(f)
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

    netbox_relations = None
    nb_for_relations = None
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
        debug=debug,
    )
    if util_plan is None:
        return None, util_current
    agg_plan, agg_current = _plan_aggregate_hosts(zabbix_url, zabbix_token, providers, debug=debug)
    if agg_plan is None:
        return None, agg_current

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
        },
        "planned": {
            "macros": macro_plan,
            "util_triggers": util_plan,
            "burst_triggers": burst_plan,
            "aggregate_hosts": agg_plan,
            "maps": _category_not_evaluated("Full map diff not evaluated in plan mode"),
            "dashboards": _category_not_evaluated("Full dashboard diff not evaluated in plan mode"),
            "services": _category_not_evaluated("Full service/SLA diff not evaluated in plan mode"),
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
        if data.get("items_and_triggers") == NOT_EVALUATED:
            line += "; items_and_triggers={} ({})".format(
                NOT_EVALUATED, data.get("reason", "")
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

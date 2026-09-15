#!/usr/bin/env python3
"""Calculate SLA for provider aggregate limits and Burst link SLA breach (Zabbix trigger history)."""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone

from env_urls import load_env_file_if_present
from uplinks.data import load_devices_json
from uplinks.netbox.inventory import (
    ERROR_AUTH_DENIED,
    ERROR_PARTIAL_READ,
    ERROR_PROVIDERS_UNAVAILABLE,
    arm_netbox_incomplete_guard,
    burst_metadata_from_inventory,
    expand_burst_metadata_for_zabbix,
    inventory_read_failed,
    is_burst_billing_model,
    project_circuit_scope,
    providers_from_complete_inventory,
    _zabbix_iface_from_inventory_iface,
)
from zabbix_map import (
    _get_zabbix_url_token,
    zabbix_request,
)
from uplinks_config import (
    PROJECT_PROVIDER_SLO_PERCENT,
    UPLINKS_AGGREGATE_HOST_PREFIX,
    TRIGGER_DESC_90_SUFFIX,
    TRIGGER_DESC_100_SUFFIX,
    TRIGGER_DESC_SLA_BREACH_SUFFIX,
)

load_env_file_if_present()

DEFAULT_COMMIT_RATES = "commit_rates.json"
_NETBOX_AUTH_MESSAGE = (
    "NetBox error: token has expired or access is denied (403). "
    "Check NETBOX_TOKEN and update the token if necessary."
)
_NETBOX_READ_INCOMPLETE_MESSAGE = (
    "NetBox error: inventory read was incomplete; SLA report not produced."
)


def _load_commit_rates(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, "file not found: {}".format(path)
    except json.JSONDecodeError as e:
        return None, "invalid JSON in {}: {}".format(path, e)
    if not isinstance(data, dict):
        return None, "unexpected JSON root in {}".format(path)
    return data, None


def _get_providers_from_limits(commit_rates):
    limits = commit_rates.get("_provider_limits")
    if not isinstance(limits, dict):
        return []
    providers = []
    for name, val in limits.items():
        if not name or val is None:
            continue
        providers.append(str(name).strip())
    return sorted(set(p for p in providers if p))


def _get_global_provider_sla(commit_rates):
    """Return global target SLA (float) from commit_rates['_provider_sla'] or None."""
    val = (commit_rates or {}).get("_provider_sla")
    if isinstance(val, (int, float)):
        try:
            return float(val)
        except (TypeError, ValueError):
            return None
    return None


def _iter_burst_links(commit_rates):
    """Yield (device, iface, provider, circuit_id) for billing_model Burst."""
    for dev_name, ifaces in (commit_rates or {}).items():
        if not isinstance(dev_name, str) or dev_name.startswith("_"):
            continue
        if not isinstance(ifaces, dict):
            continue
        for iface_name, entry in ifaces.items():
            if not isinstance(entry, dict):
                continue
            if (entry.get("billing_model") or "").strip().lower() != "burst":
                continue
            cid = (entry.get("circuit_id") or "").strip()
            prov = (entry.get("provider") or "").strip()
            if not cid or not prov:
                continue
            yield dev_name, iface_name, prov, cid


def _burst_report_rows(commit_rates):
    """One row per circuit_id: the first interface encountered."""
    seen = set()
    rows = []
    for dev_name, iface_name, prov, cid in _iter_burst_links(commit_rates):
        if cid in seen:
            continue
        seen.add(cid)
        entry = commit_rates.get(dev_name, {}).get(iface_name)
        cr = None
        if isinstance(entry, dict):
            cr = entry.get("commit_rate_gbps")
        rows.append((cid, prov, dev_name, iface_name, cr))
    return sorted(rows, key=lambda x: x[0])


def _load_netbox_services_context(debug=False):
    """
    Read-only NetBox context for SLA report.
    Return dict with report, providers, burst_circuits, provider_slo_percent,
    provider_limits_gbps, stats, read_error; or None when NetBox is unavailable.
    """
    from zabbix_provider_services import (
        collect_provider_limits_gbps,
        collect_provider_slo_percent,
        collect_uplink_inventory,
        netbox_border_tag,
        netbox_client_from_env,
    )
    from uplinks.netbox.inventory import burst_circuits_unique_from_inventory

    nb = netbox_client_from_env(debug=debug)
    if nb is None:
        return None

    scope = project_circuit_scope()
    tag = netbox_border_tag()
    report = collect_uplink_inventory(
        nb, tag=tag, debug=debug, active_only=True, circuit_scope=scope
    )
    stats = dict(report.get("stats") or {})
    read_error = inventory_read_failed(report)

    provider_slo_percent, slo_error = collect_provider_slo_percent(nb, debug=debug)
    if slo_error:
        if slo_error == ERROR_AUTH_DENIED:
            stats["error"] = ERROR_AUTH_DENIED
        elif slo_error == ERROR_PARTIAL_READ:
            if stats.get("error") not in (ERROR_AUTH_DENIED, ERROR_PROVIDERS_UNAVAILABLE):
                stats["error"] = ERROR_PARTIAL_READ
        read_error = True

    provider_limits_gbps, limits_error = collect_provider_limits_gbps(nb, debug=debug)
    if limits_error:
        if limits_error == ERROR_AUTH_DENIED:
            stats["error"] = ERROR_AUTH_DENIED
        elif limits_error == ERROR_PARTIAL_READ:
            if stats.get("error") not in (ERROR_AUTH_DENIED, ERROR_PROVIDERS_UNAVAILABLE):
                stats["error"] = ERROR_PARTIAL_READ
        read_error = True

    return {
        "report": report,
        "providers": providers_from_complete_inventory(report),
        "burst_circuits": burst_circuits_unique_from_inventory(report),
        "provider_slo_percent": provider_slo_percent,
        "provider_limits_gbps": provider_limits_gbps,
        "stats": stats,
        "read_error": read_error,
    }


def _resolve_providers(netbox_ctx, commit_rates, legacy_commit_rates_fallback=False, debug=False):
    """Provider names: NetBox inventory first; _provider_limits only with legacy flag."""
    providers = set()
    netbox_available = bool(netbox_ctx) and not netbox_ctx.get("read_error")
    if netbox_available:
        providers = set(netbox_ctx.get("providers") or [])
        if providers and debug:
            print(
                "Providers from NetBox inventory: {}".format(", ".join(sorted(providers))),
                file=sys.stderr,
            )

    if not legacy_commit_rates_fallback:
        return sorted(providers)

    json_providers = _get_providers_from_limits(commit_rates or {})
    added = []
    for provider in json_providers:
        if provider not in providers:
            providers.add(provider)
            added.append(provider)

    if added:
        if netbox_available:
            for provider in added:
                print(
                    "Warning: provider {!r} not in NetBox inventory; "
                    "using commit_rates.json _provider_limits (legacy fallback)".format(provider),
                    file=sys.stderr,
                )
        else:
            print(
                "Warning: no providers in NetBox inventory; "
                "using commit_rates.json _provider_limits (legacy fallback)",
                file=sys.stderr,
            )
        if debug:
            print(
                "Legacy fallback providers from _provider_limits: {}".format(
                    ", ".join(added)
                ),
                file=sys.stderr,
            )
    return sorted(providers)


def _commit_rate_gbps_from_inventory_row(row):
    kbps = row.get("commit_rate_kbps")
    if kbps is None:
        return None
    try:
        return float(kbps) / 1e6
    except (TypeError, ValueError):
        return None


def _burst_report_rows_from_inventory(report, dry_ssh_devices=None, debug=False):
    """Burst SLA rows from NetBox inventory; optional dry-ssh maps physical to logical iface."""
    meta = burst_metadata_from_inventory(report)
    if dry_ssh_devices:
        meta = expand_burst_metadata_for_zabbix(meta, dry_ssh_devices, debug=debug)

    cr_by_pair = {}
    for row in report.get("complete") or []:
        if not is_burst_billing_model(row.get("billing_model")):
            continue
        device_name = (row.get("device") or "").strip()
        iface_name = (row.get("interface") or "").strip()
        if not device_name or not iface_name:
            continue
        cr_gbps = _commit_rate_gbps_from_inventory_row(row)
        zabbix_iface = (
            _zabbix_iface_from_inventory_iface(device_name, iface_name, dry_ssh_devices)
            or iface_name
        )
        cr_by_pair[(device_name, zabbix_iface)] = cr_gbps
        cr_by_pair[(device_name, iface_name)] = cr_gbps

    seen = set()
    rows = []
    for (dev_name, iface_name), md in sorted(meta.items()):
        cid = md.get("circuit_id")
        prov = md.get("provider")
        if not cid or not prov or cid in seen:
            continue
        seen.add(cid)
        rows.append((cid, prov, dev_name, iface_name, cr_by_pair.get((dev_name, iface_name))))
    return rows


def _resolve_burst_report_rows(
    netbox_ctx,
    commit_rates,
    dry_ssh_devices=None,
    legacy_commit_rates_fallback=False,
    debug=False,
):
    """Unique Burst SLA rows: NetBox inventory first; commit_rates.json only with legacy flag."""
    rows = []
    seen = set()
    netbox_available = bool(netbox_ctx) and not netbox_ctx.get("read_error")
    if netbox_available:
        inv_rows = _burst_report_rows_from_inventory(
            netbox_ctx.get("report") or {},
            dry_ssh_devices=dry_ssh_devices,
            debug=debug,
        )
        for row in inv_rows:
            if row[0] not in seen:
                seen.add(row[0])
                rows.append(row)
        if rows and debug:
            print(
                "Burst circuits from NetBox inventory: {}".format(len(rows)),
                file=sys.stderr,
            )

    if not legacy_commit_rates_fallback:
        return sorted(rows, key=lambda x: x[0])

    json_rows = _burst_report_rows(commit_rates or {})
    added = []
    for row in json_rows:
        cid = row[0]
        if cid in seen:
            continue
        seen.add(cid)
        rows.append(row)
        added.append(cid)

    if added:
        if netbox_available:
            for cid in added:
                print(
                    "Warning: Burst circuit {!r} not in NetBox inventory; "
                    "using commit_rates.json billing_model (legacy fallback)".format(cid),
                    file=sys.stderr,
                )
        else:
            print(
                "Warning: no Burst billing_model in NetBox inventory; "
                "using commit_rates.json billing_model (legacy fallback)",
                file=sys.stderr,
            )
        if debug:
            print(
                "Legacy fallback Burst circuits from commit_rates.json: {}".format(len(added)),
                file=sys.stderr,
            )
    return sorted(rows, key=lambda x: x[0])


def _resolve_provider_slo(
    provider,
    netbox_ctx,
    project_slo=None,
    legacy_global_slo=None,
    legacy_commit_rates_fallback=False,
    debug=False,
):
    """Per-provider slo_percent from NetBox, else project default or legacy _provider_sla."""
    netbox_slo = (netbox_ctx or {}).get("provider_slo_percent") or {}
    if provider in netbox_slo:
        return netbox_slo[provider]

    if legacy_commit_rates_fallback and legacy_global_slo is not None:
        if netbox_ctx and provider in (netbox_ctx.get("providers") or set()):
            print(
                "Warning: provider {!r} has no slo_percent in NetBox; "
                "using commit_rates.json _provider_sla (legacy fallback)".format(provider),
                file=sys.stderr,
            )
            if debug:
                print(
                    "Legacy fallback SLA for {} from _provider_sla: {:.4f}%".format(
                        provider, legacy_global_slo
                    ),
                    file=sys.stderr,
                )
        return legacy_global_slo

    if project_slo is not None:
        return project_slo
    return None


def _resolve_provider_limit_gbps(
    provider,
    netbox_ctx,
    commit_rates,
    legacy_commit_rates_fallback=False,
    debug=False,
):
    """Aggregate limit column: NetBox aggregate_limit_gbps, else legacy _provider_limits."""
    limits = (netbox_ctx or {}).get("provider_limits_gbps") or {}
    if provider in limits:
        return limits[provider]

    if not legacy_commit_rates_fallback:
        return None

    legacy_entry = (commit_rates or {}).get("_provider_limits", {}).get(provider)
    if legacy_entry is None:
        return None

    print(
        "Warning: provider {!r} has no aggregate_limit_gbps in NetBox; "
        "using commit_rates.json _provider_limits (legacy fallback)".format(provider),
        file=sys.stderr,
    )
    if debug:
        print(
            "Legacy fallback limit for {} from commit_rates.json: {} Gbps".format(
                provider, legacy_entry
            ),
            file=sys.stderr,
        )
    try:
        return float(legacy_entry)
    except (TypeError, ValueError):
        return None


def _netbox_read_error_message(stats):
    error = (stats or {}).get("error")
    if error == ERROR_AUTH_DENIED:
        return _NETBOX_AUTH_MESSAGE
    if error in (ERROR_PARTIAL_READ, ERROR_PROVIDERS_UNAVAILABLE):
        return _NETBOX_READ_INCOMPLETE_MESSAGE
    return _NETBOX_READ_INCOMPLETE_MESSAGE


def _get_hostid_for_device(url, token, dev_name, debug=False):
    res, err = zabbix_request(
        url,
        token,
        "host.get",
        {"output": ["hostid"], "filter": {"host": [dev_name]}},
        debug=debug,
    )
    if err:
        return None, err
    if res:
        return str(res[0]["hostid"]), None
    res2, err2 = zabbix_request(
        url,
        token,
        "host.get",
        {"output": ["hostid"], "filter": {"name": [dev_name]}},
        debug=debug,
    )
    if err2:
        return None, err2
    if res2:
        return str(res2[0]["hostid"]), None
    return None, None


def _get_burst_link_triggers(url, token, hostid, iface_name, debug=False):
    """Triggers 90% / 100% / SLA breach on the interface (as an aggregate: priority sla, otherwise high)."""
    prefix = "Interface {}:".format((iface_name or "").strip())
    res, err = zabbix_request(
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
    if err or not res:
        return None, None, None
    warn_id = high_id = sla_id = None
    for t in res:
        desc = (t.get("description") or "").strip()
        if not desc.startswith(prefix):
            continue
        tid = t.get("triggerid")
        if not tid:
            continue
        if desc.endswith(TRIGGER_DESC_SLA_BREACH_SUFFIX):
            sla_id = tid
        elif desc.endswith(TRIGGER_DESC_100_SUFFIX):
            high_id = tid
        elif desc.endswith(TRIGGER_DESC_90_SUFFIX):
            warn_id = tid
    return warn_id, high_id, sla_id


def _unix_ts(dt):
    if isinstance(dt, (int, float)):
        return int(dt)
    if isinstance(dt, str):
        try:
            parsed = datetime.fromisoformat(dt)
        except ValueError:
            return int(time.time())
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return int(time.time())


def _default_window(days):
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    return _unix_ts(start), _unix_ts(now)


def _get_aggregate_triggers(url, token, providers, debug=False):
    """Return provider -> (triggerid_warn, triggerid_high, triggerid_sla) for aggregate hosts."""
    if not providers:
        return {}
    host_names = [UPLINKS_AGGREGATE_HOST_PREFIX + p for p in providers]
    hostid_by_provider = {}

    res_host, err = zabbix_request(
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
        for h in res_host or []:
            host = h.get("host") or ""
            name = h.get("name") or ""
            for p in providers:
                wanted = UPLINKS_AGGREGATE_HOST_PREFIX + p
                if host == wanted or name == wanted:
                    hostid_by_provider[p] = str(h.get("hostid"))

    missing = [p for p in providers if p not in hostid_by_provider]
    if missing:
        names_filter = [UPLINKS_AGGREGATE_HOST_PREFIX + p for p in missing]
        res_name, err2 = zabbix_request(
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
            for h in res_name or []:
                host = h.get("host") or ""
                name = h.get("name") or ""
                for p in missing:
                    wanted = UPLINKS_AGGREGATE_HOST_PREFIX + p
                    if host == wanted or name == wanted:
                        hostid_by_provider[p] = str(h.get("hostid"))

    hostids = list({hid for hid in hostid_by_provider.values() if hid})
    if not hostids:
        return {}

    trig_res, err = zabbix_request(
        url,
        token,
        "trigger.get",
        {
            "hostids": hostids,
            "output": ["triggerid", "description", "priority"],
            "selectHosts": ["hostid"],
            "search": {"description": "Provider aggregate"},
        },
        debug=debug,
    )
    if err or not trig_res:
        return {}

    hostid_to_provider = {hid: p for p, hid in hostid_by_provider.items()}
    out = {p: {"warn": None, "high": None, "sla": None} for p in providers}
    for t in trig_res:
        hosts = t.get("hosts") or []
        if not hosts or not isinstance(hosts[0], dict):
            continue
        hostid = str(hosts[0].get("hostid") or "")
        provider = hostid_to_provider.get(hostid)
        if not provider:
            continue
        desc_full = t.get("description") or ""
        tid = t.get("triggerid")
        if not tid:
            continue
        if desc_full.startswith("Provider aggregate SLA breach:"):
            out[provider]["sla"] = tid
        elif desc_full.startswith("Provider aggregate traffic >=") and "90%" in desc_full:
            out[provider]["warn"] = tid
        elif desc_full.startswith("Provider aggregate traffic >=") and "100%" in desc_full:
            out[provider]["high"] = tid

    return {p: (ids["warn"], ids["high"], ids["sla"]) for p, ids in out.items()}


def _load_events_for_trigger(url, token, triggerid, time_from, time_till, debug=False):
    """Return list of (clock, value) events (value 0/1) ordered by time."""
    res, err = zabbix_request(
        url,
        token,
        "event.get",
        {
            "output": ["eventid", "clock", "value"],
            "object": 0,
            "objectids": [triggerid],
            "time_from": time_from,
            "time_till": time_till,
            "sortfield": ["clock", "eventid"],
            "sortorder": "ASC",
        },
        debug=debug,
    )
    if err or not res:
        return []
    events = []
    for e in res:
        try:
            clk = int(e.get("clock", 0))
        except (TypeError, ValueError):
            continue
        try:
            val = int(e.get("value", 0))
        except (TypeError, ValueError):
            val = 0
        events.append((clk, val))
    return events


def _compute_sla_from_events(events, time_from, time_till):
    """Compute total time and time in PROBLEM (value=1) from ordered events."""
    if time_till <= time_from:
        return 0, 0
    total = time_till - time_from
    if not events:
        return total, 0

    problem_time = 0
    current_state = 0
    last_ts = time_from

    for clk, val in events:
        if clk < time_from:
            current_state = val
            continue
        if clk > time_till:
            break
        if current_state == 1:
            problem_time += clk - last_ts
        current_state = val
        last_ts = clk

    if current_state == 1 and last_ts < time_till:
        problem_time += time_till - last_ts

    if problem_time < 0:
        problem_time = 0
    if problem_time > total:
        problem_time = total
    return total, problem_time


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Calculate SLA from trigger history: provider aggregates and Burst links "
            "(SLA breach if present, else 100%)."
        ),
    )
    parser.add_argument(
        "-f",
        "--commit-rates",
        default=DEFAULT_COMMIT_RATES,
        help="Path to commit_rates.json (legacy mode only, with --legacy-commit-rates-fallback).",
    )
    parser.add_argument(
        "--legacy-commit-rates-fallback",
        action="store_true",
        help="Explicitly allow transition fallback to commit_rates.json for providers, Burst, and SLO.",
    )
    parser.add_argument(
        "--dry-ssh",
        default=None,
        metavar="FILE",
        help="dry-ssh.json: map physical inventory interface to logical Zabbix trigger name.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Number of days back for SLA window (default: 30).",
    )
    parser.add_argument(
        "--from-ts",
        type=int,
        default=None,
        help="Window start (Unix timestamp). Overrides --days if set.",
    )
    parser.add_argument(
        "--to-ts",
        type=int,
        default=None,
        help="Window end (Unix timestamp). Defaults to now.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Verbose Zabbix API debug output.",
    )
    args = parser.parse_args()

    dry_ssh_devices = None
    if args.dry_ssh:
        data, err = load_devices_json(args.dry_ssh)
        if err:
            print(err, file=sys.stderr)
            sys.exit(1)
        dry_ssh_devices = data.get("devices")

    commit_rates = {}
    legacy_global_slo = None
    if args.legacy_commit_rates_fallback:
        commit_rates, err = _load_commit_rates(args.commit_rates)
        if err:
            print(err, file=sys.stderr)
            sys.exit(1)
        commit_rates = commit_rates or {}
        legacy_global_slo = _get_global_provider_sla(commit_rates)

    netbox_ctx = _load_netbox_services_context(debug=args.debug)
    if netbox_ctx and netbox_ctx.get("read_error"):
        arm_netbox_incomplete_guard(netbox_ctx.get("stats"))
        print(_netbox_read_error_message(netbox_ctx.get("stats")), file=sys.stderr)
        sys.exit(1)

    providers = _resolve_providers(
        netbox_ctx,
        commit_rates,
        legacy_commit_rates_fallback=args.legacy_commit_rates_fallback,
        debug=args.debug,
    )
    burst_rows = _resolve_burst_report_rows(
        netbox_ctx,
        commit_rates,
        dry_ssh_devices=dry_ssh_devices,
        legacy_commit_rates_fallback=args.legacy_commit_rates_fallback,
        debug=args.debug,
    )

    if not providers and not burst_rows:
        print(
            "No providers in NetBox inventory and no Burst circuits; nothing to calculate.",
            file=sys.stderr,
        )
        sys.exit(0)

    if args.from_ts is not None:
        time_from = int(args.from_ts)
        time_till = int(args.to_ts) if args.to_ts is not None else int(time.time())
    else:
        time_from, time_till = _default_window(args.days)

    url, token = _get_zabbix_url_token()
    if not url or not token:
        print("ZABBIX_URL and ZABBIX_TOKEN are required.", file=sys.stderr)
        sys.exit(1)

    project_slo = getattr(
        sys.modules.get("uplinks_config"),
        "PROJECT_PROVIDER_SLO_PERCENT",
        PROJECT_PROVIDER_SLO_PERCENT,
    )

    trig_by_provider = {}
    if providers:
        trig_by_provider = _get_aggregate_triggers(url, token, providers, debug=args.debug)
        if not trig_by_provider:
            print(
                "Warning: no aggregate triggers for inventory providers.",
                file=sys.stderr,
            )

    print("SLA window: {} .. {}".format(time_from, time_till))
    if args.legacy_commit_rates_fallback and legacy_global_slo is not None:
        print("Target SLA (_provider_sla): {:.5f}%".format(legacy_global_slo))
    else:
        print(
            "Target SLA (PROJECT_PROVIDER_SLO_PERCENT): {:.5f}%".format(project_slo)
        )
    print(
        "SLA%% by window: SLA breach trigger (sustained exceedance), if any;"
        "otherwise instant 100%%. Breach period: uplinks_config.SLA_TRIGGER_FUNCTION_PERIOD."
    )
    print("")

    def _row_sla(trig_for_sla, target_sla):
        if not trig_for_sla:
            return "n/a", "n/a", ""
        events = _load_events_for_trigger(
            url, token, trig_for_sla, time_from, time_till, debug=args.debug
        )
        total, problem = _compute_sla_from_events(events, time_from, time_till)
        if total <= 0:
            return "n/a", "n/a", ""
        sla = (total - problem) / float(total) * 100.0
        sla_text = "{:.5f}".format(sla)
        over_hours = "{:.3f}".format(problem / 3600.0)
        if target_sla is not None and sla < target_sla:
            below = "YES"
        else:
            below = ""
        return sla_text, over_hours, below

    if providers:
        header = "{:<20} {:>10} {:>12} {:>12} {:>10}".format(
            "Provider", "LimitGbps", "SLA%", "Breach(h)", "BelowSLA"
        )
        print("--- Aggregate providers ---")
        print(header)
        for provider in providers:
            limit = _resolve_provider_limit_gbps(
                provider,
                netbox_ctx,
                commit_rates,
                legacy_commit_rates_fallback=args.legacy_commit_rates_fallback,
                debug=args.debug,
            )
            triple = trig_by_provider.get(provider, (None, None, None))
            trig_warn, trig_high, trig_sla = (
                triple[0],
                triple[1],
                triple[2] if len(triple) > 2 else None,
            )
            trig_for_sla = trig_sla or trig_high
            target = _resolve_provider_slo(
                provider,
                netbox_ctx,
                project_slo=project_slo,
                legacy_global_slo=legacy_global_slo,
                legacy_commit_rates_fallback=args.legacy_commit_rates_fallback,
                debug=args.debug,
            )
            sla_text, over_hours, below = _row_sla(trig_for_sla, target)
            limit_str = "-" if limit is None else str(limit)
            print(
                "{:<20} {:>10} {:>12} {:>12} {:>10}".format(
                    provider, limit_str, sla_text, over_hours, below
                )
            )
        print("")

    if burst_rows:
        print("--- Burst circuits ---")
        bh = "{:<28} {:>12} {:>10} {:>12} {:>12} {:>10}".format(
            "Circuit", "Provider", "CommitGbps", "SLA%", "Breach(h)", "BelowSLA"
        )
        print(bh)
        for cid, prov, dev_name, iface_name, cr in burst_rows:
            hostid, herr = _get_hostid_for_device(url, token, dev_name, debug=args.debug)
            target = _resolve_provider_slo(
                prov,
                netbox_ctx,
                project_slo=project_slo,
                legacy_global_slo=legacy_global_slo,
                legacy_commit_rates_fallback=args.legacy_commit_rates_fallback,
                debug=args.debug,
            )
            if herr or not hostid:
                cr_s = "-" if cr is None else str(cr)
                print(
                    "{:<28} {:>12} {:>10} {:>12} {:>12} {:>10}".format(
                        cid, prov, cr_s, "n/a", "n/a", ""
                    )
                )
                print(
                    "  (host not found: {})".format(dev_name),
                    file=sys.stderr,
                )
                continue
            triple = _get_burst_link_triggers(url, token, hostid, iface_name, debug=args.debug)
            trig_warn, trig_high, trig_sla = triple
            trig_for_sla = trig_sla or trig_high
            sla_text, over_hours, below = _row_sla(trig_for_sla, target)
            cr_s = "-" if cr is None else str(cr)
            print(
                "{:<28} {:>12} {:>10} {:>12} {:>12} {:>10}".format(
                    cid, prov, cr_s, sla_text, over_hours, below
                )
            )


if __name__ == "__main__":
    main()

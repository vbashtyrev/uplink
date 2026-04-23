#!/usr/bin/env python3
"""Calculate SLA for provider aggregate limits and Burst link SLA breach (Zabbix trigger history)."""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from env_urls import load_env_file_if_present
from zabbix_map import (
    _get_zabbix_url_token,
    zabbix_request,
)
from uplinks_config import (
    UPLINKS_AGGREGATE_HOST_PREFIX,
    TRIGGER_DESC_90_SUFFIX,
    TRIGGER_DESC_100_SUFFIX,
    TRIGGER_DESC_SLA_BREACH_SUFFIX,
)

load_env_file_if_present()

DEFAULT_COMMIT_RATES = "commit_rates.json"


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
    """Один ряд на circuit_id: первый встреченный интерфейс."""
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
    """
    Триггеры 90% / 100% / SLA breach на интерфейсе (как aggregate: приоритет sla, иначе high).
    """
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
        # assume ISO8601 or similar parseable by datetime.fromisoformat
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
    """
    Return provider -> (triggerid_warn, triggerid_high, triggerid_sla) for aggregate hosts.

    Выбор по описанию (как в zabbix_provider_aggregate), не по priority: у 100% и SLA breach
    оба priority=2, иначе zabbix_provider_sla мог бы взять «не тот» триггер.
    """
    if not providers:
        return {}
    host_names = [UPLINKS_AGGREGATE_HOST_PREFIX + p for p in providers]
    # Find aggregate hosts by host and name to handle sanitized technical names
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
            "object": 0,  # trigger
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

    # assume OK (0) before first event
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
        help="Path to commit_rates.json (_provider_limits, Burst billing_model, _provider_sla).",
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

    commit_rates, err = _load_commit_rates(args.commit_rates)
    if err:
        print(err, file=sys.stderr)
        sys.exit(1)

    providers = _get_providers_from_limits(commit_rates)
    burst_rows = _burst_report_rows(commit_rates)
    if not providers and not burst_rows:
        print(
            "No _provider_limits and no Burst circuits; nothing to calculate.",
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

    trig_by_provider = {}
    if providers:
        trig_by_provider = _get_aggregate_triggers(url, token, providers, debug=args.debug)
        if not trig_by_provider:
            print(
                "Warning: no aggregate triggers for _provider_limits providers.",
                file=sys.stderr,
            )

    target_sla = commit_rates.get("_provider_sla")
    if isinstance(target_sla, (int, float)):
        try:
            target_sla = float(target_sla)
        except (TypeError, ValueError):
            target_sla = None
    else:
        target_sla = None

    print("SLA window: {} .. {}".format(time_from, time_till))
    if target_sla is not None:
        print("Target SLA (_provider_sla): {:.5f}%".format(target_sla))
    print(
        "SLA%% по окну: триггер SLA breach (устойчивое превышение), если есть; "
        "иначе мгновенный 100%%. Период breach: uplinks_config.SLA_TRIGGER_FUNCTION_PERIOD."
    )
    print("")

    def _row_sla(trig_for_sla):
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
            limit = commit_rates.get("_provider_limits", {}).get(provider)
            triple = trig_by_provider.get(provider, (None, None, None))
            trig_warn, trig_high, trig_sla = (
                triple[0],
                triple[1],
                triple[2] if len(triple) > 2 else None,
            )
            trig_for_sla = trig_sla or trig_high
            sla_text, over_hours, below = _row_sla(trig_for_sla)
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
            sla_text, over_hours, below = _row_sla(trig_for_sla)
            cr_s = "-" if cr is None else str(cr)
            print(
                "{:<28} {:>12} {:>10} {:>12} {:>12} {:>10}".format(
                    cid, prov, cr_s, sla_text, over_hours, below
                )
            )


if __name__ == "__main__":
    main()


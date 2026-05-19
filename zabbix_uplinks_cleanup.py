#!/usr/bin/env python3
"""Cleaning automation artifacts in Zabbix:
- simple triggers 90% / 100% / SLA breach on links created by zabbix_sync_commit_rate.py;
- old threshold items net.if.threshold["..."];
- uplinks map ([test] uplinks);
- uplinks dashboards (main, “by location”, “by provider”) by default.

Whenever possible, elements are marked with the Zabbix tag (trigger tags): scripts=automatization.
The script deletes only objects created by automation and does not touch hosts, traffic items and commit rate macros.

Environment variables: ZABBIX_URL, ZABBIX_TOKEN."""

import argparse
import os
import sys

from env_urls import load_env_file_if_present
from zabbix_map import (
    _get_zabbix_url_token,
    validate_zabbix_token,
    zabbix_request,
)
from uplinks_config import (
    DASHBOARD_NAME,
    DASHBOARD_NAME_BY_LOCATION,
    DASHBOARD_NAME_BY_PROVIDER,
    MAP_NAME,
    THRESHOLD_ITEM_KEY,
    THRESHOLD_PERCENT_WARN,
    TRIGGER_DESC_90_SUFFIX,
    TRIGGER_DESC_100_SUFFIX,
    TRIGGER_DESC_SLA_BREACH_SUFFIX,
    TRIGGER_DESC_UTIL_CRIT_SUFFIX,
    TRIGGER_DESC_UTIL_WARN_SUFFIX,
    TRIGGER_TAG_NAME,
    TRIGGER_TAG_VALUE,
)

load_env_file_if_present()

# As in zabbix_sync_commit_rate.delete_link_triggers - legacy end of description
LEGACY_TRIGGER_DESC_90_SUFFIX = "High bandwidth ({}%)".format(THRESHOLD_PERCENT_WARN)
LEGACY_TRIGGER_DESC_100_SUFFIX = "High bandwidth (threshold line)"


def _validate_zabbix(debug=False):
    url, token = _get_zabbix_url_token()
    if not url or not token:
        print("Set ZABBIX_URL and ZABBIX_TOKEN", file=sys.stderr)
        sys.exit(1)
    if not validate_zabbix_token(url, token, debug=debug):
        print("Invalid or expired ZABBIX_TOKEN", file=sys.stderr)
        sys.exit(1)
    return url, token


def cleanup_threshold_items(url, token, dry_run=False, debug=False):
    """Remove all items of the net.if.threshold[...] threshold (historical artifact)."""
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
    """Check if scripts:automatization is included among the tags."""
    for t in tags or []:
        if t.get("tag") == TRIGGER_TAG_NAME and t.get("value") == TRIGGER_TAG_VALUE:
            return True
    return False


def cleanup_triggers(url, token, dry_run=False, debug=False):
    """Remove simple 90% / 100% / SLA breach triggers on interfaces created by zabbix_sync_commit_rate.py.
    Filter by Interface... description prefix + scripts:automatization tag (if specified)."""
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
            desc.endswith(TRIGGER_DESC_90_SUFFIX)
            or desc.endswith(TRIGGER_DESC_100_SUFFIX)
            or desc.endswith(TRIGGER_DESC_SLA_BREACH_SUFFIX)
            or desc.endswith(TRIGGER_DESC_UTIL_WARN_SUFFIX)
            or desc.endswith(TRIGGER_DESC_UTIL_CRIT_SUFFIX)
            or desc.endswith(LEGACY_TRIGGER_DESC_90_SUFFIX)
            or desc.endswith(LEGACY_TRIGGER_DESC_100_SUFFIX)
        ):
            continue
        tags = t.get("tags") or []
        # If there is a tag, use it as the main filter.
        if tags and not _has_our_tag(tags):
            continue
        tid = t.get("triggerid")
        if tid:
            to_delete.append(str(tid))
    if not to_delete:
        return 0
    if dry_run:
        print("dry-run: trigger.delete {} (uplinks 90%/100%/SLA breach)".format(len(to_delete)))
        return len(to_delete)
    _, del_err = zabbix_request(url, token, "trigger.delete", to_delete, debug=debug)
    if del_err:
        print("trigger.delete error: {}".format(del_err), file=sys.stderr)
        return 0
    return len(to_delete)


def cleanup_map(url, token, dry_run=False, debug=False):
    """Remove the uplinks map created by zabbix_map.py (MAP_NAME)."""
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
    """Delete dashboards by name (main uplinks and by location)."""
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
            "Clear uplinks automation artifacts in Zabbix"
            "(90%/100% triggers, old net.if.threshold, map and uplinks dashboards)."
        )
    )
    parser.add_argument(
        "--dashboard-name",
        default=DASHBOARD_NAME,
        help="Name of the main uplinks dashboard (as in zabbix_uplinks_dashboard.py)",
    )
    parser.add_argument(
        "--dashboard-by-location",
        default=DASHBOARD_NAME_BY_LOCATION,
        help="Dashboard name by location (empty line - do not touch)",
    )
    parser.add_argument(
        "--dashboard-by-provider",
        default=DASHBOARD_NAME_BY_PROVIDER,
        help="Name of the summary dashboard by providers (empty line - do not touch)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only show what will be deleted (no changes in Zabbix)",
    )
    parser.add_argument(
        "--debug", action="store_true", help="Debugging output of requests to Zabbix API"
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
    if args.dashboard_by_provider.strip():
        dash_names.add(args.dashboard_by_provider.strip())
    total_dash = cleanup_dashboards(
        url, token, dash_names, dry_run=args.dry_run, debug=args.debug
    )

    mode = "dry-run" if args.dry_run else "completed"
    print(
        "{}: removed items threshold: {}, triggers: {}, maps: {}, dashboards: {}".format(
            mode, total_items, total_triggers, total_maps, total_dash
        )
    )


if __name__ == "__main__":
    main()


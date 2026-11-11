#!/usr/bin/env python3
"""Create or update the Zabbix DCI dashboard from NetBox DCI circuits (read-only NetBox)."""

import argparse
import os
import sys

import pynetbox

from env_urls import load_env_file_if_present
from uplinks.netbox.dci import (
    collect_dci_channels,
    find_duplicate_cid_provider,
    format_channel_line,
)
from uplinks.zabbix.client import (
    get_zabbix_url_token,
    interface_from_item_name,
    interface_from_key,
    normalize_interface_name,
    validate_zabbix_token,
)
from uplinks_config import DCI_DASHBOARD_NAME, DCI_CIRCUIT_TYPE, DCI_TAG_SLUG
from zabbix_map import zabbix_request

load_env_file_if_present()

WIDGET_HEIGHT = 5
WIDGET_WIDTH_A = 35
WIDGET_WIDTH_Z = 37
WIDGET_X_Z = 35
PAGE_NAME_NO_REGION = "Без региона"
ITEM_GET_LIMIT = 100000
PROMPT_TEXT = "Записать дашборд? [да/нет]"

BITS_RECEIVED_SUBSTR = "Bits received"
BITS_SENT_SUBSTR = "Bits sent"
KEY_PREFIX_IN = "net.if.in["
KEY_PREFIX_OUT = "net.if.out["

WARNING_PREFIX = "ПРЕДУПРЕЖДЕНИЕ:"


def _netbox_configured():
    url = os.environ.get("NETBOX_URL", "").strip()
    token = os.environ.get("NETBOX_TOKEN", "").strip()
    return bool(url and token)


def _page_name_for_channel(channel):
    region = (channel.get("region") or "").strip()
    return region if region else PAGE_NAME_NO_REGION


def group_full_channels_by_region(full_channels):
    """Group complete channels by region; preserve region and channel order."""
    page_names = []
    buckets = {}
    for ch in full_channels:
        name = _page_name_for_channel(ch)
        if name not in buckets:
            buckets[name] = []
            page_names.append(name)
        buckets[name].append(ch)
    return [(name, buckets[name]) for name in page_names]


def normalize_dashboard_pages(pages):
    """Zabbix dashboard.create requires at least one page."""
    if not pages:
        return [{"name": PAGE_NAME_NO_REGION, "widgets": []}]
    return pages


def _channel_row_y(row_index):
    return row_index * WIDGET_HEIGHT


def _cid_needs_provider_prefix(channels):
    """True when the same cid appears under more than one provider among DCI channels."""
    by_cid = {}
    for ch in channels:
        cid = (ch.get("cid") or "").strip()
        provider = (ch.get("provider") or "").strip()
        if not cid:
            continue
        by_cid.setdefault(cid, set()).add(provider)
    return {cid for cid, provs in by_cid.items() if len(provs) > 1}


def graph_header(channel, side, ambiguous_cids):
    """Widget title: cid device interface; prefix provider when cid is ambiguous."""
    cid = (channel.get("cid") or "").strip()
    provider = (channel.get("provider") or "").strip()
    if side == "A":
        device = channel.get("a_device") or ""
        iface = channel.get("a_iface") or ""
    else:
        device = channel.get("z_device") or ""
        iface = channel.get("z_iface") or ""
    parts = []
    if cid in ambiguous_cids and provider:
        parts.append(provider)
    if cid:
        parts.append(cid)
    if device:
        parts.append(device)
    if iface:
        parts.append(iface)
    return " ".join(parts) if parts else "—"


def _select_item_by_status(candidates):
    """Pick one item id among candidates by Zabbix item status rules."""
    enabled = [c for c in candidates if str(c.get("status")) == "0"]
    disabled = [c for c in candidates if str(c.get("status")) == "1"]
    if len(enabled) == 1:
        return enabled[0].get("itemid"), None
    if len(enabled) > 1:
        return None, "несколько включённых показателей"
    if len(disabled) == 1:
        return disabled[0].get("itemid"), None
    if len(disabled) > 1:
        return None, "несколько выключенных показателей"
    return None, None


def select_items_for_end(hostname, hostid, iface_name, all_items):
    """
    Resolve Bits received/sent itemids for one end.

    Return (itemid_in, itemid_out, warnings).
    """
    warnings = []
    if not hostid or not iface_name:
        return "", "", warnings
    iface_norm = normalize_interface_name(iface_name)
    in_candidates = []
    out_candidates = []
    for item in all_items:
        if str(item.get("hostid")) != str(hostid):
            continue
        name = item.get("name") or ""
        key = item.get("key_") or item.get("key") or ""
        parsed_iface = interface_from_item_name(name) or interface_from_key(key)
        if not parsed_iface:
            continue
        if normalize_interface_name(parsed_iface) != iface_norm:
            continue
        if BITS_RECEIVED_SUBSTR in name and key.startswith(KEY_PREFIX_IN):
            in_candidates.append(item)
        if BITS_SENT_SUBSTR in name and key.startswith(KEY_PREFIX_OUT):
            out_candidates.append(item)

    itemid_in, warn_in = _select_item_by_status(in_candidates)
    if warn_in:
        warnings.append(
            "{} {} {}: приём — {}".format(WARNING_PREFIX, hostname, iface_name, warn_in)
        )
        itemid_in = None
    itemid_out, warn_out = _select_item_by_status(out_candidates)
    if warn_out:
        warnings.append(
            "{} {} {}: отправка — {}".format(
                WARNING_PREFIX, hostname, iface_name, warn_out
            )
        )
        itemid_out = None

    def _valid_itemid(raw, direction):
        if raw is None:
            return ""
        s = str(raw).strip()
        if not s.isdigit():
            warnings.append(
                "{} {} {}: {} — некорректный itemid {!r}".format(
                    WARNING_PREFIX, hostname, iface_name, direction, raw
                )
            )
            return ""
        return s

    return (
        _valid_itemid(itemid_in, "приём"),
        _valid_itemid(itemid_out, "отправка"),
        warnings,
    )


def build_dci_graph_widget(widget_index, title, itemid_in, itemid_out, x, y, width):
    """Build svggraph widget with item-list datasets (not _make_graph_widget)."""
    if not itemid_in and not itemid_out:
        return None
    ref = "W{:04d}".format(widget_index)[:5]
    fields = [
        {"type": 1, "name": "reference", "value": ref},
        {"type": 0, "name": "legend_statistic", "value": 1},
        {"type": 0, "name": "legend_lines", "value": 2},
    ]
    colors = ["1A7F37", "E02F44"]
    ds_k = 0
    if itemid_in:
        fields.extend(
            [
                {"type": 0, "name": "ds.{}.dataset_type".format(ds_k), "value": 0},
                {"type": 4, "name": "ds.{}.itemids.0".format(ds_k), "value": itemid_in},
                {
                    "type": 1,
                    "name": "ds.{}.data_set_label".format(ds_k),
                    "value": "Bits received",
                },
                {"type": 1, "name": "ds.{}.color.0".format(ds_k), "value": colors[0]},
                {"type": 0, "name": "ds.{}.width".format(ds_k), "value": 2},
                {"type": 0, "name": "ds.{}.transparency".format(ds_k), "value": 5},
                {"type": 0, "name": "ds.{}.fill".format(ds_k), "value": 3},
            ]
        )
        ds_k = 1 if itemid_out else 0
    if itemid_out:
        k = ds_k if itemid_in else 0
        fields.extend(
            [
                {"type": 0, "name": "ds.{}.dataset_type".format(k), "value": 0},
                {"type": 4, "name": "ds.{}.itemids.0".format(k), "value": itemid_out},
                {
                    "type": 1,
                    "name": "ds.{}.data_set_label".format(k),
                    "value": "Bits sent",
                },
                {"type": 1, "name": "ds.{}.color.0".format(k), "value": colors[1]},
                {"type": 0, "name": "ds.{}.width".format(k), "value": 2},
                {"type": 0, "name": "ds.{}.transparency".format(k), "value": 5},
                {"type": 0, "name": "ds.{}.fill".format(k), "value": 3},
            ]
        )
    return {
        "type": "svggraph",
        "name": title,
        "x": x,
        "y": y,
        "width": width,
        "height": WIDGET_HEIGHT,
        "view_mode": 0,
        "fields": fields,
    }


def load_zabbix_hosts(url, token, hostnames, debug=False):
    """host.get by technical host name only (visible name is not accepted)."""
    if not hostnames:
        return {}, None
    names = sorted(set(hostnames))
    result, err = zabbix_request(
        url,
        token,
        "host.get",
        {
            "output": ["hostid", "host", "name"],
            "filter": {"host": names},
        },
        debug=debug,
    )
    if err:
        return None, err
    host_id_by_name = {}
    for h in result or []:
        hname = h.get("host")
        if hname in names:
            host_id_by_name[hname] = h.get("hostid")
    return host_id_by_name, None


def load_zabbix_items(url, token, hostids, debug=False):
    """item.get for hostids with limit ITEM_GET_LIMIT."""
    if not hostids:
        return [], None
    result, err = zabbix_request(
        url,
        token,
        "item.get",
        {
            "output": ["itemid", "hostid", "name", "key_", "status"],
            "hostids": list(hostids),
            "limit": ITEM_GET_LIMIT,
        },
        debug=debug,
    )
    if err:
        return None, err
    items = result or []
    if len(items) >= ITEM_GET_LIMIT:
        return None, "item.get: limit {} reached (truncated)".format(ITEM_GET_LIMIT)
    return items, None


def write_dci_dashboard(url, token, pages, dashboard_name, debug=False):
    """dashboard.get/create/update for a multi-page DCI dashboard (one page per region)."""
    pages = normalize_dashboard_pages(pages)
    existing, err = zabbix_request(
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
        return None, "dashboard.get: {}".format(err)
    if not existing:
        result, err = zabbix_request(
            url,
            token,
            "dashboard.create",
            {
                "name": dashboard_name,
                "display_period": 30,
                "auto_start": 1,
                "pages": pages,
            },
            debug=debug,
        )
        if err:
            return None, "dashboard.create: {}".format(err)
        return result.get("dashboardids", [None])[0], None
    if len(existing) > 1:
        return None, "dashboard.get: more than one dashboard named {!r}".format(
            dashboard_name
        )
    dashboardid = existing[0]["dashboardid"]
    result, err = zabbix_request(
        url,
        token,
        "dashboard.update",
        {
            "dashboardid": dashboardid,
            "name": dashboard_name,
            "pages": pages,
        },
        debug=debug,
    )
    if err:
        return None, "dashboard.update: {}".format(err)
    return dashboardid, None


def _widgets_for_channel_end(
    ch,
    side,
    device_key,
    iface_key,
    host_id_by_name,
    all_items,
    ambiguous_cids,
    y,
    widget_index,
):
    """Build one side widget at fixed x/y or return (None, widget_index)."""
    device = ch.get(device_key) or ""
    iface = ch.get(iface_key) or ""
    warnings = []
    if not device or not iface:
        return None, widget_index, warnings
    hostid = host_id_by_name.get(device)
    if not hostid:
        return None, widget_index, warnings
    itemid_in, itemid_out, end_warnings = select_items_for_end(
        device, hostid, iface, all_items
    )
    warnings.extend(end_warnings)
    if side == "A":
        x, width = 0, WIDGET_WIDTH_A
    else:
        x, width = WIDGET_X_Z, WIDGET_WIDTH_Z
    title = graph_header(ch, side, ambiguous_cids)
    wg = build_dci_graph_widget(widget_index, title, itemid_in, itemid_out, x, y, width)
    if wg:
        return wg, widget_index + 1, warnings
    return None, widget_index, warnings


def build_graph_plan(full_channels, all_channels, host_id_by_name, all_items):
    """
    Build dashboard pages: one region per page, one channel per row (A then Z).

    Return (pages, warnings, graph_count).
    """
    warnings = []
    ambiguous_cids = _cid_needs_provider_prefix(all_channels)
    pages = []
    widget_index = 0
    graph_count = 0
    for page_name, region_channels in group_full_channels_by_region(full_channels):
        page_widgets = []
        for row_index, ch in enumerate(region_channels):
            y = _channel_row_y(row_index)
            for side, device_key, iface_key in (
                ("A", "a_device", "a_iface"),
                ("Z", "z_device", "z_iface"),
            ):
                wg, widget_index, end_warnings = _widgets_for_channel_end(
                    ch,
                    side,
                    device_key,
                    iface_key,
                    host_id_by_name,
                    all_items,
                    ambiguous_cids,
                    y,
                    widget_index,
                )
                warnings.extend(end_warnings)
                if wg:
                    page_widgets.append(wg)
                    graph_count += 1
        pages.append({"name": page_name, "widgets": page_widgets})
    return normalize_dashboard_pages(pages), warnings, graph_count


def prompt_write_dashboard():
    """Ask user to confirm dashboard write. Return True only for 'да' after strip."""
    try:
        sys.stdout.write(PROMPT_TEXT + " ")
        sys.stdout.flush()
        line = sys.stdin.readline()
    except KeyboardInterrupt:
        raise
    if line is None:
        return False
    return line.strip() == "да"


def _print_summary(
    full_count, graph_count, wrote=False, dry_run=False, zabbix_ok=False
):
    if dry_run and not zabbix_ok:
        print(
            "Итого: полных пар {}, графиков {}; записи не было".format(
                full_count, graph_count
            )
        )
    elif dry_run:
        print(
            "Итого: полных пар {}, графиков {}; записи не было".format(
                full_count, graph_count
            )
        )
    elif wrote:
        print(
            "Итого: полных пар {}, нарисовано графиков {}, записано".format(
                full_count, graph_count
            )
        )
    else:
        print(
            "Итого: полных пар {}, графиков {}; записи не было".format(
                full_count, graph_count
            )
        )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Create or update the Zabbix DCI dashboard from NetBox DCI circuits.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print pairs and graph plan without writing the Zabbix dashboard",
    )
    parser.add_argument(
        "--debug", action="store_true", help="Verbose stderr diagnostics"
    )
    args = parser.parse_args(argv)

    if not _netbox_configured():
        print("NETBOX_URL and NETBOX_TOKEN must be set", file=sys.stderr)
        return 1

    nb_url = os.environ.get("NETBOX_URL", "").strip().rstrip("/")
    nb_token = os.environ.get("NETBOX_TOKEN", "").strip()
    try:
        nb = pynetbox.api(nb_url, token=nb_token)
    except Exception as e:
        print("NetBox: failed to connect: {}".format(e), file=sys.stderr)
        return 1

    try:
        report = collect_dci_channels(
            nb,
            debug=args.debug,
            circuit_type=DCI_CIRCUIT_TYPE,
            tag_slug=DCI_TAG_SLUG,
        )
    except Exception as e:
        from uplinks.netbox.inventory import NetBoxAuthError

        if isinstance(e, NetBoxAuthError):
            print(
                "Error: NetBox authentication failed. Check NETBOX_TOKEN.",
                file=sys.stderr,
            )
            return 1
        raise

    channels = report.get("channels") or []
    stats = report.get("stats") or {}
    read_errors = stats.get("read_errors", 0)

    for ch in channels:
        print(format_channel_line(ch))

    dup = find_duplicate_cid_provider(channels)
    if dup:
        print(
            "Ошибка: дубликат cid и провайдера: {!r} / {!r}".format(dup[0], dup[1]),
            file=sys.stderr,
        )
        _print_summary(
            sum(1 for c in channels if c.get("complete")),
            0,
            wrote=False,
            dry_run=args.dry_run,
            zabbix_ok=False,
        )
        return 1

    if read_errors > 0 or report.get("fatal"):
        print(
            "Ошибка: чтение NetBox не завершено (read_errors={})".format(read_errors),
            file=sys.stderr,
        )
        _print_summary(
            sum(1 for c in channels if c.get("complete")),
            0,
            wrote=False,
            dry_run=args.dry_run,
            zabbix_ok=False,
        )
        return 1

    full_channels = [c for c in channels if c.get("complete")]
    full_count = len(full_channels)

    url, token = get_zabbix_url_token()
    zabbix_configured = bool(url and token)

    if not zabbix_configured:
        if args.dry_run:
            _print_summary(full_count, 0, dry_run=True, zabbix_ok=False)
            return 0
        print("Set ZABBIX_URL and ZABBIX_TOKEN", file=sys.stderr)
        return 1

    valid, auth_err = validate_zabbix_token(url, token, debug=args.debug)
    if not valid:
        print(auth_err or "Zabbix token validation failed", file=sys.stderr)
        return 1

    host_id_by_name = {}
    all_items = []
    graph_count = 0
    extra_warnings = []

    if full_channels:
        hostnames = set()
        for ch in full_channels:
            for key in ("a_device", "z_device"):
                name = (ch.get(key) or "").strip()
                if name:
                    hostnames.add(name)
        host_id_by_name, err = load_zabbix_hosts(
            url, token, hostnames, debug=args.debug
        )
        if err:
            print(err, file=sys.stderr)
            return 1
        hostids = [host_id_by_name[h] for h in hostnames if h in host_id_by_name]
        if hostids:
            all_items, err = load_zabbix_items(url, token, hostids, debug=args.debug)
            if err:
                print(err, file=sys.stderr)
                return 1
        _, extra_warnings, graph_count = build_graph_plan(
            full_channels,
            channels,
            host_id_by_name or {},
            all_items or [],
        )

    for w in extra_warnings:
        print(w)

    if args.dry_run:
        _print_summary(full_count, graph_count, dry_run=True, zabbix_ok=True)
        return 0

    try:
        confirmed = prompt_write_dashboard()
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130

    if not confirmed:
        _print_summary(full_count, graph_count, wrote=False, dry_run=False)
        return 0

    pages, _, final_graph_count = build_graph_plan(
        full_channels,
        channels,
        host_id_by_name or {},
        all_items or [],
    )
    _, err = write_dci_dashboard(
        url, token, pages, DCI_DASHBOARD_NAME, debug=args.debug
    )
    if err:
        print(err, file=sys.stderr)
        return 1

    _print_summary(full_count, final_graph_count, wrote=True, dry_run=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

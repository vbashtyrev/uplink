"""zabbix_uplinks_dashboard.main with location and provider dashboards."""

import sys
from pathlib import Path
from unittest.mock import patch

from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker
from uplinks_config import UPLINKS_AGGREGATE_HOST_PREFIX
from zabbix_uplinks_dashboard import AGGREGATE_ITEM_KEY_IN, AGGREGATE_ITEM_KEY_OUT

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _dashboard_items():
    return [
        {
            "itemid": "501",
            "hostid": "101",
            "name": "Interface Ethernet51/1: Bits received",
            "key_": 'net.if.in["Ethernet51/1"]',
        },
        {
            "itemid": "502",
            "hostid": "101",
            "name": "Interface Ethernet51/1: Bits sent",
            "key_": 'net.if.out["Ethernet51/1"]',
        },
        {
            "itemid": "601",
            "hostid": "102",
            "name": "Interface ae5.0: Bits received",
            "key_": 'net.if.in["ae5.0"]',
        },
        {
            "itemid": "602",
            "hostid": "102",
            "name": "Interface ae5.0: Bits sent",
            "key_": 'net.if.out["ae5.0"]',
        },
    ]


def test_main_all_dashboards(monkeypatch, zabbix_env, capsys):
    import zabbix_uplinks_dashboard as mod

    agg_host = UPLINKS_AGGREGATE_HOST_PREFIX + "Cogent"
    agg_items = [
        {"itemid": "901", "hostid": "200", "key_": AGGREGATE_ITEM_KEY_IN},
        {"itemid": "902", "hostid": "200", "key_": AGGREGATE_ITEM_KEY_OUT},
    ]

    def item_get(params):
        hostids = {str(x) for x in (params.get("hostids") or [])}
        if "200" in hostids:
            return agg_items
        return _dashboard_items()

    hosts = [
        {"hostid": "101", "host": "ALA-KZT-7280TR-1", "name": "ALA-KZT-7280TR-1"},
        {"hostid": "102", "host": "FRN-MX-1", "name": "FRN-MX-1"},
        {"hostid": "200", "host": agg_host, "name": agg_host},
    ]

    created = []

    def host_get(params):
        filt = params.get("filter") or {}
        if "host" in filt:
            want = set(filt["host"])
            return [h for h in hosts if h["host"] in want]
        if "name" in filt:
            want = set(filt["name"])
            return [h for h in hosts if h["name"] in want]
        return hosts

    mocker = (
        build_standard_zabbix_mocker(hosts=hosts, items=_dashboard_items())
        .on("host.get", host_get)
        .on("item.get", item_get)
        .on("dashboard.get", lambda p: [])
        .on("dashboard.create", lambda p: created.append(p) or {"dashboardids": ["55"]})
    )
    mocker.activate(monkeypatch)

    desc_map = {
        "Uplink: Cogent 10G": "Cogent",
        "Uplink: Hurricane": "Hurricane",
        "Uplink: Hurricane member": "Hurricane",
        "Uplink: Hurricane LAG": "Hurricane",
    }
    with patch.object(mod, "load_description_map", return_value=desc_map):
        with patch.object(
            mod, "_get_providers_from_netbox", return_value=["Hurricane"]
        ):
            monkeypatch.setattr(
                sys,
                "argv",
                [
                    "zabbix_uplinks_dashboard.py",
                    "-f",
                    str(FIXTURES / "dry_ssh_minimal.json"),
                    "--no-cache",
                    "--providers",
                    "Cogent",
                    "Hurricane",
                ],
            )
            mod.main()

    out = capsys.readouterr().out
    assert out.count("OK: dashboard") >= 3
    assert len(created) >= 3
